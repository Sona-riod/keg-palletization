# modules/camera_select.py - layer 1 from a USB webcam, the rest from the iCam
"""
Two cameras behind the one camera interface the app already uses.

The iCam (primary) reads every layer exactly as before. When a USB webcam is
plugged into the iCam and found, it reads layer 1 instead: the layer furthest
from the iCam, whose codes are smallest in its picture. Layers 2 and up go
back to the iCam.

Rules, all in this module so the operator page only asks "which layer now":
  - No webcam found at start-up: every call goes straight to the iCam. The
    app behaves exactly as it did before this module existed.
  - The webcam is chosen only between layers (no keg counted yet), never in
    the middle of one, so all box coordinates of a layer come from one camera.
  - The webcam stops sending pictures while it reads: the iCam takes over at
    once for the rest of that layer and the webcam is looked for again later.
  - Both cameras stay open. The one not reading only takes frames off its
    device without decoding them (CameraManager.set_idle), so switching is
    immediate and costs almost nothing while it stands by.

Focus, exposure, white balance and light keep driving the iCam through its
REST service: they are given `primary`, never this object.

No Qt here: the operator page calls select_for_layer() from the GUI thread;
finding and starting a webcam runs on its own thread.
"""
import glob
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

ICAM = "icam"
WEBCAM = "webcam"


# Finding the webcam
_realpath = os.path.realpath     # replaced in tools/layer1_webcam_test.py


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _node_number(dev):
    """'/dev/video10' -> 10, a by-id link -> the number it points at, else None."""
    try:
        real = _realpath(str(dev))
        base = os.path.basename(real)
        return int(base[len("video"):]) if base.startswith("video") else None
    except (TypeError, ValueError):
        return None


def find_usb_webcams(sys_root="/sys/class/video4linux", exclude=(), name_contains=""):
    """[(device path, name)] of USB video-capture nodes, lowest number first.

    A USB webcam usually creates two nodes (pictures and metadata); only the
    picture node has index 0. The iCam's own sensor is not on USB, and its
    node is excluded by number as well, so it can never be picked.
    """
    excluded = {n for n in (_node_number(e) if isinstance(e, str) else e for e in exclude)
                if isinstance(n, int)}
    wanted = (name_contains or "").strip().lower()
    found = []
    for node in sorted(glob.glob(os.path.join(sys_root, "video*")),
                       key=lambda p: _node_number(p) or 0):
        number = _node_number(node)
        if number is None or number in excluded:
            continue
        index = _read(os.path.join(node, "index"))
        if index not in ("", "0"):
            continue                     # metadata node of a webcam
        if "/usb" not in _realpath(os.path.join(node, "device")).replace("\\", "/"):
            continue                     # not a USB device (the iCam's sensor, ...)
        name = _read(os.path.join(node, "name"))
        if wanted and wanted not in name.lower():
            continue
        found.append((f"/dev/video{number}", name))
    return found


def resolve_webcam_device(cfg, primary_cfg):
    """(device, name) for the layer-1 webcam, or (None, reason)."""
    fixed = cfg.get("device")
    if fixed not in (None, ""):
        if isinstance(fixed, str) and fixed.startswith("/dev/") and not os.path.exists(fixed):
            return None, f"{fixed} is not there"
        return fixed, str(fixed)
    exclude = [primary_cfg.get("device")]
    found = find_usb_webcams(exclude=exclude, name_contains=cfg.get("name_contains", ""))
    if not found:
        return None, "no USB webcam plugged in"
    return found[0]


# Keg positions
def to_primary_orientation(bbox, rotate=0, mirror=False):
    """A box from the webcam turned into the iCam's orientation, for keg
    positions only (row 1 = top of the iCam picture, column 1 its left).
    Only relative order matters there, so no frame size is needed."""
    x1, y1, x2, y2 = (float(v) for v in bbox)
    pts = [(x1, y1), (x2, y2)]
    turns = (int(rotate) // 90) % 4
    for _ in range(turns):                  # 90 degrees clockwise each
        pts = [(-y, x) for x, y in pts]
    if mirror:
        pts = [(-x, y) for x, y in pts]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# The two cameras behind one interface
class LayerCamera:
    """Looks like a CameraManager; frames come from whichever camera reads now."""

    def __init__(self, primary, settings, webcam_factory, find_device=None,
                 clock=time.monotonic, start_async=True, enabled=True):
        self.primary = primary
        self.webcam = None                   # a started CameraManager, or None
        self.webcam_name = ""
        self.webcam_device = None
        # Settings -> Cameras: off = the webcam is not opened and the iCam reads
        # every layer, exactly as before the webcam was added.
        self._enabled = bool(enabled)
        # Focus test (Settings -> USB webcam focus): the webcam stays awake;
        # _test_cam is a webcam opened only for the test (feature turned off).
        self._focus_test = False
        self._test_cam = None
        self._settings = settings            # dict, see from_config()
        self._factory = webcam_factory       # config dict -> CameraManager
        self._find = find_device or (lambda: resolve_webcam_device(
            settings["config"], getattr(primary, "config", {}) or {}))
        self._clock = clock
        self._async = start_async
        self._lock = threading.Lock()
        self._active = ICAM
        self._starting = False
        self._last_try = None
        self._preview = None                 # set_preview_size args, for a later webcam
        self._woke_at = None                 # clock() when the webcam last started reading
        self._listeners = []
        # After the webcam stalled, the iCam keeps the layer (and the next
        # attempts) until the count has gone up and back to 0: the next layer
        # or pallet. Stops the screen switching back and forth every
        # retry_s while a weak webcam keeps stalling.
        self._hold_icam = False
        self._hold_saw_kegs = False

    @classmethod
    def from_config(cls, primary, config_module):
        """None when the feature is off or does not apply: the app then uses
        the iCam directly, with no wrapper at all."""
        cfg = config_module
        if not getattr(cfg, "LAYER1_WEBCAM_ENABLED", False):
            return None
        if str(getattr(cfg, "CAMERA_SOURCE", "")).lower() != ICAM:
            return None
        from modules.camera import CameraManager
        settings = {
            "config": dict(getattr(cfg, "LAYER1_WEBCAM_CONFIG", {}) or {}),
            "rotate": getattr(cfg, "LAYER1_WEBCAM_ROTATE", 0),
            "mirror": bool(getattr(cfg, "LAYER1_WEBCAM_MIRROR", False)),
            "single_pallet": bool(getattr(cfg, "LAYER1_WEBCAM_SINGLE_PALLET", True)),
            "stall_s": float(getattr(cfg, "LAYER1_WEBCAM_STALL_S", 3.0)),
            "retry_s": float(getattr(cfg, "LAYER1_WEBCAM_RETRY_S", 30.0)),
            "hold_after_stall": bool(getattr(cfg, "LAYER1_WEBCAM_HOLD_AFTER_STALL", True)),
        }
        use = getattr(cfg, "layer1_webcam_use", None)
        try:
            enabled = bool(use()) if callable(use) else True
        except Exception:
            enabled = True
        # The focus saved in Settings -> USB webcam focus wins over the code default.
        saved_focus = getattr(cfg, "layer1_webcam_focus", None)
        try:
            focus = saved_focus() if callable(saved_focus) else None
        except Exception:
            focus = None
        if focus is not None:
            settings["config"]["focus"] = int(focus)
            settings["config"]["autofocus"] = False
        return cls(primary, settings, CameraManager, enabled=enabled)

    # On / off (Settings -> Cameras)
    @property
    def enabled(self):
        return self._enabled

    def set_enabled(self, on):
        """Turn the layer-1 webcam on or off, live.

        Off: the iCam takes over at once (even mid-layer, as when the webcam
        stalls) and the webcam is closed, so the iCam reads every layer as it
        did before. On: the webcam is looked for now and reads from the next
        layer 1 that has no keg counted yet."""
        on = bool(on)
        with self._lock:
            if on == self._enabled:
                return False
            self._enabled = on
        if on:
            self._hold_icam = self._hold_saw_kegs = False
            log.info("[CAMERA] Layer-1 webcam turned ON in Settings")
            self.look_for_webcam(force=True)
        else:
            log.info("[CAMERA] Layer-1 webcam turned OFF in Settings - the iCam reads "
                     "every layer")
            self._drop_webcam("the USB webcam was turned off in Settings")
        return True

    def detect(self):
        """(device, name) of the USB webcam that would be used, or (None, reason).
        Only looks (sysfs), never opens it: safe while it is off."""
        cam = self.webcam
        if cam is not None:
            return self.webcam_device, self.webcam_name
        try:
            return self._find()
        except Exception as e:
            return None, f"could not look for a USB webcam: {e}"

    def status(self):
        """What the Settings page shows. Keys: enabled, active, connected
        (opened and ready), device, name, detected_device, detected_name, reason,
        icam_device, icam_running."""
        device, name = self.detect()
        return {
            "enabled": self._enabled,
            "active": self._active,
            "connected": self.webcam is not None,
            "device": self.webcam_device,
            "name": self.webcam_name,
            "detected_device": device,
            "detected_name": name if device is not None else "",
            "reason": "" if device is not None else name,
            "icam_device": (getattr(self.primary, "config", {}) or {}).get("device"),
            "icam_running": bool(getattr(self.primary, "is_running", False)),
            "single_pallet": self._settings.get("single_pallet", True),
        }

    # State
    @property
    def active(self):
        return self._active

    @property
    def webcam_active(self):
        return self._active == WEBCAM

    def on_change(self, callback):
        """callback(active, reason) after every switch, on the calling thread."""
        self._listeners.append(callback)

    def _set_active(self, which, reason):
        with self._lock:
            if which == self._active:
                return False
            if which == WEBCAM and self.webcam is None:
                return False
            self._active = which
            if which == WEBCAM:
                self._woke_at = self._clock()
            if self.webcam is not None:
                self.webcam.set_idle(which != WEBCAM and not self._focus_test)
        log.info("[CAMERA] %s reads now: %s", "USB webcam" if which == WEBCAM else "iCam",
                 reason)
        for cb in list(self._listeners):
            try:
                cb(which, reason)
            except Exception:
                log.exception("Camera switch listener failed")
        return True

    # Finding and starting the webcam
    def start(self):
        ok = self.primary.start() if not getattr(self.primary, "is_running", False) else True
        self.look_for_webcam(force=True)
        return ok

    def look_for_webcam(self, force=False):
        """Find and start the webcam, at most once every retry_s seconds."""
        now = self._clock()
        with self._lock:
            if (not self._enabled or self.webcam is not None or self._starting
                    or self._test_cam is not None):
                return
            if (not force and self._last_try is not None
                    and now - self._last_try < self._settings["retry_s"]):
                return
            self._last_try = now
            self._starting = True
        if self._async:
            threading.Thread(target=self._start_webcam, name="Layer1Webcam",
                             daemon=True).start()
        else:
            self._start_webcam()

    def _start_webcam(self):
        cam = None
        try:
            device, name = self._find()
            if device is None:
                log.info("[CAMERA] Layer-1 webcam: %s - every layer is read by the iCam",
                         name)
                return
            cfg = dict(self._settings["config"])
            cfg["device"] = device
            cam = self._factory(cfg)
            if not cam.start():
                log.warning("[CAMERA] Layer-1 webcam %s (%s) did not start - every layer "
                            "is read by the iCam", device, name)
                return
            cam.set_idle(True)
            if self._preview is not None:
                cam.set_preview_size(*self._preview[0], **self._preview[1])
            with self._lock:
                if not self._enabled:
                    # Turned off in Settings while it was starting: close it.
                    log.info("[CAMERA] Layer-1 webcam is turned off - not used")
                    return
                self.webcam, self.webcam_name, self.webcam_device = cam, name, device
                cam = None
            log.info("[CAMERA] Layer-1 webcam found: %s (%s) - it reads layer 1", device,
                     name)
        except Exception:
            log.exception("[CAMERA] Layer-1 webcam could not be started")
        finally:
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
            with self._lock:
                self._starting = False

    def _drop_webcam(self, reason):
        with self._lock:
            cam, self.webcam = self.webcam, None
            self.webcam_device = None
            self._last_try = self._clock()
        self._set_active(ICAM, reason)
        if cam is not None:
            threading.Thread(target=cam.stop, name="Layer1WebcamStop", daemon=True).start()

    # Choosing the camera
    def wants_webcam(self, layer, stacking=True):
        if stacking:
            return int(layer or 1) == 1
        return self._settings["single_pallet"]

    def select_for_layer(self, layer, stacking=True, kegs_counted=0):
        """Called every detection round with the layer expected now. Switches
        only while no keg of that layer is counted. Returns the camera that
        reads now (ICAM or WEBCAM)."""
        self.check_stalled()
        want = WEBCAM if (self._enabled and self.wants_webcam(layer, stacking)) else ICAM
        if self._hold_icam:
            if kegs_counted:
                self._hold_saw_kegs = True
            elif self._hold_saw_kegs or want == ICAM:
                # The stalled layer is done (count back to 0) or a layer the
                # iCam reads anyway is due: the webcam may read again.
                self._hold_icam = self._hold_saw_kegs = False
                log.info("[CAMERA] Webcam hold after stall cleared - it may read "
                         "the next layer 1 again")
        if want == WEBCAM and self.webcam is None:
            # Still looked for during a hold, so it is ready for the next layer 1.
            self.look_for_webcam()
            want = ICAM
        if self._hold_icam:
            want = ICAM
        if want != self._active and not kegs_counted:
            self._set_active(want, f"layer {layer}" if stacking else "single pallet")
        return self._active

    def check_stalled(self):
        """The reading webcam stopped sending pictures: back to the iCam now."""
        cam = self.webcam
        if cam is None or self._active != WEBCAM:
            return False
        stall = self._settings["stall_s"]
        # It gets stall_s from when it woke: its last picture before the
        # stand-by (the previous stack's layer 1) is minutes old. After that a
        # picture younger than stall_s can only have been taken since waking.
        awake = self._clock() - (self._woke_at or 0.0)
        age = cam.frame_age() if hasattr(cam, "frame_age") else None
        fresh = age is not None and age < stall
        if getattr(cam, "is_running", True) and (awake < stall or fresh):
            return False
        if self._settings.get("hold_after_stall", True):
            self._hold_icam, self._hold_saw_kegs = True, False
        self._drop_webcam("the USB webcam stopped sending pictures - the iCam "
                          "finishes this layer")
        return True

    # Focus test (Settings -> USB webcam focus)
    def begin_focus_test(self):
        """(camera, message) of the webcam to focus, kept awake until
        end_focus_test(); (None, why) when there is none. Blocks while a
        webcam is opened just for the test (turned off in Settings): call it
        off the GUI thread."""
        with self._lock:
            self._focus_test = True
            cam = self.webcam
        if cam is not None:
            cam.set_idle(False)
            return cam, self.webcam_name
        with self._lock:
            starting = self._starting
        if starting:
            # Being opened right now (just turned on): wait for it.
            for _ in range(100):
                time.sleep(0.1)
                if self.webcam is not None or not self._starting:
                    break
            if self.webcam is not None:
                self.webcam.set_idle(False)
                return self.webcam, self.webcam_name
        device, name = self.detect()
        if device is None:
            self.end_focus_test()
            return None, name
        cfg = dict(self._settings["config"])
        cfg["device"] = device
        try:
            cam = self._factory(cfg)
            if not cam.start():
                self.end_focus_test()
                return None, f"the USB webcam {name} did not start"
        except Exception as e:
            self.end_focus_test()
            return None, f"the USB webcam could not be opened: {e}"
        if self._preview is not None:
            cam.set_preview_size(*self._preview[0], **self._preview[1])
        with self._lock:
            self._test_cam = cam
        log.info("[CAMERA] Layer-1 webcam %s opened for the focus test", device)
        return cam, name

    def end_focus_test(self):
        with self._lock:
            self._focus_test = False
            test_cam, self._test_cam = self._test_cam, None
            cam = self.webcam
            if cam is not None:
                cam.set_idle(self._active != WEBCAM)
        if test_cam is not None:
            try:
                test_cam.stop()
            except Exception:
                log.exception("Focus-test webcam stop failed")
            if self._enabled:
                self.look_for_webcam(force=True)

    @property
    def focus_testing(self):
        return self._focus_test

    def set_webcam_focus(self, cam, value):
        """Autofocus off and the lens to `value` on `cam` (the test's webcam).
        False when the webcam has no focus control."""
        import cv2
        cam.set_property(cv2.CAP_PROP_AUTOFOCUS, 0)
        return bool(cam.set_property(cv2.CAP_PROP_FOCUS, float(value)))

    def remember_focus(self, value):
        """Focus to set each time the webcam opens from now on (None: leave the lens)."""
        self._settings["config"]["focus"] = None if value is None else int(value)
        if value is not None:
            self._settings["config"]["autofocus"] = False

    @property
    def configured_focus(self):
        return self._settings["config"].get("focus")

    def to_primary_orientation(self, bbox):
        return to_primary_orientation(bbox, self._settings["rotate"],
                                      self._settings["mirror"])

    # The CameraManager interface
    def _cam(self):
        cam = self.webcam
        return cam if (self._active == WEBCAM and cam is not None) else self.primary

    @property
    def config(self):
        return self._cam().config

    @property
    def is_running(self):
        return getattr(self.primary, "is_running", False)

    def get_frame(self):
        return self._cam().get_frame()

    def get_frame_no_copy(self):
        return self._cam().get_frame_no_copy()

    def get_detect_inputs(self, width):
        cam = self._cam()
        return cam.get_detect_inputs(width) if hasattr(cam, "get_detect_inputs") else None

    def wait_for_new_frames(self, count=1, timeout=2.0):
        return self._cam().wait_for_new_frames(count, timeout)

    def get_preview(self):
        cam = self._cam()
        preview, src = cam.get_preview()
        if preview is None and cam is not self.primary:
            # The webcam has just woken and has no new picture yet: keep the
            # iCam's live picture on screen instead of an old or blank one.
            return self.primary.get_preview()
        return preview, src

    def set_preview_size(self, width, height, min_interval=0.0):
        self._preview = ((width, height), {"min_interval": min_interval})
        self.primary.set_preview_size(width, height, min_interval=min_interval)
        if self.webcam is not None:
            self.webcam.set_preview_size(width, height, min_interval=min_interval)

    def set_property(self, prop, value):
        # Lens controls belong to the iCam (focus.py is given `primary`).
        return self.primary.set_property(prop, value)

    def stop(self):
        with self._lock:
            cam, self.webcam = self.webcam, None
            test_cam, self._test_cam = self._test_cam, None
        if test_cam is not None:
            try:
                test_cam.stop()
            except Exception:
                log.exception("Focus-test webcam stop failed")
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                log.exception("Layer-1 webcam stop failed")
        self.primary.stop()


def primary_camera(camera):
    """The iCam behind a LayerCamera, or the camera itself."""
    return getattr(camera, "primary", camera)
