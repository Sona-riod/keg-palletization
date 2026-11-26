"""
Background QThread workers around the palletization backend.

Every heavy or blocking call runs here so the GUI thread never stalls:
    - InitWorker        : build db / api / printer / detector / camera  (deferred_init)
    - CameraInitBridge  : Advantech REST camera-init sequence            (CameraInitSplash)
    - FocusWorker       : operator reset auto-focus on the running machine (focus.py)
    - FocusSearchWorker : focus on the QR codes / a tapped point (focus.py)
    - FocusReadWorker   : read the lens position for the Camera Focus card
    - DetectWorker      : continuous YOLO+QR decode on the live 4K frame (Lane 2)
    - BlacklistFetcher  : cloud blacklist fetch
    - BatchSubmitWorker : process_worker.submit_batch(...).result()       (pallet report)
    - PrintWorker       : Settings test print only; pallet labels go
                          through modules/print_queue.py (one print thread)
    - CloudSyncWorker   : POST /current-config (macId) -> keg count/type

All cross-thread communication uses queued Qt signals.
"""

import time
import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal

from .i18n import tr

try:
    from modules import perf_log      # [PERF] efficiency log; optional
except Exception:  # pragma: no cover
    perf_log = None

log = logging.getLogger("ui_pyside.workers")


# Captured-frame save (off the GUI thread)
def save_frame_async(frame, path):
    """Write `frame` to `path` as JPEG on a background thread and return the thread.

    Encoding a 4K JPEG on the GUI thread freezes the touchscreen. `frame` must
    be a private copy (camera.get_frame()) because the caller keeps running
    while it is encoded. Anything that reads `path` must call
    wait_for_frame_save() first."""
    import cv2

    def _write():
        try:
            if not cv2.imwrite(path, frame):
                log.warning("imwrite returned False for %s", path)
        except Exception as e:
            log.warning("imwrite failed: %s", e)

    t = threading.Thread(target=_write, name="FrameSave", daemon=True)
    t.start()
    return t


def wait_for_frame_save(thread, timeout=30.0):
    """Block until a save_frame_async() write has finished.

    Call from a worker thread, not the GUI. Returns at once for a finished or
    missing thread."""
    if thread is not None and thread.is_alive():
        thread.join(timeout)


# Heavy init: build all backend components off the GUI thread
class InitWorker(QThread):
    """Mirrors SimpleKegHMI.deferred_init() + start_detection_system()."""
    progress = Signal(str)            # status text for the splash
    step     = Signal(int, int)       # (step just started, total) for the splash %
    ready    = Signal(object)         # dict of components
    failed   = Signal(str)

    # Splash % steps. The model load is by far the longest, so it spans two
    # steps (3 -> 5) of the bar.
    STEPS = 5

    def run(self):
        try:
            from config import CAMERA_CONFIG

            self.step.emit(1, self.STEPS)
            self.progress.emit(tr("Initialising Database & API…"))
            from modules.database import DatabaseManager
            from modules.api_sender import APISender
            import modules.process_worker as process_worker
            from modules.printer import get_printer

            database = DatabaseManager()

            # Camera login happens before the API sender's first calls so the
            # blacklist / operator / beer-type reads already carry the token.
            # A failure is not fatal: the line runs offline, reports queue up,
            # and the sign-in page shows the reason.
            self.step.emit(2, self.STEPS)
            self.progress.emit(tr("Signing in to the cloud…"))
            from modules.camera_auth import get_camera_auth
            camera_auth = get_camera_auth().login()

            api_sender = APISender()                 # single instance, one retry monitor
            process_worker.set_api_sender(api_sender)
            # Serial matching: load the saved serial list now, refresh it from
            # the cloud on its own thread (does nothing when QR_SERIAL_MATCH is off).
            try:
                from modules import serial_match
                serial_match.start(api_sender)
            except Exception:
                log.exception("Serial list start failed — serial matching off")
            printer = get_printer()                  # any installed/default printer
            # Every real label goes through one print thread (started by the
            # operator page). Leftover temp label images are cleared first.
            from modules.printer import cleanup_temp_labels
            from modules.print_queue import PrintQueue, set_label_queue
            cleanup_temp_labels()
            print_queue = PrintQueue(printer)
            # Upload threads hand confirmed-pallet labels straight to it.
            set_label_queue(print_queue)

            # Fetch the dashboard's cloud data on side threads now so it overlaps
            # the model load. Operators are not included; the sign-in page
            # fetches its own list.
            preload_started = time.monotonic()
            preload = self._start_cloud_preload(api_sender)

            self.step.emit(3, self.STEPS)
            self.progress.emit(tr("Loading…"))
            qr_detector = self._load_detector()

            self.step.emit(5, self.STEPS)
            self.progress.emit(tr("Starting Camera Hardware…"))
            from modules.camera import CameraManager
            camera = CameraManager(CAMERA_CONFIG)
            cam_ok = False
            try:
                cam_ok = camera.start()
            except Exception as e:
                log.exception("Camera start failed")
                self.progress.emit(tr("Camera error: {error}", error=str(e)[:50]))

            # Layer 1 from a USB webcam plugged into the iCam, when there is one
            # (modules/camera_select.py). None: no wrapper, the iCam alone, as before.
            if cam_ok:
                try:
                    import config as _config
                    from modules.camera_select import LayerCamera
                    layered = LayerCamera.from_config(camera, _config)
                    if layered is not None:
                        layered.look_for_webcam(force=True)
                        camera = layered
                except Exception:
                    log.exception("Layer-1 webcam setup failed - the iCam reads every layer")

            self.progress.emit(tr("Loading cloud data (blacklist, beer types)…"))
            cloud_data = self._finish_cloud_preload(preload, preload_started)

            self.ready.emit({
                "database":    database,
                "api_sender":  api_sender,
                "printer":     printer,
                "print_queue": print_queue,
                "qr_detector": qr_detector,
                "camera":      camera,
                "camera_ok":   cam_ok,
                "camera_auth": camera_auth,   # result of the camera login
                # {"blacklist", "beer_types", "cloud_config"}. A key is missing
                # if its fetch did not finish in time; the page fetches it then.
                "cloud_data":  cloud_data,
            })
        except Exception as e:
            log.exception("InitWorker failed")
            self.failed.emit(str(e))

    @staticmethod
    def _start_cloud_preload(api_sender):
        import config
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="CloudPreload")
        futures = {"blacklist": pool.submit(api_sender.get_blacklist),
                   "cloud_config": pool.submit(fetch_cloud_config)}
        if config.BEER_TYPE_ENABLED:
            futures["beer_types"] = pool.submit(api_sender.get_beer_types)
        pool.shutdown(wait=False)          # threads finish on their own
        return futures

    @staticmethod
    def _finish_cloud_preload(futures, started):
        """Collect what finished, waiting at most CLOUD_PRELOAD_TIMEOUT_SECONDS
        from the start of the preload. Never raises."""
        import config
        from concurrent.futures import TimeoutError as FutureTimeout
        deadline = started + float(config.CLOUD_PRELOAD_TIMEOUT_SECONDS)
        data = {}
        for key, fut in futures.items():
            try:
                data[key] = fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                log.warning("Cloud preload: %s not ready in time — fetched later", key)
            except Exception:
                log.exception("Cloud preload: %s failed", key)
        log.info("Cloud preload done in %.1f s: %s", time.monotonic() - started,
                 ", ".join(data) or "nothing")
        return data

    @staticmethod
    def _load_detector():
        """Same graceful fallback as main.get_qr_detector_class()."""
        try:
            from modules.detector import QRDetector
            return QRDetector()
        except Exception as e:
            log.warning("QRDetector import failed: %s — using dummy", e)

            class _DummyQRDetector:
                def detect_and_decode(self, *args, **kwargs):
                    return [], 0

                def shutdown(self):
                    pass
            return _DummyQRDetector()


# Advantech camera REST init (port of CameraInitSplash flow)
class CameraInitBridge(QObject):
    """Wraps modules.camera_init.CameraInitializer, marshalling its worker-thread
    callbacks onto Qt signals (delivered to the GUI thread)."""
    status = Signal(str)
    step   = Signal(int, int)
    done   = Signal(bool, str)

    def start(self):
        try:
            from modules.camera_init import CameraInitializer
        except Exception as e:
            log.warning("CameraInitializer unavailable: %s", e)
            self.done.emit(False, f"camera-init unavailable ({e})")
            return
        self._init = CameraInitializer(
            status_cb=self.status.emit,
            step_cb=self.step.emit,
            done_cb=self.done.emit,
        )
        self._init.run_async()


# Operator auto-focus (Settings -> Camera focus)
class FocusWorker(QThread):
    """Runs FocusController.autofocus() off the GUI thread.

    The camera does not answer autofocus_start until the lens has physically
    locked (up to CAMERA_INIT_AUTOFOCUS_TIMEOUT seconds). On the GUI thread
    that would freeze the live feed and every control for the whole wait.
    """
    done = Signal(bool, str)   # ok, message for the operator

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._focus = controller

    def run(self):
        try:
            ok, message = self._focus.autofocus()
        except Exception as e:               # autofocus() shouldn't raise; report it if it does
            log.exception("FocusWorker failed")
            ok, message = False, f"Auto-focus failed: {e}"
        self.done.emit(bool(ok), message)


# QR / tap-to-focus (operator page live feed -> Settings camera focus card)
class FocusSearchWorker(QThread):
    """Runs a FocusController search off the GUI thread: focus_on_point(*point)
    with a point, focus_on_region(region) with a region, else focus_on_qr().

    The search moves the lens several times and waits for fresh frames after
    each move. `progress` carries the curve so far for the chart on the card."""
    progress = Signal(str, object, int)   # step text, [(position, score)], lens position
    done = Signal(bool, str, object)      # ok, message for the operator, result dict

    def __init__(self, controller, point=None, parent=None, region=None,
                 measure_range=False, save=True):
        super().__init__(parent)
        self._focus = controller
        self._point = point
        self._region = region
        self._measure_range = measure_range   # MEASURE LAYER: measure_range()
        self._save = save                     # False: a layer's refocus, not start-up

    def run(self):
        def progress(text, points, pos):
            self.progress.emit(text, list(points), int(pos))
        try:
            if self._measure_range:
                ok, message, result = self._focus.measure_range(progress)
            elif self._point is not None:
                ok, message, result = self._focus.focus_on_point(*self._point, progress)
            elif self._region is not None:
                ok, message, result = self._focus.focus_on_region(self._region, progress)
            else:
                ok, message, result = self._focus.focus_on_qr(progress, save=self._save)
        except Exception as e:               # the search shouldn't raise; report it if it does
            log.exception("FocusSearchWorker failed")
            ok, message, result = False, f"Focusing failed: {e}", {}
        self.done.emit(bool(ok), message, result)


class LayerFocusWorker(QThread):
    """The focus for one layer of a stack (FocusController.focus_layer)."""
    progress = Signal(str, object, int)
    done = Signal(bool, str, object)

    def __init__(self, controller, center=None, parent=None, region=None, save=False,
                 full=True):
        super().__init__(parent)
        self._focus = controller
        self._center = center
        self._region = region
        self._save = save                     # True: the start-up focus, saved for start-up
        self._full = full                     # False: quick check and area glide only

    def run(self):
        def progress(text, points, pos):
            self.progress.emit(text, list(points), int(pos))
        try:
            ok, message, result = self._focus.focus_layer(self._center, progress,
                                                          self._region, save=self._save,
                                                          full=self._full)
        except Exception as e:
            log.exception("LayerFocusWorker failed")
            ok, message, result = False, f"Focusing failed: {e}", {}
        self.done.emit(bool(ok), message, result)


class FocusStepWorker(QThread):
    """Moves the lens a number of steps from where it is (FocusController.move_by)."""
    done = Signal(bool, str, int, int)   # ok, message, start, target (-1 unknown)

    def __init__(self, controller, delta, parent=None):
        super().__init__(parent)
        self._focus = controller
        self._delta = delta

    def run(self):
        try:
            ok, message, start, target = self._focus.move_by(self._delta)
        except Exception as e:
            log.exception("FocusStepWorker failed")
            ok, message, start, target = False, f"The lens did not move: {e}", None, None
        self.done.emit(bool(ok), message, -1 if start is None else int(start),
                       -1 if target is None else int(target))


class FocusMoveWorker(QThread):
    """Moves the lens to one position (FocusController.move_to), saved for
    start-up unless save=False."""
    done = Signal(bool, str)

    def __init__(self, controller, position, parent=None, save=True):
        super().__init__(parent)
        self._focus = controller
        self._position = position
        self._save = save

    def run(self):
        try:
            ok, message = self._focus.move_to(self._position, save=self._save)
        except Exception as e:
            log.exception("FocusMoveWorker failed")
            ok, message = False, f"The lens did not move: {e}"
        self.done.emit(bool(ok), message)


class FocusReadWorker(QThread):
    """Reads the lens position once (a local HTTP call that can still hang on
    a stuck camera service, so not on the GUI thread)."""
    done = Signal(bool, int)   # ok, position

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self._focus = controller

    def run(self):
        try:
            ok, position, _ = self._focus.get_position()
        except Exception:
            log.exception("FocusReadWorker failed")
            ok, position = False, None
        self.done.emit(bool(ok), int(position) if ok else -1)


# Settings -> USB webcam focus (modules/webcam_focus.py)
class WebcamFocusWorker(QThread):
    """The layer-1 webcam's automatic focus, off the GUI thread.

    Opens (or wakes) the webcam and focuses it at once (auto_focus): the focus
    is moved through its range by itself until the codes in view decode, and
    the middle of the range where they all decode is set. The live picture is
    measured all the while, and about twice a second afterwards until stop(),
    so the operator sees the result. FOCUS AGAIN queues another run."""
    opened = Signal(bool, str, object)    # ok, message, (min, max, step, current)
    measured = Signal(object, object)     # measure() dict, preview frame (BGR) or None
    focus_set = Signal(bool, int)         # ok, value
    sweep_point = Signal(int, object)     # value, measure() dict
    # ok (every code decoded), value (-1 none), message, codes found, codes read
    focus_done = Signal(bool, int, str, int, int)
    closed = Signal()

    MEASURE_EVERY_S = 0.5

    def __init__(self, layer_camera, detector, parent=None):
        super().__init__(parent)
        self._lc = layer_camera
        self._detector = detector
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._again = threading.Event()
        self._cam = None

    # Called from the GUI thread
    def focus_again(self):
        self._cancel.clear()
        self._again.set()

    def cancel(self):
        """Stop the focus run now; the lens goes back to where it started."""
        self._cancel.set()

    def stop(self):
        self._cancel.set()
        self._stop.set()

    # Worker thread
    def _wait_frames(self, count, timeout):
        """Let `count` new frames arrive (best effort: a slow webcam is
        measured on the frame it has)."""
        try:
            self._cam.wait_for_new_frames(count, timeout=timeout)
        except Exception:
            log.debug("Waiting for webcam frames failed", exc_info=True)

    def _measure(self):
        from modules import webcam_focus
        ok, frame = self._cam.get_frame_no_copy() if self._cam is not None else (False, None)
        frame = frame if ok else None
        return webcam_focus.measure(frame, self._detector), frame

    def run(self):
        from modules import webcam_focus
        try:
            cam, message = self._lc.begin_focus_test()
        except Exception as e:
            log.exception("Webcam auto focus could not start")
            cam, message = None, str(e)
        if cam is None:
            self.opened.emit(False, message, None)
            self.closed.emit()
            return
        self._cam = cam
        device = (getattr(cam, "config", {}) or {}).get("device")
        lo, hi, step, current = webcam_focus.focus_range(device)
        if current is None:
            current = self._lc.configured_focus
        self._range = (lo, hi, step)
        self._start = current
        self.opened.emit(True, message, (lo, hi, step, current))
        self._wait_frames(1, 3.0)
        self._again.set()                     # focus straight away
        last = 0.0
        try:
            while not self._stop.is_set():
                if self._again.is_set():
                    self._again.clear()
                    self._auto()
                    last = 0.0
                    continue
                due = self.MEASURE_EVERY_S - (time.monotonic() - last)
                if due <= 0:
                    last = time.monotonic()
                    m, frame = self._measure()
                    self.measured.emit(m, frame)
                else:
                    # Wakes at once for FOCUS AGAIN; stop() is seen within 0.1 s.
                    self._again.wait(min(due, 0.1))
        except Exception:
            log.exception("Webcam auto focus failed")
        finally:
            try:
                self._lc.end_focus_test()
            except Exception:
                log.exception("Webcam focus test end failed")
            self.closed.emit()

    def _set(self, value):
        ok = self._lc.set_webcam_focus(self._cam, value)
        if ok:
            self.focus_set.emit(True, int(value))
        return ok

    def _auto(self):
        from modules import webcam_focus
        lo, hi, step = self._range

        def read():
            self._wait_frames(2, 2.0)
            m, frame = self._measure()
            self.measured.emit(m, frame)
            return m

        try:
            best, _, _ = webcam_focus.auto_focus(
                self._set, read, lo, hi, step,
                on_point=lambda v, mm: self.sweep_point.emit(int(v), mm),
                cancelled=lambda: self._cancel.is_set() or self._stop.is_set())
        except Exception as e:
            log.exception("Webcam auto focus failed")
            self._back_to_start()
            self.focus_done.emit(False, -1, tr("Auto focus failed: {error}", error=e), 0, 0)
            return
        if best is None:
            self.focus_done.emit(False, -1, tr("The USB webcam did not accept a focus "
                                               "value — it may have a fixed lens."), 0, 0)
            return
        if self._cancel.is_set():
            self._back_to_start()
            self.focus_done.emit(False, -1, tr("Auto focus stopped. The focus was not "
                                               "changed."), 0, 0)
            return
        # Set it and look again: the result is what the webcam reads now.
        self._set(best)
        time.sleep(0.4)
        m = read()
        if webcam_focus.all_read(m):
            self.focus_done.emit(True, int(best), tr(
                "Focus set to {v}: all {n} codes decode.", v=best, n=m["read"]),
                m["found"], m["read"])
        elif m["read"]:
            self.focus_done.emit(True, int(best), tr(
                "Focus set to {v}: {r} of {f} codes decode. Check the others are "
                "clean, flat and lit, then focus again.", v=best, r=m["read"],
                f=m["found"]), m["found"], m["read"])
        else:
            self._back_to_start()
            self.focus_done.emit(False, -1, tr(
                "No code could be decoded at any focus, so the focus was not changed. "
                "Check the kegs' QR codes are in the picture and lit."),
                m["found"], 0)

    def _back_to_start(self):
        if self._start is not None:
            try:
                self._set(self._start)
            except Exception:
                log.debug("Could not put the focus back", exc_info=True)


class Detections(list):
    """One round's decoded codes (a plain list for every existing consumer),
    plus `unread`: the QR boxes found in the same round that did not decode."""

    def __init__(self, codes=(), unread=()):
        super().__init__(codes)
        self.unread = tuple(unread)


def round_is_active(codes, boxes, unread, recent):
    """(active, signature) for idle mode.

    Active (full rate) when YOLO sees anything, unless DETECT_IDLE_WHEN_STEADY
    allows a steady scene to rest: every box read and the same codes and box
    count as a recent round. `recent` holds the last two rounds' signatures:
    with ZOOM_AREA_ONLY the whole-picture and the area rounds alternate and
    may each see a little more or less, which is not a change in the scene."""
    signature = (frozenset(codes), int(boxes))
    if not codes and not boxes:
        return False, signature
    return bool(unread) or signature not in recent, signature


# Lane 2: continuous detection on the live 4K frame
class DetectWorker(QThread):
    """Continuously decodes QR codes from the latest camera frame.

    Throttled to about DETECT_MIN_INTERVAL. The decoded list is emitted to the
    GUI thread, where the operator page handles session and auto-trigger logic.

    Idle mode: after DETECT_IDLE_AFTER seconds with no QR box from YOLO, the
    rate drops to one round per DETECT_IDLE_INTERVAL. The first box restores
    full rate. `paused` is an optional callable; while it returns True no
    detection runs (the page would discard the results anyway)."""
    detections_ready = Signal(object)   # list[dict] each with 'bbox' and 'data'
    error            = Signal(str)

    def __init__(self, camera, detector, parent=None, paused=None):
        super().__init__(parent)
        self._camera = camera
        self._detector = detector
        self._paused = paused
        self._running = False
        try:
            from config import DETECT_MIN_INTERVAL
            self._interval = DETECT_MIN_INTERVAL
        except Exception:
            self._interval = 0.10
        try:
            from config import DETECT_IDLE_ENABLED, DETECT_IDLE_AFTER, DETECT_IDLE_INTERVAL
            self._idle_enabled = bool(DETECT_IDLE_ENABLED)
            self._idle_after = float(DETECT_IDLE_AFTER)
            self._idle_interval = max(float(DETECT_IDLE_INTERVAL), self._interval)
        except Exception:
            self._idle_enabled = False
            self._idle_after = 0.0
            self._idle_interval = self._interval
        try:
            from config import DETECT_IDLE_WHEN_STEADY
            self._idle_steady = bool(DETECT_IDLE_WHEN_STEADY)
        except Exception:
            self._idle_steady = False
        self._recent = ()              # last two rounds' (codes, box count)
        # DETECT_FROM_RAW: take YOLO's small picture and the black-and-white
        # crops straight from the raw camera frame (CameraManager.get_detect_inputs).
        try:
            from config import DETECT_FROM_RAW, YOLO_IMGSZ
            self._raw_width = int(YOLO_IMGSZ) if DETECT_FROM_RAW else 0
        except Exception:
            self._raw_width = 0
        if not hasattr(camera, "get_detect_inputs"):
            self._raw_width = 0
        self._raw_logged = None

    def _next_frame(self):
        """(ret, frame, extra detect_and_decode kwargs) for one round.

        From the raw frame when possible; otherwise the converted BGR frame,
        exactly as before (webcam / RTSP / file / test, or no raw frames)."""
        inputs = (self._camera.get_detect_inputs(self._raw_width)
                  if self._raw_width else None)
        raw = inputs is not None
        if raw != self._raw_logged:
            self._raw_logged = raw
            log.info("Detection input: %s", "raw frame (small picture for YOLO, "
                     "black-and-white crops)" if raw else "converted colour frame")
        if raw:
            ret, small, gray, k = inputs
            return ret, small, {"gray": gray, "scale": k}
        ret, frame = self._camera.get_frame_no_copy()
        return ret, frame, {}

    def run(self):
        self._running = True
        last = 0.0
        last_active = time.time()      # last round in which YOLO saw any QR box
        idle_logged = False
        while self._running:
            try:
                now = time.time()
                if self._paused is not None and self._paused():
                    # Resume at full rate: the next layer is about to arrive.
                    last_active = now
                    self.msleep(50)
                    continue
                idle = self._idle_enabled and (now - last_active) > self._idle_after
                if idle != idle_logged:
                    idle_logged = idle
                    log.info("Detection %s", "IDLE (%.1f Hz)" % (1 / self._idle_interval)
                             if idle else "ACTIVE (full rate)")
                interval = self._idle_interval if idle else self._interval
                wait = interval - (now - last)
                if wait > 0:
                    self.msleep(max(5, min(50, int(wait * 1000))))
                    continue
                last = now
                # No copy: the detector only reads the frame, and the capture
                # thread publishes a new array per frame instead of reusing one.
                ret, frame, extra = self._next_frame()
                if not ret or frame is None:
                    self.msleep(15)
                    continue
                t0 = time.perf_counter()
                results = self._detector.detect_and_decode(frame, False, live=True, **extra)
                if perf_log is not None:
                    perf_log.inc("detect.rounds")
                    perf_log.inc("detect.rounds_idle" if idle else "detect.rounds_active")
                    perf_log.add("detect.round_ms", (time.perf_counter() - t0) * 1000)
                qr_list = results[0] if results else []
                unread = tuple(getattr(self._detector, "last_unread_boxes", ()) or ())
                if self._active(qr_list, unread):
                    last_active = time.time()
                self.detections_ready.emit(Detections(qr_list or [], unread))
            except Exception as e:
                log.exception("DetectWorker error")
                self.error.emit(str(e))
                self.msleep(100)

    def _active(self, qr_list, unread):
        """Whether this round keeps detection at full rate (see idle mode)."""
        boxes = getattr(self._detector, "last_box_count", None)
        if boxes is None:
            return True             # a detector without box counts never goes idle
        if not self._idle_steady:
            return bool(qr_list or boxes)
        codes = [q.get("data") for q in qr_list or [] if q.get("data")]
        active, signature = round_is_active(codes, boxes, unread, self._recent)
        self._recent = (self._recent + (signature,))[-2:]
        return active

    def stop(self):
        self._running = False
        self.quit()
        if not self.wait(1500):
            self.terminate()
            self.wait(500)


# Cloud blacklist fetch
class BlacklistFetcher(QThread):
    """Fetches the cloud blacklist off the GUI thread.

    Uses `result_ready` so it does not shadow QThread.finished.
    """
    result_ready = Signal(object)   # list[dict]
    failed       = Signal(str)

    def __init__(self, api_sender, parent=None):
        super().__init__(parent)
        self._api = api_sender

    def run(self):
        try:
            self.result_ready.emit(self._api.get_blacklist())
        except Exception as e:
            log.exception("BlacklistFetcher failed")
            self.failed.emit(str(e))


# Beer-type catalogue fetch
class BeerTypeFetcher(QThread):
    """Fetches the beer-type catalogue off the GUI thread.

    The call is a POST that can sit on the cloud's 10 s timeout, so it must
    not run on the GUI thread. Emits `result_ready(mapping, warning)`:
    `mapping` is an ordered {name: _id} dict (possibly from the disk cache);
    `warning` is None on a clean fetch, or an operator-readable reason when the
    cloud could not be reached. The mapping may still be usable then, which is
    why both travel together instead of using `failed`.
    """
    result_ready = Signal(object, object)   # ({name: _id}, warning|None)
    failed       = Signal(str)

    def __init__(self, api_sender, parent=None):
        super().__init__(parent)
        self._api = api_sender

    def run(self):
        try:
            mapping, warning = self._api.get_beer_types()
            self.result_ready.emit(mapping, warning)
        except Exception as e:
            log.exception("BeerTypeFetcher failed")
            self.failed.emit(str(e))


# Operator list fetch
class OperatorFetcher(QThread):
    """Fetches the brewery's operator list off the GUI thread.

    Same two-value contract as BeerTypeFetcher: `result_ready(operators,
    warning)`, where `operators` is a list of {id, name, role} dicts (possibly
    from the disk cache) and `warning` is None on a clean fetch or an
    operator-readable reason when the cloud could not be reached. The list may
    still be usable then, so both travel together.
    """
    result_ready = Signal(object, object)   # ([{id, name, role}], warning|None)
    failed       = Signal(str)

    def __init__(self, api_sender, parent=None):
        super().__init__(parent)
        self._api = api_sender

    def run(self):
        try:
            operators, warning = self._api.get_operators()
            self.result_ready.emit(operators, warning)
        except Exception as e:
            log.exception("OperatorFetcher failed")
            self.failed.emit(str(e))


# Keg registration (Camera Keg Registration API)
class KegCheckWorker(QThread):
    """Asks the cloud, one code at a time, whether newly seen kegs are registered.

    Takes a batch of codes so a whole layer costs one thread, not one per keg.
    Emits `checked(code, result)` per code, where result is the dict from
    APISender.check_keg ({state, reason, code}).
    """
    checked = Signal(str, object)

    def __init__(self, api_sender, codes, parent=None):
        super().__init__(parent)
        self._api = api_sender
        self._codes = list(codes)

    def run(self):
        for code in self._codes:
            try:
                res = self._api.check_keg(code)
            except Exception as e:
                log.exception("KegCheckWorker failed")
                res = {"state": "error", "reason": str(e), "code": ""}
            self.checked.emit(code, res)


class KegFormFetcher(QThread):
    """Fetches the keg registration form. Emits `result_ready(form, warning)`;
    `form` may come from the disk cache (or be None), like BeerTypeFetcher."""
    result_ready = Signal(object, object)

    def __init__(self, api_sender, parent=None):
        super().__init__(parent)
        self._api = api_sender

    def run(self):
        try:
            form, warning = self._api.get_keg_form()
        except Exception as e:
            log.exception("KegFormFetcher failed")
            form, warning = None, str(e)
        self.result_ready.emit(form, warning)


class KegRegisterWorker(QThread):
    """Sends one keg registration. Emits `done(code, result)` with the dict
    from APISender.register_keg."""
    done = Signal(str, object)

    def __init__(self, api_sender, code, values, form_version, parent=None):
        super().__init__(parent)
        self._api = api_sender
        self._args = (code, values, form_version)

    def run(self):
        try:
            res = self._api.register_keg(*self._args)
        except Exception as e:
            log.exception("KegRegisterWorker failed")
            res = {"ok": False, "status": 0, "error": "UNEXPECTED", "message": str(e),
                   "fieldErrors": {}, "keg": None, "formChanged": False}
        self.done.emit(self._args[0], res)


class KegBulkRegisterWorker(QThread):
    """Registers several kegs with the same answers, one request at a time.

    Emits `progress(code, result)` after each keg (result as from
    APISender.register_keg). Stops at the first refusal that is not about that
    keg alone (bad answers, sign-in, network): the rest would be refused the
    same way, so they are left for the operator to retry.
    """
    progress = Signal(str, object)
    KEG_ONLY_ERRORS = {"PREFIX_MISMATCH"}   # this keg can't be registered; carry on

    def __init__(self, api_sender, codes, values, form_version, parent=None):
        super().__init__(parent)
        self._api = api_sender
        self._codes = list(codes)
        self._values = dict(values or {})
        self._form_version = form_version

    def run(self):
        for code in self._codes:
            try:
                res = self._api.register_keg(code, self._values, self._form_version)
            except Exception as e:
                log.exception("KegBulkRegisterWorker failed")
                res = {"ok": False, "status": 0, "error": "UNEXPECTED", "message": str(e),
                       "fieldErrors": {}, "keg": None, "formChanged": False}
            self.progress.emit(code, res)
            if not res.get("ok") and res.get("error") not in self.KEG_ONLY_ERRORS:
                break


# Camera / operator login calls
class AuthWorker(QThread):
    """Runs one modules.camera_auth call (login, operator login/logout) off the GUI thread.

    Each call can sit on the cloud's timeout. Emits the result dict."""
    done = Signal(object)

    def __init__(self, fn, *args, parent=None, **kwargs):
        super().__init__(parent)
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self):
        try:
            res = self._fn(*self._args, **self._kwargs)
        except Exception as e:
            log.exception("AuthWorker failed")
            res = {"ok": False, "error": "UNEXPECTED", "message": str(e)}
        self.done.emit(res)


# Advanced tiled decode (modules/advanced.py) for manual-mode capture
class AdvancedDecodeWorker(QThread):
    """Runs the heavy tile-based QR decode on a saved frame.

    This is what separates MANUAL mode from AUTO. Instead of the fast live
    detector, the captured frame is tiled with overlap and each tile is
    YOLO-detected, upscaled, unblurred and decoded (QReader + pyzbar) for
    maximum recall. Emits the list of unique decoded QR strings.
    """
    result_ready = Signal(object)   # list[str] of decoded QR codes
    failed       = Signal(str)

    def __init__(self, image_path, parent=None, frame_save=None):
        super().__init__(parent)
        self._path = image_path
        self._frame_save = frame_save   # thread still writing image_path, if any

    def run(self):
        try:
            wait_for_frame_save(self._frame_save)
            from modules.advanced import run_advanced_detection
            codes = run_advanced_detection(self._path)
            self.result_ready.emit([str(c).strip() for c in (codes or []) if c])
        except Exception as e:
            log.exception("AdvancedDecodeWorker failed")
            self.failed.emit(str(e))


# Send pallet report to the cloud (process_worker.submit_batch)
class BatchSubmitWorker(QThread):
    """Submits a pallet report. Payload keys mirror process_worker.submit_batch parameters."""
    # result dict: {success, pallet_id, error, retryable, qr_count,
    #               palletised_count, unmatched, moved}
    success = Signal(object)
    failed  = Signal(str)

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self._p = payload

    def run(self):
        try:
            # The frame may still be being written by save_frame_async(); the
            # short-layer path in submit_batch reads it.
            wait_for_frame_save(self._p.get("frame_save"))
            from modules.process_worker import submit_batch
            future = submit_batch(
                frame_path     = self._p.get("frame_path"),
                image_name     = self._p.get("image_name"),
                session_id     = self._p.get("session_id"),
                qr_codes       = self._p.get("qr_codes"),
                required_count = self._p.get("required_count", 6),
                batch          = self._p.get("batch", ""),
                scanned_time   = self._p.get("scanned_time"),
                palette_id     = self._p.get("palette_id"),
                layer          = self._p.get("layer"),
                reassign_kegs  = self._p.get("reassign_kegs"),
                beer_type      = self._p.get("beer_type", ""),
                beer_type_id   = self._p.get("beer_type_id", ""),
                operator_id    = self._p.get("operator_id", ""),
                operator_name  = self._p.get("operator_name", ""),
                stack          = self._p.get("stack"),
                positions      = self._p.get("positions"),
                layout         = self._p.get("layout"),
            )
            self.success.emit(future.result())
        except Exception as e:
            log.exception("BatchSubmitWorker failed")
            self.failed.emit(str(e))


# Print pallet label
class PrintWorker(QThread):
    """Settings test print.

    Real pallet labels use the print queue so they never run alongside another
    job on the same printer."""
    done = Signal(bool, str)

    def __init__(self, printer, pallet_id, parent=None):
        super().__init__(parent)
        self._printer = printer
        self._pallet_id = pallet_id

    def run(self):
        try:
            ok, err = self._printer.print_pallet_qr(self._pallet_id)
            self.done.emit(bool(ok), err or "")
        except Exception as e:
            log.exception("PrintWorker failed")
            self.done.emit(False, str(e))


# Cloud config sync (port of SimpleKegHMI._sync_thread)
def fetch_cloud_config():
    """POST /current-config for this camera.

    Returns (count, keg_type, None) on success or (None, None, reason).
    Never raises. A plain function so the loading screen and CloudSyncWorker
    can share it.
    """
    try:
        import requests
        from config import (CLOUD_CONFIG_ENDPOINT, CLOUD_SYNC_ENABLED,
                            CAMERA_MAC_ID, DEFAULT_KEG_COUNT, SSL_VERIFY)

        if not CLOUD_SYNC_ENABLED:
            # No current-config route is deployed; see config.CLOUD_SYNC_ENABLED.
            return None, None, "Cloud sync disabled — using the local keg count"

        # Always send the registered MAC. A MAC derived from this machine's
        # NIC (uuid.getnode()) is unknown to the cloud and only gets a 404.
        payload = {"macId": CAMERA_MAC_ID}
        from modules.camera_auth import get_camera_auth
        resp = requests.post(CLOUD_CONFIG_ENDPOINT, json=payload, timeout=5,
                             verify=SSL_VERIFY, auth=get_camera_auth())
        if resp.status_code == 200:
            data = resp.json()
            keg_type = data.get("keg_type", "30L")
            count = int(data.get("keg_count", DEFAULT_KEG_COUNT))
            return count, keg_type, None
        return None, None, f"Cloud sync failed: HTTP {resp.status_code}"
    except Exception as e:
        log.warning("Cloud config fetch error: %s", e)
        return None, None, f"Sync error: {str(e)[:60]}"


class CloudSyncWorker(QThread):
    success = Signal(int, str)   # keg_count, keg_type
    failed  = Signal(str)

    def run(self):
        count, keg_type, reason = fetch_cloud_config()
        if reason is None:
            self.success.emit(count, keg_type)
        else:
            self.failed.emit(reason)
