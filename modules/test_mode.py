# modules/test_mode.py
"""
Test mode: switched on and off in Settings -> Line setup.

While it is on, kegs that are already on a pallet are NOT checked: no
"already on a pallet" reminder, no MOVE / KEEP question and AUTO does not wait
for one. The same kegs can be put under the camera again and again while the
line is being tested. Pallets are still created and sent as usual.

Kept for the device in device_state.json (section "test_mode"), so it
survives a restart; the operator page shows a banner the whole time it is on,
so the line is never left in test mode by mistake.

Listeners (the operator page) are called on the GUI thread when it changes.
"""

import logging

import config

log = logging.getLogger(__name__)

_SECTION = "test_mode"
_listeners = []


def enabled():
    """True while test mode is on."""
    try:
        return bool(config._state(_SECTION, {}).get("enabled", False))
    except Exception:
        log.exception("test mode could not be read")
        return False


def set_enabled(on):
    """Turn test mode on or off, save it, and tell the listeners. Raises
    OSError if the setting could not be saved."""
    from datetime import datetime
    config._save_state(_SECTION, {"enabled": bool(on),
                                  "changed_at": datetime.now().isoformat(timespec="seconds")})
    for fn in list(_listeners):
        try:
            fn(bool(on))
        except Exception:
            log.exception("test mode listener failed")


def add_listener(fn):
    """fn(enabled) is called after every change."""
    if fn not in _listeners:
        _listeners.append(fn)
