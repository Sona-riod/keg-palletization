# modules/camera.py - camera manager with a background capture thread
"""
Camera manager that reads frames on its own thread.

Built for the Jetson Orin:
- A dedicated capture thread, so the UI never blocks on the camera
- A lock around the shared latest frame
- Periodic FPS / capture-time logging
"""
import cv2
import logging
import numpy as np
import re
import shutil
import subprocess  # nosec B404
import threading
import time
from config import CAMERA_CONFIG

# A video device node or a /dev/v4l/by-id|by-path link: never something
# v4l2-ctl would read as an option.
_V4L2_DEVICE = re.compile(r"/dev/video\d{1,3}|/dev/v4l/by-(?:id|path)/[\w.:+-]+")

try:
    from modules import perf_log      # [PERF] efficiency log; optional
except Exception:  # pragma: no cover
    perf_log = None

try:
    from config import CAPTURE_PREVIEW_SUBSAMPLE as _PREVIEW_SUBSAMPLE
except ImportError:
    _PREVIEW_SUBSAMPLE = False
try:
    from config import CAPTURE_CONVERT_ON_DEMAND as _CONVERT_ON_DEMAND
except ImportError:
    _CONVERT_ON_DEMAND = False


def _i420_view(raw, w, h):
    """The raw I420 buffer as (h * 3/2, w) rows, or None if it is not one."""
    if raw is None or w <= 0 or h <= 0 or raw.size != w * h * 3 // 2:
        return None
    return raw.reshape(h * 3 // 2, w)


def _i420_preview(raw, w, h, k):
    """BGR preview of an I420 frame at 1/k size, built from every k-th pixel of
    each plane: a small conversion instead of converting the whole 4K frame."""
    yuv = raw.reshape(-1)
    y = yuv[:w * h].reshape(h, w)[::k, ::k]
    u = yuv[w * h:w * h * 5 // 4].reshape(h // 2, w // 2)[::k, ::k]
    v = yuv[w * h * 5 // 4:].reshape(h // 2, w // 2)[::k, ::k]
    small = np.concatenate((y.ravel(), u.ravel(), v.ravel())).reshape(-1, w // k)
    return cv2.cvtColor(small, cv2.COLOR_YUV2BGR_I420)

# Setup debug logger
logger = logging.getLogger("CameraManager")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(handler)

log = logging.getLogger(__name__)


class CameraManager:
    """
    Opens, reads and closes the camera.

    Frames are read on a background thread; callers get the latest one
    under a lock. FPS and capture time are logged once a second.
    """

    def __init__(self, config=None):
        # Falls back to the module-level CAMERA_CONFIG
        self.config = config if config is not None else CAMERA_CONFIG
        self.cap = None
        self.is_running = False

        # Background capture thread
        self._capture_thread = None
        self._frame_lock = threading.Lock()
        # Guards the capture device itself (read/set), not the shared frame.
        # A VideoCapture.set() racing a read() can wedge a v4l2 node, and the
        # focus control (modules/focus.py) sets CAP_PROP_AUTOFOCUS from a worker
        # thread while this loop is reading continuously.
        self._device_lock = threading.Lock()
        self._latest_frame = None
        self._latest_ret = False
        self._stop_event = threading.Event()

        # Display preview, downscaled in the capture thread so the GUI thread
        # never resizes a 4K frame. Off until set_preview_size() is called.
        self._preview_size = None          # (w, h)
        self._preview_interval = 0.0       # min seconds between preview resizes
        self._last_preview_time = 0.0
        self._latest_preview = None
        self._latest_preview_src = None    # (w, h) of the frame it came from

        # Performance tracking
        self._frame_count = 0
        self._last_fps_time = time.time()
        self._current_fps = 0.0
        self._shape_logged = False   # log the actual decoded frame size once
        # [PERF]: frame numbers, to count how many frames anyone actually used,
        # and whether reads are split into grab (wait) + retrieve (convert).
        self._frame_seq = 0
        self._used_seq = 0
        self._split_read = False
        self._last_frame_time = None       # monotonic time of the last frame published

        # Convert on demand (CAPTURE_CONVERT_ON_DEMAND): the capture thread keeps
        # the camera's raw YU12 (I420) frame and only frames someone takes are
        # converted to BGR, each at most once. _latest_frame then holds the
        # converted copy of the newest raw frame, or None until someone asks.
        self._raw_mode = False             # raw I420 frames are being delivered
        self._raw_checked = False          # first raw frame checked
        self._raw_size = None              # (w, h) of the raw frames
        self._latest_raw = None

        # Idle (a second camera standing by, modules/camera_select.py): frames
        # are only taken off the device (grab), never decoded or published, so
        # a 4K MJPEG webcam costs almost no CPU while the other camera reads.
        self._idle = False

        logger.info(f"CameraManager initialized with config: {self.config.get('type', 'unknown')}")

    def start(self):
        """Open the camera from the current config and start the capture thread."""
        if self.is_running and self.cap is not None:
            logger.warning("Camera is already running.")
            return True

        cam_type = self.config.get('type', 'v4l2').lower()
        logger.info(f"Starting camera type: {cam_type}")

        try:
            # 1. V4L2 (USB Webcams)
            if cam_type == 'v4l2':
                device = self.config.get('device', 0)
                logger.debug(f"Opening V4L2 device: {device}")
                self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.get('width', 1920))
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.get('height', 1080))
                self.cap.set(cv2.CAP_PROP_FPS, self.config.get('fps', 30))
                # A one-frame buffer keeps latency low
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                # Raw frames, converted only when used. If OpenCV does not
                # accept this, frames stay converted as before.
                self._raw_mode = bool(_CONVERT_ON_DEMAND
                                      and self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 0))
                self._raw_checked = False
                self._raw_size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
                                  int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0))
                logger.info(f"[CAMERA] Started V4L2: {device}")

            # 1b. Generic webcam (Windows/Mac/Linux)
            elif cam_type == 'webcam':
                device = self.config.get('device', 0)
                logger.debug(f"Opening Webcam device: {device}")
                # Default backend for the widest compatibility. On Windows,
                # cv2.CAP_DSHOW is sometimes faster. A /dev path (the layer-1
                # webcam on the iCam) goes through V4L2 so MJPG can be asked for.
                if isinstance(device, str) and device.startswith("/dev/"):
                    self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
                else:
                    self.cap = cv2.VideoCapture(device)
                fourcc = str(self.config.get('fourcc') or "")
                if len(fourcc) == 4:
                    # Before the size: many webcams only offer 4K as MJPG.
                    self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.get('width', 640))
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.get('height', 480))
                self.cap.set(cv2.CAP_PROP_FPS, self.config.get('fps', 30))
                self._apply_lens_controls()
                logger.info(f"[CAMERA] Started Webcam: {device}")

            # 2. RTSP (IP Cameras)
            elif cam_type == 'rtsp':
                url = self.config.get('rtsp_url')
                logger.debug(f"Opening RTSP stream: {url}")
                self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                logger.info(f"[CAMERA] Started RTSP: {url}")

            # 3. CSI (Jetson/Pi Camera Module)
            elif cam_type == 'csi':
                sensor_id = self.config.get('sensor_id', 0)
                width = self.config.get('width', 1920)
                height = self.config.get('height', 1080)
                fps = self.config.get('fps', 30)

                gst_pipeline = (
                    f"nvarguscamerasrc sensor-id={sensor_id} ! "
                    f"video/x-raw(memory:NVMM), width={width}, height={height}, "
                    f"format=NV12, framerate={fps}/1 ! "
                    "nvvidconv ! video/x-raw, format=BGRx ! videoconvert ! video/x-raw, format=BGR ! appsink"
                )
                logger.debug(f"Opening CSI with pipeline: {gst_pipeline}")
                self.cap = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
                logger.info(f"[CAMERA] Started CSI: Sensor {sensor_id}")

            # 4. Video file (testing)
            elif cam_type == 'file':
                file_path = self.config.get('file_path', 'sample.mp4')
                logger.debug(f"Opening video file: {file_path}")
                self.cap = cv2.VideoCapture(file_path)
                logger.info(f"[CAMERA] Started FILE: {file_path}")

            # 5. Test (dummy black frame)
            elif cam_type == 'test':
                self.cap = self._create_dummy_cap()
                logger.info("[CAMERA] Started TEST mode")

            else:
                raise ValueError(f"Unknown Camera Type: {cam_type}")

            if not self.cap.isOpened():
                raise RuntimeError("Camera failed to open (isOpened=False)")

            # cap.read() is grab() + retrieve(); doing the two separately lets
            # [PERF] time the wait for the camera apart from the conversion.
            self._split_read = (cam_type != 'test' and hasattr(self.cap, 'grab')
                                and hasattr(self.cap, 'retrieve'))
            self._record_stream_info()

            self.is_running = True

            self._stop_event.clear()
            self._capture_thread = threading.Thread(
                target=self._capture_loop,
                name="CameraCapture",
                daemon=True
            )
            self._capture_thread.start()
            logger.info("[CAMERA] Background capture thread started")

            return True

        except Exception:
            logger.exception("Critical Camera Error during start")
            self.cap = None
            self.is_running = False
            return False

    def _capture_loop(self):
        """Capture thread: read frames until stopped."""
        logger.info("[CAMERA] Capture loop started")

        while not self._stop_event.is_set():
            if self.cap is None:
                time.sleep(0.01)
                continue

            if self._idle and self._split_read:
                # Keep the driver's buffer fresh so the first frame after
                # waking is current, without decoding anything.
                try:
                    with self._device_lock:
                        self.cap.grab()
                except Exception:
                    logger.debug("Idle grab failed", exc_info=True)
                    time.sleep(0.1)
                continue

            try:
                start = time.perf_counter()
                with self._device_lock:
                    if self._split_read:
                        ret = self.cap.grab()                 # wait for the camera
                        grabbed = time.perf_counter()
                        ret, frame = self.cap.retrieve() if ret else (False, None)
                    else:
                        ret, frame = self.cap.read()
                        grabbed = None
                done = time.perf_counter()
                capture_time = (done - start) * 1000

                # Raw mode: check the first frame really is I420 at the stream
                # size. If not, go back to converted frames for good.
                if self._raw_mode and ret and frame is not None and not self._raw_checked:
                    self._raw_checked = True
                    w, h = self._raw_size
                    if _i420_view(frame, w, h) is None:
                        logger.warning(f"[CAMERA] Raw frames are not I420 {w}x{h} (got shape "
                                       f"{frame.shape}); converting every frame as before")
                        with self._device_lock:
                            self.cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
                        self._raw_mode = False
                        continue
                    self._shape_logged = True
                    logger.info(f"[CAMERA] Raw I420 {w}x{h} frames: converted to BGR only "
                                f"when used (CAPTURE_CONVERT_ON_DEMAND)")

                # One-time check that we decode the resolution we asked for. REST init
                # sets 4K; this confirms OpenCV is not clamping to something else.
                if not self._shape_logged and frame is not None:
                    self._shape_logged = True
                    logger.info(f"[CAMERA] ACTUAL decoded frame shape: {frame.shape} "
                                f"(requested {self.config.get('width')}x{self.config.get('height')})")

                # Loop video files
                if not ret and self.config.get('type') == 'file':
                    with self._device_lock:
                        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, frame = self.cap.read()

                raw = bool(self._raw_mode and ret and frame is not None)
                with self._frame_lock:
                    self._latest_ret = ret
                    if raw:
                        self._latest_raw = frame
                        self._latest_frame = None      # converted when someone asks
                    else:
                        self._latest_raw = None
                        self._latest_frame = frame
                    if ret and frame is not None:
                        self._frame_seq += 1
                        self._last_frame_time = time.monotonic()

                if perf_log is not None and ret and frame is not None:
                    perf_log.inc("camera.frames")
                    if grabbed is not None:
                        perf_log.add("camera.wait_ms", (grabbed - start) * 1000)
                        # Raw: retrieve() only copies the buffer; the conversion
                        # is timed where it happens (_current_bgr).
                        perf_log.add("camera.copy_ms" if raw else "camera.convert_ms",
                                     (done - grabbed) * 1000)

                self._update_preview(ret, frame, raw)

                # FPS counter
                self._frame_count += 1
                now = time.time()
                elapsed = now - self._last_fps_time
                if elapsed >= 1.0:
                    self._current_fps = self._frame_count / elapsed
                    self._frame_count = 0
                    self._last_fps_time = now
                    logger.debug(f"Camera FPS: {self._current_fps:.1f}, capture time: {capture_time:.1f}ms")

            except Exception:
                logger.exception("Capture error")
                time.sleep(0.1)

        logger.info("[CAMERA] Capture loop stopped")

    def get_frame(self):
        """
        Return a copy of the latest frame. Does not block on the camera.
        """
        ret, frame = self._current_bgr()
        if frame is None:
            return False, None
        # A copy, so the caller can modify it freely
        return ret, frame.copy()

    def get_frame_no_copy(self):
        """
        Return the latest frame without copying. Do not modify it.

        Safe for read-only callers: cap.read() returns a new array for every
        frame, and the capture thread only replaces the reference, it never
        writes into an array it has already published.
        """
        return self._current_bgr()

    def wait_for_new_frames(self, count=1, timeout=2.0):
        """Block until `count` frames newer than now have arrived.

        For QR / tap-to-focus (modules/focus.py): after the lens moves, a frame that
        was already captured or queued still shows the old focus. Returns False
        on timeout (camera stalled)."""
        with self._frame_lock:
            target = self._frame_seq + count
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._frame_lock:
                if self._frame_seq >= target:
                    return True
            time.sleep(0.01)
        return False

    def get_detect_inputs(self, width):
        """(ret, small, gray, k) for live detection, straight from the raw frame.

        small is a BGR picture `width` pixels wide built from every k-th pixel
        (for YOLO); gray is the full-size brightness (Y) plane of the same
        frame, a view with no conversion (for cutting the QR crops). A box
        found on small maps to gray by multiplying it by k.

        Returns None when there is no raw I420 frame to use (not in raw mode,
        no frame yet, or `width` is not an exact 1/k of the frame). The caller
        then uses get_frame_no_copy() as before. Do not modify the arrays."""
        if not self._raw_mode or self._raw_size is None:
            return None
        w, h = self._raw_size
        if width <= 0 or w % width:
            return None
        k = self._subsample_factor(w, h, (width, h * width // w), raw=True)
        if not k:
            return None
        # Raw frame and its number together, so small and gray are one frame.
        with self._frame_lock:
            ret, raw, seq = self._latest_ret, self._latest_raw, self._frame_seq
            if raw is None:
                return None
            self._mark_used(seq)
        # Outside the lock, like _current_bgr(): the capture thread publishes a
        # new array per frame and never writes into this one.
        t0 = time.perf_counter()
        small = _i420_preview(raw, w, h, k)
        gray = raw.reshape(-1)[:w * h].reshape(h, w)
        if perf_log is not None:
            perf_log.add("camera.detect_small_ms", (time.perf_counter() - t0) * 1000)
        return ret, small, gray, k

    def _current_bgr(self):
        """(ret, BGR frame) for the newest frame.

        In raw mode the newest frame is converted here, outside the lock so the
        capture thread is never held up, and cached so a second caller of the
        same frame does not convert it again."""
        with self._frame_lock:
            ret, frame = self._latest_ret, self._latest_frame
            raw, seq = self._latest_raw, self._frame_seq
            if frame is not None or raw is None:
                if frame is not None:
                    self._mark_used(seq)
                return ret, frame
        w, h = self._raw_size
        t0 = time.perf_counter()
        frame = cv2.cvtColor(_i420_view(raw, w, h), cv2.COLOR_YUV2BGR_I420)
        if perf_log is not None:
            perf_log.add("camera.convert_ms", (time.perf_counter() - t0) * 1000)
        with self._frame_lock:
            if self._frame_seq == seq:
                self._latest_frame = frame
            self._mark_used(seq)
        return ret, frame

    def _mark_used(self, seq):
        """[PERF]: count each frame once when someone first takes it.
        Caller holds _frame_lock."""
        if seq > self._used_seq:
            self._used_seq = seq
            if perf_log is not None:
                perf_log.inc("camera.frames_used")

    def _record_stream_info(self):
        """[PERF]: what was asked for and what the device actually accepted."""
        try:
            requested = self.config.get('fps', '?')
            driver_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0) or None
            code = int(self.cap.get(cv2.CAP_PROP_FOURCC) or 0)
            fourcc = ("".join(chr((code >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ")
                      if code else "")
            w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            fmt = f"{fourcc or '?'} {w}x{h}"
            logger.info(f"[CAMERA] Stream: requested {requested} fps, driver reports "
                        f"{driver_fps if driver_fps else '?'} fps, format {fmt}")
            if perf_log is not None:
                perf_log.set_info("camera.requested_fps", requested)
                perf_log.set_info("camera.driver_fps", driver_fps)
                perf_log.set_info("camera.format", fmt)
        except Exception:
            logger.debug("Could not read the stream info", exc_info=True)

    def set_preview_size(self, width, height, min_interval=0.0):
        """Make the capture thread also produce a (width, height) display preview,
        at most once every `min_interval` seconds. Match the screen refresh so it
        does not resize frames nobody will draw."""
        self._preview_interval = max(0.0, float(min_interval))
        self._preview_size = (int(width), int(height))

    @staticmethod
    def _subsample_factor(w, h, size, raw=False):
        """k when the preview is exactly 1/k of the frame (4K -> 1280x720: 3),
        else 0. For raw I420 the colour planes must divide evenly too."""
        tw, th = size
        if tw <= 0 or th <= 0 or w % tw or h % th or w // tw != h // th:
            return 0
        k = w // tw
        if raw and (th % 2 or tw % 2 or (h // 2) % k or (w // 2) % k):
            return 0
        return k

    def _update_preview(self, ret, frame, raw=False):
        size = self._preview_size
        if size is None or not ret or frame is None:
            return
        now = time.perf_counter()
        if now - self._last_preview_time < self._preview_interval:
            return
        self._last_preview_time = now
        try:
            # Display only; detection always uses the full 4K frame.
            if raw:
                w, h = self._raw_size
                k = self._subsample_factor(w, h, size, raw=True)
                if k:
                    # Every k-th pixel of the raw planes, converted at preview size.
                    preview = _i420_preview(frame, w, h, k)
                else:
                    bgr = cv2.cvtColor(_i420_view(frame, w, h), cv2.COLOR_YUV2BGR_I420)
                    preview = cv2.resize(bgr, size, interpolation=cv2.INTER_LINEAR)
            else:
                h, w = frame.shape[:2]
                # Nearest pixel (every 3rd pixel for 4K -> 1280x720): about half
                # the time of INTER_LINEAR. A numpy [::3, ::3] copy was measured
                # 4x slower than either, so OpenCV does the picking.
                # INTER_LINEAR, not INTER_AREA, otherwise: INTER_AREA on a 4K
                # frame measurably slowed capture.
                preview = cv2.resize(frame, size, interpolation=(
                    cv2.INTER_NEAREST if _PREVIEW_SUBSAMPLE else cv2.INTER_LINEAR))
            if perf_log is not None:
                perf_log.inc("camera.previews")
                perf_log.add("camera.preview_ms", (time.perf_counter() - now) * 1000)
        except Exception:
            logger.debug("Preview resize failed", exc_info=True)
            return
        with self._frame_lock:
            self._latest_preview = preview
            self._latest_preview_src = (w, h)

    def get_preview(self):
        """Return (preview, (src_w, src_h)) or (None, None). The preview is
        shared, so copy it before drawing on it."""
        with self._frame_lock:
            return self._latest_preview, self._latest_preview_src

    def set_idle(self, idle):
        """Stand by (True) or deliver frames again (False). While idle no new
        frame is published, so wait_for_new_frames() and frame_age() see it.

        On waking, the preview from before the stand-by (minutes old) is
        dropped, so the screen never shows that old picture: until the first
        new frame, get_preview() returns (None, None) and the caller keeps
        showing what it showed before."""
        idle = bool(idle)
        if self._idle and not idle:
            with self._frame_lock:
                self._latest_preview = None
                self._latest_preview_src = None
        self._idle = idle

    def frame_age(self):
        """Seconds since the last frame was published, or None before the first."""
        t = self._last_frame_time
        return None if t is None else time.monotonic() - t

    def _apply_lens_controls(self):
        """Autofocus / focus / exposure from the config, for a USB webcam.
        Keys that are absent are not touched, so a config without them opens
        the camera exactly as before. A control the driver refuses is logged."""
        cfg = self.config
        wanted = []
        if 'autofocus' in cfg:
            wanted.append(("autofocus", cv2.CAP_PROP_AUTOFOCUS, 1 if cfg['autofocus'] else 0))
        if cfg.get('focus') is not None:
            wanted.append(("focus", cv2.CAP_PROP_FOCUS, float(cfg['focus'])))
        if cfg.get('exposure') is not None:
            # V4L2: 1 = manual exposure, 3 = aperture priority (automatic).
            wanted.append(("manual exposure", cv2.CAP_PROP_AUTO_EXPOSURE, 1))
            wanted.append(("exposure", cv2.CAP_PROP_EXPOSURE, float(cfg['exposure'])))
        for name, prop, value in wanted:
            try:
                ok = bool(self.cap.set(prop, value))
            except Exception:
                ok = False
            if ok:
                logger.info(f"[CAMERA] {name} set to {value}")
            else:
                logger.warning(f"[CAMERA] The camera did not accept {name} = {value}")
        self._apply_power_line()

    def _apply_power_line(self):
        """Mains frequency of the lights (config 'power_line_hz': 50 or 60), so
        a USB webcam's auto exposure does not pulse or band under factory
        lights. Set with v4l2-ctl (OpenCV has no property for it). Linux /dev
        devices only; any failure is logged and the camera works as before."""
        hz = self.config.get('power_line_hz')
        device = self.config.get('device')
        value = {50: 1, 60: 2}.get(hz)
        if value is None or not isinstance(device, str):
            return
        if not _V4L2_DEVICE.fullmatch(device):
            return
        tool = shutil.which("v4l2-ctl")
        if not tool:
            logger.info("[CAMERA] v4l2-ctl not installed - power-line frequency not set")
            return
        try:
            r = subprocess.run([tool, "-d", device, "-c",  # nosec B603
                                f"power_line_frequency={value}"],
                               capture_output=True, text=True, timeout=3, check=False)
            if r.returncode == 0:
                logger.info(f"[CAMERA] power-line frequency set to {hz} Hz")
            else:
                logger.warning(f"[CAMERA] The camera did not accept power-line frequency "
                               f"{hz} Hz: {(r.stderr or '').strip()[:200]}")
        except Exception:
            logger.warning("[CAMERA] Could not set the power-line frequency", exc_info=True)

    def set_property(self, prop, value):
        """Set one driver property (autofocus, exposure, ...) on the live capture.

        Used by the focus control (modules/focus.py) on the webcam / generic
        OpenCV path, where the lens is driven through the capture instead of
        the vendor REST daemon.

        Takes the device lock because the capture thread is reading the same
        device, and a set() racing a read() can wedge a v4l2 node. Costs the
        caller at most one frame time.

        Returns False if the driver does not support the control (many UVC
        webcams have no motorised lens). The caller should tell the operator
        instead of assuming the lens moved.
        """
        if self.cap is None:
            return False
        try:
            with self._device_lock:
                return bool(self.cap.set(int(prop), float(value)))
        except Exception:
            logger.debug("Could not set camera property %s=%s", prop, value,
                         exc_info=True)
            return False

    def stop(self):
        """Stop the capture thread and release the camera."""
        logger.info("[CAMERA] Stopping camera...")

        self._stop_event.set()

        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2.0)
            logger.debug("[CAMERA] Capture thread joined")

        if self.cap:
            self.cap.release()

        self.cap = None
        self.is_running = False
        self._latest_frame = None
        self._latest_raw = None
        self._latest_preview = None
        logger.info("[CAMERA] Stopped")

    def _create_dummy_cap(self):
        """Return a fake capture object for test mode."""
        config = self.config

        class TestCap:
            def __init__(self):
                self._config = config

            def read(self):
                # Black frame with a timestamp
                h = self._config.get('height', 1080)
                w = self._config.get('width', 1920)
                frame = np.zeros((h, w, 3), np.uint8)
                cv2.putText(frame, f"TEST MODE: {time.strftime('%H:%M:%S')}",
                           (50, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                return True, frame

            def isOpened(self):
                return True

            def release(self):
                """Dummy method: no resource to release for test camera"""
                pass

            def set(self, *args):
                """Dummy method: property setting not supported in test mode"""
                pass

            def get(self, *args):
                """Dummy method: property getting not supported in test mode"""
                return 0

        return TestCap()
