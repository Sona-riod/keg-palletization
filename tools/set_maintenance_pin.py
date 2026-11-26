#!/usr/bin/env python3
"""Set, change or remove the maintenance PIN (see modules/maintenance_pin.py).

Run on the device as the user the app runs as, from the app folder:

    python3 tools/set_maintenance_pin.py               # set or change the PIN
    python3 tools/set_maintenance_pin.py --support-key # install the support unlock key
    python3 tools/set_maintenance_pin.py --clear       # remove it (protection off)
    python3 tools/set_maintenance_pin.py --status
    python3 tools/set_maintenance_pin.py --unlock      # clear a wrong-entry lockout

The PIN is typed without echo and only its salted hash is saved. The app does
not need a restart: the PIN file is read each time a protected action is used.

Support unlock (for a forgotten PIN, once per device at installation):
  1. Support runs   tools/support/pin_unlock_code.py device-key <camera MAC>
     and gives the device key and its check value to the installer.
  2. The installer runs   set_maintenance_pin.py --support-key   here, types
     the key (no echo) and compares the check value printed with support's.
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from modules import maintenance_pin  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the maintenance PIN.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--clear", action="store_true",
                       help="remove the PIN and the support key")
    group.add_argument("--status", action="store_true", help="show whether a PIN is set")
    group.add_argument("--unlock", action="store_true",
                       help="clear the wrong-entry counter and any lockout")
    group.add_argument("--support-key", action="store_true",
                       help="install the device key from support (forgotten-PIN unlock)")
    parser.add_argument("--mac", help="camera MAC for --support-key "
                                      "(default: the MAC saved on this device)")
    args = parser.parse_args()

    if args.status:
        print("Maintenance PIN is " + ("SET" if maintenance_pin.is_set() else "NOT set"))
        if maintenance_pin.is_set():
            print("Support unlock is " + (f"SET for {maintenance_pin.support_mac()}"
                                          if maintenance_pin.support_ready() else "NOT set"))
        print("Approval required: " + ("YES" if maintenance_pin.required() else "no"))
        left = maintenance_pin.seconds_locked()
        print(f"Locked for another {left} s" if left else "Not locked")
        return 0
    if args.unlock:
        maintenance_pin.reset_lockout(by="tools/set_maintenance_pin.py")
        print("Lockout cleared.")
        return 0
    if args.clear:
        removed = maintenance_pin.clear_pin()
        print("Maintenance PIN removed." if removed else "No maintenance PIN was set.")
        if removed:
            maintenance_pin.audit("pin_cleared", by="tools/set_maintenance_pin.py")
        return 0
    if args.support_key:
        return _install_support_key(args.mac)

    pin = getpass.getpass(f"New PIN ({maintenance_pin.MIN_LENGTH}-"
                          f"{maintenance_pin.MAX_LENGTH} digits): ").strip()
    if not maintenance_pin.valid_format(pin):
        print(f"Not saved: the PIN must be {maintenance_pin.MIN_LENGTH} to "
              f"{maintenance_pin.MAX_LENGTH} digits.")
        return 1
    if getpass.getpass("Repeat the PIN: ").strip() != pin:
        print("Not saved: the two entries differ.")
        return 1
    maintenance_pin.set_pin(pin)
    maintenance_pin.audit("pin_set", by="tools/set_maintenance_pin.py")
    print("Maintenance PIN saved.")
    if not maintenance_pin.support_ready():
        print("Support unlock is not set up yet: run with --support-key so a "
              "forgotten PIN can be reset.")
    return 0


def _install_support_key(mac_arg) -> int:
    if not maintenance_pin.is_set():
        print("Set the maintenance PIN first.")
        return 1
    mac = mac_arg or config.CAMERA_MAC_ID
    if not mac:
        print("No camera MAC is saved on this device. Pass it with --mac.")
        return 1
    print(f"Camera MAC: {mac}  (support must create the key for this MAC)")
    key = getpass.getpass("Device key from support: ").strip()
    try:
        check = maintenance_pin.set_support_key(key, mac)
    except ValueError as e:
        print(f"Not saved: {e}.")
        return 1
    print(f"Support unlock saved. Check value: {check}")
    print("It must match the check value support gave you. If not, run this again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
