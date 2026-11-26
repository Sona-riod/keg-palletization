# modules/focus.py
"""
Lens focus for the operator, while the machine is running. camera_init.py
does the same once at boot.

Why this exists
---------------
The start-up sequence focuses the camera once, on the splash screen, with no
chance to intervene. Focus is also the camera setting most likely to drift in
service: the lens gets knocked during a wash-down, a different keg/pallet height
changes the working distance, or the boot autofocus locked onto an empty
conveyor. Then the QR codes go soft, kegs stop being decoded, and the operator
has no console to run `camera_configure.sh` from.

Three ways the lens gets focused while running:

    QR codes, automatic   focus_on_qr(): the first time codes are in view after
                          start-up (the operator page triggers it)
    Manual focus          focus_on_point(): the QR code or area the operator
                          double-taps on the live feed
    RESET AUTO-FOCUS      POST reset_focus_position -> POST autofocus_start

RESET AUTO-FOCUS is the last two steps of camera_init.py, on the same daemon,
through the same rest_post() transport, with the same long read timeout
(autofocus_start holds the response open until the lens locks).

The camera's auto-focus cannot be told where to look: it locks on whatever has
the most contrast, which on this line can be an object at the side of the view
rather than the keg labels. The other two focus in the app instead: the lens is
moved with POST focus_abs_position and the sharpness is measured only inside
the target (the QR boxes YOLO finds, or the tapped box/area). The search
hill-climbs from where the lens is, halving its step, so it takes a handful of
moves rather than a sweep of the whole range. The lens is left at the sharpest
position and that position is saved; start-up then moves the lens there instead
of auto-focusing (camera_init.py). RESET AUTO-FOCUS clears it again.

The lens position endpoint (GET reads, POST moves, 0-1600) is the daemon's own,
from its API list at localhost:5000/apidocs.

Backends
--------
"rest"     - CAMERA_SOURCE == "icam": the Advantech daemon on
             CAMERA_API_BASE_URL. The production path.
"property" - webcam / generic OpenCV source: autofocus is enabled through the
             capture itself (CAP_PROP_AUTOFOCUS), as a UVC camera on a dev PC
             does it. Goes through CameraManager.set_property(), which takes
             the device lock the capture thread reads under.
"none"     - no focus control available (no requests, no cv2, no camera).

Failure policy
--------------
autofocus() returns (ok, message) and focus_on_qr() (ok, message, result);
neither raises. The operator reads the message, so it reports what the camera
answered. It is never a silent no-op and never claims a lens moved when it did
not. A sweep that fails or is stopped puts the lens back where it started.
"""

import time
import logging
import threading

import config

# Logging (same pattern as modules/camera_init.py)
logger = logging.getLogger("CameraFocus")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(_h)

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except Exception:  # pragma: no cover
    _HAS_CV2 = False

try:
    # The boot sequence's transport. Importing it also proves `requests` is
    # installed, since camera_init imports it at module level.
    from modules.camera_init import rest_post, rest_json
    _HAS_REST = True
except Exception:  # pragma: no cover
    rest_post = rest_json = None
    _HAS_REST = False

BACKEND_REST = "rest"
BACKEND_PROPERTY = "property"
BACKEND_NONE = "none"

# Set while a QR / tap-to-focus search moves the lens. The operator page's DetectWorker pauses
# on it: frames in the middle of a sweep are out of focus on purpose.
_qr_focus_running = threading.Event()


# Quick check: when no QR box is found at the expected lens position, look
# this many steps either side (twice each way) before the full search.
QUICK_FIND_STEP = 20
# When the quick check finds no QR box at all (codes far too soft), one
# continuous glide this many steps either side of the expected position,
# scoring the sharpness of the stack's area: no QR box needed. About 1.5 s of
# lens travel, instead of the 6-15 s search that needs a box to start.
AREA_GLIDE_SPAN = 200


def qr_focus_running():
    return _qr_focus_running.is_set()


class FocusController:
    """Focus the camera on demand. Safe to call from a worker thread.

    `camera` is the CameraManager (optional). The "property" backend uses it to
    reach the live capture under the same lock the capture thread holds (a
    VideoCapture.set() racing a read() can wedge a v4l2 device); the focus search
    reads its frames. `detector` is the QRDetector, whose YOLO model finds the
    codes for the focus search.
    """

    def __init__(self, camera=None, detector=None):
        self.camera = camera
        self.detector = detector
        self.last_position = None      # lens position last read or set
        self._cancel = threading.Event()
        self._start = None             # lens position before the sweep
        self._pos = None               # where the lens is during a sweep
        self._up_travel = 0            # upward travel since the last downward move

    def attach_camera(self, camera, detector=None):
        """Set the camera (and detector) later. The HMI builds this card before
        InitWorker has started the camera, so the webcam path and the focus search
        are only available once the backend is ready."""
        self.camera = camera
        if detector is not None:
            self.detector = detector

    # What this camera supports
    @property
    def backend(self):
        source = str(getattr(config, "CAMERA_SOURCE", "") or "").lower()
        if source == "icam":
            # The daemon owns the lens on this camera whether or not the boot
            # sequence ran; CAMERA_INIT_ENABLED only gates the boot.
            return BACKEND_REST if _HAS_REST else BACKEND_NONE
        if self.camera is not None and _HAS_CV2:
            return BACKEND_PROPERTY
        return BACKEND_NONE

    @property
    def available(self):
        return self.backend != BACKEND_NONE

    def describe(self):
        """One line for the HMI saying what is being driven and where."""
        backend = self.backend
        if backend == BACKEND_REST:
            return (f"iCam-540 lens via the camera service at "
                    f"{config.CAMERA_API_BASE_URL}")
        if backend == BACKEND_PROPERTY:
            return (f"camera driver properties (source "
                    f"'{getattr(config, 'CAMERA_SOURCE', '?')}')")
        if str(getattr(config, "CAMERA_SOURCE", "") or "").lower() == "icam":
            return ("unavailable — the 'requests' package is not installed on "
                    "this machine")
        return "unavailable until the camera has started"

    # Actions
    def autofocus(self):
        """Run autofocus and report what the hardware answered.

        REST: reset the lens to its home position, let it settle, then start
        autofocus and wait. The daemon holds the response open until the lens
        locks, so this needs its own thread and the long
        CAMERA_INIT_AUTOFOCUS_TIMEOUT.

        Unlike at boot, a lens that never confirms is reported as a failure.
        At boot the line has to come up regardless, but an operator who just
        pressed the button should know it did not work.
        """
        backend = self.backend

        if backend == BACKEND_REST:
            base_url = config.CAMERA_API_BASE_URL
            connect_timeout = config.CAMERA_INIT_CONNECT_TIMEOUT
            logger.info("Operator started auto-focus at %s.", base_url)

            ok, msg = rest_post(base_url, config.CAMERA_RESET_FOCUS_PATH, None,
                                connect_timeout, config.CAMERA_INIT_TIMEOUT)
            if not ok:
                logger.error("Operator auto-focus: reset failed (%s).", msg)
                return False, f"Resetting the focus position failed: {msg}"

            settle = max(float(config.CAMERA_INIT_SETTLE_AFTER_RESET), 0.0)
            if settle:
                time.sleep(settle)

            ok, msg = rest_post(base_url, config.CAMERA_AUTOFOCUS_PATH, None,
                                connect_timeout,
                                config.CAMERA_INIT_AUTOFOCUS_TIMEOUT)
            if not ok:
                logger.error("Operator auto-focus did not confirm (%s).", msg)
                return False, (f"Auto-focus did not confirm a lock: {msg}. "
                               f"It may still be running on the camera.")
            logger.info("Operator auto-focus completed — the lens reported a lock.")
            message = "Auto-focus finished — the lens reported a lock"
            ok, position, _ = self.get_position()
            if ok:
                message += f" at position {position}"
            message += "."
            # The operator chose the camera's own focus over the saved QR focus,
            # so start-up auto-focuses again too.
            if config.camera_focus_position() is not None:
                if config.save_camera_focus_position(None):
                    logger.info("Saved QR focus cleared — start-up auto-focuses again.")
                    message += " Start-up uses auto-focus again."
            return True, message

        if backend == BACKEND_PROPERTY:
            if not self._set_prop(cv2.CAP_PROP_AUTOFOCUS, 1):
                return False, ("This camera did not accept the auto-focus "
                               "control (a fixed-focus lens has none).")
            logger.info("Auto-focus enabled on the capture device.")
            return True, ("Auto-focus enabled on the camera — it focuses "
                          "continuously from now on.")

        return False, f"No focus control is available: {self.describe()}."

    # Lens position (iCam-540 daemon only)
    def get_position(self):
        """(ok, position, message): where the focus motor is now."""
        if self.backend != BACKEND_REST:
            return False, None, "the lens position is only readable on the iCam-540"
        ok, msg, body = rest_json(config.CAMERA_API_BASE_URL,
                                  config.CAMERA_FOCUS_POSITION_PATH, None,
                                  config.CAMERA_INIT_CONNECT_TIMEOUT,
                                  config.CAMERA_INIT_TIMEOUT, method="GET")
        if not ok:
            return False, None, msg
        try:
            self.last_position = int(body.get("result"))
        except (TypeError, ValueError):
            return False, None, f"the camera answered {body.get('result')!r}"
        return True, self.last_position, ""

    def set_position(self, position):
        """(ok, message): move the focus motor to `position`."""
        position = max(config.CAMERA_FOCUS_POSITION_MIN,
                       min(config.CAMERA_FOCUS_POSITION_MAX, int(position)))
        ok, msg, _ = rest_json(config.CAMERA_API_BASE_URL,
                               config.CAMERA_FOCUS_POSITION_PATH,
                               {config.CAMERA_FOCUS_POSITION_FIELD: position},
                               config.CAMERA_INIT_CONNECT_TIMEOUT,
                               config.CAMERA_INIT_TIMEOUT)
        if ok:
            self.last_position = position
        return ok, msg

    # Focus on the QR codes / on a tapped point
    @property
    def qr_focus_available(self):
        return not self.qr_focus_unavailable_reason()

    def qr_focus_unavailable_reason(self):
        """Why focusing on the QR codes or a tapped point cannot run, or ""."""
        if self.backend != BACKEND_REST:
            return "it needs the iCam-540 camera service"
        if self.camera is None or not hasattr(self.camera, "wait_for_new_frames"):
            return "the camera has not started"
        if (self.detector is None or not hasattr(self.detector, "locate_boxes")
                or getattr(self.detector, "model", None) is None):
            return "the QR model is not loaded"
        return ""

    def cancel(self):
        """Stop a running search (app exit). The lens goes back where it was."""
        self._cancel.set()

    def focus_on_qr(self, progress=None, save=True):
        """Focus the lens on all the QR codes in view and save the position
        for start-up (save=False: the per-layer refocus, which only learns the
        position for that layer).

        progress(text, points, position) is called from this thread after
        every lens move: points is [(lens position, score 0-1), ...] sorted by
        position, position is where the lens is now.

        Returns (ok, message, result); result has "position", "start",
        "points", "boxes" and "target" ("qr" here).
        """
        return self._run(self._qr_target, progress, save=save)

    def focus_on_point(self, x, y, progress=None):
        """Focus the lens on the operator's tap at (x, y), full-frame pixels.

        On the QR box under the tap if YOLO finds one there (a soft code
        included), else on a square of CAMERA_TAP_FOCUS_AREA of the frame width
        around it. Same returns as focus_on_qr(); "target" is "qr" or "area".
        """
        return self._run(lambda: self._point_target(x, y), progress)

    def focus_on_region(self, region, progress=None):
        """Focus on the QR codes inside `region` (x1, y1, x2, y2 as fractions
        of the frame): the FOCUS button on the live feed. If no code is found
        there, on the region itself. Same returns as focus_on_qr()."""
        return self._run(lambda: self._region_target(region), progress)

    def measure_range(self, progress=None):
        """How far the focus reaches for the layer under the camera.

        Finds the sharpest lens position for the QR codes in view, then steps
        the lens up through FOCUS_RANGE_SPAN either side of it, scoring every
        position, and puts the lens back where it was. Nothing is saved for
        start-up: this only measures. Each layer height has its own sharp band;
        the Camera Focus card compares them to show whether one lens position
        reads every layer.

        Same progress and returns as focus_on_qr(); result also has "band"
        (see focus_band) and "peak".
        """
        return self._run(self._qr_target, progress, measure_range=True)

    def focus_layer(self, center=None, progress=None, region=None, save=False, full=True):
        """Focus for a layer of a stack: the QR focus search (focus_on_qr),
        started from `center` (a position remembered or predicted for the
        layer) when given, so its glide begins next to the peak. A quick check
        runs first, around `center` or, without one, around where the lens is
        now. save=True (the start-up focus) also saves a position where the
        codes read for start-up; a layer's refocus does not. full=False (a
        search started with no QR box in view, which may be an empty pallet):
        the quick check and the area glide only, never the 6-15 s search.
        When nothing reads, the lens goes back to `center` (else to where it
        was), never left at the end of a glide.

        A stepped read-scan was tried first and dropped (log 2026-11-03 16:05):
        the cans read only from about lens 1011 to 1020 (sharpness 56 at 1001,
        178 at 1010, 1110 at 1011), so 15-step moves stepped over the readable
        zone at all 17 positions. The search scores sharpness while gliding and
        closes in step by step until the code reads, and found it in 6-9 s.

        Returns (ok, message, result) like focus_on_qr()."""
        reason = self.qr_focus_unavailable_reason()
        if reason:
            return False, f"Focusing is not available: {reason}.", {}
        started = time.monotonic()
        if center is not None:
            ok, here, _ = self.get_position()
            if ok and abs(int(center) - here) > 5:
                self._cancel.clear()
                self._start, self._pos, self._up_travel = None, here, 0
                _qr_focus_running.set()
                try:
                    ok, msg = self._approach(int(center))
                    logger.info("Layer focus: lens %d -> %d (remembered or predicted) %s",
                                here, center, "" if ok else f"failed: {msg}")
                except Exception:
                    logger.exception("Layer focus: lens move failed")
                finally:
                    _qr_focus_running.clear()
        # Quick check first: the motor lands a few steps off and a layer is
        # sharp over only ~10, so small steps around the remembered position
        # usually find it in 1-3 s, without the 6-9 s search. Without a
        # remembered position, around where the lens is: at start-up that is
        # the saved focus, and 2026-11-03 the codes read 14 steps from it while
        # the full search took 5.9 s.
        quick_center = center
        if quick_center is None:
            ok, here, _ = self.get_position()
            quick_center = here if ok else None
        home = quick_center
        def finished(quick, how):
            quick["seconds"] = round(time.monotonic() - started, 1)
            message = f"{how}: reads at lens {quick['position']} (expected {quick_center})."
            if save:
                message += (" Saved for start-up."
                            if config.save_camera_focus_position(quick["position"])
                            else " Could not save it for start-up.")
            logger.info("Layer focus done in %.1f s: %s", quick["seconds"], message)
            return True, message, quick

        if quick_center is not None:
            quick = self._quick_check(int(quick_center), region, progress)
            if quick is not None:
                return finished(quick, "Quick check")
            # No QR box anywhere near: the codes are far too soft. One fast
            # glide scoring the stack's area finds where it gets sharp, then
            # the quick check reads there.
            peak = self._area_glide(int(quick_center), region, progress)
            if peak is not None:
                quick = self._quick_check(peak, region, progress)
                if quick is not None:
                    return finished(quick, "Area glide and quick check")
        if not full:
            ok, message, result = False, ("No QR code in view near the expected focus "
                                          "(quick check only)."), {}
        else:
            # From the expected position, a narrow glide is enough; only the
            # codes where the stack stands are looked at.
            self._window = int(config.CAMERA_LAYER_FOCUS_WINDOW) if center is not None else None
            try:
                ok, message, result = self._run(lambda: self._qr_target(region), progress,
                                                save=save)
            finally:
                self._window = None
        if not ok and home is not None:
            # 2026-11-05: a failed search left the lens at the end of the area
            # glide (+200 steps): the picture stayed blurred and the next
            # search started from there. Back to where this layer reads.
            self._return_to(int(home))
        result["seconds"] = round(time.monotonic() - started, 1)
        logger.info("Layer focus %s in %.1f s: %s", "done" if ok else "failed",
                    result["seconds"], message)
        return ok, message, result

    def _return_to(self, position):
        """Bring the lens back to `position` (going up, like every measured
        position) after a search that did not read."""
        self._cancel.clear()
        _qr_focus_running.set()
        try:
            ok, here, _ = self.get_position()
            if not ok:
                return
            self._pos, self._up_travel = here, 0
            if abs(here - position) > 3:
                ok, msg = self._approach(position)
                logger.info("Layer focus: lens back to %d (was %d)%s", position, here,
                            "" if ok else f" failed: {msg}")
        except Exception:
            logger.exception("Layer focus: lens could not be moved back")
        finally:
            _qr_focus_running.clear()

    def _area_glide(self, center, region, progress):
        """One continuous upward glide over center +/- AREA_GLIDE_SPAN,
        scoring the sharpness of the stack's area (`region`, else the middle
        of the picture) on every frame. Needs no QR box, so it works when the
        codes are too soft for YOLO. Returns the lens position where the area
        was sharpest, or None."""
        progress = progress or (lambda *a: None)
        self._cancel.clear()
        _qr_focus_running.set()
        started = time.monotonic()
        try:
            grabbed = self._grab()
            if grabbed is None:
                return None
            h, w = grabbed[1].shape[:2]
            if region:
                x1, y1 = max(0, int(region[0])), max(0, int(region[1]))
                x2, y2 = min(w, int(region[2])), min(h, int(region[3]))
            else:
                r = config.CAMERA_FOCUS_DEFAULT_REGION
                x1, y1, x2, y2 = int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)
            if x2 - x1 < 16 or y2 - y1 < 16:
                return None
            ok, start, _ = self.get_position()
            if not ok:
                return None
            self._start = self._pos = start
            self._up_travel = 0
            found = self._glide_up(center - AREA_GLIDE_SPAN, center + AREA_GLIDE_SPAN,
                                   [(x1, y1, x2, y2)], progress)
        except (_Stopped, _LensError) as e:
            logger.info("Area glide stopped: %s", e)
            return None
        except Exception:
            logger.exception("Area glide failed")
            return None
        finally:
            _qr_focus_running.clear()
        if found is None:
            logger.info("Area glide: nothing got sharper around lens %d (%.1f s)",
                        center, time.monotonic() - started)
            return None
        logger.info("Area glide: sharpest near lens %d (expected %d, %.1f s)",
                    found[0], center, time.monotonic() - started)
        return int(found[0])

    def _quick_check(self, center, region, progress):
        """Read the codes at the lens now, then in CAMERA_LAYER_QUICK_STEP
        steps up through center +/- CAMERA_LAYER_QUICK_SPAN, every position
        reached going up (backlash). Stops as soon as every code reads.
        The result dict (like focus_on_qr's), or None: then the search runs."""
        progress = progress or (lambda *a: None)
        self._cancel.clear()
        _qr_focus_running.set()
        try:
            ok, start, _ = self.get_position()
            if not ok:
                return None
            self._start = self._pos = start
            self._up_travel = 0
            def find():
                found = self._find_boxes()
                if found and region:
                    x1, y1, x2, y2 = region
                    inside = [b for b in found
                              if x1 <= (b[0] + b[2]) / 2 <= x2 and y1 <= (b[1] + b[3]) / 2 <= y2]
                    found = inside or found
                return found

            if abs(start - center) > 5:
                # Look where it is expected (after an area glide the lens has
                # run past it).
                ok, _msg = self._approach(center)
                if not ok:
                    return None
            boxes = find()
            hits = []
            if not boxes:
                # Too soft for even a QR box here: the motor lands 20-40 steps
                # off a remembered position (backlash, drift), and a layer is
                # sharp over only ~20. Look a little either side, going up
                # (about 1 s), before anything slower; read around the middle
                # of where the boxes showed up (the sharp spot is inside it).
                lo, hi = config.CAMERA_FOCUS_POSITION_MIN, config.CAMERA_FOCUS_POSITION_MAX
                for pos in range(center - 2 * QUICK_FIND_STEP, center + 2 * QUICK_FIND_STEP + 1,
                                 QUICK_FIND_STEP):
                    pos = max(lo, min(hi, pos))
                    if pos == center or self._cancel.is_set():
                        continue
                    ok, _msg = self._approach(pos)
                    if not ok:
                        return None
                    found = find()
                    if found:
                        hits.append(pos)
                        boxes = found
                if hits:
                    middle = int(round(sum(hits) / len(hits)))
                    logger.info("Layer quick check: codes found at lens %s (expected %d) — "
                                "reading from %d to %d", hits, center,
                                min(hits) - 10, max(hits) + 10)
                    center = middle
            if not boxes:
                return None              # too soft to find: the search looks wider
            want = len(boxes)
            result = {"start": start, "points": [], "boxes": want, "target": "qr",
                      "read": True}
            if len(self._read_target(boxes)) >= want:
                return dict(result, position=start)
            span = int(config.CAMERA_LAYER_QUICK_SPAN)
            step = max(1, int(config.CAMERA_LAYER_QUICK_STEP))
            best_n, best_pos = 0, None
            lo_r, hi_r = center - span, center + span
            if hits:
                # The codes were boxed from the lowest to the highest of hits
                # (2026-11-05: 971, 991 and 1031, read only at 998 +/- 20, and
                # missed): read across all of it.
                lo_r, hi_r = min(lo_r, min(hits) - 10), max(hi_r, max(hits) + 10)
            for pos in range(lo_r, hi_r + 1, step):
                if self._cancel.is_set():
                    return None
                ok, _msg = self._approach(pos)
                if not ok:
                    return None
                n = len(self._read_target(boxes, fresh=False))
                result["points"].append((pos, n / want))
                progress("Quick check…", list(result["points"]), pos)
                if n >= want:
                    logger.info("Layer quick check: all %d code(s) read at lens %d", want, pos)
                    return dict(result, position=pos)
                if n > best_n:
                    best_n, best_pos = n, pos
            if best_pos is not None:
                # Some read but never all together: stay where most did; the
                # rest are probably not a focus matter (glare, damage).
                self._approach(best_pos)
                logger.info("Layer quick check: best %d of %d code(s) at lens %d",
                            best_n, want, best_pos)
                return dict(result, position=best_pos)
            logger.info("Layer quick check: nothing read from %d to %d", lo_r, hi_r)
            return None
        except Exception:
            logger.exception("Layer quick check failed")
            return None
        finally:
            _qr_focus_running.clear()

    def move_by(self, delta):
        """Move the lens `delta` steps from where it is, not saved for start-up:
        the step to the next layer of a stack. Returns (ok, message, start,
        target)."""
        ok, start, msg = self.get_position()
        if not ok:
            return False, f"Could not read the lens position: {msg}", None, None
        target = max(config.CAMERA_FOCUS_POSITION_MIN,
                     min(config.CAMERA_FOCUS_POSITION_MAX, int(start) + int(delta)))
        ok, msg = self.move_to(target, save=False)
        return ok, msg, start, target

    def move_to(self, position, save=True):
        """(ok, message): bring the lens to `position` and save it for start-up
        (save=False: a layer's learned position, not the start-up one).

        The lens arrives going up with the backlash taken up, the way every
        measured position was reached, so it lands where the measurement was
        made (within the motor's drift)."""
        reason = self.qr_focus_unavailable_reason()
        if reason:
            return False, f"Focusing is not available: {reason}."
        ok, start, msg = self.get_position()
        if not ok:
            return False, f"Could not read the lens position: {msg}"
        position = max(config.CAMERA_FOCUS_POSITION_MIN,
                       min(config.CAMERA_FOCUS_POSITION_MAX, int(position)))
        self._cancel.clear()
        self._start, self._pos, self._up_travel = None, start, 0
        _qr_focus_running.set()
        try:
            ok, msg = self._approach(position)
        except Exception as e:
            logger.exception("Lens move failed")
            ok, msg = False, str(e)
        finally:
            _qr_focus_running.clear()
        if not ok:
            return False, f"The lens did not move: {msg}"
        logger.info("Focus: lens set to %d (was %d)", position, start)
        if not save:
            return True, f"Lens set to {position}."
        if config.save_camera_focus_position(position):
            return True, f"Lens set to {position} and saved for start-up."
        return True, f"Lens set to {position}, but it could not be saved for start-up."

    def _run(self, target, progress, measure_range=False, save=True):
        reason = self.qr_focus_unavailable_reason()
        if reason:
            return False, f"Focusing is not available: {reason}.", {}
        self._cancel.clear()
        self._start = self._pos = None
        _qr_focus_running.set()
        started = time.monotonic()
        try:
            if measure_range:
                ok, message, result = self._range(target, progress or (lambda *a: None))
            else:
                ok, message, result = self._focus(target, progress or (lambda *a: None),
                                                  save=save)
        except Exception as e:
            logger.exception("Focus search failed")
            ok, message, result = self._fail(f"Focusing failed: {e}")
        finally:
            _qr_focus_running.clear()
        result["seconds"] = round(time.monotonic() - started, 1)
        logger.info("Focus %s in %.1f s: %s", "done" if ok else "failed",
                    result["seconds"], message)
        return ok, message, result

    def _qr_target(self, region=None):
        """(boxes, kind, error) for all the QR codes in view. Looks at the lens
        position as it is first; if YOLO sees nothing there (the codes may be
        too soft to find), at the positions learned for the layers of a stack,
        then near the lens, then at a few spread positions.

        Near before far: on 2026-11-03 the codes were lost at lens 1034 and
        read at 1014, but only 400 / 800 / 1200 were tried, all far too soft."""
        lo, hi = config.CAMERA_FOCUS_POSITION_MIN, config.CAMERA_FOCUS_POSITION_MAX
        here = self._pos
        try:
            learned = sorted(set(config.layer_focus_positions().values()),
                             key=lambda p: abs(p - here))
        except Exception:
            learned = []
        near = [here + d for d in (-40, 40, -80, 80, -120, 120)]
        spread = [round(lo + f * (hi - lo)) for f in (0.5, 0.25, 0.75)]
        tries = []
        for pos in [here] + learned + near + spread:
            pos = max(lo, min(hi, int(pos)))
            if pos not in tries:
                tries.append(pos)
        for pos in tries:
            if pos != self._pos:
                ok, msg = self._move(pos)
                if not ok:
                    return [], "", f"The lens did not move: {msg}"
            if self._cancel.is_set():
                return [], "", "Focusing was stopped."
            boxes = self._find_boxes()
            if boxes and region:
                # Only the stack's own codes (where the last layer stood); the
                # floor, the crate and other kegs would pull the focus away.
                x1, y1, x2, y2 = region
                inside = [b for b in boxes
                          if x1 <= (b[0] + b[2]) / 2 <= x2 and y1 <= (b[1] + b[3]) / 2 <= y2]
                if inside:
                    logger.info("Focus target: %d of %d code(s) inside the stack area %s",
                                len(inside), len(boxes), tuple(int(v) for v in region))
                    boxes = inside
            if boxes:
                return boxes, "qr", ""
        return [], "", ("No QR code in view. Put a pallet of labelled kegs under "
                         "the camera, or double-tap the area to focus on.")

    def _region_target(self, region):
        """(boxes, kind, error) for the FOCUS button: the QR codes whose centre
        is inside `region`, else the region itself."""
        grabbed = self._grab()
        if grabbed is None:
            return [], "", "No picture from the camera."
        h, w = grabbed[1].shape[:2]
        rx1, ry1, rx2, ry2 = (int(region[0] * w), int(region[1] * h),
                              int(region[2] * w), int(region[3] * h))
        inside = [b for b in self._find_boxes(grabbed)
                  if rx1 <= (b[0] + b[2]) / 2 <= rx2 and ry1 <= (b[1] + b[3]) / 2 <= ry2]
        if inside:
            return inside, "qr", ""
        return [(rx1, ry1, rx2, ry2)], "area", ""

    def _point_target(self, x, y):
        """(boxes, kind, error) for a tap: the QR box under it, else a square."""
        grabbed = self._grab()
        if grabbed is None:
            return [], "", "No picture from the camera."
        h, w = grabbed[1].shape[:2]
        margin = 0.02 * w                     # a fingertip's slack around a box
        for x1, y1, x2, y2 in self._find_boxes(grabbed):
            if x1 - margin <= x <= x2 + margin and y1 - margin <= y <= y2 + margin:
                return [(x1, y1, x2, y2)], "qr", ""
        half = max(16, int(float(config.CAMERA_TAP_FOCUS_AREA) * w / 2))
        x, y = int(min(max(x, half), w - half)), int(min(max(y, half), h - half))
        return [(x - half, y - half, x + half, y + half)], "area", ""

    def _focus(self, target, progress, save=True):
        ok, start, msg = self.get_position()
        if not ok:
            return False, f"Could not read the lens position: {msg}", {}
        self._start = self._pos = start
        progress("Looking for the target…", [], start)
        boxes, kind, err = target()
        if err:
            return self._fail(err)
        logger.info("Focus target: %d %s box(es) %s, lens at %d", len(boxes), kind,
                    boxes, self._pos)

        # The focus motor cannot be sent back to a position measured earlier:
        # on the iCam-540 a position reached going down is 55+ steps off the
        # same one reached going up (backlash), and runs drift by tens of
        # steps, against a sharp zone only ~30 steps wide (logs 2026-10-28).
        # So every move that is measured goes up, and the lens is finally
        # brought up to the peak in small steps, stopping as soon as the
        # picture is actually sharp (closed loop) instead of at a number.
        self._up_travel = 0          # upward travel since the last downward move
        logger.info("[FOCUS-SCORE] lens %d (start, approach unknown): sharpness %s",
                    self._pos, ", ".join(f"{v:.0f}" for v in self._measure(boxes)))

        # samples[position] = sharpness of each box there, reached going up
        samples = {}

        def measure(pos):
            """Mean sharpness at `pos`, reached going up."""
            if self._cancel.is_set():
                raise _Stopped()
            ok, msg = self._approach(pos)
            if not ok:
                raise _LensError(msg)
            samples[pos] = self._measure(boxes)
            logger.info("[FOCUS-SCORE] lens %d: sharpness %s", pos,
                        ", ".join(f"{v:.0f}" for v in samples[pos]))
            progress("Focusing…", _curve(samples), pos)
            return sum(samples[pos]) / len(samples[pos])

        try:
            window = int(getattr(self, "_window", None) or config.CAMERA_FOCUS_LOCAL_WINDOW)
            backlash = int(config.CAMERA_FOCUS_BACKLASH_STEPS)
            # 1. Roughly where the peak is: one continuous upward glide through
            #    the neighbourhood of where the target was found, scoring
            #    frames on the move (most targets are near focus already).
            origin = self._pos
            scan = self._glide_up(origin - window, origin + window, boxes, progress)
            if scan is None:
                # Flat nearby: far from focus. Sweep the whole range, then
                # glide up through what it found. A peak seen while the lens
                # went down is really about the backlash higher.
                found = self._sweep(boxes, progress)
                if found is not None:
                    peak, going_down = found
                    scan = self._glide_up(peak - window,
                                          peak + window + (backlash if going_down else 0),
                                          boxes, progress)
            if scan is None:
                return self._fail("Nothing there got sharper anywhere in the lens range. "
                                  "Double-tap a QR code or an area with detail and try "
                                  "again.")
            # 2. Come up to it once more in small steps, stopping when it reads.
            best, final, top = self._final_pass(*scan, boxes, kind, measure)
        except _Stopped:
            return self._fail("Focusing was stopped.")
        except _LensError as e:
            return self._fail(f"The lens did not move: {e}")

        points = _curve(samples)
        if best is None:
            return self._fail(f"The lens did not come back to the sharp position (best "
                              f"sharpness {top:.0f}). Try again.")
        progress("Done", points, best)

        box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
        # Sharpest is not the same as readable: check the code reads now. For
        # an area, read the whole QR codes in it (the square can cut one).
        if kind == "area":
            boxes = self._codes_in(box) or boxes
        texts = self._read_target(boxes)
        shown = [box] + list(boxes)           # the square and any whole code in it
        self._save_crop((min(b[0] for b in shown), min(b[1] for b in shown),
                         max(b[2] for b in shown), max(b[3] for b in shown)),
                        best, kind, "read" if texts else "unread")
        what = (f"{len(boxes)} QR code(s)" if kind == "qr" and len(boxes) > 1 else
                "the QR code" if kind == "qr" else "the chosen area")
        message = f"Focused on {what}: lens at {best} (was {start})."
        if texts:
            message += f" Read: {', '.join(texts[:3])}."
        elif kind == "qr":
            # The sharpest the lens can make it, and still unreadable: the code
            # may be too small, glared or at an angle. Not saved for start-up,
            # so an unreadable focus never becomes the boot default.
            logger.info("Focus: QR still unreadable at its sharpest (lens %d)", best)
            return True, (message + " The code still does not read at its sharpest "
                          "— it may be too small, glared or at an angle. Not saved for "
                          "start-up."), {"position": best, "start": start, "points": points,
                                         "boxes": len(boxes), "target": kind, "box": box,
                                         "read": False}
        result = {"position": best, "start": start, "points": points,
                  "boxes": len(boxes), "target": kind, "box": box,
                  "read": bool(texts) or kind != "qr"}
        if not save:
            return True, message, result
        if kind == "area" and not texts:
            # An area is often the floor beside the kegs (2026-11-03: a tap on
            # the floor saved 1034 for start-up, and every start after that
            # had layer 1, 0.25 m higher, too soft to find). Start-up keeps
            # only a focus that reads a code.
            return True, (message + " Used now, not saved for start-up: no code "
                          "read there."), result
        if config.save_camera_focus_position(best):
            message += " Saved for start-up."
        else:
            message += " Could not save it for start-up."
        return True, message, result

    def _range(self, target, progress):
        """measure_range(): focus without saving, then score a band of lens
        positions around the peak, every one reached going up (backlash)."""
        ok, message, found = self._focus(target, progress, save=False)
        if not ok:
            return ok, message, found
        peak, start = found["position"], found["start"]
        boxes = self._find_boxes()
        if not boxes:
            return self._fail("The QR codes were lost after focusing. Keep the layer "
                              "still under the camera and try again.")
        span, step = int(config.FOCUS_RANGE_SPAN), max(1, int(config.FOCUS_RANGE_STEP))
        lo = max(config.CAMERA_FOCUS_POSITION_MIN, peak - span)
        hi = min(config.CAMERA_FOCUS_POSITION_MAX, peak + span)
        samples = {}
        try:
            for pos in range(lo, hi + 1, step):
                if self._cancel.is_set():
                    raise _Stopped()
                ok, msg = self._approach(pos)
                if not ok:
                    raise _LensError(msg)
                samples[pos] = self._measure(boxes)
                progress("Measuring the focus range…", _curve(samples), pos)
            ok, msg = self._approach(start)
        except _Stopped:
            return self._fail("Measuring was stopped.")
        except _LensError as e:
            return self._fail(f"The lens did not move: {e}")
        points = _curve(samples)
        band = focus_band(points)
        if band is None:
            return self._fail("No sharp range found for this layer. Try again.")
        progress("Done", points, start)
        logger.info("[FOCUS-RANGE] %d box(es): sharp from lens %d to %d, peak %d",
                    len(boxes), band["lo"], band["hi"], band["peak"])
        message = (f"Sharp from lens {band['lo']} to {band['hi']} (sharpest {band['peak']}).")
        message += (f" The lens is back at {start}." if ok else
                    " The lens could not be moved back — press RESET AUTO-FOCUS.")
        return True, message, {"position": start, "start": start, "points": points,
                               "band": band, "peak": band["peak"], "boxes": len(boxes),
                               "target": "qr", "read": found.get("read")}

    def _codes_in(self, area):
        """QR boxes YOLO finds now that overlap `area` (x1, y1, x2, y2)."""
        grabbed = self._grab()
        if grabbed is None:
            return []
        return [b for b in self._find_boxes(grabbed)
                if b[0] < area[2] and b[2] > area[0] and b[1] < area[3] and b[3] > area[1]]

    def _read_target(self, boxes, fresh=True):
        """Texts the line's readers get from the target boxes, in a fresh frame
        (or with fresh=False the newest one, already after the last move)."""
        if not hasattr(self.detector, "read_crop"):
            return []
        if fresh:
            self.camera.wait_for_new_frames(
                max(1, int(config.CAMERA_QR_FOCUS_FRESH_FRAMES)), timeout=2.0)
        grabbed = self._grab()
        if grabbed is None:
            return []
        gray = grabbed[1]
        h, w = gray.shape[:2]
        texts = []
        for x1, y1, x2, y2 in boxes:
            pad = 15                       # as the detector pads its crops
            crop = np.ascontiguousarray(gray[max(0, y1 - pad):min(h, y2 + pad),
                                             max(0, x1 - pad):min(w, x2 + pad)])
            texts += [t for t in self.detector.read_crop(crop) if t not in texts]
        logger.info("[FOCUS-READ] %s", ", ".join(texts) if texts else "nothing read")
        return texts

    def _save_crop(self, box, position, kind, read):
        """Save the target at full resolution as it looks at the chosen focus,
        to logs/focus/ (the last CAMERA_FOCUS_KEEP_CROPS are kept). The screen
        preview is shrunk to 1280x720, so it cannot show whether a code is
        really sharp; this picture can. Never raises."""
        keep = int(getattr(config, "CAMERA_FOCUS_KEEP_CROPS", 0))
        if keep <= 0:
            return
        try:
            self.camera.wait_for_new_frames(
                max(1, int(config.CAMERA_QR_FOCUS_FRESH_FRAMES)), timeout=2.0)
            grabbed = self._grab()
            if grabbed is None:
                return
            gray = grabbed[1]
            h, w = gray.shape[:2]
            pad = 40
            x1, y1 = max(0, box[0] - pad), max(0, box[1] - pad)
            x2, y2 = min(w, box[2] + pad), min(h, box[3] + pad)
            folder = config.BASE_DIR / "logs" / "focus"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{kind}_lens{position}_{read}.png"
            cv2.imwrite(str(path), np.ascontiguousarray(gray[y1:y2, x1:x2]))
            for old in sorted(folder.glob("*.png"))[:-keep]:
                old.unlink()
            logger.info("[FOCUS-CROP] %s (%dx%d px, sharpness %.0f)", path.name,
                        x2 - x1, y2 - y1, _box_sharpness(gray, [box])[0])
        except Exception:
            logger.debug("Focus crop not saved", exc_info=True)

    def _approach(self, pos):
        """Move to `pos` so the lens arrives going up with the motor's backlash
        taken up: from above, or after less than CAMERA_FOCUS_BACKLASH_STEPS
        of upward travel, the lens first goes that far below `pos`."""
        lo = config.CAMERA_FOCUS_POSITION_MIN
        backlash = int(config.CAMERA_FOCUS_BACKLASH_STEPS)
        if pos < self._pos or self._up_travel + (pos - self._pos) < backlash:
            under = max(lo, pos - backlash)
            if under != self._pos:
                ok, msg = self._move(under, wait_frames=False)
                if not ok:
                    return False, msg
        if pos == self._pos:
            if not self.camera.wait_for_new_frames(
                    max(1, int(config.CAMERA_QR_FOCUS_FRESH_FRAMES)), timeout=3.0):
                return False, "no new frames from the camera"
            return True, ""
        return self._move(pos)

    def _glide_up(self, a, b, boxes, progress):
        """Run the lens up from `a` to `b` in one move, scoring every frame on
        the way (and on up while still getting sharper at `b`). The lens first
        goes CAMERA_FOCUS_BACKLASH_STEPS below `a`, so the backlash is taken
        up before `a`; frames below `a` are not used. Returns (estimated peak
        position, its sharpness), or None when nothing got clearly sharper."""
        lo, hi = config.CAMERA_FOCUS_POSITION_MIN, config.CAMERA_FOCUS_POSITION_MAX
        chunk = max(1, int(config.CAMERA_FOCUS_SWEEP_CHUNK))
        a, b = max(lo, a), min(hi, b)
        under = max(lo, a - int(config.CAMERA_FOCUS_BACKLASH_STEPS))
        if under != self._pos:
            ok, msg = self._move(under, wait_frames=False)
            if not ok:
                raise _LensError(msg)
        seen, down, history = {}, {}, []
        started = time.monotonic()
        self._scored_move(b, boxes, seen, down, history)
        progress("Searching…", _curve(seen), self._pos)
        while (self._pos < hi and not _peak_passed(history) and history
               and history.index(max(history)) >= len(history) - 2):
            self._scored_move(min(hi, self._pos + chunk), boxes, seen, down, history)
            progress("Searching…", _curve(seen), self._pos)
        used = {p: v for p, v in seen.items() if p >= a}
        if len(used) < 3 or not _has_contrast(used):
            logger.info("[FOCUS-GLIDE] lens %d -> %d: nothing got sharper (%.1f s)",
                        a, self._pos, time.monotonic() - started)
            return None
        seen_at = max(_curve(used), key=lambda q: q[1])[0]
        top = sum(used[seen_at]) / len(used[seen_at])
        # Where the final pass finds it: on the iCam-540 the glide's estimate
        # came out 90-120 steps low in every run (2026-10-28 logs).
        peak = min(hi, seen_at + int(config.CAMERA_FOCUS_GLIDE_OFFSET))
        logger.info("[FOCUS-GLIDE] lens %d -> %d: peak seen near %d, expected at %d, "
                    "sharpness %.0f (%.1f s, %d frames)", a, self._pos, seen_at, peak, top,
                    time.monotonic() - started, len(used))
        return peak, top

    def _final_pass(self, peak, top, boxes, kind, measure):
        """Bring the lens up to `peak` from below in CAMERA_FOCUS_FINE_STEP
        steps (double steps while still blurred, and on past the expected
        peak while still getting sharper), watching the picture, and stop:
          - QR code: at the first position where it reads;
          - area: as soon as it starts getting softer again (one step past
            the peak).
        If the pass goes over the peak without that (the motor drifted, or the
        code will not read), it aims at the sharpest position this pass saw
        and comes up once more, stopping at CAMERA_FOCUS_STOP_FRACTION of that
        sharpness. Returns (position, sharpness, best seen), or
        (None, 0, best seen) when it never got sharp again."""
        lo, hi = config.CAMERA_FOCUS_POSITION_MIN, config.CAMERA_FOCUS_POSITION_MAX
        fine = max(1, int(config.CAMERA_FOCUS_FINE_STEP))
        stop_at = float(config.CAMERA_FOCUS_STOP_FRACTION)
        reach = int(config.CAMERA_FOCUS_FINAL_REACH)
        for attempt in (1, 2):
            pos, seen = max(lo, peak - reach), []
            while pos <= hi:
                score = measure(pos)
                seen.append((pos, score))
                values = [v for _, v in seen]
                if attempt == 1:
                    if kind == "qr":
                        if score >= 0.3 * top and self._read_target(boxes, fresh=False):
                            logger.info("[FOCUS-PEAK] stopped at lens %d: reads "
                                        "(sharpness %.0f)", pos, score)
                            return pos, score, max(top, score)
                    elif (len(seen) >= 2 and score < seen[-2][1]
                          and seen[-2][1] == max(values)
                          and max(values) >= float(config.CAMERA_QR_FOCUS_MIN_CONTRAST)
                          * min(values)):
                        if score >= stop_at * seen[-2][1]:
                            logger.info("[FOCUS-PEAK] stopped at lens %d, just past the "
                                        "peak (sharpness %.0f, peak %.0f)", pos, score,
                                        seen[-2][1])
                            return pos, score, seen[-2][1]
                        # Far softer one step on (2026-11-03: 185 against a peak of
                        # 521, saved as the start-up focus): the peak is that narrow,
                        # so go back to it, coming up again (backlash).
                        back = seen[-2][0]
                        logger.info("[FOCUS-PEAK] lens %d is much softer than the peak "
                                    "(%.0f vs %.0f) — back to %d", pos, score,
                                    seen[-2][1], back)
                        score = measure(back)
                        return back, score, max(top, seen[-2][1])
                elif score >= stop_at * top:
                    logger.info("[FOCUS-PEAK] stopped at lens %d: sharpness %.0f "
                                "(best %.0f, pass 2)", pos, score, top)
                    return pos, score, top
                if _peak_passed(values):
                    break                     # over the peak without stopping
                if pos >= peak + 2 * reach and values.index(max(values)) < len(values) - 1:
                    break                     # past the window and not rising
                # Double steps through blur, single ones once it gets sharper.
                pos += fine if score >= 2 * min(values) else 2 * fine
            peak, top = max(seen, key=lambda q: q[1])
            logger.info("[FOCUS-PEAK] pass %d peaked at lens %d (sharpness %.0f)",
                        attempt, peak, top)
        return None, 0.0, top

    def _scored_move(self, b, boxes, seen, down, history):
        """Move the lens from where it is to `b` in one go, scoring every
        frame while it moves. The camera answers a move only when the motor
        stops, so the move runs on a helper thread. A frame's lens position is
        estimated from the time since the move started (motor speed, move
        overhead, frame latency). Adds to `seen` (position -> sharpness of
        each box), `down` (position -> lens going down) and `history` (mean
        sharpness in time order)."""
        ms_per_step = float(config.CAMERA_FOCUS_MOTOR_MS_PER_STEP)
        delay_ms = (float(config.CAMERA_FOCUS_MOTOR_OVERHEAD_MS)
                    + float(config.CAMERA_FOCUS_FRAME_LATENCY_MS))
        if self._cancel.is_set():
            raise _Stopped()
        a = self._pos
        answer = {}
        t0 = time.monotonic()
        mover = threading.Thread(target=lambda: answer.update(r=self.set_position(b)),
                                 name="FocusScoredMove", daemon=True)
        mover.start()
        frames = 0
        while mover.is_alive():
            if not self.camera.wait_for_new_frames(1, timeout=0.5):
                continue
            t = time.monotonic()
            grabbed = self._grab()
            if grabbed is None:
                continue
            moved = ((t - t0) * 1000 - delay_ms) / ms_per_step
            est = int(round(a + (1 if b > a else -1) * max(0.0, min(abs(b - a), moved))))
            values = _box_sharpness(grabbed[1], boxes)
            seen[est], down[est] = values, b < a
            history.append(sum(values) / len(values))
            frames += 1
        mover.join()
        ok, msg = answer.get("r", (False, "no answer from the camera"))
        if not ok:
            raise _LensError(msg)
        self._pos = b
        self._up_travel = (self._up_travel + b - a) if b > a else 0
        # A move quicker than a frame scored nothing: score its end.
        if not frames and self.camera.wait_for_new_frames(
                max(1, int(config.CAMERA_QR_FOCUS_FRESH_FRAMES)), timeout=3.0):
            grabbed = self._grab()
            if grabbed is not None:
                values = _box_sharpness(grabbed[1], boxes)
                seen[b], down[b] = values, b < a
                history.append(sum(values) / len(values))
                frames = 1
        logger.info("[FOCUS-SWEEP] lens %d -> %d: %d frames scored in %.0f ms",
                    a, b, frames, (time.monotonic() - t0) * 1000)

    def _sweep(self, boxes, progress):
        """Run the lens across its range, scoring frames while it moves.

        The camera answers a move only when the motor stops, so each move of
        CAMERA_FOCUS_SWEEP_CHUNK steps runs on a helper thread while this one
        scores every new frame. A frame's lens position is estimated from the
        time since the move started (motor speed, move overhead, frame
        latency); the glide afterwards narrows it down. Goes to the
        nearer end of the range first, then to the other end, and stops once
        a clear peak has been passed. Returns (estimated peak position, whether
        the lens was going down there), or None when nothing got sharper."""
        lo, hi = config.CAMERA_FOCUS_POSITION_MIN, config.CAMERA_FOCUS_POSITION_MAX
        chunk = max(1, int(config.CAMERA_FOCUS_SWEEP_CHUNK))
        seen = {}          # estimated position -> sharpness of each box
        down = {}          # estimated position -> the lens was going down
        history = []       # mean sharpness in time order, for the early stop
        started = time.monotonic()
        near = lo if self._pos - lo <= hi - self._pos else hi
        for end in (near, hi if near == lo else lo):
            while self._pos != end:
                self._scored_move(self._pos + max(-chunk, min(chunk, end - self._pos)),
                                  boxes, seen, down, history)
                progress("Searching…", _curve(seen), self._pos)
                if _peak_passed(history):
                    break
            else:
                continue
            break
        if not _has_contrast(seen):
            logger.info("[FOCUS-SWEEP] nothing got sharper (%.1f s)",
                        time.monotonic() - started)
            return None
        peak = max(_curve(seen), key=lambda p: p[1])[0]
        logger.info("[FOCUS-SWEEP] peak near lens %d going %s (%.1f s, %d frames)", peak,
                    "down" if down[peak] else "up", time.monotonic() - started, len(history))
        return peak, down[peak]

    def _fail(self, message):
        """Put the lens back where the sweep started and report `message`."""
        start = self._start
        if start is not None and self._pos is not None and self._pos != start:
            ok, _ = self.set_position(start)
            message += (" The lens is back where it was." if ok else
                        " The lens could not be moved back — press RESET AUTO-FOCUS.")
        return False, message, {}

    def _move(self, position, wait_frames=True):
        """Move the lens and wait until the frames show the new focus (not
        with wait_frames=False: a move nothing is measured after).

        Logs one [FOCUS-TIMING] line per move: how long the camera took to
        answer the move, the settle wait, and the wait for fresh frames. If the
        answer time grows with the distance moved, the camera answers only once
        the motor has stopped, and CAMERA_QR_FOCUS_SETTLE can come down."""
        old = self._pos if self._pos is not None else position
        t0 = time.monotonic()
        ok, msg = self.set_position(position)
        t1 = time.monotonic()
        if not ok:
            return False, msg
        self._pos = position
        self._up_travel = (self._up_travel + position - old) if position > old else 0
        if not wait_frames:
            logger.info("[FOCUS-TIMING] lens %d -> %d (%d steps): camera answered %.0f ms "
                        "(backlash approach)", old, position, abs(position - old),
                        (t1 - t0) * 1000)
            return True, ""
        settle = (float(config.CAMERA_QR_FOCUS_SETTLE)
                  + abs(position - old) * float(config.CAMERA_QR_FOCUS_SETTLE_PER_STEP))
        if self._cancel.wait(settle):
            return True, ""
        t2 = time.monotonic()
        # A frame captured before or during the move still shows the old focus.
        fresh = self.camera.wait_for_new_frames(
            max(1, int(config.CAMERA_QR_FOCUS_FRESH_FRAMES)), timeout=3.0)
        t3 = time.monotonic()
        logger.info("[FOCUS-TIMING] lens %d -> %d (%d steps): camera answered %.0f ms, "
                    "settle %.0f ms, fresh frames %.0f ms, total %.0f ms",
                    old, position, abs(position - old), (t1 - t0) * 1000,
                    (t2 - t1) * 1000, (t3 - t2) * 1000, (t3 - t0) * 1000)
        if not fresh:
            return False, "no new frames from the camera"
        return True, ""

    def _grab(self):
        """(small, gray, k): a picture for YOLO, the full-size grey frame, and
        how many times larger gray is. The same inputs DetectWorker uses."""
        width = int(getattr(config, "YOLO_IMGSZ", 1280))
        inputs = (self.camera.get_detect_inputs(width)
                  if hasattr(self.camera, "get_detect_inputs") else None)
        if inputs is not None and inputs[0]:
            _, small, gray, k = inputs
            return small, gray, k
        ret, frame = self.camera.get_frame_no_copy()
        if not ret or frame is None:
            return None
        return frame, cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 1

    def _find_boxes(self, grabbed=None):
        """The largest QR boxes in the current frame (full-size coordinates)."""
        grabbed = grabbed or self._grab()
        if grabbed is None:
            return []
        small, gray, k = grabbed
        h, w = gray.shape[:2]
        boxes = []
        for x1, y1, x2, y2 in self.detector.locate_boxes(small, scale=k):
            x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
            if x2 - x1 >= 16 and y2 - y1 >= 16:
                boxes.append((x1, y1, x2, y2))
        boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        return boxes[:max(1, int(config.CAMERA_QR_FOCUS_MAX_BOXES))]

    def _measure(self, boxes):
        """Sharpness of each box (variance of the Laplacian), averaged over
        CAMERA_QR_FOCUS_SAMPLES frames."""
        n = max(1, int(config.CAMERA_QR_FOCUS_SAMPLES))
        totals = [0.0] * len(boxes)
        taken = 0
        for i in range(n):
            if i:
                self.camera.wait_for_new_frames(1, timeout=1.0)
            grabbed = self._grab()
            if grabbed is None:
                continue
            for j, v in enumerate(_box_sharpness(grabbed[1], boxes)):
                totals[j] += v
            taken += 1
        return [t / taken for t in totals] if taken else totals

    # Helpers
    def _set_prop(self, prop, value):
        if self.camera is None:
            return False
        try:
            return bool(self.camera.set_property(prop, value))
        except Exception:
            logger.debug("Camera property %s=%s failed.", prop, value,
                         exc_info=True)
            return False


class _Stopped(Exception):
    """The search was cancelled (app exit)."""


class _LensError(Exception):
    """The camera did not accept a lens move."""


def _box_sharpness(gray, boxes):
    """Sharpness (variance of the Laplacian) inside each box of `gray`."""
    return [float(cv2.Laplacian(np.ascontiguousarray(gray[y1:y2, x1:x2]), cv2.CV_64F).var())
            for x1, y1, x2, y2 in boxes]


def _peak_passed(history):
    """True once the sweep has clearly gone past a peak: something got at
    least CAMERA_QR_FOCUS_MIN_CONTRAST times sharper than the background, and
    the latest frame is back near the background."""
    if len(history) < 3:
        return False
    floor, top = min(history), max(history)
    return (floor > 0 and top / floor >= float(config.CAMERA_QR_FOCUS_MIN_CONTRAST)
            and history.index(top) < len(history) - 1
            and history[-1] < floor + 0.3 * (top - floor))


def _has_contrast(samples):
    """True when the sharpest position measured is clearly sharper than the
    softest (CAMERA_QR_FOCUS_MIN_CONTRAST): the target has detail, the lens
    moves, and the search has seen focus change."""
    scores = [s for _, s in _curve(samples)]
    return (len(scores) > 1 and min(scores) > 0
            and max(scores) / min(scores) >= float(config.CAMERA_QR_FOCUS_MIN_CONTRAST))


def focus_band(points, sharp=None):
    """The sharp band of a measured curve: the lens positions around its peak
    scoring at least `sharp` (CAMERA_FOCUS_STOP_FRACTION, the same "sharp now"
    the focus search stops at). Edges are interpolated between the measured
    positions; one the measurement never fell below is marked open, meaning the
    band reaches at least that far. None for an empty curve.

    {"lo", "hi", "peak", "lo_open", "hi_open"}
    """
    if not points:
        return None
    sharp = float(config.CAMERA_FOCUS_STOP_FRACTION if sharp is None else sharp)
    i = max(range(len(points)), key=lambda k: points[k][1])

    def edge(step):
        j = i
        while 0 <= j + step < len(points) and points[j + step][1] >= sharp:
            j += step
        if not 0 <= j + step < len(points):
            return points[j][0], True
        (p1, s1), (p2, s2) = points[j], points[j + step]
        return round(p1 + (p2 - p1) * (s1 - sharp) / ((s1 - s2) or 1)), False

    lo, lo_open = edge(-1)
    hi, hi_open = edge(1)
    return {"lo": lo, "hi": hi, "peak": points[i][0], "lo_open": lo_open, "hi_open": hi_open}


def score_at(points, position):
    """A measured curve's score at `position`, interpolated. None outside it."""
    if not points or position is None or not points[0][0] <= position <= points[-1][0]:
        return None
    for (p1, s1), (p2, s2) in zip(points, points[1:]):
        if p1 <= position <= p2:
            return s1 if p2 == p1 else s1 + (s2 - s1) * (position - p1) / (p2 - p1)
    return points[-1][1]


def common_band(bands):
    """The lens positions sharp for every band, as (lo, hi), or None."""
    if not bands:
        return None
    lo, hi = max(b["lo"] for b in bands), min(b["hi"] for b in bands)
    return (lo, hi) if lo <= hi else None


def _curve(samples):
    """[(position, score), ...] sorted by position from {position: [sharpness
    of each box]}.

    Each box's sharpness is divided by its best value over all positions, and
    a position's score is the mean over the boxes (0-1). Without that, one
    high-contrast label would decide the focus alone; with it, a pallet of
    mixed keg heights gets the position that suits the codes overall."""
    if not samples:
        return []
    n = len(next(iter(samples.values())))
    peaks = [max(values[i] for values in samples.values()) or 1.0 for i in range(n)]
    return sorted((pos, sum(v / p for v, p in zip(values, peaks)) / n)
                  for pos, values in samples.items())
