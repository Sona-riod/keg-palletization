#!/usr/bin/env python3
"""Support tool: unlock codes for a forgotten maintenance PIN.

FOR SUPPORT ONLY. Runs on the support laptop, not on the devices, and needs
only Python 3 and modules/pin_unlock.py from the app (no other app code).

The master key is the secret everything rests on:
  * keep it in the company password manager, never in git, e-mail or chat;
  * this tool reads it from the environment variable PIN_SUPPORT_MASTER_KEY,
    or from a file given with --master-file (readable by you only).

Commands:
  new-master                      create a master key (once, ever)
  device-key  <MAC>               device key + check value, for installation
  unlock      <MAC> <REQUEST> --caller "Name, site"
                                  unlock code for the request number on screen

Before giving an unlock code, confirm who is calling (call back on the site's
known number, or check with the site manager). Every code given is appended to
the support log (--log, default ~/keg_pin_unlock_log.csv).
"""

import argparse
import csv
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve()
_spec = importlib.util.spec_from_file_location(
    "pin_unlock", _HERE.parent.parent.parent / "modules" / "pin_unlock.py")
pin_unlock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin_unlock)

ENV_VAR = "PIN_SUPPORT_MASTER_KEY"
DEFAULT_LOG = Path.home() / "keg_pin_unlock_log.csv"


def _master(args) -> bytes:
    text = ""
    if args.master_file:
        text = Path(args.master_file).read_text(encoding="utf-8").strip()
    else:
        text = os.environ.get(ENV_VAR, "").strip()
    if not text:
        sys.exit(f"No master key: set {ENV_VAR} or pass --master-file.")
    try:
        return pin_unlock.parse_key(text)
    except ValueError as e:
        sys.exit(f"Master key is not valid: {e}.")


def _log(path: Path, row: dict) -> None:
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


def main() -> int:
    p = argparse.ArgumentParser(description="Maintenance PIN unlock codes (support only).")
    p.add_argument("--master-file", help=f"file holding the master key (else ${ENV_VAR})")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new-master", help="create a new master key")
    dk = sub.add_parser("device-key", help="device key for installing a device")
    dk.add_argument("mac")
    un = sub.add_parser("unlock", help="unlock code for a request number")
    un.add_argument("mac")
    un.add_argument("request", help="request number shown on the device")
    un.add_argument("--caller", required=True, help="who asked (name, site, phone)")
    un.add_argument("--log", default=str(DEFAULT_LOG), help="support log CSV")
    args = p.parse_args()

    if args.cmd == "new-master":
        print(pin_unlock.new_master_key())
        print("\nStore this in the password manager now. Every device key and unlock "
              "code depends on it; a new master key needs every device re-installed.",
              file=sys.stderr)
        return 0

    master = _master(args)
    try:
        mac = pin_unlock.pretty_mac(args.mac)
    except ValueError as e:
        sys.exit(str(e))
    key = pin_unlock.device_key(master, mac)

    if args.cmd == "device-key":
        print(f"Device MAC:   {mac}")
        print(f"Device key:   {key.hex()}")
        print(f"Check value:  {pin_unlock.check_value(key)}")
        return 0

    try:
        code = pin_unlock.unlock_code(key, mac, args.request)
    except ValueError as e:
        sys.exit(str(e))
    _log(Path(args.log), {
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "device_mac": mac,
        "request": "".join(ch for ch in args.request if ch.isdigit()),
        "caller": args.caller,
        "given_by": os.environ.get("USERNAME") or os.environ.get("USER") or "",
    })
    print(f"Unlock code:  {pin_unlock.group(code)}")
    print(f"Valid once, on {mac} only, for the request number {args.request.strip()}, "
          f"within {pin_unlock.CHALLENGE_TTL // 60} minutes of it being shown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
