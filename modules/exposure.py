# modules/exposure.py
"""
Camera auto-exposure range for the operator, driven through the same on-device
REST daemon as the focus and the light (CAMERA_API_BASE_URL).

Settings -> Camera Exposure (ui_pyside/exposure_card.py) has two sliders, AE
Min and AE Max. The camera's auto exposure picks its exposure time inside this
range, so a lower AE Max gives sharper codes on a moving pallet (less motion
blur) at the cost of a darker image.

    SET    POST /camera/auto_exposure_range   min_exposure=<n>  max_exposure=<n>
    READ   GET  /camera/auto_exposure_range   -> "result": [min, max]

Both values go in one request, as the vendor page's Confirm button sends them.
Units are the camera's: 100 µs (50 = 5 ms).

Auto exposure itself is switched on by the start-up sequence
(camera_init.py step 6). The card also has an ON / OFF switch for it:

    POST /camera/auto_exposure_op      auto_exposure_op=1 (on) / 0 (off)

The range only matters while auto exposure is on.

Failure policy
--------------
Every method returns a value and does not raise. The operator reads the
message, so it reports what the camera answered.
"""

import logging

import config

# Logging (same pattern as modules/focus.py)
logger = logging.getLogger("CameraExposure")
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


def exposure_limits():
    """(lowest, highest) value the sliders allow, in 100 µs units."""
    return int(config.CAMERA_AE_LIMIT_MIN), int(config.CAMERA_AE_LIMIT_MAX)


def format_exposure(value):
    """100 µs units -> operator text, e.g. 50 -> '5.0 ms', 10000 -> '1000 ms'."""
    ms = int(value) / 10.0
    return f"{ms:.1f} ms" if ms < 100 else f"{ms:.0f} ms"


class ExposureController:
    """Set and read the auto-exposure range. Safe to call from a worker thread."""

    @property
    def available(self):
        source = str(getattr(config, "CAMERA_SOURCE", "") or "").lower()
        return source == "icam" and _HAS_REQUESTS

    def describe(self):
        """One line for the HMI saying what is being driven and where."""
        if str(getattr(config, "CAMERA_SOURCE", "") or "").lower() != "icam":
            return "unavailable — only the iCam-540 exposure is controlled here"
        if not _HAS_REQUESTS:
            return ("unavailable — the 'requests' package is not installed on "
                    "this machine")
        return f"iCam-540 auto exposure via the camera service at {config.CAMERA_API_BASE_URL}"

    # Actions
    def set_range(self, ae_min, ae_max):
        """Send AE Min and AE Max together. Returns (ok, message)."""
        if not self.available:
            return False, f"No exposure control is available: {self.describe()}."
        lo, hi = exposure_limits()
        ae_min = max(lo, min(hi, int(ae_min)))
        ae_max = max(lo, min(hi, int(ae_max)))
        if ae_min >= ae_max:
            return False, f"AE Min ({ae_min}) must be below AE Max ({ae_max})"

        text = (f"{ae_min}–{ae_max} "
                f"({format_exposure(ae_min)} – {format_exposure(ae_max)})")
        ok, msg, _ = rest_json(config.CAMERA_API_BASE_URL, config.CAMERA_AE_RANGE_PATH,
                               {config.CAMERA_AE_MIN_FIELD: ae_min,
                                config.CAMERA_AE_MAX_FIELD: ae_max},
                               config.CAMERA_INIT_CONNECT_TIMEOUT, config.CAMERA_INIT_TIMEOUT)
        if not ok:
            logger.error("Exposure range %s was not applied: %s", text, msg)
            return False, f"Exposure range {text} was not applied: {msg}"
        logger.info("Exposure range set to %s.", text)
        return True, f"Exposure range set to {text}."

    def set_auto(self, on):
        """Switch auto exposure on or off. Returns (ok, message)."""
        if not self.available:
            return False, f"No exposure control is available: {self.describe()}."
        word = "on" if on else "off"
        value = config.CAMERA_AE_ON_VALUE if on else config.CAMERA_AE_OFF_VALUE
        ok, msg, _ = rest_json(config.CAMERA_API_BASE_URL, config.CAMERA_AE_OP_PATH,
                               {config.CAMERA_AE_OP_FIELD: value},
                               config.CAMERA_INIT_CONNECT_TIMEOUT, config.CAMERA_INIT_TIMEOUT)
        if not ok:
            logger.error("Auto exposure %s was not applied: %s", word, msg)
            return False, f"Auto exposure {word} was not applied: {msg}"
        logger.info("Auto exposure switched %s.", word)
        return True, f"Auto exposure switched {word}."

    def read_range(self):
        """(ae_min, ae_max) the camera is using now, or None."""
        if not self.available:
            return None
        ok, msg, body = rest_json(config.CAMERA_API_BASE_URL, config.CAMERA_AE_RANGE_PATH,
                                  None, config.CAMERA_INIT_CONNECT_TIMEOUT,
                                  config.CAMERA_INIT_TIMEOUT, method="GET")
        if not ok:
            logger.warning("Could not read the exposure range: %s", msg)
            return None
        try:
            ae_min, ae_max = body.get("result")
            return int(ae_min), int(ae_max)
        except (TypeError, ValueError):
            logger.warning("Exposure range read returned %r, not [min, max].",
                           body.get("result"))
            return None
