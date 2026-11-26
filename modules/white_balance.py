# modules/white_balance.py
"""
Camera white balance, driven through the same on-device REST daemon as the
focus, the light and the exposure (CAMERA_API_BASE_URL).

Why fix it: the camera's auto white balance makes the average of the picture
grey. A large coloured area pulls the whole view the opposite way: a blue test
pallet filling the frame turned everything yellow (2026-11-04). Fixed, the
colours stay put whatever is under the camera.

How: the camera's auto_white_balance_op switch (1 = auto, 0 = manual), sent
like auto_exposure_op (names from the camera's own settings page; the older
/camera/awb_op answered HTTP 405):

    POST /camera/auto_white_balance_op   auto_white_balance_op=1   auto
    POST /camera/auto_white_balance_op   auto_white_balance_op=0   manual: the colour gains stay where they are

fix() lets auto white balance settle on what the camera sees for
CAMERA_AWB_SETTLE_S, then switches to manual. Point the camera at a neutral
scene first (the empty floor, a grey or white sheet), not at a coloured pallet.

Every method returns (ok, message) and does not raise, as in modules/exposure.py.
"""

import logging
import time

import config

logger = logging.getLogger("CameraWhiteBalance")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    logger.addHandler(_h)

try:
    from modules.camera_init import rest_json
    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    rest_json = None
    _HAS_REQUESTS = False


class WhiteBalanceController:
    """Switch the camera's auto white balance. Safe to call from a worker thread."""

    @property
    def available(self):
        source = str(getattr(config, "CAMERA_SOURCE", "") or "").lower()
        return source == "icam" and _HAS_REQUESTS

    def set_auto(self, on):
        """Auto white balance on (True) or manual (False). Returns (ok, message)."""
        if not self.available:
            return False, "No white balance control: only the iCam-540 is controlled here."
        value = config.CAMERA_AWB_AUTO_VALUE if on else config.CAMERA_AWB_MANUAL_VALUE
        word = "auto" if on else "fixed (manual)"
        ok, msg, _ = rest_json(config.CAMERA_API_BASE_URL, config.CAMERA_AWB_OP_PATH,
                               {config.CAMERA_AWB_OP_FIELD: value},
                               config.CAMERA_INIT_CONNECT_TIMEOUT, config.CAMERA_INIT_TIMEOUT)
        if not ok:
            logger.error("White balance %s was not applied (%s %s=%s): %s", word,
                         config.CAMERA_AWB_OP_PATH, config.CAMERA_AWB_OP_FIELD, value, msg)
            return False, (f"White balance {word} was not applied: {msg}. If the camera "
                           f"answered with a web page, set CAMERA_AWB_OP_PATH / "
                           f"CAMERA_AWB_OP_FIELD in config.py to this firmware's names.")
        logger.info("White balance set to %s.", word)
        return True, f"White balance set to {word}."

    def hold(self):
        """Keep the colours as they are now (manual), without learning them
        from the scene first: what start-up does, since the scene then is
        often a coloured pallet (2026-11-04: locked onto a blue crate, the
        picture stayed yellow). Returns (ok, message)."""
        ok, msg = self.set_auto(False)
        return ok, (msg if not ok else "White balance held (manual); press SET NOW on a "
                                       "neutral surface to set the colours again.")

    def fix(self, settle_s=None):
        """Let auto white balance settle on the scene, then hold it there.
        Returns (ok, message)."""
        ok, msg = self.set_auto(True)
        if not ok:
            return ok, msg
        settle = float(config.CAMERA_AWB_SETTLE_S if settle_s is None else settle_s)
        time.sleep(max(0.0, settle))
        ok, msg = self.set_auto(False)
        if not ok:
            return ok, msg
        logger.info("White balance fixed after %.1f s of auto white balance.", settle)
        return True, f"White balance fixed (auto ran {settle:.0f} s on the scene, then held)."
