# modules/lighting.py
"""
Camera light for the operator: the iCam-540's built-in Flash LED, driven
through the same on-device REST daemon as the focus (CAMERA_API_BASE_URL).

Two settings, each a slider in Settings -> Camera Light
(ui_pyside/lighting_card.py):

    LIGHT MODE   POST /camera/led_status   form field led_status = 0..7
    INTENSITY    POST /camera/led_gain     form field led_gain   = 0..25

These are the calls the vendor's own page (localhost:5000/camera/settings ->
Lighting Settings) makes when its Mode and Intensity sliders move. The field
names were worked out from the request sizes in the browser inspector, so they
are in config.py (CAMERA_LED_*): a firmware that names them differently is a
config edit, not a code change.

/camera/led_status posted with no field is a read: the daemon answers with the
current mode in `result`. There is no read for the intensity.

The transport is camera_init.rest_json: multipart form data like the vendor
page, and only the daemon's JSON answer with code 200 counts as applied.

Failure policy
--------------
Every method returns a value and does not raise. The operator reads the
message, so it reports what the camera answered.
"""

import logging

import config

# Logging (same pattern as modules/focus.py)
logger = logging.getLogger("CameraLight")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(_h)

try:
    # Importing it also proves `requests` is installed (same as modules/focus.py).
    from modules.camera_init import rest_json
    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    rest_json = None
    _HAS_REQUESTS = False

MODE = "mode"
INTENSITY = "intensity"


def light_range(kind):
    """(min, max) of a setting, from config."""
    if kind == MODE:
        return int(config.CAMERA_LED_MODE_MIN), int(config.CAMERA_LED_MODE_MAX)
    return int(config.CAMERA_LED_INTENSITY_MIN), int(config.CAMERA_LED_INTENSITY_MAX)


class LightController:
    """Set and read the camera light. Safe to call from a worker thread."""

    @property
    def available(self):
        source = str(getattr(config, "CAMERA_SOURCE", "") or "").lower()
        return source == "icam" and _HAS_REQUESTS

    def describe(self):
        """One line for the HMI saying what is being driven and where."""
        if str(getattr(config, "CAMERA_SOURCE", "") or "").lower() != "icam":
            return "unavailable — only the iCam-540 has a controllable light"
        if not _HAS_REQUESTS:
            return ("unavailable — the 'requests' package is not installed on "
                    "this machine")
        return f"iCam-540 Flash LED via the camera service at {config.CAMERA_API_BASE_URL}"

    # Actions
    def apply(self, kind, value):
        """Send one setting (MODE or INTENSITY). Returns (ok, message)."""
        if not self.available:
            return False, f"No light control is available: {self.describe()}."
        lo, hi = light_range(kind)
        value = max(lo, min(hi, int(value)))
        if kind == MODE:
            label = "Light mode"
            path, field = config.CAMERA_LED_MODE_PATH, config.CAMERA_LED_MODE_FIELD
        else:
            label = "Light intensity"
            path, field = config.CAMERA_LED_INTENSITY_PATH, config.CAMERA_LED_INTENSITY_FIELD

        ok, msg, _ = self._post(path, {field: str(value)})
        if not ok:
            logger.error("%s %s was not applied: %s", label, value, msg)
            return False, f"{label} {value} was not applied: {msg}"
        logger.info("%s set to %s.", label, value)
        return True, f"{label} set to {value}."

    def turn_off(self):
        """Send CAMERA_LED_OFF_VALUES (used on exit). Returns (ok, message)."""
        results = [self.apply(kind, value)
                   for kind, value in config.CAMERA_LED_OFF_VALUES.items()
                   if kind in (MODE, INTENSITY)]
        if not results:
            return False, "CAMERA_LED_OFF_VALUES has no mode or intensity"
        failed = [msg for ok, msg in results if not ok]
        if failed:
            return False, "; ".join(failed)
        return True, "Camera light turned off."

    def read_mode(self):
        """The mode the light is in now, or None if the camera did not say."""
        if not self.available:
            return None
        ok, msg, body = self._post(config.CAMERA_LED_MODE_PATH, None)
        if not ok:
            logger.warning("Could not read the light mode: %s", msg)
            return None
        try:
            return int(body.get("result"))
        except (TypeError, ValueError):
            logger.warning("Light mode read returned %r, not a number.",
                           body.get("result"))
            return None

    # Helpers
    def _post(self, path, fields):
        """POST to the daemon. Returns (ok, message, json_body). No fields ->
        an empty POST (the mode read)."""
        return rest_json(config.CAMERA_API_BASE_URL, path, fields,
                         config.CAMERA_INIT_CONNECT_TIMEOUT, config.CAMERA_INIT_TIMEOUT)
