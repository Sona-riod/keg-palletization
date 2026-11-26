# modules/pin_unlock.py
"""
One-time unlock codes for a forgotten maintenance PIN (challenge-response).

Standard library only and no app imports, so the support tool
(tools/support/pin_unlock_code.py) can use this file on a laptop that has
nothing else of the app.

How it fits together:
  * Support keeps ONE master key (never on a device, never in git).
  * Each device gets its own device key = HMAC(master, device MAC), entered
    once at installation. Reading one device's key does not help with any
    other device, and the master key cannot be worked out from it.
  * When the PIN is forgotten, the device shows a fresh random request number
    (the challenge). Support checks who is calling, then computes
    unlock code = HMAC(device key, MAC + challenge), cut to UNLOCK_DIGITS digits.
  * The device accepts the code once, for that challenge only, within
    CHALLENGE_TTL seconds. Wrong codes count towards the PIN lockout.
"""

import hashlib
import hmac
import re
import secrets

VERSION = "v1"
CHALLENGE_DIGITS = 8
UNLOCK_DIGITS = 10
CHALLENGE_TTL = 15 * 60
KEY_BYTES = 32


def normalize_mac(mac: str) -> str:
    """"aa:bb:cc-dd.ee ff" -> "AABBCCDDEEFF". Raises ValueError if not a MAC."""
    hexdigits = re.sub(r"[^0-9A-Fa-f]", "", str(mac or "")).upper()
    if len(hexdigits) != 12:
        raise ValueError(f"Not a MAC address: {mac!r}")
    return hexdigits


def pretty_mac(mac: str) -> str:
    m = normalize_mac(mac)
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def new_master_key() -> str:
    return secrets.token_hex(KEY_BYTES)


def parse_key(text: str) -> bytes:
    """Hex key (spaces / dashes allowed) -> bytes. Raises ValueError."""
    key = bytes.fromhex(re.sub(r"[\s-]", "", str(text or "")))
    if len(key) != KEY_BYTES:
        raise ValueError(f"A key is {KEY_BYTES * 2} hex characters")
    return key


def device_key(master: bytes, mac: str) -> bytes:
    return hmac.new(master, f"keg-pin-device|{VERSION}|{normalize_mac(mac)}".encode(),
                    hashlib.sha256).digest()


def check_value(key: bytes) -> str:
    """6-character fingerprint of a device key, to confirm it was typed right.
    Reveals nothing useful about the key."""
    return hmac.new(key, b"keg-pin-check", hashlib.sha256).hexdigest()[:6].upper()


def new_challenge() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CHALLENGE_DIGITS))


def unlock_code(key: bytes, mac: str, challenge: str) -> str:
    challenge = re.sub(r"\D", "", str(challenge or ""))
    if len(challenge) != CHALLENGE_DIGITS:
        raise ValueError(f"The request number has {CHALLENGE_DIGITS} digits")
    msg = f"keg-pin-reset|{VERSION}|{normalize_mac(mac)}|{challenge}".encode()
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    return str(int.from_bytes(digest[:8], "big") % 10 ** UNLOCK_DIGITS).zfill(UNLOCK_DIGITS)


def group(digits: str, size: int = 4) -> str:
    """"1234567890" -> "1234 5678 90" for reading out over the phone."""
    return " ".join(digits[i:i + size] for i in range(0, len(digits), size))
