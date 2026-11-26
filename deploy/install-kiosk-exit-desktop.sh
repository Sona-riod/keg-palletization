#!/bin/bash
# =============================================================================
#  Install "Exit App -> normal Ubuntu desktop" for the kiosk boot
# =============================================================================
#  Power on   -> kiosk session -> the app (as before)
#  Exit App   -> GDM restarts  -> normal Ubuntu desktop (files, terminal, ...)
#  On the desktop: "Palletization App" icon, or reboot -> back to the app
#
#  Run once on the device, as the kiosk user (NOT with sudo; it asks for sudo):
#      cd ~/Desktop/video_vision/palletization_final
#      sed -i 's/\r$//' deploy/*.sh deploy/keg-session-switch deploy/*.service deploy/*.desktop
#      bash deploy/install-kiosk-exit-desktop.sh
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as the kiosk user (e.g. icam-540), not as root." >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$APP_DIR/deploy"
USER_NAME="$(id -un)"
echo "App folder : $APP_DIR"
echo "Kiosk user : $USER_NAME"

# Windows line endings break bash scripts: strip them (dos2unix not needed).
sed -i 's/\r$//' "$DEPLOY/keg-session-switch" "$DEPLOY/keg-kiosk-session.sh" \
    "$DEPLOY/keg-kiosk-boot.service" "$DEPLOY/keg-start-app.desktop" \
    "$DEPLOY/keg-kiosk.desktop"
chmod +x "$DEPLOY/keg-kiosk-session.sh"

# 1. Root helper that picks the next session (kiosk / desktop).
sudo install -o root -g root -m 0755 "$DEPLOY/keg-session-switch" /usr/local/sbin/keg-session-switch

# The desktop after Exit App = the camera's ORIGINAL desktop session, taken
# from the AccountsService backup made before the kiosk was set up. Override
# with: DESKTOP=<name> bash deploy/install-kiosk-exit-desktop.sh
DESKTOP="${DESKTOP:-}"
BACKUP="$HOME/accountsservice-$USER_NAME.bak"
if [[ -z "$DESKTOP" && -r "$BACKUP" ]]; then
    DESKTOP="$(grep -m1 -E '^XSession=' "$BACKUP" | cut -d= -f2)"
    [[ -z "$DESKTOP" ]] && DESKTOP="$(grep -m1 -E '^Session=' "$BACKUP" | cut -d= -f2)"
fi
if [[ -z "$DESKTOP" || "$DESKTOP" == "keg-kiosk" ]]; then
    DESKTOP=ubuntu
fi
if [[ ! -f "/usr/share/xsessions/$DESKTOP.desktop" ]]; then
    echo "WARNING: /usr/share/xsessions/$DESKTOP.desktop not found. Available:" >&2
    ls /usr/share/xsessions >&2
fi
echo "Desktop    : $DESKTOP"
printf 'KIOSK_USER=%s\nKIOSK_SESSION=keg-kiosk\nDESKTOP_SESSION_NAME=%s\n' "$USER_NAME" "$DESKTOP" |
    sudo tee /etc/default/keg-kiosk >/dev/null

# 2. Let the kiosk user run ONLY that helper without a password.
TMP_SUDOERS="$(mktemp)"
echo "$USER_NAME ALL=(root) NOPASSWD: /usr/local/sbin/keg-session-switch" > "$TMP_SUDOERS"
if sudo visudo -cf "$TMP_SUDOERS" >/dev/null; then
    sudo install -o root -g root -m 0440 "$TMP_SUDOERS" /etc/sudoers.d/keg-kiosk
else
    echo "sudoers check failed, nothing installed" >&2
    rm -f "$TMP_SUDOERS"
    exit 1
fi
rm -f "$TMP_SUDOERS"

# 3. Every boot starts in the kiosk, whatever was chosen before.
sudo install -o root -g root -m 0644 "$DEPLOY/keg-kiosk-boot.service" /etc/systemd/system/keg-kiosk-boot.service
sudo systemctl daemon-reload
sudo systemctl enable keg-kiosk-boot.service

# 4. Kiosk session entry (Exec= points at this app folder). Started through
#    /bin/bash: if the script is not executable (copied again from Windows),
#    Ubuntu's Xsession silently starts a plain GNOME desktop instead.
sed "s|^Exec=.*|Exec=/bin/bash $DEPLOY/keg-kiosk-session.sh|" "$DEPLOY/keg-kiosk.desktop" |
    sudo tee /usr/share/xsessions/keg-kiosk.desktop >/dev/null

# 5. "Palletization App" icon on the desktop and in the app list.
#    Checkology app icon; Icon= is rewritten to this user's home.
mkdir -p "$HOME/.local/share/applications" "$HOME/Desktop" "$HOME/.local/share/icons"
ICON="$HOME/.local/share/icons/checkology-app-icon.png"
[[ -f "$DEPLOY/checkology-app-icon.png" ]] && install -m 0644 "$DEPLOY/checkology-app-icon.png" "$ICON"
for dest in "$HOME/.local/share/applications" "$HOME/Desktop"; do
    sed "s|^Icon=.*|Icon=$ICON|" "$DEPLOY/keg-start-app.desktop" > "$dest/keg-start-app.desktop"
    chmod 0755 "$dest/keg-start-app.desktop"
done
gio set "$HOME/Desktop/keg-start-app.desktop" metadata::trusted true 2>/dev/null || true

# 6. Make sure the desktop does not start the app on its own.
systemctl --user disable keg-palletization.service 2>/dev/null || true
rm -f "$HOME/.config/autostart/keg-palletization-autostart.desktop"

# 6b. Desktop guard: a desktop that opens at power-on WITHOUT Exit App goes
#     back to the kiosk (once per boot).
sed -i 's/\r$//' "$DEPLOY/keg-desktop-guard.sh" "$DEPLOY/keg-desktop-guard.desktop"
chmod +x "$DEPLOY/keg-desktop-guard.sh"
mkdir -p "$HOME/.config/autostart"
sed "s|^Exec=.*|Exec=/bin/bash $DEPLOY/keg-desktop-guard.sh|" "$DEPLOY/keg-desktop-guard.desktop" \
    > "$HOME/.config/autostart/keg-desktop-guard.desktop"

# 7. Check.
sudo -n /usr/local/sbin/keg-session-switch kiosk
echo
echo "Current auto-login session:"
sudo -n /usr/local/sbin/keg-session-switch status
echo
echo "Done. Reboot, then press Exit App to test: the Ubuntu desktop should open."
