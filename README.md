# Keg Counting & QR Detection System

> Part of the [Checkology keg-line vision suite](../README.md). This is
> **Position 3 â€” the pallet camera** at the end of the keg washing line.

## Overview

The **Keg Counting & QR Detection System** is a computer visionâ€“based solution for automated keg counting, QR code detection, and pallet management in brewery and industrial environments. The system uses **YOLO-based object detection**, **multi-method QR recognition**, and **cloud synchronization** to support both manual and automated workflows through a simple operator HMI.

The operator guide is in [docs/Keg_Palletization_Operator_Manual.pdf](docs/Keg_Palletization_Operator_Manual.pdf).

## Key Features

- Real-time keg detection and counting
- QR code detection using multiple methods (YOLO localizes â†’ pyzbar decodes, QReader fallback, tiled decoding for small/distant codes)
- **Camera sign-in** to the cloud (camera password â†’ 7-day Bearer token) and **operator sign-in** by PIN or email & password
- **AUTO and MANUAL capture modes** â€” AUTO sends the pallet when the keg count reaches the target and is stable
- **Batch number** chosen after sign-in â€” alphanumeric, 3â€“32 characters, at least one digit; NEXT increments it; a new day prompts Change / Keep
- **Already-palletized kegs** â€” a keg already on another pallet pauses AUTO and the operator chooses MOVE, KEEP or cancel
- **Cloud blacklist** â€” flagged kegs are shown in red with a warning (the keg is still counted)
- **Unregistered kegs** â€” codes the brewery does not know get a **REGISTER** button; the registration form is drawn from the cloud
- **Unmatched codes warning** â€” if the cloud matches only some codes, the pallet is still created and the unmatched codes are shown
- **Zebra label printing** through a persistent print queue, with reprint, printer status banner and Test Print
- **Offline outbox** â€” unsent pallet reports survive restarts and power loss and are retried automatically
- **26 UI languages**, switched live, remembered per operator
- **Guided tour** of the app and of Settings, shown once per operator
- Optional **beer type** selection (off by default)
- Light / dark theme
- Operator lens re-focus from the HMI (Settings â†’ Camera Focus)

## Supported Platforms

- Ubuntu 20.04+ / NVIDIA Jetson (Advantech iCAM-540)
- Windows 10+ (development, with `CAMERA_SOURCE = "webcam"`)
- Python 3.8+

## Installation

From inside this directory:

```bash
# --system-site-packages: JetPack's torch / OpenCV live outside the venv
python3 -m venv venv --system-site-packages
source venv/bin/activate
```

(Windows: `venv\Scripts\activate`)

Install system dependencies (Ubuntu):

```bash
sudo apt-get update
sudo apt-get install -y libzbar0 ffmpeg libgl1-mesa-glx
```

Install Python dependencies:

```bash
pip install --upgrade pip
pip install -r requirements.txt
pip install PySide6 pyusb
```

> `requirements.txt` does not list **PySide6** (the HMI) or **pyusb** (Zebra
> printing fallback when `/dev/usb/lp*` is missing) â€” install them as above.
> OpenCV and NumPy come in with `ultralytics`.

The QR model is `models/model_qr/best.pt` (committed). If
`models/model_qr/best.engine` exists and a GPU is present, the TensorRT engine
is used instead (`USE_TENSORRT_ENGINE`); it falls back to `best.pt` if the
engine fails its warm-up.

## Running the Application

Start the system:

```bash
python main.py
```

### One-Click Execution

**Linux (Jetson):** the app starts by itself at power-on (kiosk, see
[Deployment](#deployment)). To run it by hand from a terminal on the desktop:
```bash
./start_prod.sh
```

### Start-up sequence

1. **Splash** â€” camera REST init on the iCAM-540 (close â†’ disable timestamp â†’
   4K â†’ open â†’ play â†’ auto-exposure â†’ reset focus â†’ auto-focus). The whole
   sequence is retried up to 3 times. **RETRY / EXIT** appear after 90 s
   (`SPLASH_CAMERA_ACTIONS_AFTER`) so a hung camera never needs a power cycle.
   A failed camera init is not fatal.
2. **Backend** â€” local data wipe and outbox restore, camera sign-in to the
   cloud, API sender + printer + print queue, YOLO model load, camera start.
   Blacklist, beer types and cloud config are preloaded in parallel (waits at
   most 15 s).
3. **Login page** â€” camera connection, operator sign-in, batch selection.
4. **Dashboard** â€” the Operator page.

## Signing In

Sign-in is required (`OPERATOR_LOGIN_REQUIRED = True`). The login page has three
steps:

**Camera connection** â€” shown only when the camera is not signed in. Enter the
camera password generated in Keg Management (**Scanner â†’ Cameras â†’ this camera
â†’ Camera login â†’ Generate password**) and press **SAVE & CONNECT**. The password
is saved to `camera_credentials.json` only after the cloud accepts it;
`KEG_CAMERA_PASSWORD` in the environment overrides the file. On a network error
(not a wrong password) the operator can **CONTINUE WITHOUT SIGN-IN**.

**Operator sign-in** â€” pick a name from the cloud operator list for this camera,
then enter a 4â€“6 digit **PIN** on the on-screen pad, or switch to **EMAIL &
PASSWORD**. A locked operator sees a countdown. Up to 10 emails are remembered;
passwords never are. Without cloud sign-in the operator just picks a name.

**Batch selection** â€” confirm the batch number, or press **Next batch** to
increment it, then **START WITH THIS BATCH**.

While signed in, the app checks every 5 s and signs the operator out if the
cloud ended the shift. **Switch operator** and **Sign out** are in the sidebar.

## Operator Page

- **Live Camera Feed** with Target / Detected / Stability / Status tiles and a
  "labels to print" badge.
- **Scanned QR Codes** list (Copy, Clear). A URL in a QR code is reduced to its
  last path segment.
- **Batch Configuration** â€” AUTO / MANUAL, batch number + NEXT, target count
  (1â€“20, default 6), beer type (when enabled), CAPTURE, SEND TO SERVER, Clear
  history.

**AUTO** (default) sends when the count equals the target and has been stable
for 3 ticks, then prints the label and resets. A keg missing for 2.5 s is
dropped from the count. After 7 s without a decode a "No QR detected" banner
appears; AUTO stays on.

**MANUAL** â€” **CAPTURE** runs a tiled decode of the full 4K frame, then **SEND
TO SERVER**. Both ask for confirmation.

Before sending, the app checks that the count equals the target and the batch
number is valid, and asks for confirmation if no operator is signed in.

**Already-palletized kegs** are flagged amber and AUTO pauses. The operator
chooses **MOVE** (to this pallet), **KEEP** (create another pallet) or cancel.
Kegs still waiting in the outbox are held as **PENDING UPLOAD** for the shift.

**Blacklisted kegs** are shown red as **FLAGGED** with a "set it aside" toast.

**Unregistered kegs** â€” each new code is checked once in the background. An
unknown keg gets a **REGISTER** button that opens a form built from the cloud's
field list (required fields, number ranges, text patterns, select options). The
form is cached for offline use and refreshed hourly. Registration never blocks
counting or sending. Set `KEG_REGISTRATION_ENABLED = False` to turn it off.

**Beer type** is off by default (`BEER_TYPE_ENABLED`) and is switched on in
Settings. When on, a choice is required before sending if a catalogue is loaded.

## Pallet Records

- **Pallets Confirmed By Cloud** â€” Reprint, Remove (local only)
- **Pending Upload â€” Not Yet A Pallet** â€” Discard
- **Labels Not Printed Yet** â€” Reprint, Label OK

Labels go through a print queue (`print_queue.db`) that prints one job at a
time, waits for the printer (re-checks every 15 s) and fails a job after 3
attempts. A label that was mid-print at power loss is shown as **CHECK** so the
operator can confirm or reprint it. History is kept 7 days / 500 labels.

### Any USB printer, no setup

With `PRINTER_CONFIG['backend'] = 'auto'` (the default) any USB printer can be
plugged in. For every label the app reads the printer's IEEE 1284 Device ID and
prints in the language it reports:

| Printer reports         | Label sent                                      |
|-------------------------|-------------------------------------------------|
| ZPL (Zebra)             | ZPL, the same label as before                   |
| TSPL (TSC and clones)   | TSPL                                            |
| ESC/POS (receipt type)  | ESC/POS QR, text and cut                        |
| PCL, PostScript, URFâ€¦   | QR image through CUPS (office printer)          |
| anything else (EPLâ€¦)    | refused; the label waits and Settings says why  |

The printer can be swapped while the app runs. With no USB printer the CUPS
default printer is used, if there is one. Settings â†’ Label Printer shows the
language and model, e.g. `auto (TSPL)` Â· `TSC TE210`. `'zebra'` or `'generic'`
force one backend.

Supported: Zebra ZPL (ZD220, ZD230, ZD411, ZD421, ZD621, ZT230, ZT411, ZT610,
GK/GX/GC420 with ZPL), TSC TSPL (TE200, TE210, TE244, TTP-244 Pro, DA210,
TX200), Xprinter / Munbyn / Gprinter label printers that report TSPL, and
ESC/POS receipt printers (Epson TM-T20, TM-T88).

When a label cannot print, the operator banner, a notification and the
Settings card say why and what to do:

| Status                              | Meaning                                       |
|-------------------------------------|-----------------------------------------------|
| No printer connected                | nothing on USB                                |
| Printer not supported               | language not supported (the list is shown)    |
| Printer in use by another program   | another program holds the printer             |
| No permission to use the printer    | app not started with sudo / not in `lp`       |
| Printer does not answer             | switch it off and on, check the cable         |
| Office printer needs the print service | CUPS is not set up on the camera           |

- The printer does not report its label size: check the label on the smallest
  roll in use.
- Not covered: Brother P-touch, Datamax (DPL), Sato (SBPL), EPL-only printers.
- Office printers need CUPS on the camera (driverless printing through ipp-usb).
- The app user must be in the `lp` group (`sudo usermod -aG lp <user>`), or
  passwordless `sudo tee` must be allowed.
- The iCAM-540 kernel (`5.15.148-rt-tegra`) has no `usblp` driver, so there is
  no `/dev/usb/lp*`: every printer goes through PyUSB, which must be installed
  in the app's venv (`sudo venv/bin/python -m pip install pyusb`).

## Settings & Diagnostics

A side panel on the left opens one section at a time:

- **Line setup**
  - **Device Identity** â€” location name and line number (camera number
    `Cam3.<line>` is derived). Applies to the next report without a restart.
  - **Operator** â€” on-the-line picker, used only when nobody is signed in.
  - **Beer type** â€” enable / disable, refresh catalogue.
- **Label printer** â€” status (also a green / red dot in the side panel),
  what to do when it cannot print, Refresh, Test print, and the supported
  printers.
- **Camera** â€” Focus (manual-focus hint, RESET AUTO-FOCUS, lens position and
  sharpness curve, see below), Light and Exposure. On a narrow screen the three
  cards stack in one column.
- **Audit Log** â€” last 30 events.

The sidebar also holds **App tour**, the **language** button and the **theme**
switch (changing the theme restarts the app).

**Maintenance PIN.** Once a PIN is set with `tools/set_maintenance_pin.py`
(see deploy/DEPLOY.md, Step 6), changing Device Identity or the camera MAC,
turning test mode on or off, clearing the print queue and **Exit App** ask for
it. A cloud operator with the role `admin` is not asked. With no PIN set
nothing is asked.

**Kept across restarts.** The local database is new at every start, but the
"already on a pallet" warning still knows the pallets of the last
`KEG_LEDGER_KEEP_DAYS` days (`keg_ledger.db`), and the last cloud blacklist,
operator list, beer types and registration form are kept for
`CLOUD_CACHE_MAX_AGE_DAYS` (3) days so the device works when it boots offline.

**Settings check.** At start-up, invalid values in `config.py` (for example a
target count of 0, an unknown printer backend or a webcam rotation of 45) are
replaced by safe defaults and listed in the Audit Log and `logs/app.log`.

## Languages

26 languages: English, Spanish, Portuguese, French, German, Italian, Dutch,
Polish, Czech, Romanian, Turkish, Russian, Ukrainian, Arabic (right-to-left),
Hindi, Marathi, Bengali, Tamil, Telugu, Chinese, Japanese, Korean, Vietnamese,
Thai, Indonesian and Swahili.

Chosen from the sidebar or the login page and applied live. The choice is saved
per operator and restored at sign-in. A missing translation falls back to
English; a language with no installed font is shown disabled. To add a
language, copy `ui_pyside/locales/_template.json`.

## Guided Tour

A highlighted step-by-step overlay (Back / Next / Skip; arrow keys, Enter, Esc).
The **App tour** (16 steps) starts once per operator when the dashboard first
opens; the **Settings tour** (9 steps, opening each section in turn) on their first
Settings visit. **App
tour** in the sidebar replays it.

## Camera focus

Start-up focuses the camera once, on the splash (steps 7-8 of the REST init:
`reset_focus_position` â†’ `autofocus_start`). Focus then drifts in service â€” a
knock during wash-down, a changed pallet height, or a boot auto-focus that
locked onto an empty conveyor â€” and when it does the QR codes go soft and kegs
stop decoding.

**Settings â†’ Camera Focus â†’ RESET AUTO-FOCUS** re-runs exactly those two steps,
on the same daemon, through the same transport (`modules/camera_init.rest_post`).
It runs on a worker thread because `autofocus_start` holds the response open
until the lens physically locks (up to `CAMERA_INIT_AUTOFOCUS_TIMEOUT`, 60 s),
so the live feed and detection keep running; the button disables itself and
reads FOCUSINGâ€¦ until the camera answers. The result â€” including a failure, and
what the camera said â€” goes to the card's status line and the Audit Log.

Keep a pallet in view while it runs: an auto-focus hunt on an empty conveyor can
lock onto the conveyor surface.

### Focus on the QR codes / manual focus (double-tap)

The camera's auto-focus cannot be told where to look; it locks on whatever has
the most contrast, which can be an object at the side of the view instead of
the keg labels. So the app focuses itself, in two ways:

- **Automatically on the QR codes**, the first time YOLO sees any after
  start-up (once per run, `CAMERA_FOCUS_AUTO_QR_AT_START`). If the conveyor is
  empty at boot it waits for the first pallet.
- **FOCUS button** (live camera header): focus on the QR codes in the middle
  of the picture (`CAMERA_FOCUS_DEFAULT_REGION`), or on that region itself
  when it has none.
- **Manual focus**: double-tap the live feed, any time (no panel needed). A
  double-tap on a QR code (YOLO's box, a soft code included) focuses on that
  code; anywhere else focuses on a square around it (`CAMERA_TAP_FOCUS_AREA`).
  A single tap does nothing, so a stray touch cannot refocus the line. A
  square on the feed shows the target: amber while focusing, then green or
  red.

Both move the lens with `POST /camera/focus_abs_position` (`abs_position`,
0â€“1600) and measure sharpness (variance of the Laplacian) only inside the
target; with several QR codes each is scaled to its own best and averaged, so
mixed keg heights get the position that suits them all.

The search is built around what the iCam-540's focus motor does (measured
2026-10-28): it answers a move only once the motor stops (about 75 ms + 3.6 ms
per lens step); the picture comes at 14 frames/s; the sharp zone is only ~30
steps wide; and the motor has **backlash and drift** â€” a position reached
going down is 55+ steps off the same one reached going up, and runs drift by
tens of steps. So the lens can never be sent back to a position number
measured earlier, and every measured move goes up:

1. **Glide**: one continuous upward move through 120 steps either side of
   where the target was found, scoring every frame on the move (~1.5 s).
   If nothing gets sharper there, the whole range is swept the same way
   first.
2. **Closed-loop finish**: start at the glide's estimate (it reads 50â€“120
   steps low on the device, so `CAMERA_FOCUS_GLIDE_OFFSET` corrects it) and
   come up watching the picture â€” 20-step moves through blur, 10-step ones
   once it sharpens, on past the expected peak while still getting sharper.
   A QR code stops at the first position where it reads; an area stops as
   soon as it gets softer again (and then reads the whole codes in it, not the
   square). If it goes over the peak without that, it comes up once more and
   stops at half the best sharpness seen.

About 4â€“6 s near focus, 10â€“12 s from far away (simulated with the measured motor,
backlash and drift). No settle wait is needed after a move. Each move is logged in `logs/app.log`
as `[FOCUS-TIMING]` and `[FOCUS-SCORE]` (`[FOCUS-SWEEP]` for the sweep).

The lens is left at the sharpest position and that position is saved
(`device_state.json`, `camera_focus`). At start-up a saved position replaces
the auto-focus step (`CAMERA_FOCUS_USE_SAVED_AT_START`); **RESET AUTO-FOCUS**
clears it. The Camera Focus card shows the lens position and the sharpness
curve as it fills in. Detection pauses during a search. A search that finds no
target, sees no change in sharpness, or is stopped by an app exit moves the
lens back where it was. The settle times (`CAMERA_QR_FOCUS_SETTLE*`) are first
guesses, not yet measured on the device.

On a `CAMERA_SOURCE = "webcam"` dev box the same button enables
`CAP_PROP_AUTOFOCUS` on the capture instead; a fixed-focus webcam that refuses
the control says so rather than pretending the lens moved.

## Layer 1 from a USB webcam

Layer 1 is the layer furthest from the iCam, so its QR codes are the smallest
in the iCam's picture and the hardest to read. A 4K USB webcam plugged into
one of the iCam's USB 3.0 ports, mounted low and close to the pallet, can read
layer 1 instead. Layers 2 and up stay on the iCam.

**Without a webcam nothing changes.** At start-up the app looks for one. If
none is plugged in, if it does not start, or if it later stops sending
pictures, every layer is read by the iCam exactly as before. A webcam plugged
in later is picked up at the next layer 1 (looked for at most every
`LAYER1_WEBCAM_RETRY_S` seconds, 30 by default).

How it works (`modules/camera_select.py`):

- **Choosing.** The camera is chosen for the layer expected next, and only
  between layers (no keg counted yet), so every box of one layer comes from
  one camera. A single pallet (stacking off) stands at layer-1 height and is
  read by the webcam too (`LAYER1_WEBCAM_SINGLE_PALLET`).
- **Both cameras stay open.** The camera that is not reading only takes frames
  off its device without decoding them, so the switch is immediate and a 4K
  MJPEG webcam costs almost no CPU while it stands by.
- **Falling back.** No new webcam picture for `LAYER1_WEBCAM_STALL_S`
  (3 s) while it reads: the iCam finishes the layer at once. The Audit Log
  says so.
- **Focus stays with the iCam.** The webcam's focus is fixed (layer 1 is always
  the same distance away). FOCUS and double-tap on the live feed are turned off
  while the webcam reads, and layer focus, the start-up focus, exposure, white
  balance and the light keep driving the iCam through its REST service. While
  the webcam reads layer 1, nothing moves the iCam's lens.
- **Stack area and height check.** The stack's area is learned from iCam
  boxes only, the detector does not use it on the webcam's picture, and a layer
  read by the webcam gives no QR size, so the height check does not compare
  layer 2 with it.
- **Keg positions.** Row 1 is the top of the iCam's picture and column 1 its
  left. If the webcam looks at the pallet from another side, set
  `LAYER1_WEBCAM_ROTATE` (0, 90, 180, 270, clockwise) and `LAYER1_WEBCAM_MIRROR`
  so its boxes are turned into the iCam's orientation before the grid is
  worked out. If the webcam dropped out part-way through a layer, the camera
  that read most of its kegs keeps its boxes; the others get no cell, like a
  keg read without a box.

Set-up on site:

1. Plug the webcam into a USB 3.0 port of the iCam.
2. Mount it outside the pallet's footprint, below the height of layer 2, so
   neither the robot nor the layers above can hit it. Looking down at an angle
   of up to about 30-40Â° is fine for QR codes. Layer 2 later blocks its view;
   it is only used for layer 1.
3. Check it is found: `v4l2-ctl --list-devices`, then start the app. The log
   says `Layer-1 webcam found: /dev/videoN (<name>)`, or why not.
4. Fix its focus on layer 1: `v4l2-ctl -d /dev/videoN -L` shows the focus
   range; try values with
   `v4l2-ctl -d /dev/videoN -c focus_automatic_continuous=0 -c focus_absolute=<value>`
   while layer 1 is under it, then put the best value in
   `LAYER1_WEBCAM_CONFIG['focus']`.
5. Check the picture is bright enough under the iCam's light. If not, set
   `LAYER1_WEBCAM_CONFIG['exposure']`, or add a small LED bar.
6. Put a layer down and compare the keg positions with the real pallet. If
   rows or columns come out swapped, set `LAYER1_WEBCAM_ROTATE` / `_MIRROR`.

Settings, all in `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `LAYER1_WEBCAM_ENABLED` | `True` | `False`: never look for a webcam |
| `LAYER1_WEBCAM_CONFIG['device']` | `None` | `None`: found automatically. Or a fixed path, e.g. `/dev/v4l/by-id/usb-...-video-index0` |
| `LAYER1_WEBCAM_CONFIG['name_contains']` | `''` | Part of the name, to pick one of several USB cameras |
| `LAYER1_WEBCAM_CONFIG['width' / 'height' / 'fps']` | 3840 / 2160 / 15 | 15 fps keeps a CPU core free for YOLO |
| `LAYER1_WEBCAM_CONFIG['fourcc']` | `'MJPG'` | Needed for 4K over USB |
| `LAYER1_WEBCAM_CONFIG['autofocus' / 'focus']` | `False` / `None` | Autofocus off; `None` leaves the lens where it is |
| `LAYER1_WEBCAM_CONFIG['exposure']` | `None` | `None`: the webcam's automatic exposure |
| `LAYER1_WEBCAM_ROTATE` / `_MIRROR` | `0` / `False` | Webcam picture turned into the iCam's orientation, for positions |
| `LAYER1_WEBCAM_SINGLE_PALLET` | `True` | A single pallet is read by the webcam too |
| `LAYER1_WEBCAM_STALL_S` | `3.0` | No picture for this long: the iCam takes over |
| `LAYER1_WEBCAM_RETRY_S` | `30.0` | How often to look for a webcam that is not there |

Only used with `CAMERA_SOURCE = "icam"`; the laptop test path has one camera.

## Cloud Integration

All cloud calls go to **one API base** plus the `/api` prefix:

```
POST {base}/api/camera/auth/login                       # camera sign-in â†’ Bearer token
POST {base}/api/camera/operator/login                   # operator sign-in
GET  {base}/api/camera/{macId}/operators                # operators for this camera
POST {base}/api/camera/pallet-report                    # one report per completed pallet
GET  {base}/api/camera/{macId}/blacklist                # kegs this camera must flag
POST {base}/api/camera/keg-registration/check           # is this keg known?
GET  {base}/api/camera/{macId}/keg-registration/form    # registration form
POST {base}/api/camera/keg-registration                 # register a keg
POST {base}/api/kegs/cam/beer-types                     # beer types (MAC in the body)
```

Every request carries `Authorization: Bearer <camera token>`. On an expired or
ended session (`401`) the app signs in again once and repeats the call. The
token is kept in memory only.

Every keg listed in one report is grouped onto **one pallet** under **one batch
number**. The response returns the `paletteId` that the pallet label is printed
from.

> **Two hosts â€” do not mix them up.**
> `https://dev.checkology-cloud.io` is the Keg Management **web app** (browser
> only). It answers every path with its own page, so a camera pointed at it gets
> HTML with a `200` and looks like it has an empty blacklist.
> `https://local-keg-management-api.checkology-cloud.io` is the **REST API** the
> camera uses.

### Report body

```json
{
  "macId":        "3C:6D:66:2C:D8:16",
  "cameraName":   "Cam3.1",
  "lineNumber":   "1",
  "batchNumber":  "B-2026-0829",
  "operatorId":   "...",
  "operatorName": "...",
  "kegIds":       ["SONA-2026-0005", "SONA-2026-0001", "SONA-2026-0004"],
  "scannedTime":  "2026-08-29T09:15:04.120Z",
  "timestamp":    "2026-08-29T09:15:11.480Z",
  "layer":        1,
  "result":       "pass"
}
```

* Both timestamps are **UTC with milliseconds and a trailing `Z`** â€”
  `scannedTime` is when the camera read the layer, `timestamp` is when the
  report was sent.
* `batchNumber`, `operatorId` / `operatorName` and `beerType` / `beerTypeId`
  are **omitted when blank**. Beer type is sent only when the feature is on.
* `result: "pass"` is always sent. Anything other than `pass` tells the cloud
  the layer was abandoned and **nothing is written**.
* `kegIds` is trimmed and de-duplicated in first-read order, capped at 500.
* `paletteId` is sent only when adding a layer to an existing pallet; omit it
  and the cloud starts a new one.

### Switching environment

The cloud API is the default. To point a device at a test server, set both
environment variables before starting:

```bash
export KEG_PALLET_API_ENV=test
export KEG_PALLET_API_TEST_URL=https://<test host>:<port>
```

If `KEG_PALLET_API_TEST_URL` is missing the app stays on the cloud API. The
active endpoints and the camera MAC are shown on the **Settings** page.

### Blacklist

Fetched per camera with the MAC in the **path** (not the body). Response shape:

```json
{ "macId": "...", "breweryId": "...", "count": 2, "items": [ ... ] }
```

The blacklist, beer types and cloud config are re-fetched every 3 s while
online. Every successful fetch is cached, and the cache is served when the cloud
is unreachable. The cache only ever holds real cloud responses.

### Before it will work

In the Keg Management web app, under **Scanner â†’ Cameras**:

1. Register the camera with its exact `macId`.
2. Set its Line Position to **Pallet** and give it a line number.
3. Generate a camera login password (shown once; note it down).
4. Add the operators who work this line.

The MAC is **not** edited in `config.py`. On the first start of a new device the
app shows the **Camera Setup** screen: the MAC field is pre-filled from the
device's wired Ethernet port (`eth0`), the installer confirms or corrects it,
enters the camera password and presses **Verify with cloud**. Only a MAC + password
the cloud accepts can be saved (`device_state.json`, section `camera`, and
`camera_credentials.json`); the app then restarts and runs normally. To change
it later: **Settings > Device Identity > Camera MAC ID > Change** (same check,
then a restart). `KEG_CAMERA_MAC` in the environment overrides the saved value
(dev box / tests). Devices set up before this screen existed keep working: their
MAC is taken from `camera_credentials.json` on the first start after the update.

An unregistered MAC returns `404 Camera not found`; a camera registered to the
wrong brewery returns `409`. Neither is retried â€” both are surfaced to the
operator as "needs attention".

### Retry behaviour

| Result | Handling |
|--------|----------|
| `200` + `applied: true` | Pallet created, label queued for printing |
| `200` + `unmatched: [...]` | Pallet still created â€” unmatched codes are logged and toasted, they are **not** on the pallet |
| `400` / `404` / `409` | Permanent â€” flagged for the operator, **not** queued |
| `401` / `429` / `5xx` / timeout | Queued and retried in the background (1, 2, 4, 8, 16 min), flagged for attention after 10 tries |

Queued reports are also written to `offline_outbox.db`, so they survive a
restart or power loss. New reports wait behind older queued ones so pallets
reach the cloud in order. A report is never dropped silently.

## Local Data

All files are created beside `main.py`. Never copy them between cameras.

**Wiped on every start and exit:** `keg_detection.db` and the cloud caches
(`blacklist_cache.json`, `operators_cache.json`, `beer_types_cache.json`,
`keg_reg_form_cache.json`).

**Kept:**

| File | Holds |
|------|-------|
| `camera_credentials.json` | Camera password (secret â€” do not commit) |
| `device_state.json` | Device identity, theme, language, per-operator language, batch day, last logins, remembered emails, tours seen |
| `offline_outbox.db` | Reports not yet delivered (deleted when empty) |
| `print_queue.db` | Labels waiting to print and recent print history |
| `last_batch.txt` | Last batch number |
| `keg_frames/` | Captured frames (deleted after 3 days, folder capped at 2 GB; `FRAMES_*` in `config.py`) |
| `logs/` | `app.log` (rotated at 10 MB by `start_prod.sh`) |

**Free disk space** is checked when the dashboard opens and every 5 minutes
(`modules/disk_check.py`). It only warns, it never stops the line: below 2 GB
free the operator gets a warning toast and an Audit Log line; below 500 MB
an error toast that repeats every 30 minutes, and captured frames older than
1 hour are cleaned up at once. Each change is logged as `[DISK]` in `app.log`.
Thresholds: `DISK_*` in `config.py`.

## Exit, Restart and Shut Down

- **Exit** offers **Exit App** or **Shut Down** (powers off the device), each
  with a second confirmation. It waits up to 15 s for pending uploads and
  prints before closing. Under systemd it also stops the service, so the app
  stays closed until started again.
- **Restart** (top bar) relaunches the app in place and skips the camera REST
  init and auto-focus, which the camera daemon still holds.
- `SIGTERM` / `SIGINT` / `SIGHUP` close the app cleanly.

## Deployment

`deploy/` contains everything for hands-off startup on the iCAM-540 (see
[deploy/DEPLOY.md](deploy/DEPLOY.md)): a systemd **user** service
(`keg-palletization.service`, restarts after a crash), the kiosk session
(power-on â†’ app only; Exit App â†’ Ubuntu desktop) and the unattended launcher
`start_prod.sh`, which sets `KEG_KIOSK=1` for full-screen kiosk mode.

The test and lint tools were moved out of the shipped folder (to
`palletization_final_unused_backup/` on the development PC).
`tools/build_trt_engine.py` stays: it builds `models/model_qr/best.engine` on
each new camera.

## Version

* Version: 2.1.0
* Last Updated: October 2026

## License

Proprietary â€“ All rights reserved.
