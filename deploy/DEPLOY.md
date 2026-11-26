# Production Deployment — Auto-start on Power-On (Advantech iCam-540)

Goal: **power on the camera → it boots → the Keg Palletization app launches
full-screen automatically**, with no double-click, no keyboard, and automatic
recovery if the app ever crashes.

Because the app is a **GUI (PySide6/Qt)** program, it needs a running graphical
session. So there are **two layers** and both must be configured:

| Layer | What it does | Mechanism |
|-------|--------------|-----------|
| **1. Auto-login** | Boots straight into a desktop session, no password | Display-manager config |
| **2. Auto-launch** | Starts our app in that session, restarts on crash, kiosk full-screen | systemd **user** service |

All commands below assume the deployment used on the device:

- Login user: `icam-540`
- App folder: `/home/icam-540/Desktop/video_vision/palletization_final`
- A working virtualenv at `.../palletization_final/venv`

> If your user or path differs, adjust the paths in `keg-palletization.service`
> and in the commands below.

---

## Step 0 — Copy files & fix line endings

The repo ships these production files:

```
start_prod.sh                         # unattended kiosk launcher (the service runs it)
deploy/keg-palletization.service      # systemd user unit (crash -> restart)
deploy/keg-kiosk.desktop              # "keg-kiosk" login session
deploy/keg-kiosk-session.sh           # what that session runs (app only)
deploy/keg-session-switch             # Exit App -> desktop, boot -> kiosk
deploy/keg-kiosk-boot.service         # every boot -> kiosk
deploy/keg-start-app.desktop          # desktop icon "Palletization App"
deploy/checkology-app-icon.png        # its icon
deploy/install-kiosk-exit-desktop.sh  # installs the kiosk / Exit App parts
deploy/DEPLOY.md                      # this guide
```

On the device, from the app folder:

```bash
cd /home/icam-540/Desktop/video_vision/palletization_final

# If the files were edited on Windows, normalise line endings (CRLF -> LF):
sed -i 's/\r$//' start_prod.sh deploy/*.sh deploy/keg-session-switch deploy/*.service deploy/*.desktop

chmod +x start_prod.sh deploy/keg-kiosk-session.sh
```

---

## Step 1 — Enable auto-login

Find which display manager is in use:

```bash
cat /etc/X11/default-display-manager
```

### If it prints `/usr/sbin/gdm3` (GNOME — common on recent L4T)

Edit `/etc/gdm3/custom.conf`:

```bash
sudo nano /etc/gdm3/custom.conf
```

Under `[daemon]` set:

```ini
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=icam-540
```

### If it prints `/usr/sbin/lightdm`

Edit `/etc/lightdm/lightdm.conf` (create the `[Seat:*]` section if missing):

```ini
[Seat:*]
autologin-user=icam-540
autologin-user-timeout=0
```

> **X11 vs Wayland:** the systemd unit assumes X11 (`DISPLAY=:0`), which is the
> default on Jetson/L4T. On GDM3, keep Wayland off for a predictable kiosk —
> in `/etc/gdm3/custom.conf` ensure `WaylandEnable=false` is uncommented.

Reboot and confirm the machine lands on the desktop with no password prompt
before continuing.

---

## Step 2 — Install the systemd **user** service

We use a *user* service (not a system service) because the app must run inside
the logged-in graphical session that owns the display.

```bash
# 1. Place the unit in the user systemd directory
mkdir -p ~/.config/systemd/user
cp deploy/keg-palletization.service ~/.config/systemd/user/

# 2. Reload and enable it (starts on every graphical login)
systemctl --user daemon-reload
systemctl --user enable keg-palletization.service

# 3. Allow user services to run for this user even before an interactive login
#    (survives across the auto-login boot). Run ONCE, needs sudo:
sudo loginctl enable-linger icam-540

# 4. Start it now to test without rebooting
systemctl --user start keg-palletization.service
systemctl --user status keg-palletization.service
```

You should see the app go full-screen. `status` should read `active (running)`.

---

## Step 3 — (removed)

The old desktop-autostart fallback is no longer used: the kiosk session
(Step 4b) starts the app, and the desktop must NOT start it (it is the
maintenance screen after Exit App).

---

## Step 4 — Kiosk hardening

The launcher already disables screen blanking (`xset`) each start. Additionally:

```bash
# GNOME: never blank / suspend the screen
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing'
gsettings set org.gnome.desktop.screensaver lock-enabled false

# GNOME: suppress the "software updates available" / notification popups
gsettings set org.gnome.desktop.notifications show-banners false
```

The app itself runs **full-screen** in production (the launcher sets
`KEG_KIOSK=1`), so the operator cannot fall out to the OS. If the app crashes,
systemd relaunches it within ~3 seconds. A deliberate **Exit** from the app
stops the service, so it stays closed until started again; **Shut Down** powers
off the device.

---

## Step 4b — Fast kiosk boot (no desktop, no delay) — recommended

With Steps 1–3 the device boots the whole Ubuntu desktop first and only then
starts the app, so the operator sees the desktop for a while. This step logs
in to a session that contains **only the app**:

* the screen turns white (with the logo, if `hsetroot` is installed) as soon
  as the session starts, and the app's splash follows a few seconds later
* the camera wait and warm-up now run behind the splash, not before the window
  opens (`CAMERA_BOOT_WAIT_S` / `CAMERA_BOOT_WARMUP_S` in `config.py`)
* crash → the service restarts the app in 3 s, as before
* **Exit App** opens the **normal Ubuntu desktop** (files, icons, terminal)
  for maintenance, as before the kiosk; a reboot (or the desktop icon
  **Palletization App**) returns to the kiosk. **Shut Down** powers off.
  This needs the installer below, step 6 (`install-kiosk-exit-desktop.sh`).
  Without it, Exit App tries `gnome-session` inside the kiosk session, which
  showed a black screen on the iCam-540.

6. **Exit App → desktop** (run once, as the kiosk user, not with sudo):

   ```bash
   cd ~/Desktop/video_vision/palletization_final
   sed -i 's/\r$//' deploy/*.sh deploy/keg-session-switch deploy/*.service deploy/*.desktop
   bash deploy/install-kiosk-exit-desktop.sh
   sudo reboot
   ```

   It installs `/usr/local/sbin/keg-session-switch` (selects the next
   auto-login session and restarts GDM), a sudoers rule for that one command
   only, and `keg-kiosk-boot.service`, which selects the kiosk at every boot.

```bash
cd /home/icam-540/Desktop/video_vision/palletization_final
dos2unix deploy/keg-kiosk-session.sh deploy/keg-kiosk.desktop
chmod +x deploy/keg-kiosk-session.sh

# 1. Small helpers: window manager (dialogs/keyboard) and the logo painter
sudo apt-get install -y matchbox-window-manager hsetroot x11-xserver-utils

# 2. Register the session (edit Exec= first if the app folder differs)
sudo cp deploy/keg-kiosk.desktop /usr/share/xsessions/

# 3. The kiosk session starts the app itself: stop the desktop from also
#    starting it (otherwise it would open in the technician's desktop too)
systemctl --user disable keg-palletization.service
rm -f ~/.config/autostart/keg-palletization-autostart.desktop
```

4. Auto-login **into the kiosk session** (Step 1 must already be done):

   * **GDM3** — tell AccountsService which session the user gets:

     ```bash
     sudo tee /var/lib/AccountsService/users/icam-540 >/dev/null <<'EOF'
     [User]
     XSession=keg-kiosk
     Session=keg-kiosk
     SystemAccount=false
     EOF
     ```

     (If the file already exists, only change/add the `XSession=` and
     `Session=` lines.) Keep `WaylandEnable=false` in `/etc/gdm3/custom.conf`.

   * **LightDM** — in `/etc/lightdm/lightdm.conf` under `[Seat:*]` add
     `autologin-session=keg-kiosk`.

5. `sudo reboot` — boot → white/logo screen → app splash. No desktop.

**Undo:** set `XSession=ubuntu` / `Session=ubuntu` (or remove
`autologin-session`), then `systemctl --user enable keg-palletization.service`
and `sudo systemctl disable keg-kiosk-boot.service`.

### Optional — hide the boot text as well

The Linux boot messages before the login can be hidden too. On the Jetson
this is the `APPEND` line of `/boot/extlinux/extlinux.conf`. **Back the file
up first** — a typo there can stop the device booting:

```bash
sudo cp /boot/extlinux/extlinux.conf /boot/extlinux/extlinux.conf.bak
sudo nano /boot/extlinux/extlinux.conf
# on the APPEND line of the active entry, add at the end:
#   quiet splash loglevel=3 vt.global_cursor_default=0
```

`splash` shows the boot logo only if Plymouth is installed
(`plymouth --version`); without it the screen simply stays dark until the
kiosk session starts.

### Measuring it

```bash
systemd-analyze                                  # kernel + userspace boot time
grep -n "Palletization PRODUCTION launch\|Waiting for the camera\|warming up" \
    logs/app.log | tail                          # when the app window started
```

---

## Step 5 — Final test

```bash
sudo reboot
```

Expected: boot → (brief desktop) → app appears full-screen automatically,
camera feed live, no clicks. Done. With Step 4b there is no desktop at all:
boot → white/logo screen → app splash.

---

## Step 6 — Set the maintenance PIN (recommended)

Without a PIN any operator can change the device identity or the camera MAC,
turn test mode on, clear the print queue, and use **Exit App**, which opens the
Ubuntu desktop. Set a PIN of 6 to 12 digits, as the user the app runs as, from
the app folder:

```bash
python3 tools/set_maintenance_pin.py            # set or change
python3 tools/set_maintenance_pin.py --status
python3 tools/set_maintenance_pin.py --clear    # remove (protection off)
```

Those actions then ask for the PIN on screen. An operator signed in through the
cloud with the role `admin` is not asked. Power Off, focus, light, exposure,
test print and beer type stay open to every operator. Five wrong PINs lock the
prompt for 5 minutes. Only a salted hash is saved, in `maintenance_pin.json`
(mode 0600). No restart is needed.

---

## Servicing the device (over SSH)

To manage the app remotely:

```bash
# Log in from a laptop on the same network
ssh icam-540@<device-ip>

# Control the app
systemctl --user restart keg-palletization.service
systemctl --user stop    keg-palletization.service   # e.g. to service on-site
systemctl --user start   keg-palletization.service

# Live logs (app stdout/stderr)
tail -f /home/icam-540/Desktop/video_vision/palletization_final/logs/app.log

# systemd's own view (restarts, exit codes)
journalctl --user -u keg-palletization.service -f
```

> `systemctl --user` over a fresh SSH session may need:
> `export XDG_RUNTIME_DIR=/run/user/$(id -u)` first.

---

## Updating the app in the field

```bash
ssh icam-540@<device-ip>
systemctl --user stop keg-palletization.service
cd /home/icam-540/Desktop/video_vision/palletization_final
# ...pull / copy the new code, update venv if needed...
systemctl --user start keg-palletization.service
```

If you changed `start_prod.sh` or the `.service` file, also run:
`cp deploy/keg-palletization.service ~/.config/systemd/user/ && systemctl --user daemon-reload`.

---

## Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| Boots to a **login prompt** | Auto-login not applied — recheck Step 1 for the right display manager; confirm the username. |
| Screen **blank**, service `active` | App started before the display was ready, or wrong `DISPLAY`. Confirm `echo $DISPLAY` in the session is `:0`; if not, set the real value in the `.service` `Environment=DISPLAY=` line. On Wayland, set `WaylandEnable=false` (Step 1). |
| `status` shows **restart loop** | Read `logs/app.log`. Usual causes: venv missing/broken, or a Python import error. The preflight line in the log shows torch/CUDA availability. |
| Camera feed **"NO CAMERA"** | The internal camera daemon (`localhost:5000`) wasn't up in time or the video node changed. App keeps retrying; check the daemon and `ICAM_CONFIG['device']` in `config.py`. Launch is non-fatal by design. |
| `systemctl --user` says **"Failed to connect to bus"** over SSH | Run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` first, or confirm `enable-linger` (Step 2.3) was run. |
| Old **double-click launcher** still used | Remove/disable `Keg_Palletization.desktop` from the Desktop so operators can't launch a second, windowed instance. |

---

## What changed in the app for kiosk mode

- `main.py` honours `KEG_KIOSK=1`: full-screen (`showFullScreen`). Without the
  env var (your dev double-click), the app runs windowed.
- **Exit** offers Exit App or Shut Down. Under systemd, Exit App runs
  `systemctl --user stop keg-palletization.service` so the app is not
  relaunched (`KEG_NO_SERVICE_STOP=1` disables this; `KEG_KIOSK_SERVICE`
  overrides the unit name).
- **Restart** in the top bar relaunches the app in place and skips the camera
  REST init and auto-focus.

## Before first use

On the first start the app opens **Camera Setup** instead of the login page.
Confirm the MAC (pre-filled from `eth0`) or type the one registered in Keg
Management, enter the camera password, press **Verify with cloud**, then
**Save & Start**. The device needs network for this step; nothing is saved
until the cloud accepts the MAC and password. The app restarts and continues
as normal. No file needs editing per camera.

When preparing a master image to clone onto several devices, delete
`device_state.json` and `camera_credentials.json` from it first, so every
cloned device asks for its own MAC on its first start.

If the camera password changes later, enter the new one on the login page. It is
generated in Keg Management (**Scanner → Cameras → this camera → Camera login → Generate password**) and
saved to `camera_credentials.json` beside `main.py`; set `KEG_CAMERA_PASSWORD`
in the service environment instead if you prefer not to keep it on disk.
