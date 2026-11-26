# modules/detector.py - QR detection: YOLO finds codes, then each crop is decoded
"""
QR detector: YOLO locates the codes, Pyzbar decodes the crops.

- YOLO finds codes even at a distance.
- Only the crops are decoded, not the full frame.
- If a plain decode fails, preprocessed versions are tried (upscale, CLAHE, sharpen).
"""
import os
import cv2
import numpy as np
import threading
import time
import logging
from pathlib import Path

from modules.utils import extract_keg_id

# Setup debug logger
logger = logging.getLogger("QRDetector")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(handler)

# Initialization
print("\n" + "="*60)
print("INITIALIZING QR DETECTOR (YOLO + ENHANCED DECODE)")
print("="*60)

# Pyzbar is the main decoder (fast)
PYZBAR_AVAILABLE = False
pyzbar_decode = None
ZBarSymbol = None
try:
    from pyzbar.pyzbar import decode as _pyzbar_decode, ZBarSymbol as _ZBarSymbol
    pyzbar_decode = _pyzbar_decode
    ZBarSymbol = _ZBarSymbol
    PYZBAR_AVAILABLE = True
    print("[DETECTOR] Pyzbar: LOADED")
except ImportError as e:
    print(f"[DETECTOR] Pyzbar: NOT AVAILABLE ({e})")

# zxing-cpp is tried first on each crop: it reads a clean code in a millisecond
# or two and copes with rotation and inverted labels. Pyzbar and its
# enhancement chain stay as the fallback.
ZXING_AVAILABLE = False
zxingcpp = None
try:
    import zxingcpp
    ZXING_AVAILABLE = True
    print("[DETECTOR] zxing-cpp: LOADED")
except ImportError as e:
    print(f"[DETECTOR] zxing-cpp: NOT AVAILABLE ({e})")

# Import YOLO
YOLO_AVAILABLE = False
YOLO = None
try:
    from ultralytics import YOLO as _YOLO
    YOLO = _YOLO
    YOLO_AVAILABLE = True
    print("[DETECTOR] YOLO: LOADED")
except Exception as e:
    print(f"[DETECTOR] YOLO: NOT AVAILABLE ({e})")
    print("[DETECTOR] Warning: Running without YOLO (Deep Learning) support.")

# QReader is loaded lazily, only when a deep scan needs it
QREADER_AVAILABLE = False
QReaderClass = None
try:
    from qreader import QReader as _QReaderClass
    QReaderClass = _QReaderClass
    QREADER_AVAILABLE = True
    print("[DETECTOR] QReader: AVAILABLE (lazy-loaded on capture)")
except ImportError as e:
    print(f"[DETECTOR] QReader: NOT AVAILABLE ({e})")

# Import config
try:
    from config import QR_MODEL_PATH
except ImportError:
    from pathlib import Path
    QR_MODEL_PATH = Path(__file__).parent.parent / "models" / "model_qr" / "best.pt"

# YOLO inference size. Large enough to find small or distant QR codes in a 4K frame.
try:
    from config import ZOOM_AREA_EVERY
except ImportError:
    ZOOM_AREA_EVERY = 2
try:
    from config import ZOOM_AREA_ONLY
except ImportError:
    ZOOM_AREA_ONLY = False
try:
    from config import YOLO_IMGSZ
except ImportError:
    YOLO_IMGSZ = 1280

try:
    from config import QR_DECODER_ZXING
except ImportError:
    QR_DECODER_ZXING = True
# tools/run_timing_test.sh sets this to compare the readers without editing
# config.py: "0" = pyzbar chain only, "1" = zxing first.
if os.environ.get("QR_DECODER_ZXING") in ("0", "1"):
    QR_DECODER_ZXING = os.environ["QR_DECODER_ZXING"] == "1"

try:
    from config import LIVE_FULLFRAME_FALLBACK, LIVE_ONE_ENHANCEMENT_PER_ROUND
except ImportError:
    LIVE_FULLFRAME_FALLBACK = True
    LIVE_ONE_ENHANCEMENT_PER_ROUND = False

try:
    from config import USE_TENSORRT_ENGINE, QR_ENGINE_PATH
except ImportError:
    USE_TENSORRT_ENGINE = False
    QR_ENGINE_PATH = None

try:
    from config import GPU_CONFIG
    YOLO_HALF = bool(GPU_CONFIG.get('half_precision', False))
except ImportError:
    YOLO_HALF = False

try:
    from config import SERIAL_MATCH_PER_ROUND
except ImportError:
    SERIAL_MATCH_PER_ROUND = 1

# zxing on a 2x enlarged copy when the plain zxing read fails on a crop no
# larger than this (pixels, longer side). Codes with only 2-3 pixels per square
# (a wide 4K view) or slightly blurred read far more often that way: on damaged
# label photos shrunk to camera size, 50 of 80 read instead of 36, ~1.5 ms each
# on a PC. 0 = off.
try:
    from config import QR_ZXING_UPSCALE_MAX_PX
except ImportError:
    QR_ZXING_UPSCALE_MAX_PX = 600

# Serial matching: the last resort for a box no decoder could read (see
# modules/serial_match.py). Without it the box simply stays unread.
try:
    from modules import serial_match
except Exception as e:
    serial_match = None
    print(f"[DETECTOR] Serial matching: NOT AVAILABLE ({e})")

# Enhancement stages tried after the plain decode fails (see _enhanced_decode).
ENHANCE_STAGES = ('upscale', 'clahe', 'sharpen', 'binary')

print("="*60)
print("DETECTOR STATUS:")
print(f"   Pyzbar: {'OK' if PYZBAR_AVAILABLE else 'FAILED'}")
print(f"   YOLO: {'OK' if YOLO_AVAILABLE else 'FAILED'}")
print(f"   QReader: {'OK (lazy)' if QREADER_AVAILABLE else 'NOT AVAILABLE'}")
print(f"   Model Path: {QR_MODEL_PATH}")
print("="*60 + "\n")


def _overlap(a, b):
    """Intersection over the smaller box: 1.0 when one box lies inside the other."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / float(max(1, smaller))


def _unread_boxes(unread, results, same=0.5):
    """The unread boxes of a round, each label once: a box that overlaps a code
    read this round (the other pass read it) or an unread box already kept is
    the same label."""
    read = [r["bbox"] for r in results if r.get("bbox")]
    kept = []
    for box in unread:
        if any(_overlap(box, other) >= same for other in read + kept):
            continue
        kept.append(tuple(int(v) for v in box))
    return tuple(kept)


class QRDetector:
    """
    QR detector: YOLO for locating codes, Pyzbar for decoding them.

    Pipeline:
    1. YOLO finds QR bounding boxes (works at a distance).
    2. Each box is cropped with some padding.
    3. Decode: Pyzbar, then upscale, CLAHE and sharpen variants if that fails.
    4. Still unread: match the visible squares against the brewery's serials.
    """

    def __init__(self):
        self.pyzbar_available = PYZBAR_AVAILABLE
        self.yolo_available = YOLO_AVAILABLE

        # YOLO runs on the GPU if CUDA is available, else on the CPU.
        # This is decided before loading the model because the TensorRT engine is GPU-only.
        self.device = 0
        try:
            import torch
            self.device = 0 if torch.cuda.is_available() else 'cpu'
        except Exception:
            self.device = 'cpu'

        # Initialize YOLO model for QR detection
        self.model = None
        self.model_kind = None
        if self.yolo_available:
            self.model, self.model_kind = self._load_yolo()

        # FP16 only applies on the GPU. A TensorRT engine has its precision
        # built in, so this flag does not affect it.
        self.half = YOLO_HALF and self.device != 'cpu'
        # One YOLO call at a time: QR / tap-to-focus (modules/focus.py) locates the
        # codes from its own thread while DetectWorker may be mid-round.
        self._model_lock = threading.Lock()
        logger.info(f"YOLO inference model={self.model_kind}, device={self.device}, "
                    f"imgsz={YOLO_IMGSZ}, half={self.half}")

        # OpenCV QR detector, used as the last fallback
        self.cv_detector = cv2.QRCodeDetector()

        # QReader is created on the first capture that needs it
        self.qreader = None
        self._qreader_initialized = False
        self.qreader_available = QREADER_AVAILABLE

        # Per-stage timing stats, logged and reset every 30 rounds so changes
        # show up in the log instead of disappearing into a since-boot average.
        self._win = self._new_window()
        self._round_times = self._new_round()

        # Live-stream state: which enhancement stage this round tries, and how
        # many QR boxes YOLO found last round (DetectWorker's idle mode reads it).
        self._round = 0
        self.last_box_count = 0
        # Area pass (live only): the stack's area in full-frame pixels, set by
        # the operator page while a layer is incomplete, else None. See
        # _run_area_pass. Assigned whole, so the detection thread reads either
        # the old value or the new one.
        self.zoom_area = None
        self._zoom_round = 0
        # QR boxes YOLO found in the last round that no reader could decode, in
        # the result coordinates (full frame). Assigned whole at the end of a
        # round; the operator page marks them on screen ("NOT READ").
        self.last_unread_boxes = ()
        self._unread = []
        # Serial-match attempts left this round, and whether the round is live.
        self._match_left = 0
        self._live = False

        # Configuration
        self.crop_padding = 15  # Pixels to add around detected QR
        self.min_crop_size = 100  # Minimum crop size for upscaling
        self.upscale_factor = 2  # How much to upscale small crops
        self.confidence_threshold = 0.5  # YOLO confidence threshold

        logger.info("QRDetector initialized - YOLO: %s, Pyzbar: %s, QReader: %s",
                    self.model is not None, self.pyzbar_available, self.qreader_available)
        logger.info("QR reading: zxing=%s (enabled=%s), then pyzbar chain",
                    ZXING_AVAILABLE, QR_DECODER_ZXING)

    def _load_yolo(self):
        """Return (model, 'tensorrt' | 'pytorch'), or (None, None) if nothing loads.

        The TensorRT engine is used only when USE_TENSORRT_ENGINE is on, the
        file exists, the GPU is available and a 4K test inference succeeds.
        Any failure falls back to QR_MODEL_PATH (best.pt), so a missing, stale
        (built on another JetPack) or broken engine never stops detection.
        """
        engine = QR_ENGINE_PATH
        if (USE_TENSORRT_ENGINE and engine is not None and Path(engine).exists()
                and self.device != 'cpu'):
            try:
                logger.info(f"Loading TensorRT engine from: {engine}")
                model = YOLO(str(engine), task='detect')
                # Warm up on a camera-sized frame. The first engine call is slow,
                # and this also proves the engine runs on this device.
                model(np.zeros((2160, 3840, 3), np.uint8), verbose=False,
                      imgsz=YOLO_IMGSZ, device=self.device)
                logger.info("TensorRT engine loaded and warmed up")
                return model, 'tensorrt'
            except Exception:
                logger.exception("TensorRT engine failed — falling back to %s", QR_MODEL_PATH)
        try:
            model_path = str(QR_MODEL_PATH)
            logger.info(f"Loading YOLO model from: {model_path}")
            model = YOLO(model_path)
            logger.info("YOLO model loaded successfully")
            return model, 'pytorch'
        except Exception:
            logger.exception("Failed to load YOLO model")
            return None, None

    def _init_qreader(self):
        """Create QReader on first use (first capture)."""
        if self._qreader_initialized:
            return self.qreader

        if self.qreader_available and QReaderClass is not None:
            try:
                logger.info("Initializing QReader model (one-time, may take a moment)...")
                start = time.perf_counter()
                self.qreader = QReaderClass(model_size='s', min_confidence=0.5)
                elapsed = (time.perf_counter() - start) * 1000
                logger.info(f"QReader initialized in {elapsed:.0f}ms")
            except Exception:
                logger.exception("QReader init failed")
                self.qreader_available = False
                self.qreader = None

        self._qreader_initialized = True
        return self.qreader

    def _decode_qreader(self, image):
        """Decode QR codes with QReader (ML-based, slower but handles harder images)."""
        if self.qreader is None:
            return []

        results = []
        try:
            start = time.perf_counter()
            texts = self.qreader.detect_and_decode(image=image)
            elapsed = (time.perf_counter() - start) * 1000

            if texts:
                for text in texts:
                    if text:
                        h, w = image.shape[:2]
                        results.append({
                            'data': text,
                            'bbox': (0, 0, w, h),
                            'source': 'qreader'
                        })

            if len(results) > 0:
                logger.info(f"QReader found {len(results)} QR codes in {elapsed:.1f}ms")

        except Exception as e:
            logger.warning(f"QReader error: {e}")

        return results

    def _decode_pyzbar(self, image):
        """Decode QR codes with Pyzbar (fast)."""
        if not self.pyzbar_available or pyzbar_decode is None:
            return []

        results = []
        try:
            # Pyzbar wants grayscale
            if len(image.shape) == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image

            decoded = pyzbar_decode(gray, symbols=[ZBarSymbol.QRCODE])

            for obj in decoded:
                text = obj.data.decode("utf-8")
                if text:
                    rect = obj.rect
                    bbox = (rect.left, rect.top, rect.left + rect.width, rect.top + rect.height)
                    results.append({'data': text, 'bbox': bbox, 'source': 'pyzbar'})
        except Exception as e:
            logger.warning(f"Pyzbar error: {e}")

        return results

    def _decode_zxing(self, image):
        """Decode QR codes with zxing-cpp (fastest on a readable crop)."""
        if not ZXING_AVAILABLE:
            return []
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
            found = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
        except Exception as e:
            logger.warning(f"zxing error: {e}")
            return []
        results = []
        for b in found:
            if b.text:
                p = b.position
                xs = (p.top_left.x, p.top_right.x, p.bottom_right.x, p.bottom_left.x)
                ys = (p.top_left.y, p.top_right.y, p.bottom_right.y, p.bottom_left.y)
                results.append({'data': b.text, 'bbox': (min(xs), min(ys), max(xs), max(ys)),
                                'source': 'zxing'})
        return results

    def _enhanced_decode(self, crop, stage=None):
        """
        Decode a crop, trying several preprocessing steps if needed.
        Order: zxing, zxing on a 2x copy (small crops), then pyzbar on raw,
        upscale, CLAHE, sharpen, binary.

        stage=None runs the whole chain (saved frames, capture). With a stage
        from ENHANCE_STAGES, only the plain decode plus that one enhancement
        runs. The live stream rotates the stage each round, so every stage is
        still tried every few rounds without one label costing all four each time.
        """
        # 0. zxing-cpp on the crop as it is. If it reads nothing (or is not
        #    installed), the pyzbar chain below runs exactly as before.
        if QR_DECODER_ZXING:
            results = self._decode_zxing(crop)
            if results:
                return results
            # 0b. Small or soft code: zxing's binarizer needs more than 2-3
            #     pixels per square, so give it a 2x enlarged copy.
            if 0 < max(crop.shape[:2]) <= QR_ZXING_UPSCALE_MAX_PX:
                big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                results = self._decode_zxing(big)
                if results:
                    return [{**r, 'bbox': tuple(v // 2 for v in r['bbox'])} for r in results]

        # 1. Original crop first (fastest)
        results = self._decode_pyzbar(crop)
        if results:
            return results

        # Convert to grayscale for preprocessing
        if len(crop.shape) == 3:
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        else:
            gray = crop.copy()

        h, w = gray.shape[:2]
        small = w < self.min_crop_size or h < self.min_crop_size
        if stage == 'upscale' and not small:
            stage = 'clahe'   # nothing to upscale, so spend this round on contrast instead

        # 2. Upscale small crops (distant codes)
        if small:
            upscaled = cv2.resize(gray, (w * self.upscale_factor, h * self.upscale_factor),
                                   interpolation=cv2.INTER_CUBIC)
            if stage in (None, 'upscale'):
                results = self._decode_pyzbar(upscaled)
                if results or stage == 'upscale':
                    return results
            gray = upscaled  # later steps work on the upscaled image

        # 3. CLAHE (Contrast Limited Adaptive Histogram Equalization)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        if stage in (None, 'clahe'):
            results = self._decode_pyzbar(enhanced)
            if results or stage == 'clahe':
                return results

        # 4. Sharpening kernel
        if stage in (None, 'sharpen'):
            kernel = np.array([[-1, -1, -1],
                              [-1,  9, -1],
                              [-1, -1, -1]])
            sharpened = cv2.filter2D(enhanced, -1, kernel)
            results = self._decode_pyzbar(sharpened)
            if results or stage == 'sharpen':
                return results

        # 5. Binary threshold as last resort
        _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        results = self._decode_pyzbar(binary)

        return results

    def _decode_opencv(self, image):
        """Fallback decode with OpenCV's QRCodeDetector."""
        results = []
        try:
            if len(image.shape) == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image

            data, points, _ = self.cv_detector.detectAndDecode(gray)

            if data and points is not None:
                pts = points[0]
                x_min = int(min(pts[:, 0]))
                y_min = int(min(pts[:, 1]))
                x_max = int(max(pts[:, 0]))
                y_max = int(max(pts[:, 1]))
                results.append({
                    'data': data,
                    'bbox': (x_min, y_min, x_max, y_max),
                    'source': 'opencv'
                })
        except Exception:
            pass

        return results

    def detect_and_decode(self, frame, use_qreader=False, live=False, gray=None, scale=1):
        """
        Detect and decode QR codes in a frame (YOLO boxes, then Pyzbar on the crops).

        Args:
            frame: Input BGR frame (never modified)
            use_qreader: Enable QReader deep scan (slower, use only for capture)
            live: True for the continuous camera stream (DetectWorker). Applies
                  LIVE_FULLFRAME_FALLBACK and LIVE_ONE_ENHANCEMENT_PER_ROUND;
                  saved-frame callers get the full pipeline.
            gray: Optional full-size grayscale frame (the camera's Y plane).
                  When given, `frame` is a 1/scale-size picture used only for
                  YOLO, and the crops are cut from `gray` with no conversion.
            scale: How many times larger `gray` is than `frame`.

        Returns: (list of decoded objects, total count); boxes are in the
        coordinates of `gray` when given, else of `frame`.
        """
        if frame is None:
            return [], 0
        crop_src = gray if gray is not None else frame
        scale = scale if gray is not None else 1

        start_total = time.perf_counter()
        self._round_times = self._new_round()

        # Capture mode may need QReader
        self._init_qreader_if_needed(use_qreader)

        all_results = []
        seen_texts = set()

        # A serial-match attempt costs tens of ms, so the live stream allows
        # SERIAL_MATCH_PER_ROUND of them; saved frames try every unread box.
        self._live = live
        self._match_left = SERIAL_MATCH_PER_ROUND if live else 1_000_000

        stage = None
        if live and LIVE_ONE_ENHANCEMENT_PER_ROUND:
            stage = ENHANCE_STAGES[self._round % len(ENHANCE_STAGES)]
            self._round += 1

        self.last_box_count = 0
        self._unread = []

        # Is this an area round? The stack's area, cut from the full frame,
        # every ZOOM_AREA_EVERY-th live round while a layer is incomplete.
        area = self.zoom_area if live and int(ZOOM_AREA_EVERY) > 0 else None
        area_round = False
        if area is not None and self.model is not None:
            self._zoom_round += 1
            area_round = self._zoom_round % max(1, int(ZOOM_AREA_EVERY)) == 0
        # ZOOM_AREA_ONLY: an area round looks at the area only, so every round
        # costs one YOLO pass instead of two. The other rounds still see the
        # whole picture (never with ZOOM_AREA_EVERY = 1, which would blind it).
        area_only = area_round and ZOOM_AREA_ONLY and int(ZOOM_AREA_EVERY) >= 2

        # 1. YOLO detection on the whole picture
        if self.model is not None and not area_only:
             self._run_yolo_detection(frame, use_qreader, all_results, seen_texts, stage,
                                      crop_src, scale)

        # 1b. The stack's area at full resolution
        if area_round:
            self._run_area_pass(crop_src, area, all_results, seen_texts, stage)
        self.last_unread_boxes = _unread_boxes(self._unread, all_results)

        # 2. Fallbacks (Pyzbar, OpenCV, QReader). These scan the full 4K frame,
        #    so the live stream skips them unless explicitly enabled.
        if not all_results and (not live or LIVE_FULLFRAME_FALLBACK):
             t0 = time.perf_counter()
             self._run_fallbacks(crop_src, use_qreader, all_results, seen_texts)
             self._round_times['fallback'] = (time.perf_counter() - t0) * 1000

        elapsed_total = (time.perf_counter() - start_total) * 1000

        self._update_stats(elapsed_total, len(all_results), use_qreader)

        return all_results, len(all_results)

    def _init_qreader_if_needed(self, use_qreader):
        if use_qreader and self.qreader_available and not self._qreader_initialized:
            logger.info("[CAPTURE MODE] Initializing QReader for deep scan...")
            self._init_qreader()

    def _run_yolo_detection(self, frame, use_qreader, all_results, seen_texts, stage=None,
                            crop_src=None, scale=1):
        """YOLO on `frame`; crops are cut from `crop_src` (default `frame`),
        with each box multiplied by `scale` to reach its coordinates."""
        if crop_src is None:
            crop_src = frame
        try:
            # imgsz is large so small QRs survive in 4K; device pins the GPU.
            t0 = time.perf_counter()
            with self._model_lock:
                yolo_results = self.model(frame, verbose=False, conf=self.confidence_threshold,
                                          imgsz=YOLO_IMGSZ, device=self.device, half=self.half)
            t1 = time.perf_counter()
            self._round_times['yolo'] = (t1 - t0) * 1000

            h, w = crop_src.shape[:2]

            for result in yolo_results:
                self.last_box_count += len(result.boxes)
                for box in result.boxes:
                    self._process_yolo_box(box, crop_src, h, w, use_qreader, all_results,
                                           seen_texts, stage, scale)
            self._round_times['decode'] = (time.perf_counter() - t1) * 1000
        except Exception:
            logger.exception("YOLO detection error")

    def _run_area_pass(self, src, area, all_results, seen_texts, stage=None):
        """YOLO on the stack's area cut from the full-resolution frame `src`
        (the Y plane or the 4K picture). The main pass sees the whole frame
        shrunk to YOLO_IMGSZ wide, where a label on the bottom layer, the one
        farthest from the camera, is only ~40 px and is sometimes missed
        (2026-11-04: layers 2 and 3 read, layer 1 missed some). Cut out, the
        same label is 2-3 times bigger to YOLO. Codes found here are decoded
        from `src` at full resolution like any other; codes already read in
        this round are skipped."""
        try:
            h, w = src.shape[:2]
            x1, y1, x2, y2 = (int(v) for v in area)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 - x1 < 64 or y2 - y1 < 64:
                return
            region = np.ascontiguousarray(src[y1:y2, x1:x2])
            if region.ndim == 2:
                region = cv2.cvtColor(region, cv2.COLOR_GRAY2BGR)
            t0 = time.perf_counter()
            with self._model_lock:
                results = self.model(region, verbose=False, conf=self.confidence_threshold,
                                     imgsz=YOLO_IMGSZ, device=self.device, half=self.half)
            # In the [TIMING] YOLO figure too, so rounds stay comparable.
            self._round_times['yolo'] += (time.perf_counter() - t0) * 1000
            boxes = 0
            pad = self.crop_padding
            for result in results:
                for box in result.boxes:
                    boxes += 1
                    bx1, by1, bx2, by2 = (int(v) for v in box.xyxy[0])
                    bbox = (bx1 + x1, by1 + y1, bx2 + x1, by2 + y1)
                    crop = src[max(0, bbox[1] - pad):min(h, bbox[3] + pad),
                               max(0, bbox[0] - pad):min(w, bbox[2] + pad)]
                    if crop.ndim == 2:
                        crop = np.ascontiguousarray(crop)
                    if crop.size > 0:
                        before = len(all_results)
                        if not self._decode_crop(crop, False, bbox, all_results, seen_texts,
                                                 stage):
                            self._unread.append(bbox)
                        for r in all_results[before:]:
                            r["area_pass"] = True
            # The same codes are counted by both passes: the larger count is
            # how many there are.
            self.last_box_count = max(self.last_box_count, boxes)
            self._round_times['zoom'] = (time.perf_counter() - t0) * 1000
        except Exception:
            logger.exception("Area pass failed")

    def read_crop(self, crop):
        """Texts read from one crop (BGR or grey) with the full reader chain
        (zxing, then pyzbar and its enhancements), [] when nothing reads.
        For the focus check (modules/focus.py): did focusing make it readable?"""
        try:
            return [r["data"] for r in self._enhanced_decode(crop) if r.get("data")]
        except Exception:
            logger.debug("read_crop failed", exc_info=True)
            return []

    def locate_boxes(self, frame, scale=1):
        """YOLO's QR boxes in `frame`, read or not, as (x1, y1, x2, y2) times
        `scale`. For QR / tap-to-focus: a soft code YOLO still finds is what the lens
        has to be focused on. Empty without a model."""
        if self.model is None:
            return []
        with self._model_lock:
            results = self.model(frame, verbose=False, conf=self.confidence_threshold,
                                 imgsz=YOLO_IMGSZ, device=self.device, half=self.half)
        return [tuple(int(v * scale) for v in box.xyxy[0])
                for result in results for box in result.boxes]

    def _process_yolo_box(self, box, frame, h, w, use_qreader, all_results, seen_texts,
                          stage=None, scale=1):
        x1, y1, x2, y2 = (int(v * scale) for v in box.xyxy[0])

        # Padding around the box helps decoding
        pad = self.crop_padding
        crop_x1 = max(0, x1 - pad)
        crop_y1 = max(0, y1 - pad)
        crop_x2 = min(w, x2 + pad)
        crop_y2 = min(h, y2 + pad)

        crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        if crop.ndim == 2:
            # A slice of the Y plane is a strided view; give the readers a
            # plain array (a small copy, the crop only).
            crop = np.ascontiguousarray(crop)

        if crop.size > 0 and not self._decode_crop(crop, use_qreader, (x1, y1, x2, y2),
                                                   all_results, seen_texts, stage):
            self._unread.append((x1, y1, x2, y2))

    def _decode_crop(self, crop, use_qreader, bbox, all_results, seen_texts, stage=None):
        """Decode one crop into `all_results`. True when it gave a code (also
        one already read this round), False when nothing could be read."""
        t0 = time.perf_counter()
        decoded = self._enhanced_decode(crop, stage)

        # In capture mode, try QReader on the crop if Pyzbar failed
        if not decoded and use_qreader and self.qreader is not None:
             logger.info("[CAPTURE] Pyzbar failed, trying QReader on crop...")
             decoded = self._decode_qreader(crop)

        # Last resort: compare the squares still visible with the brewery's
        # serials. Returns nothing unless one serial fits clearly best.
        if not decoded and serial_match is not None:
            decoded, attempted = serial_match.match_crop(
                crop, bbox if self._live else None, self._match_left > 0)
            self._match_left -= int(attempted)
        self._count_crop(decoded, (time.perf_counter() - t0) * 1000, max(crop.shape[:2]))

        read = False
        for result_item in decoded:
            text = extract_keg_id(result_item['data'])
            read = read or bool(text)
            # A normal read teaches serial matching what the labels carry.
            if text and serial_match is not None and \
                    result_item.get('source') != serial_match.SOURCE:
                serial_match.note_label(result_item['data'], text)
            if text and text not in seen_texts:
                seen_texts.add(text)
                # Report the box in full-frame coordinates, keeping the decoder source
                all_results.append({
                    'data': text,
                    'bbox': bbox,
                    'source': result_item.get('source', '')
                })
        return read

    def _run_fallbacks(self, frame, use_qreader, all_results, seen_texts):
        # Direct Pyzbar decode (fast, works for large, clearly visible QRs)
        if not all_results:
            self._collect_results(self._decode_pyzbar(frame), all_results, seen_texts)

        # OpenCV QR detector
        if not all_results:
            self._collect_results(self._decode_opencv(frame), all_results, seen_texts)

        # Last fallback: QReader on the full frame (capture mode only)
        if not all_results and use_qreader and self.qreader is not None:
            logger.info("[CAPTURE] All methods failed, trying QReader on full frame...")
            self._collect_results(self._decode_qreader(frame), all_results, seen_texts)

    def _collect_results(self, source_results, all_results, seen_texts):
        for result in source_results:
            text = extract_keg_id(result['data'])
            if text and text not in seen_texts:
                seen_texts.add(text)
                all_results.append({**result, 'data': text})

    # Which decoder read each box, from the result's source: 'zxing', 'pyzbar'
    # (raw crop or its enhancement chain), 'matched' (serial matching),
    # anything else (QReader in capture).
    _COUNTERS = ('boxes', 'read', 'zxing', 'pyzbar', 'matched', 'other', 'unread',
                 'read_ms', 'unread_ms', 'unread_px')
    _SOURCE_COUNTER = {'zxing': 'zxing', 'pyzbar': 'pyzbar', 'serial_match': 'matched'}

    @classmethod
    def _new_round(cls):
        r = {'yolo': 0.0, 'decode': 0.0, 'fallback': 0.0, 'crop_max': 0.0}
        r.update({k: 0 for k in cls._COUNTERS})
        return r

    @classmethod
    def _new_window(cls):
        w = cls._new_round()
        w.update({'n': 0, 'total': 0.0, 'fallback_runs': 0,
                  'started': time.perf_counter()})
        return w

    def _count_crop(self, decoded, ms, px=0):
        r = self._round_times
        r['boxes'] += 1
        r['crop_max'] = max(r['crop_max'], ms)
        if not decoded:
            r['unread'] += 1
            r['unread_ms'] += ms
            r['unread_px'] += px
            return
        r['read'] += 1
        r['read_ms'] += ms
        source = str(decoded[0].get('source', ''))
        r[self._SOURCE_COUNTER.get(source, 'other')] += 1

    def _update_stats(self, elapsed_total, count, use_qreader):
        win = self._win
        rt = self._round_times
        win['n'] += 1
        win['total'] += elapsed_total
        for key in ('yolo', 'decode', 'fallback') + self._COUNTERS:
            win[key] += rt.get(key, 0)
        win['crop_max'] = max(win['crop_max'], rt.get('crop_max', 0.0))
        if rt.get('fallback'):
            win['fallback_runs'] += 1

        if win['n'] >= 30:  # log and reset every 30 rounds
            self._log_window(win)
            self._win = self._new_window()

        if use_qreader:
            logger.info(f"[CAPTURE] Detection complete: {count} QRs in {elapsed_total:.0f}ms "
                        f"[yolo {rt['yolo']:.0f} | decode {rt['decode']:.0f} | "
                        f"fallback {rt['fallback']:.0f} ms] boxes {rt['boxes']}, "
                        f"read {rt['read']}, unread {rt['unread']}")

    @staticmethod
    def _log_window(win):
        """Two [TIMING] lines per 30 rounds: where the time went, and how the
        boxes were read. grep TIMING in the log to follow it."""
        n = win['n']
        wall = max(time.perf_counter() - win['started'], 0.001)
        avg = win['total'] / n
        logger.info(
            "[TIMING] %d rounds in %.1f s = %.1f rounds/s | round %.0f ms = YOLO %.0f "
            "+ decode %.0f + fallback %.0f (fallback ran %dx) | %.1f FPS potential",
            n, wall, n / wall, avg, win['yolo'] / n, win['decode'] / n,
            win['fallback'] / n, win['fallback_runs'], 1000 / max(avg, 0.001))
        read, unread = win['read'], win['unread']
        logger.info(
            "[TIMING] boxes %.1f/round, read %.1f/round | per box: read %.1f ms, "
            "unread %.1f ms, slowest %.0f ms | read by zxing %d, pyzbar %d, other %d "
            "| unread %d | matched from serial list %d | unread box %.0f px",
            win['boxes'] / n, read / n, win['read_ms'] / max(read, 1),
            win['unread_ms'] / max(unread, 1), win['crop_max'], win['zxing'],
            win['pyzbar'], win['other'], unread, win['matched'],
            win['unread_px'] / max(unread, 1))

    def shutdown(self):
        """Release the models and free GPU memory."""
        logger.info("Shutting down QRDetector")
        if self.model is not None:
            del self.model
            self.model = None
        if self.qreader is not None:
            del self.qreader
            self.qreader = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.info("CUDA cache cleared")
        except Exception:
            pass


# Shared detector for detect_qr_advanced(), built on first use so YOLO is not
# reloaded on every short-layer send. The lock guards the build and serialises
# use: process_worker sends on a 4-thread pool, and one YOLO model must not run
# on two threads at once.
_advanced_detector = None
_advanced_lock = threading.Lock()


def detect_qr_advanced(image_path):
    """Run full QR detection on an image file."""
    global _advanced_detector
    print("\n[ADVANCED] --------------------------------------------------")
    print(f"[ADVANCED] Starting Advanced Detection on: {image_path}")
    print("[ADVANCED] --------------------------------------------------")

    try:
        frame = cv2.imread(str(image_path))
        if frame is None:
            print("[ADVANCED] ERROR: Could not read image file")
            return [], 0

        print(f"[ADVANCED] Image loaded successfully: {frame.shape}")

        with _advanced_lock:
            if _advanced_detector is None:
                _advanced_detector = QRDetector()
            results, count = _advanced_detector.detect_and_decode(frame)

        print(f"[ADVANCED] Detection finished. Total QRs found: {count}")
        if count > 0:
            print(f"[ADVANCED] Detected QR Data: {[r['data'] for r in results]}")
        else:
            print("[ADVANCED] No QR codes found.")

        print("[ADVANCED] --------------------------------------------------\n")
        return results, count
    except Exception as e:
        logger.exception("detect_qr_advanced Error")
        print(f"[ADVANCED] EXCEPTION: {e}")
        return [], 0
