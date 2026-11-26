# modules/maintenance_pin.py
"""
Maintenance authorization: who may take the actions an operator on the line
should not take alone (changing the device identity or the camera MAC, test
mode, clearing the print queue, leaving the kiosk with Exit App).

Three ways to approve, checked in ui_pyside/maintenance.py:
  1. Administrator (preferred). A named Checkology admin proves who they are
     to the cloud, with their own PIN or email + password, for this one
     action. Accountable: the audit log names the person.
  2. Maintenance PIN (break-glass). A shared digit PIN for when the cloud
     cannot check anyone. With config.MAINTENANCE_PIN_OFFLINE_ONLY it is only
     offered while the cloud is unreachable.
  3. Support unlock code (modules/pin_unlock.py). One-time challenge-response
     code read out by support after checking who is calling. Works once, for
     that request only, within CHALLENGE_TTL seconds.

The PIN: only a salted PBKDF2-SHA256 hash is stored, in
config.MAINTENANCE_PIN_FILE (mode 0600). Use 6 or more digits; the hash slows
guessing, the private file and the lockout do the rest.

Lockout. Wrong PINs and wrong unlock codes share one counter. After
MAX_ATTEMPTS wrong entries in a row, entry is refused for LOCK_STEPS[0]
seconds, then longer for each further lockout without a success in between.
The counter is kept on disk (config.MAINTENANCE_LOCK_FILE), so restarting the
app or switching the device off and on does not reset it. Maintenance can clear
it with tools/set_maintenance_pin.py --unlock.

Every approval, refusal, wrong entry, lockout and reset is written to the audit
log (AUDIT_FILE, one JSON line per event, never a PIN, password or code).
"""

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import config
from modules import pin_unlock
from modules.utils import write_private_json

log = logging.getLogger("maintenance_pin")

MIN_LENGTH = 6
MAX_LENGTH = 12
MAX_ATTEMPTS = 5
# Lockout length for the 1st, 2nd, 3rd ... lockout in a row (the last value
# repeats). A success resets the series.
LOCK_STEPS = (300, 900, 3600, 4 * 3600)
LOCK_SECONDS = LOCK_STEPS[0]          # kept for callers of the old name
# Checked on the UI thread, so kept to a fraction of a second on the Jetson.
# A digit PIN's real protection is the private file plus the lockout below.
_ITERATIONS = 50_000

AUDIT_FILE = getattr(config, "MAINTENANCE_AUDIT_FILE",
                     config.BASE_DIR / "maintenance_audit.log")
LOCK_FILE = getattr(config, "MAINTENANCE_LOCK_FILE",
                    config.BASE_DIR / "maintenance_lock.json")

# Fields kept when the PIN changes (the support key is set at installation).
_KEEP_ON_PIN_CHANGE = ("support_key", "support_mac")

_lock = threading.Lock()
_challenge = None            # (digits, monotonic time created) while one is open


def _read():
    try:
        with open(config.MAINTENANCE_PIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if (isinstance(data, dict) and data.get("algorithm") == "pbkdf2_sha256"
                and data.get("salt") and data.get("hash")):
            return data
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("Maintenance PIN file unreadable (%s); treating the PIN as set "
                    "so protected actions stay locked", e)
        return {"broken": True}
    return {"broken": True}


def _derive(pin: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)


def valid_format(pin) -> bool:
    return isinstance(pin, str) and pin.isdigit() and MIN_LENGTH <= len(pin) <= MAX_LENGTH


def is_set() -> bool:
    """True when a maintenance PIN (or a damaged PIN file) is present."""
    return _read() is not None


def required() -> bool:
    """True when protected actions need an approval at all: always on the
    production camera (config.MAINTENANCE_AUTH_REQUIRED), and in any case once
    a maintenance PIN has been set."""
    return bool(getattr(config, "MAINTENANCE_AUTH_REQUIRED", False)) or is_set()


def set_pin(pin: str) -> None:
    """Store a new PIN (hash only). Raises ValueError for a bad format.
    The support key set at installation is kept."""
    if not valid_format(pin):
        raise ValueError(f"The PIN must be {MIN_LENGTH} to {MAX_LENGTH} digits")
    old = _read() or {}
    salt = os.urandom(16)
    data = {
        "algorithm": "pbkdf2_sha256",
        "iterations": _ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(pin, salt, _ITERATIONS).hex(),
    }
    if not old.get("broken"):
        data.update({k: old[k] for k in _KEEP_ON_PIN_CHANGE if k in old})
    write_private_json(config.MAINTENANCE_PIN_FILE, data)


def clear_pin() -> bool:
    """Remove the PIN and the support key (protection off). True if one was set."""
    try:
        os.remove(config.MAINTENANCE_PIN_FILE)
        return True
    except FileNotFoundError:
        return False


# Lockout state (on disk, survives restarts)

def _lock_state() -> dict:
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"failures": int(data.get("failures", 0)),
                    "lockouts": int(data.get("lockouts", 0)),
                    "locked_until": float(data.get("locked_until", 0.0))}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        # A damaged file must not open the door: treat it as one lockout.
        log.warning("Maintenance lock file unreadable (%s); locking", e)
        return {"failures": 0, "lockouts": 1,
                "locked_until": time.time() + LOCK_STEPS[0]}
    return {"failures": 0, "lockouts": 0, "locked_until": 0.0}


def _save_lock_state(state: dict) -> None:
    try:
        if not state["failures"] and not state["lockouts"] and not state["locked_until"]:
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass
            return
        write_private_json(LOCK_FILE, state)
    except OSError as e:
        log.warning("Could not save the maintenance lock state (%s)", e)


def seconds_locked() -> int:
    with _lock:
        until = _lock_state()["locked_until"]
    left = until - time.time()
    if left <= 0:
        return 0
    # Clock set back: never lock for longer than the longest step.
    return int(min(left, max(LOCK_STEPS)) + 0.999)


def reset_lockout(by: str = "") -> None:
    """Clear the wrong-entry counter and any lockout (maintenance tool)."""
    with _lock:
        _save_lock_state({"failures": 0, "lockouts": 0, "locked_until": 0.0})
    audit("lockout_cleared", by=by)


def check(pin: str):
    """Check an entered PIN. Returns (ok, reason); reason is "", "locked",
    "wrong", "broken" or "not_set" (no PIN set: nothing can match)."""
    if seconds_locked():
        return False, "locked"
    data = _read()
    if data is None:
        return False, "not_set"
    if data.get("broken"):
        return False, "broken"
    try:
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
        iterations = int(data.get("iterations") or _ITERATIONS)
    except (KeyError, TypeError, ValueError):
        return False, "broken"
    ok = (isinstance(pin, str) and pin.isdigit() and len(pin) <= MAX_LENGTH
          and hmac.compare_digest(_derive(pin, salt, iterations), expected))
    _count(ok, "PIN")
    return (True, "") if ok else (False, "wrong")


def _count(ok: bool, what: str) -> None:
    """Shared wrong-entry counter and lockout for PINs and unlock codes."""
    with _lock:
        state = _lock_state()
        if ok:
            _save_lock_state({"failures": 0, "lockouts": 0, "locked_until": 0.0})
            return
        state["failures"] += 1
        failures = state["failures"]
        seconds = 0
        if failures >= MAX_ATTEMPTS:
            seconds = LOCK_STEPS[min(state["lockouts"], len(LOCK_STEPS) - 1)]
            state.update(failures=0, lockouts=state["lockouts"] + 1,
                         locked_until=time.time() + seconds)
        _save_lock_state(state)
    audit("wrong_entry", what=what, attempt=failures)
    if seconds:
        log.warning("Maintenance %s: %d wrong entries, locked for %d s",
                    what, MAX_ATTEMPTS, seconds)
        audit("locked", what=what, seconds=seconds)


# Support unlock code (challenge-response, see modules/pin_unlock.py)

def _support():
    """(device key bytes, MAC) when support unlock is set up, else None."""
    data = _read()
    if not data or data.get("broken"):
        return None
    try:
        return pin_unlock.parse_key(data["support_key"]), pin_unlock.normalize_mac(
            data["support_mac"])
    except (KeyError, TypeError, ValueError):
        return None


def support_ready() -> bool:
    return _support() is not None


def support_mac() -> str:
    """This device's ID as support sees it ("AA:BB:CC:DD:EE:FF"), or ""."""
    s = _support()
    return pin_unlock.pretty_mac(s[1]) if s else ""


def set_support_key(key_hex: str, mac: str) -> str:
    """Store the device key from support (installation step). Needs a PIN to
    be set. Returns the key's check value to compare with support's."""
    data = _read()
    if not data or data.get("broken"):
        raise ValueError("Set the maintenance PIN first")
    key = pin_unlock.parse_key(key_hex)
    data.update(support_key=key.hex(), support_mac=pin_unlock.normalize_mac(mac))
    write_private_json(config.MAINTENANCE_PIN_FILE, data)
    audit("support_key_set", mac=pin_unlock.pretty_mac(mac))
    return pin_unlock.check_value(key)


def open_challenge() -> str:
    """Start a reset request: a fresh request number, valid CHALLENGE_TTL
    seconds. Replaces any open one, so an older number stops working."""
    global _challenge
    digits = pin_unlock.new_challenge()
    with _lock:
        _challenge = (digits, time.monotonic())
    audit("unlock_requested", challenge=digits)
    return digits


def current_challenge() -> str:
    """The open request number (to show on screen), or "" when none is valid."""
    with _lock:
        if _challenge and time.monotonic() - _challenge[1] <= pin_unlock.CHALLENGE_TTL:
            return _challenge[0]
    return ""


def challenge_seconds_left() -> int:
    with _lock:
        if not _challenge:
            return 0
        return max(0, int(_challenge[1] + pin_unlock.CHALLENGE_TTL - time.monotonic()))


def close_challenge() -> None:
    global _challenge
    with _lock:
        _challenge = None


def check_unlock(code: str):
    """Check a support unlock code against the open request. Returns
    (ok, reason); reason is "", "locked", "wrong", "expired" or "not_ready".
    A right code closes the request, so it cannot be used twice."""
    global _challenge
    if seconds_locked():
        return False, "locked"
    support = _support()
    if not support:
        return False, "not_ready"
    with _lock:
        current = _challenge
    if not current or time.monotonic() - current[1] > pin_unlock.CHALLENGE_TTL:
        close_challenge()
        return False, "expired"
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    expected = pin_unlock.unlock_code(support[0], support[1], current[0])
    ok = len(digits) == pin_unlock.UNLOCK_DIGITS and hmac.compare_digest(digits, expected)
    _count(ok, "unlock code")
    if ok:
        with _lock:
            if _challenge == current:
                _challenge = None
        audit("unlock_accepted", challenge=current[0])
        return True, ""
    audit("unlock_refused", challenge=current[0])
    return False, "wrong"


# Audit log

def audit(event: str, **fields) -> None:
    """Append one event to the audit log (JSON line, file mode 0600). Never
    pass a PIN, password or unlock code here."""
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "event": event, "camera": getattr(config, "CAMERA_MAC_ID", "") or ""}
    entry.update(fields)
    try:
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Could not write the maintenance audit log (%s)", e)
    log.info("Maintenance audit: %s %s", event,
             {k: v for k, v in fields.items() if k != "challenge"})
