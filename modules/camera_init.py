# modules/camera_init.py
"""
CameraInitializer: configures the Advantech camera through its local REST API
(http://localhost:5000) before the app opens it with cv2.VideoCapture.

Python port of camera_configure.sh. The sequence runs on a background daemon
thread so the UI (spinner / progress bar) stays responsive during the
~22 s of hardware settle waits.

Sequence (same as camera_configure.sh):
    1. Close camera        POST /camera/close              (settle 5 s)
    2. Disable timestamp   POST /camera/timestamp_switch   (mode=0)
    3. Set 4K resolution   POST /camera/resolution         (width, height)
    4. Open camera         POST /camera/open               (settle 5 s)
    5. Play stream         POST /camera/play   -> /dev/video10  (settle 5 s)
    6. Auto-exposure       POST /camera/auto_exposure_op   (auto_exposure_op=1)
    7. Reset focus         POST /camera/reset_focus_position (settle 2 s)
    8. Start auto-focus    POST /camera/autofocus_start
       or, when QR / tap-to-focus saved a lens position (config.camera_focus_position):
       Move lens           POST /camera/focus_abs_position  (abs_position=saved)

Any non-2xx response or network error aborts the sequence (same as the bash
`post()` helper) and reports the failing step back to the UI.
"""

import time
import threading
import logging

import requests

from config import (
    CAMERA_API_BASE_URL,
    CAMERA_INIT_WIDTH,
    CAMERA_INIT_HEIGHT,
    CAMERA_INIT_CONNECT_TIMEOUT,
    CAMERA_INIT_TIMEOUT,
    CAMERA_INIT_AUTOFOCUS_TIMEOUT,
    CAMERA_INIT_SETTLE_AFTER_CLOSE,
    CAMERA_INIT_SETTLE_AFTER_OPEN,
    CAMERA_INIT_SETTLE_AFTER_PLAY,
    CAMERA_INIT_SETTLE_AFTER_RESET,
    CAMERA_INIT_MAX_ATTEMPTS,
    CAMERA_INIT_RETRY_BACKOFF,
    CAMERA_RESET_FOCUS_PATH,
    CAMERA_AUTOFOCUS_PATH,
)

# Logging (same pattern as modules/api_sender.py)
logger = logging.getLogger("CameraInit")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(_h)


def rest_post(base_url, endpoint, data, connect_timeout, read_timeout):
    """POST one step to the camera daemon. Returns (ok: bool, message: str).

    Separate connect and read timeouts: a stopped daemon fails fast (connection
    refused is instant), while a slow but working endpoint still gets time. The
    messages are meant for an operator on the line, not a developer.

    This is a module-level function because the focus control (modules/focus.py)
    talks to the same daemon and should fail the same way as the boot sequence,
    with the same error strings.
    """
    url = f"{base_url.rstrip('/')}{endpoint}"
    logger.debug("POST %s | data=%s | read_timeout=%ss", url, data, read_timeout)
    try:
        # camera_configure.sh uses `curl -F` (multipart). The Advantech API also
        # accepts `data=` (urlencoded); switch to files={k:(None,v)} if a future
        # firmware requires multipart.
        resp = requests.post(url, data=data,
                             timeout=(connect_timeout, read_timeout))
    except (requests.ConnectionError, requests.ConnectTimeout) as e:
        logger.error("Cannot reach camera service at %s: %s", url, e)
        return False, "cannot reach camera service (is the camera daemon running?)"
    except requests.ReadTimeout as e:
        logger.error("No response from %s within %ss: %s", url, read_timeout, e)
        return False, f"no response within {read_timeout}s — camera may be busy"
    except requests.RequestException as e:
        logger.error("Request failed for %s: %s", url, e)
        return False, f"request failed ({e.__class__.__name__})"

    body = (resp.text or "").strip()
    logger.debug("Response | status=%s | body=%s", resp.status_code, body or "<empty>")
    if 200 <= resp.status_code < 300:
        return True, ""
    if resp.status_code == 404:
        # On this daemon a 404 means the firmware lacks that endpoint. That is
        # a config line to correct, not a fault, so say so.
        return False, f"HTTP 404 — the camera service has no {endpoint}"
    return False, f"HTTP {resp.status_code}"


def rest_json(base_url, endpoint, fields, connect_timeout, read_timeout,
              method="POST"):
    """Call the camera daemon and return (ok: bool, message: str, body: dict).

    For the Settings controls that need the answer (modules/lighting.py,
    modules/exposure.py). Two differences from rest_post:
      - fields go as multipart form data, as the vendor settings page sends
        them (None = no body);
      - only the daemon's JSON envelope {"code", "status", "result",
        "description"} with code 200 counts as success. The daemon answers an
        unknown field or route with its web page (HTML, HTTP 200), which must
        not pass as success.
    """
    url = f"{base_url.rstrip('/')}{endpoint}"
    files = {k: (None, str(v)) for k, v in (fields or {}).items()} or None
    logger.debug("%s %s | fields=%s", method, url, fields)
    try:
        resp = requests.request(method, url, files=files,
                                timeout=(connect_timeout, read_timeout))
    except (requests.ConnectionError, requests.ConnectTimeout) as e:
        logger.error("Cannot reach camera service at %s: %s", url, e)
        return False, "cannot reach camera service (is the camera daemon running?)", {}
    except requests.ReadTimeout:
        return False, f"no response within {read_timeout}s — camera may be busy", {}
    except requests.RequestException as e:
        return False, f"request failed ({e.__class__.__name__})", {}

    try:
        body = resp.json()
    except ValueError:
        body = None
    logger.debug("Response | status=%s | body=%s", resp.status_code,
                 body if body is not None else (resp.text or "")[:120])

    if resp.status_code == 404:
        return False, f"HTTP 404 — the camera service has no {endpoint}", {}
    if not 200 <= resp.status_code < 300:
        return False, f"HTTP {resp.status_code}", body if isinstance(body, dict) else {}
    if not isinstance(body, dict) or "code" not in body:
        return False, ("the camera service did not accept the request (it answered "
                       "with a web page — wrong endpoint or field name?)"), {}
    if str(body.get("code")) != "200":
        return False, f"camera answered code {body.get('code')}: {body.get('description', '')}", body
    return True, "", body


def _build_steps():
    """Step list, built per call so config values are read at call time."""
    return [
        {"label": "Closing camera",
         "endpoint": "/camera/close", "data": None,
         "settle": CAMERA_INIT_SETTLE_AFTER_CLOSE},

        {"label": "Disabling timestamp overlay",
         "endpoint": "/camera/timestamp_switch", "data": {"mode": "0"},
         "settle": 0},

        {"label": "Setting 4K resolution",
         "endpoint": "/camera/resolution",
         "data": {"width": str(CAMERA_INIT_WIDTH), "height": str(CAMERA_INIT_HEIGHT)},
         "settle": 0},

        {"label": "Opening camera",
         "endpoint": "/camera/open", "data": None,
         "settle": CAMERA_INIT_SETTLE_AFTER_OPEN},

        {"label": "Starting video stream",
         "endpoint": "/camera/play", "data": None,
         "settle": CAMERA_INIT_SETTLE_AFTER_PLAY},

        {"label": "Enabling auto-exposure",
         "endpoint": "/camera/auto_exposure_op", "data": {"auto_exposure_op": "1"},
         "settle": 0},

        {"label": "Resetting focus",
         "endpoint": CAMERA_RESET_FOCUS_PATH, "data": None,
         "settle": CAMERA_INIT_SETTLE_AFTER_RESET},

        _focus_step(),
    ]


def _focus_step():
    """Last step: the lens position QR / tap-to-focus saved, else auto-focus.

    The camera's auto-focus can lock on an object at the side of the view
    instead of the QR codes; a position saved by focusing on the QR codes
    is the one that made the codes sharp (modules/focus.py)."""
    import config
    saved = (config.camera_focus_position()
             if getattr(config, "CAMERA_FOCUS_USE_SAVED_AT_START", False) else None)
    if saved is not None:
        logger.info("Start-up focus: saved QR focus position %d", saved)
        # Multipart and a JSON answer, like the Settings controls (rest_json).
        return {"label": "Moving lens to the saved QR focus",
                "endpoint": config.CAMERA_FOCUS_POSITION_PATH,
                "fields": {config.CAMERA_FOCUS_POSITION_FIELD: saved},
                "settle": 1}
    return {"label": "Starting auto-focus",
            "endpoint": CAMERA_AUTOFOCUS_PATH, "data": None,
            "settle": 0,
            # autofocus_start holds the response open until the lens locks
            # focus, so it needs a much longer read timeout than the other steps.
            "timeout": CAMERA_INIT_AUTOFOCUS_TIMEOUT}


_warmed_up = False      # the warm-up runs once per app start, not on every RETRY


def daemon_up(base_url, timeout=2):
    """True when the camera's REST service answers below HTTP 400 (curl -f)."""
    try:
        resp = requests.get(base_url, timeout=timeout)
    except requests.RequestException:
        return False
    resp.close()
    return resp.status_code < 400


def wait_for_daemon(base_url, status_cb, sleep=time.sleep, clock=time.monotonic,
                    probe=daemon_up):
    """Power-on wait, moved here from start_prod.sh so it runs behind the splash.

    Waits up to CAMERA_BOOT_WAIT_S for the REST service, then warms up for
    CAMERA_BOOT_WARMUP_S. Returns True if the service answered. If it never
    does, init goes ahead anyway and its own retries take over (as before).
    """
    global _warmed_up
    import config
    wait_s = float(getattr(config, "CAMERA_BOOT_WAIT_S", 30))
    warmup_s = float(getattr(config, "CAMERA_BOOT_WARMUP_S", 10))
    deadline = clock() + wait_s
    announced = False
    while not probe(base_url):
        if clock() >= deadline:
            logger.warning("Camera REST service not up after %.0fs, continuing anyway", wait_s)
            return False
        if not announced:
            logger.info("Waiting for the camera REST service (up to %.0fs)", wait_s)
            status_cb("Waiting for the camera to start…")
            announced = True
        sleep(1)
    if warmup_s > 0 and not _warmed_up:
        logger.info("Camera REST service reachable, warming up %.0fs", warmup_s)
        status_cb("Camera warming up…")
        sleep(warmup_s)
    _warmed_up = True
    return True


class CameraInitializer:
    """
    Runs the camera REST configuration sequence on a background thread.

    Callbacks are all invoked from the worker thread. The UI layer must move
    them onto the GUI thread (CameraInitBridge in ui_pyside/workers.py does
    this with Qt signals).
        status_cb(text: str)              -> human-readable current action
        step_cb(index: int, total: int)   -> 1-based progress
        done_cb(success: bool, error: str)-> final result (error="" on success)
    """

    def __init__(self, base_url=None, status_cb=None, step_cb=None, done_cb=None):
        self.base_url = (base_url or CAMERA_API_BASE_URL).rstrip("/")
        self.status_cb = status_cb or (lambda *_: None)
        self.step_cb = step_cb or (lambda *_: None)
        self.done_cb = done_cb or (lambda *_: None)
        self._thread = None

    def run_async(self):
        """Start the sequence on a daemon thread and return immediately."""
        self._thread = threading.Thread(
            target=self._run, name="CameraInit", daemon=True)
        self._thread.start()

    def _post(self, endpoint, data, read_timeout):
        """POST one step of the boot sequence. Returns (ok: bool, message: str)."""
        return rest_post(self.base_url, endpoint, data,
                         CAMERA_INIT_CONNECT_TIMEOUT, read_timeout)

    def _run_once(self):
        """Run the full REST sequence once. Returns (ok: bool, error: str)."""
        steps = _build_steps()
        total = len(steps)

        for idx, step in enumerate(steps, start=1):
            label = step["label"]
            self.step_cb(idx, total)
            self.status_cb(label + "…")
            logger.info("Step %d/%d - %s", idx, total, label)

            read_timeout = step.get("timeout", CAMERA_INIT_TIMEOUT)
            if "fields" in step:
                ok, msg, _ = rest_json(self.base_url, step["endpoint"], step["fields"],
                                       CAMERA_INIT_CONNECT_TIMEOUT, read_timeout)
            else:
                ok, msg = self._post(step["endpoint"], step["data"], read_timeout)
            if not ok:
                error = f"{label} failed ({msg})"
                logger.error("Camera init step failed: %s", error)
                return False, error

            settle = step.get("settle", 0)
            if settle:
                logger.debug("Settling %ds after '%s'", settle, label)
                time.sleep(settle)

        return True, ""

    def _run(self):
        wait_for_daemon(self.base_url, self.status_cb)
        self._run_attempts()

    def _run_attempts(self):
        """Run the sequence, retrying the whole thing on failure.

        On a cold boot the camera's REST socket answers before the sensor/lens
        pipeline is ready, so the first attempt (usually 'open', 'play' or
        'autofocus_start') can fail. A few retries with a short backoff give the
        hardware time to warm up. Without this, autofocus may never run after a
        power-on reboot.
        """
        logger.info("Starting camera initialization at %s (max %d attempts)",
                    self.base_url, CAMERA_INIT_MAX_ATTEMPTS)

        last_error = ""
        for attempt in range(1, CAMERA_INIT_MAX_ATTEMPTS + 1):
            if attempt > 1:
                self.status_cb(f"Camera not ready — retry {attempt}/"
                               f"{CAMERA_INIT_MAX_ATTEMPTS}…")
                logger.warning("Camera init attempt %d/%d after failure: %s",
                               attempt, CAMERA_INIT_MAX_ATTEMPTS, last_error)
                time.sleep(CAMERA_INIT_RETRY_BACKOFF)

            ok, err = self._run_once()
            if ok:
                logger.info("Camera initialization complete on attempt %d.", attempt)
                self.done_cb(True, "")
                return
            last_error = err

        logger.error("Camera init failed after %d attempts: %s",
                     CAMERA_INIT_MAX_ATTEMPTS, last_error)
        self.done_cb(False, last_error)
