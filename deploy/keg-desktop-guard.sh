#!/bin/bash
# =============================================================================
#  Desktop guard: power-on must end in the app, never on the desktop
# =============================================================================
# Runs at the start of every normal (GNOME) desktop login, from
# ~/.config/autostart/keg-desktop-guard.desktop. The kiosk session does not run
# autostart entries, so this never runs there.
#
# The desktop is wanted only after Exit App (or "keg-session-switch desktop"),
# which leaves /run/keg-desktop-requested. If the desktop appeared without it
# (GDM fell back to the desktop at power-on), switch back to the kiosk.
# Only ONCE per boot (mark in /dev/shm, emptied at boot), so a broken kiosk
# session cannot cause an endless restart loop: then the desktop stays.
set -u

[[ -e /run/keg-desktop-requested ]] && exit 0

TRIED="/dev/shm/keg-desktop-guard-$(id -u)"
if [[ -e "$TRIED" ]]; then
    logger -t keg-desktop-guard "desktop at boot again after one kiosk retry: staying on the desktop"
    exit 0
fi
touch "$TRIED"

logger -t keg-desktop-guard "desktop opened at boot without Exit App: switching to the kiosk"
sleep 3     # let GNOME finish starting before GDM is restarted
sudo -n /usr/local/sbin/keg-session-switch kiosk --restart
