#!/bin/bash
# =============================================================================
#  Kiosk X session: the screen goes straight from boot to the app.
# =============================================================================
# Started by the display manager (auto-login) INSTEAD of the Ubuntu desktop,
# so no wallpaper, panels or icons are ever drawn. See deploy/DEPLOY.md,
# "Fast kiosk boot".
#
#   * the screen is painted the splash colour (and the logo, if hsetroot is
#     installed) the moment the session starts, while Python loads
#   * a small window manager keeps dialogs and keyboard focus working
#   * the app runs as the usual systemd user service (crash -> restart in 3 s)
#   * a deliberate "Exit App" stops the service; the normal Ubuntu desktop
#     then opens (files, icons, terminal) for maintenance. A reboot returns
#     to the kiosk.
set -u

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="${KEG_KIOSK_SERVICE:-keg-palletization.service}"
LOGO="$APP_DIR/ui_pyside/assets/checkology_logo.png"
BG="#FFFFFF"            # the splash background (ui_pyside/theme.py grad_bg_start)

# 1. Paint the screen at once: no black gap, no desktop.
if command -v hsetroot >/dev/null 2>&1 && [[ -f "$LOGO" ]]; then
    hsetroot -solid "$BG" -center "$LOGO" 2>/dev/null || xsetroot -solid "$BG"
elif command -v xsetroot >/dev/null 2>&1; then
    xsetroot -solid "$BG"
fi

# 2. Never blank the screen.
if command -v xset >/dev/null 2>&1; then
    xset s off -dpms s noblank 2>/dev/null || true
fi

# 3. A minimal window manager: dialogs stay on top and get the keyboard.
WM_PID=""
if command -v matchbox-window-manager >/dev/null 2>&1; then
    matchbox-window-manager -use_titlebar no -use_cursor yes &
    WM_PID=$!
elif command -v openbox >/dev/null 2>&1; then
    openbox &
    WM_PID=$!
fi

# 4. Start the app (the service needs this session's display).
#    The app writes EXIT_MARKER only when the operator presses Exit App
#    (modules/system_power.py stop_kiosk_service).
EXIT_MARKER="${XDG_RUNTIME_DIR:-/tmp}/keg-exit-requested-$(id -u)"
rm -f "$EXIT_MARKER"
systemctl --user import-environment DISPLAY XAUTHORITY
systemctl --user reset-failed "$SERVICE" 2>/dev/null
systemctl --user start "$SERVICE"

# 5. Stay while the app runs. If the service stops WITHOUT Exit App (failed
#    start, crash loop, camera not ready...), start it again instead of
#    leaving the kiosk. Only a deliberate Exit App goes on to step 6.
sleep 5
while true; do
    state="$(systemctl --user is-active "$SERVICE" 2>/dev/null)"
    case "$state" in
        active|activating|reloading) sleep 2; continue ;;
    esac
    if [[ -f "$EXIT_MARKER" ]]; then
        rm -f "$EXIT_MARKER"
        break
    fi
    logger -t keg-kiosk "$SERVICE is '$state' without Exit App: restarting it"
    sleep 5
    systemctl --user reset-failed "$SERVICE" 2>/dev/null
    systemctl --user start "$SERVICE"
    sleep 5
done

# 6. Exit App: open the normal Ubuntu desktop (files, icons, terminal).
#    KEG_EXIT_TO=logout ends the session instead.
if [[ "${KEG_EXIT_TO:-desktop}" != "desktop" ]]; then
    exit 0
fi
logger -t keg-kiosk "Exit App: switching to the Ubuntu desktop"

# 6a. Preferred: GDM restarts and auto-logs in to a real "ubuntu" session.
#     keg-kiosk-boot.service puts the kiosk back at the next boot.
#     Installed by deploy/install-kiosk-exit-desktop.sh.
SWITCH=/usr/local/sbin/keg-session-switch
if [[ -x "$SWITCH" ]] && sudo -n "$SWITCH" desktop --restart; then
    # GDM is restarting and will end this session. Keep the screen painted
    # until it does.
    sleep 60
    exit 0
fi
logger -t keg-kiosk "keg-session-switch not available: opening a terminal"

# 6b. Fallback (installer not run): never leave a black screen. Open a
#     terminal and a file manager; closing the terminal ends the session.
#     GNOME is NOT started inside this session any more: it came up as a
#     stripped-down desktop that did not look like the normal one.
if [[ -n "$WM_PID" ]]; then
    kill "$WM_PID" 2>/dev/null
    wait "$WM_PID" 2>/dev/null
fi
command -v matchbox-window-manager >/dev/null 2>&1 &&
    matchbox-window-manager -use_titlebar yes -use_cursor yes &
(command -v nautilus >/dev/null 2>&1 && nautilus --new-window "$APP_DIR" &)
for term in gnome-terminal xterm x-terminal-emulator; do
    if command -v "$term" >/dev/null 2>&1; then
        if [[ "$term" == "gnome-terminal" ]]; then
            gnome-terminal --wait --working-directory="$APP_DIR"
        else
            "$term"
        fi
        break
    fi
done
exit 0
