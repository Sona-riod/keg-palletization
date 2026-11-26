# config.py
import json
import re
import os
import threading
from pathlib import Path
from urllib.parse import quote

# Base paths
BASE_DIR = Path(__file__).parent
SAVE_FOLDER = BASE_DIR / "keg_frames"
DB_PATH = BASE_DIR / "keg_detection.db"
# Reports that could not reach the cloud. Kept separate from DB_PATH, which is
# wiped on every start and exit. This file survives both (and power loss) and is
# deleted once empty. See modules/outbox.py.
OUTBOX_DB_PATH = BASE_DIR / "offline_outbox.db"
# Pallet labels waiting to print, plus a short history of printed ones. Also
# separate from DB_PATH so the start/exit wipe never loses an unprinted label.
# See modules/print_queue.py.
PRINT_QUEUE_DB_PATH = BASE_DIR / "print_queue.db"
# Every pallet report the cloud confirmed, with the batch it recorded (the
# operator's Batch History). Also separate from DB_PATH, so it survives the
# start/exit wipe. See modules/batch_history.py.
BATCH_HISTORY_DB_PATH = BASE_DIR / "batch_history.db"
# Which pallets were stacked together and which kegs are on which layer. Also
# separate from DB_PATH, so a half-built stack survives a restart. See
# modules/stack_store.py.
STACK_HISTORY_DB_PATH = BASE_DIR / "stack_history.db"
# Which pallet each keg was last put on. Also separate from DB_PATH, so the
# "already on a pallet" warning still works after a restart or power cut.
# Entries older than KEG_LEDGER_KEEP_DAYS are dropped. See modules/keg_ledger.py.
KEG_LEDGER_DB_PATH = BASE_DIR / "keg_ledger.db"
KEG_LEDGER_KEEP_DAYS = 30
# Last known good cloud lists (blacklist, operators, beer types, registration
# form) survive a restart so the device still works when it boots offline.
# A copy older than CLOUD_CACHE_MAX_AGE_DAYS, or saved for another camera MAC,
# is deleted at start-up and exit. See modules/local_data.py.
CLOUD_CACHE_MAX_AGE_DAYS = 3
# Maintenance authorization for Device Identity, camera MAC, test mode, clear
# print queue and Exit App (see ui_pyside/maintenance.py). Who may approve:
#   1. an administrator, checked by the cloud (their own PIN or email+password);
#   2. the maintenance PIN (hash only, offline "break-glass" fallback), set
#      with tools/set_maintenance_pin.py;
#   3. a one-time unlock code from support.
# MAINTENANCE_AUTH_REQUIRED is defined after CAMERA_SOURCE below.
MAINTENANCE_PIN_FILE = BASE_DIR / "maintenance_pin.json"
# Wrong-entry counter and lockout, kept on disk so a restart or power cycle
# does not reset it. See modules/maintenance_pin.py.
MAINTENANCE_LOCK_FILE = BASE_DIR / "maintenance_lock.json"
MAINTENANCE_AUDIT_FILE = BASE_DIR / "maintenance_audit.log"
# True: the maintenance PIN is offered only when the cloud cannot check an
# administrator (camera not signed in, or the cloud unreachable). While the
# cloud is up, a named administrator must approve. False: the PIN is always
# offered next to the administrator sign-in.
MAINTENANCE_PIN_OFFLINE_ONLY = True
# The approval dialog closes by itself after this many seconds without input,
# so a half-filled administrator sign-in is never left on the screen.
MAINTENANCE_DIALOG_IDLE_S = 90
MODELS_DIR = BASE_DIR / "models"
LOGS_DIR = BASE_DIR / "logs"

# Captured frames in SAVE_FOLDER are cleaned up (modules/frame_store.py): older
# than FRAMES_KEEP_DAYS, or oldest first while the folder is over FRAMES_MAX_MB.
# A frame younger than FRAMES_MIN_AGE_S is never deleted (a capture can still
# be waiting for SEND). Checked at start-up and after captures, at most once
# every FRAMES_PRUNE_EVERY_S.
FRAMES_KEEP_DAYS = 3
FRAMES_MAX_MB = 2000
FRAMES_MIN_AGE_S = 3600
FRAMES_PRUNE_EVERY_S = 600

# Free disk space check (modules/disk_check.py). Warn only: nothing stops.
# Below DISK_WARN_FREE_MB the operator is warned; below DISK_CRITICAL_FREE_MB
# the warning repeats every DISK_CRITICAL_REPEAT_S and old keg_frames are
# cleaned at once. Checked every DISK_CHECK_INTERVAL_S.
DISK_WARN_FREE_MB = 2000
DISK_CRITICAL_FREE_MB = 500
DISK_CHECK_INTERVAL_S = 300
DISK_CRITICAL_REPEAT_S = 1800

# The batch number is chosen by the operator after sign-in and only changes when
# the operator changes it (NEXT button or typing), not after a pallet is sent.

# Create directories
SAVE_FOLDER.mkdir(exist_ok=True)
MODELS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)



# Camera source
# This one setting switches the whole camera path (config + REST hardware init):
#   "icam"   -> iCam-540 production (Jetson 4K v4l2 + REST init)
#   "webcam" -> local laptop/USB webcam for testing (no REST init)
# Detection is the same for both: QR codes are detected and decoded every frame
# and a green box is drawn around each one.
CAMERA_SOURCE = "icam"   # set to "webcam" only for local testing

# iCam-540: Jetson Orin production camera (4K v4l2)
ICAM_CONFIG = {
    'type': 'v4l2',    # Jetson typically uses v4l2
    'device': 10,      # iCam-540 video node (adjust if it enumerates differently)
    'width': 3840,     # 4K. Must match the REST camera init (camera_configure.sh) so the
    'height': 2160,    # decoder gets full 4K frames for QR decoding.
    'fps': 30,
}

# Local webcam: generic OpenCV capture for testing the feed on a normal PC
WEBCAM_CONFIG = {
    'type': 'webcam',  # generic OpenCV webcam (Windows/Mac/Linux)
    'device': 0,       # default webcam; try 1, 2... if you have multiple cameras
    'width': 1280,
    'height': 720,
    'fps': 30,
}

CAMERA_CONFIG = WEBCAM_CONFIG if CAMERA_SOURCE == "webcam" else ICAM_CONFIG

# Maintenance actions (Exit App, device identity, ...) always need an approval
# on the production camera, even before a maintenance PIN is set: an
# administrator through the cloud, or a support code. False = old behaviour
# (open until a PIN is set). The laptop test path (webcam) stays open.
MAINTENANCE_AUTH_REQUIRED = (CAMERA_SOURCE == "icam")


# Layer-1 USB webcam (modules/camera_select.py)
# A USB webcam plugged into the iCam, mounted low and close to the pallet,
# reads layer 1 (the layer furthest from the iCam, whose codes are the
# smallest in its picture). Layers 2 and up stay on the iCam, exactly as before.
#
# Nothing changes when no USB webcam is found: at start-up the app looks for
# one; if there is none, or it stops sending pictures, every layer is read by
# the iCam as it always was. A webcam plugged in later is picked up at the
# next layer 1 (looked for at most every LAYER1_WEBCAM_RETRY_S seconds).
#
# Only used with CAMERA_SOURCE = "icam" (the laptop test path has one camera).
# Master switch. The operator turns the webcam on / off in Settings -> Cameras
# (saved, see layer1_webcam_use()); with this False that choice is ignored.
LAYER1_WEBCAM_ENABLED = True
LAYER1_WEBCAM_CONFIG = {
    'type': 'webcam',
    # None: found automatically - the first USB video-capture device that is
    # not the iCam's own sensor. Or a fixed path, which survives a re-plug:
    # '/dev/v4l/by-id/usb-<maker>_<model>-video-index0'
    'device': None,
    # Part of the webcam's name (as in `v4l2-ctl --list-devices`) to pick one
    # of several USB cameras. Empty: the first one found.
    'name_contains': '',
    'width': 3840,      # 4K, same as the iCam, so QR crops are as sharp
    'height': 2160,
    # Pictures per second. 4K MJPEG is decoded on the CPU: 15 is plenty for
    # kegs standing still and keeps one CPU core free for YOLO.
    'fps': 15,
    # MJPG: the only way most USB webcams send 4K at a usable frame rate.
    'fourcc': 'MJPG',
    # Layer 1 is always the same distance away: autofocus off, focus fixed.
    # 'focus' is the webcam's own focus value (`v4l2-ctl -d <dev> -L` shows
    # its range); None leaves the lens where it is.
    'autofocus': False,
    'focus': None,
    # Exposure: None = the webcam's own automatic exposure.
    'exposure': None,
    # Mains frequency of the factory lights (50 or 60), so the webcam's auto
    # exposure does not pulse or band under them. Set with v4l2-ctl; None =
    # not touched (as before).
    'power_line_hz': 50,
}
# How the webcam's picture is turned compared with the iCam's, so keg
# positions (row 1 = top of the iCam picture, column 1 = its left) mean the
# same corner of the pallet whichever camera read the layer.
#   rotate: 0, 90, 180 or 270 degrees clockwise to turn the webcam picture
#           into the iCam's orientation;  mirror: flip left-right after that.
LAYER1_WEBCAM_ROTATE = 0
LAYER1_WEBCAM_MIRROR = False
# A single pallet (stacking off) stands at layer-1 height, so the webcam
# reads it too. False: single pallets stay on the iCam.
LAYER1_WEBCAM_SINGLE_PALLET = True
# No new webcam picture for this long while it is reading: fall back to the
# iCam for the rest of the layer (unplugged, cable, USB reset).
LAYER1_WEBCAM_STALL_S = 3.0
# After such a stall the iCam keeps reading until that layer / pallet is done,
# instead of handing back to the webcam every LAYER1_WEBCAM_RETRY_S while no
# keg is counted (the picture switched back and forth). False = as before.
LAYER1_WEBCAM_HOLD_AFTER_STALL = True
# How often to look again for a webcam that is not there.
LAYER1_WEBCAM_RETRY_S = 30.0


# Camera hardware init (Advantech REST API)
# Only applies to the iCam-540; the local webcam skips it.
CAMERA_INIT_ENABLED   = (CAMERA_SOURCE == "icam")   # iCam-540: run REST init | webcam: skip
CAMERA_API_BASE_URL   = "http://localhost:5000"
CAMERA_INIT_WIDTH     = 3840                   # 4K, matches camera_configure.sh
CAMERA_INIT_HEIGHT    = 2160
# Separate connect/read timeouts: a down daemon fails fast (connection refused is
# instant), while slow but working endpoints get the patience curl had (no --max-time).
CAMERA_INIT_CONNECT_TIMEOUT   = 5              # fail fast if daemon is down (s)
CAMERA_INIT_TIMEOUT           = 15             # default per-request read timeout (s)
CAMERA_INIT_AUTOFOCUS_TIMEOUT = 60             # autofocus_start blocks until lens locks (s)
# Per-step settle waits (s), mirroring the sleeps in camera_configure.sh
CAMERA_INIT_SETTLE_AFTER_CLOSE = 5
CAMERA_INIT_SETTLE_AFTER_OPEN  = 5
CAMERA_INIT_SETTLE_AFTER_PLAY  = 5
CAMERA_INIT_SETTLE_AFTER_RESET = 2
# Cold-boot retry: the camera's REST socket answers before its sensor/lens
# pipeline is ready, so the first open/play/autofocus_start can fail. The whole
# sequence is retried a few times before giving up.
# modules/camera_init.py imports both of these; if either is missing the import
# fails and camera init is skipped entirely (no 4K configure, no autofocus).
CAMERA_INIT_MAX_ATTEMPTS   = 3                 # attempts at the full init sequence
CAMERA_INIT_RETRY_BACKOFF  = 5                 # seconds between attempts
# Power-on: the app window opens at once and these waits run behind the splash
# (they used to run in start_prod.sh before the window existed). Wait up to
# CAMERA_BOOT_WAIT_S for the camera's REST service to answer, then give the
# sensor/lens CAMERA_BOOT_WARMUP_S to warm up (once per app start).
CAMERA_BOOT_WAIT_S   = 30
CAMERA_BOOT_WARMUP_S = 10

# Splash escape hatch
# Start-up can take a while: the settles alone are 17 s (5+5+5+2), and
# autofocus_start (step 8) holds its response open until the lens locks, up to
# CAMERA_INIT_AUTOFOCUS_TIMEOUT (60 s). With retries, a bad cold boot can sit on
# the splash for about 4 minutes.
#
# In kiosk mode the operator cannot reach the OS, so without these buttons a
# hung camera start means a power cycle. RETRY / EXIT appear this many seconds
# after the camera splash starts, whatever step the camera is on. The automatic
# retries keep running behind the buttons, so the operator can still just wait;
# a successful init hides them again.
SPLASH_CAMERA_ACTIONS_AFTER = 90

# Focus endpoints on the camera daemon
# Used from two places: the boot sequence (modules/camera_init.py, steps 7-8)
# and the RESET AUTO-FOCUS button in Settings (modules/focus.py). Defined once
# so both always hit the same endpoint.
CAMERA_RESET_FOCUS_PATH = "/camera/reset_focus_position"
CAMERA_AUTOFOCUS_PATH   = "/camera/autofocus_start"

# Lens position on the camera daemon (QR / tap-to-focus, modules/focus.py)
# GET reads the motor's absolute position in "result"; POST with one multipart
# field moves it there. From the daemon's own API list (localhost:5000/apidocs),
# read on the iCam-540 2026-10-28: after auto-focus it answered 910.
CAMERA_FOCUS_POSITION_PATH  = "/camera/focus_abs_position"
CAMERA_FOCUS_POSITION_FIELD = "abs_position"
CAMERA_FOCUS_POSITION_MIN   = 0
CAMERA_FOCUS_POSITION_MAX   = 1600

# Focus on the QR codes / on a tapped point (modules/focus.py). The camera's
# auto-focus locks on whatever has the most contrast, which on this line can be
# an object at the side of the view. Instead the app moves the lens itself and
# scores sharpness only inside the target: all QR boxes YOLO finds (automatic,
# the first time codes are in view after start-up), or the QR / area the
# operator double-taps on the live feed.
#   Measured on the iCam-540 (2026-10-28, logs/app.log [FOCUS-*] lines):
#   - the camera answers a move only once the motor has stopped: about 75 ms
#     plus 3.6 ms per lens step (50 steps 250 ms, 400 steps 1.5 s);
#   - the focus peak is narrow: sharpness drops ~5x 50 steps from the peak
#     and is down to the background (~11-13) by 200 steps;
#   - 14 frames/s at 4K.
#   - the motor has backlash and drifts: a position reached going down is 55+
#     steps off the same one reached going up, and two runs with the same
#     moves landed ~40 steps apart. So the lens cannot be sent back to a
#     position number measured earlier.
#   Search (every measured move goes up, so the backlash is always taken up
#   the same way):
#   1. Glide: one continuous upward move from CAMERA_FOCUS_LOCAL_WINDOW below
#      to that far above where the target was found, scoring every frame on
#      the move (~1 s). If nothing gets sharper there (far from focus), the
#      lens first runs across its range in CAMERA_FOCUS_SWEEP_CHUNK pieces the
#      same way, then glides through what that found.
#   2. Closed loop: go below the peak and come up in CAMERA_FOCUS_FINE_STEP
#      steps watching the picture. A QR code: stop at the first position where
#      it reads. An area: stop as soon as it gets softer again (one step past
#      the peak). If it goes over the peak without that, it aims at what it
#      saw and comes up once more, stopping at CAMERA_FOCUS_STOP_FRACTION of
#      that sharpness - where the picture is sharp now, not at a number.
CAMERA_FOCUS_LOCAL_WINDOW       = 120     # glide from this far below the start
# The glide's peak estimate came out low in every device run: the code first
# read 50-120 steps above it (2026-10-28: 980->1030, 887->977, 942->1032,
# 925->1025, 848->968). This much is added; the final pass starts
# CAMERA_FOCUS_FINAL_REACH below that, i.e. at the raw estimate, still below
# every readable position seen, instead of 60 below it walking through blur.
CAMERA_FOCUS_GLIDE_OFFSET       = 60
# FOCUS button on the live feed: focus on the QR codes inside this region
# (x1, y1, x2, y2 as fractions of the frame), or on the region itself when it
# has none. Default: the middle of the picture.
CAMERA_FOCUS_DEFAULT_REGION     = (0.2, 0.2, 0.8, 0.8)
CAMERA_FOCUS_FINE_STEP          = 10      # step of the final, closed-loop pass
CAMERA_FOCUS_FINAL_REACH        = 60      # final pass starts this far below the peak
# "Sharp now" = this share of the best seen. Readings at the peak vary a lot
# frame to frame (one high frame must not set an unreachable bar), and the
# sharp zone is steep enough (~10x down within 40 steps) that half the best
# is still within a few steps of the peak.
CAMERA_FOCUS_STOP_FRACTION      = 0.5
CAMERA_FOCUS_BACKLASH_STEPS     = 150     # go this far below before coming up
CAMERA_FOCUS_SWEEP_CHUNK        = 200     # lens steps per move while sweeping
CAMERA_FOCUS_MOTOR_MS_PER_STEP  = 3.6     # measured: motor speed
CAMERA_FOCUS_MOTOR_OVERHEAD_MS  = 75      # measured: fixed cost of a move
CAMERA_FOCUS_FRAME_LATENCY_MS   = 120     # guess: capture -> frame in the app
# The camera answers after the motor stops, so no settle wait is needed.
CAMERA_QR_FOCUS_SETTLE          = 0.0     # s to wait after every move
CAMERA_QR_FOCUS_SETTLE_PER_STEP = 0.0     # extra s per lens step moved
CAMERA_QR_FOCUS_FRESH_FRAMES    = 2       # new frames to wait for after a move
CAMERA_QR_FOCUS_SAMPLES         = 1       # frames averaged at each position
CAMERA_QR_FOCUS_MAX_BOXES       = 12      # the largest QR boxes are scored
# "Sharper" means at least this many times the softest score seen. The
# background alone varies 11-13 (x1.18) on the device; real peaks were x6-x50.
# Below it near the start, the sweep runs; below it overall, the focus fails
# and the lens goes back where it was.
CAMERA_QR_FOCUS_MIN_CONTRAST    = 1.5
# MEASURE LAYER on the Camera Focus card: lens positions scored either side of
# the layer's sharpest, and the step between them. 200/10 is 41 moves, about
# 15-30 s on the iCam-540 together with the focus search.
FOCUS_RANGE_SPAN                = 200
FOCUS_RANGE_STEP                = 10
# The focus check warns when every layer is sharp only within fewer lens steps
# than this: the motor lands tens of steps apart from run to run (2026-10-28).
FOCUS_CHECK_MIN_OVERLAP         = 20

# Focus zone panel on the live preview (modules/focus_zone.py): which heights
# above the floor are sharp, and how many layers that covers.
CAMERA_MOUNT_HEIGHT_M   = 1.7    # lens above the floor, measured on site
STACK_LAYER_HEIGHT_M    = 0.68   # one pallet + its kegs (150 mm + 530 mm 50 L Euro)
FOCUS_ZONE_FOCUS_LAYER  = 1      # the layer the focus is set on (its top)
CAMERA_MIN_DISTANCE_M   = 0.5    # closer than this, a layer does not fit the view
# PLACEHOLDERS, not from the iCam-540 datasheet: replace with the real lens
# values and set LENS_VALUES_ESTIMATED = False, which removes the "estimated"
# note from the panel.
LENS_FOCAL_MM           = 6.0
LENS_F_NUMBER           = 2.8
LENS_COC_MM             = 0.004  # circle of confusion: about 2 pixels of the sensor
LENS_VALUES_ESTIMATED   = True
# Focus on the QR codes automatically the first time YOLO sees any after
# start-up (once per run). Detection pauses for the few seconds it takes.
CAMERA_FOCUS_AUTO_QR_AT_START   = True
# Manual focus: double-tap the live feed (a single tap does nothing, so a stray
# touch cannot refocus the line). The square focused on when the double-tap is
# not on a QR code, as a fraction of the frame width. Kept small: a larger
# square takes in label and table edges, whose contrast outweighs the code.
CAMERA_TAP_FOCUS_AREA           = 0.06
# After each focus, save the target at full resolution to logs/focus/ (the
# screen preview is too small to judge sharpness). The last N are kept; 0 = off.
CAMERA_FOCUS_KEEP_CROPS         = 20
# At start-up, move the lens to the last saved focus position instead of
# running the camera's auto-focus (which would lock on the side object again).
# RESET AUTO-FOCUS clears the saved position, so start-up auto-focuses again.
CAMERA_FOCUS_USE_SAVED_AT_START = True

# Light endpoints on the camera daemon (Flash LED)
# Used by Settings -> Camera Light (modules/lighting.py). Same calls the vendor
# page (localhost:5000/camera/settings -> Lighting Settings) makes. Each value
# is sent as one multipart form field; posting led_status with no field reads
# the current mode back in "result". The ranges match the vendor sliders.
CAMERA_LED_MODE_PATH       = "/camera/led_status"
CAMERA_LED_MODE_FIELD      = "led_status"
CAMERA_LED_MODE_MIN        = 0
CAMERA_LED_MODE_MAX        = 7
CAMERA_LED_INTENSITY_PATH  = "/camera/led_gain"
CAMERA_LED_INTENSITY_FIELD = "led_gain"
CAMERA_LED_INTENSITY_MIN   = 0
CAMERA_LED_INTENSITY_MAX   = 25
# Light off when the app exits (Exit, Power off, splash EXIT, window close,
# systemd stop). "Off" is sent as these values; they are not saved, so the
# operator's settings come back on the next start. If intensity 0 still leaves
# the LED glowing, add "mode": 0.
CAMERA_LED_OFF_ON_EXIT     = True
CAMERA_LED_OFF_VALUES      = {"intensity": 0}
# Reset button on the Camera Light card: the initial values it sends (and saves).
CAMERA_LED_RESET_VALUES    = {"mode": 0, "intensity": 0}

# Auto-exposure range on the camera daemon
# Used by Settings -> Camera Exposure (modules/exposure.py). Same call as the
# vendor page's AE Min / AE Max + Confirm: one POST with both fields (multipart).
# A GET on the same path reads the range back as "result": [min, max].
# Units are the camera's: 100 µs (50 = 5 ms). The vendor defaults are 50 / 10000.
# Field names confirmed on the iCam-540 (2026-10-15): every other name tried
# got the daemon's web page back instead of a JSON answer.
CAMERA_AE_RANGE_PATH  = "/camera/auto_exposure_range"
CAMERA_AE_MIN_FIELD   = "min_exposure"
CAMERA_AE_MAX_FIELD   = "max_exposure"
CAMERA_AE_LIMIT_MIN   = 1        # slider ends (100 µs units)
CAMERA_AE_LIMIT_MAX   = 10000
# Auto exposure on/off: the same call the start-up sequence makes (step 6,
# auto_exposure_op=1). Off is assumed to be 0; check on the camera.
CAMERA_AE_OP_PATH     = "/camera/auto_exposure_op"
CAMERA_AE_OP_FIELD    = "auto_exposure_op"
CAMERA_AE_ON_VALUE    = 1
CAMERA_AE_OFF_VALUE   = 0
# Auto exposure off when the app exits (Exit app or Power off device), like the
# light. Not saved, so the next start brings the operator's setting back.
CAMERA_AE_OFF_ON_EXIT = True

# White balance (modules/white_balance.py, Settings -> Camera Exposure).
# With auto white balance on, a large coloured area fills the picture with the
# opposite colour: a blue test pallet turned the whole view yellow (2026-11-04).
# "fixed": the colours are learned with SET NOW on the card (auto white balance
# runs CAMERA_AWB_SETTLE_S on a neutral surface, then is switched to manual) and
# held from then on, whatever pallet is put down. Start-up only holds them
# (manual), never learns them again: the scene then may be a coloured pallet.
# "auto": the camera's own auto white balance, as before.
# The switch is the iCam programming guide's awb_op (1 auto, 0 manual), sent
# like auto_exposure_op. If the camera service answers with its web page
# instead of JSON, the path or field name differs on this firmware: correct
# them here.
CAMERA_WHITE_BALANCE    = "fixed"
# Names as the camera's own settings page (localhost:5000, main.*.chunk.js) uses
# them: GET/POST /camera/auto_white_balance_op. "/camera/awb_op" answered HTTP 405.
CAMERA_AWB_OP_PATH      = "/camera/auto_white_balance_op"
CAMERA_AWB_OP_FIELD     = "auto_white_balance_op"
CAMERA_AWB_AUTO_VALUE   = 1
CAMERA_AWB_MANUAL_VALUE = 0
CAMERA_AWB_SETTLE_S     = 3.0
# Recommended exposure ranges on the Camera Exposure card (100 us units).
#   Standard (safe): normal kegs, the default for a new operator. The line ran
#     21-22 Oct 2026 at 5 - 46.6 ms and 5 - 39.8 ms with normal reads. Reset sends
#     this range with auto exposure on.
# The card warns (do not use) when AE Max goes above CAMERA_AE_WARN_MAX (codes
# on moving kegs blur) or AE Min below CAMERA_AE_WARN_MIN (too dark).
CAMERA_AE_PRESET_STANDARD = (50, 400)    # 5 ms - 40 ms   NORMAL (safe), measured
# Surrounding-light presets (starting points, not yet measured on the line):
CAMERA_AE_PRESET_BRIGHT   = (10, 100)    # 1 ms - 10 ms   BRIGHT
CAMERA_AE_PRESET_DIM      = (100, 700)   # 10 ms - 70 ms  DIM
CAMERA_AE_PRESET_DARK     = (200, 1000)  # 20 ms - 100 ms DARK
CAMERA_AE_WARN_MAX        = 1000         # 100 ms
CAMERA_AE_WARN_MIN        = 10           # 1 ms
# The vendor defaults (5 ms - 1 s), kept for reference; Reset no longer uses them.
CAMERA_AE_RESET_RANGE = (50, 10000)


# Camera identity (needed by the cloud URLs below)
# CAMERA_MAC_ID is how the cloud recognises this camera. It is sent in the
# pallet-report body and appears in the blacklist path, so it is defined before
# the endpoints that use it.


def _normalize_mac(mac):
    """Return the MAC as the cloud stores it: trimmed and upper-case.

    The API is case-sensitive on this path segment. The same MAC in lower case
    answers `404 {"error":"Camera not found"}`, which looks like an unregistered
    camera when it is registered fine. Normalising here keeps every caller right.
    """
    return str(mac or "").strip().upper()


CAMERA_NAME   = "ICAM-540"

# The MAC is no longer written here. It is set once per device on the Camera
# Setup screen (ui_pyside/setup_page.py), checked against the cloud there and
# saved to the "camera" section of DEVICE_STATE_FILE. load_camera_mac() fills
# it in at import (below, after the device-state helpers), so every module
# that does `from config import CAMERA_MAC_ID` gets the saved value.
#   ""  -> not set up yet: main.py shows the Camera Setup screen.
# KEG_CAMERA_MAC in the environment overrides the saved value (dev box / tests).
CAMERA_MAC_ID     = ""
CAMERA_MAC_SOURCE = ""        # "env", "saved", "migrated" or "" (not set)
# Network interface whose MAC is offered as the suggestion on the setup screen.
CAMERA_MAC_IFACE  = "eth0"


# Cloud API configuration
# Checkology base URL
# Every cloud call is base + API_PREFIX + path, so no endpoint can drift onto
# another host.
#
# Two different hosts, do not mix them up:
#   dev.checkology-cloud.io                    = the Keg Management web app
#                                  (React, browser only). Its /api/* proxy
#                                  answers 500 and every other path returns the
#                                  app's HTML with a 200, so pointing the camera
#                                  here looks like success but returns a web page.
#                                  Do not use it as the API. This is why every
#                                  cloud read in api_sender.py checks the
#                                  content-type before the status code.
#   local-keg-management-api.checkology-cloud.io = the REST API the camera talks
#                                  to (pallet report, blacklist, operator list,
#                                  beer types).  <-- this one
#
# Every endpoint resolves from CHECKOLOGY_API_URL. Do not add a second host
# (e.g. api-dev.checkology-cloud.io) for individual calls.
CHECKOLOGY_APP_URL  = "https://dev.checkology-cloud.io"        # web UI (reference only)
CHECKOLOGY_API_URL  = "https://local-keg-management-api.checkology-cloud.io"  # REST API

# All camera routes are mounted under /api, then the path from the spec:
#   POST {base}/api/camera/pallet-report
#   GET  {base}/api/camera/{mac_id}/blacklist
API_PREFIX          = "/api"
PALLET_REPORT_PATH  = "/camera/pallet-report"
BLACKLIST_PATH      = "/camera/{mac_id}/blacklist"   # MAC is in the PATH, not the body
OPERATORS_PATH      = "/camera/{mac_id}/operators"   # MAC in the PATH, same as blacklist
# The brewery's keg-ID prefixes (the same endpoint the monoblock camera uses for
# its serial format check). Serial matching builds its candidates from them:
#   GET {base}/api/camera/{mac_id}/qr-prefixes -> {"prefixes": ["CHECKOLOGY-2026-", ...]}
QR_PREFIXES_PATH    = "/camera/{mac_id}/qr-prefixes" # MAC in the PATH, same as blacklist
CURRENT_CONFIG_PATH = "/current-config"

# Beer-type catalogue (cloud -> camera)
# Read this before touching the call. This endpoint differs from the other two
# reads in four ways:
#
#   pallet-report / blacklist        beer types
#   ---------------------------      ----------------------------------------
#   GET                              POST
#   MAC in the URL path              MAC in the request body: {"macId": "..."}
#   /api/camera/{mac_id}/...         /api/kegs/cam/beer-types   (kegs, not camera)
#   {macId, count, items} envelope   a bare array, no wrapper object
#
# No credential required. Each element is
#   {_id, name, description, expirationDays, breweryId, createdAt, updatedAt, __v}
# and only `_id` and `name` are used.
BEER_TYPES_PATH       = "/kegs/cam/beer-types"
BEER_TYPES_CACHE_FILE = BASE_DIR / "beer_types_cache.json"   # last known good catalogue

# Operator list (cloud -> camera)
# Who is working the line. Unlike the beer-type call, this one follows the
# blacklist's shape: GET, MAC in the path, under /camera/, an envelope object:
#
#     GET {base}/api/camera/{mac_id}/operators
#     -> {"macId": ..., "breweryId": ..., "count": N,
#         "operators": [{"id": ..., "name": ..., "role": "user"|"admin"}, ...]}
#
# 404 means this camera is not registered with the cloud yet. The operator picks
# once a shift, so the picker lives in Settings and the choice is persisted; the
# operator page never asks. Every successful fetch is cached to disk so the
# picker still works when the device boots with no cloud.
OPERATORS_CACHE_FILE = BASE_DIR / "operators_cache.json"   # last known good list

# Keg registration from the camera (Checkology "Camera Keg Registration API").
# A code the brewery does not know can be registered on the spot:
#
#     POST {base}/api/camera/keg-registration/check     {macId, serialNo, itemType}
#     GET  {base}/api/camera/{mac_id}/keg-registration/form?itemType=keg
#     POST {base}/api/camera/keg-registration           {macId, serialNo, itemType,
#                                                         formVersion, values}
#
# The brewery always comes from the logged-in camera; never send breweryId.
# The form is drawn from the cloud's field list, so a field changed in Keg
# Management needs no camera update. The last form is cached for offline use.
# False turns the whole feature off (no check calls, no Register button).
KEG_REGISTRATION_ENABLED = True
KEG_REG_CHECK_PATH       = "/camera/keg-registration/check"
KEG_REG_FORM_PATH        = "/camera/{mac_id}/keg-registration/form"
KEG_REG_PATH             = "/camera/keg-registration"
KEG_REG_FORM_CACHE_FILE  = BASE_DIR / "keg_reg_form_cache.json"
KEG_REG_FORM_REFRESH_S   = 3600   # the spec asks for at least one fetch an hour

# Palletising-camera API (Checkology "POST /api/camera/pallet-report" spec).
# One report per completed pallet (or layer): every keg in a report is grouped
# onto one pallet under one batch number.
#
# This value moves the device between environments:
#   PALLET_API_ENV = "cloud" -> the Checkology cloud API (CHECKOLOGY_API_URL)
#   PALLET_API_ENV = "test"  -> the standalone Checkology test box
# "cloud" is the default: the test box does not issue real paletteIds, so a
# device left on it reports "success" while no pallet exists in Keg Management.
#
# The test box's address is not kept in the code. Set it on the machine that
# needs it, e.g. in the systemd unit or the shell:
#   KEG_PALLET_API_ENV=test  KEG_PALLET_API_TEST_URL=https://<test host>:<port>
# Without a test URL the device stays on the cloud API.
PALLET_API_ENV      = os.environ.get("KEG_PALLET_API_ENV", "cloud").strip().lower()
PALLET_API_TEST_URL = os.environ.get("KEG_PALLET_API_TEST_URL", "").strip().rstrip("/")
if PALLET_API_ENV == "test" and not PALLET_API_TEST_URL:
    print("[config] KEG_PALLET_API_ENV=test but KEG_PALLET_API_TEST_URL is not set; "
          "using the cloud API")
    PALLET_API_ENV = "cloud"
PALLET_API_BASE_URL = PALLET_API_TEST_URL if PALLET_API_ENV == "test" else CHECKOLOGY_API_URL
API_ENDPOINT        = f"{PALLET_API_BASE_URL}{API_PREFIX}{PALLET_REPORT_PATH}"

# Spec limit: at most 500 codes (256 KB) per report. A batch over this is a
# payload error, not something to retry, so it is refused locally with a message.
PALLET_MAX_KEG_IDS = 500

# strict=False (the spec default) palletises what was read and records the rest,
# so one scuffed label never loses the whole pallet. strict=True makes the cloud
# refuse the entire layer (HTTP 409, nothing written) if any code can't be matched.
PALLET_STRICT = False

# Layer number reported when pallets are not stacked. When they are, each pallet
# is one layer of the stack: it is sent with its layer number and no paletteId,
# so it gets its own pallet ID and label (see modules/stack_tracker.py).
PALLET_DEFAULT_LAYER = 1

# Stacks in the pallet report (cloud doc "Palletising Camera: Layers, Stacks and
# Keg Positions", 2026-11-03, develop server). The cloud issues the stack id:
#   layer 1:    no stackId, with "layersPlanned"; the reply carries "stackId"
#   layer 2..:  "stackId" from that reply and "layer"
#   last layer: "stackComplete": true. A stack finished early is closed with
#               POST /api/camera/stack-close.
# Off: no stack field is sent and the cloud takes every report as a one-layer
# stack, as before. Turn off if the server the camera uses has no stacks yet.
PALLET_SEND_STACKS = True
STACK_CLOSE_PATH   = "/camera/stack-close"
STACKS_PATH        = "/camera/{mac_id}/stacks"            # ?status=building&mine=true

# How a stack of 2 or more layers is reported (cloud doc "Palletising Camera:
# Sending a Whole Stack", 2026-11-04):
#   "stack": each layer is checked and kept on the camera as it is scanned,
#            and the whole stack goes in ONE request when its last layer is
#            done (or NEW STACK is pressed): POST /api/camera/stack-report.
#            Until then a keg can be removed from any layer, or the stack
#            cleared. The cloud checks the whole stack before writing any of it.
#   "layer": each layer is sent on its own as it is scanned (pallet-report).
# A single pallet (1 layer per stack) always uses the per-layer report.
PALLET_REPORT_MODE = "stack"
STACK_REPORT_PATH  = "/camera/stack-report"
STACK_REPORT_MAX_LAYERS = 10       # cloud limits for one stack-report
STACK_REPORT_MAX_CODES  = 500
# The stack's own QR label (its stack id) is printed when the stack is closed,
# so a forklift can scan the stack and move every layer at once.
PRINT_STACK_LABEL  = True

# Where each keg stood. Sends "expectedCount", "layout" {rows, cols} and every
# keg as {"code", "row", "col"} (row 1 = top of the camera frame, column 1 =
# left), so Keg Management draws the layer as a grid. Off: plain codes, as before.
PALLET_SEND_POSITIONS = True
# The grid of one layer as the camera sees it, "rows x cols", e.g. "2x3".
# "" works it out from the keg count (4 -> 2x2, 6 -> 2x3) and turns it to match
# how the kegs lie in the frame (3x2 when the layer is taller than wide).
PALLET_LAYOUT = ""

# "result" tells the cloud whether the layer completed: "pass" (or omitted) means
# palletise these kegs; anything else means the layer was abandoned and nothing
# is written. Every report this app sends is a completed layer, so "pass" is sent
# explicitly rather than relying on a server-side default.
PALLET_DEFAULT_RESULT = "pass"

# locationName is not part of the pallet-report contract: the camera is
# identified by macId and the cloud echoes back the name/line/position it has
# registered for that MAC. Off so the body matches the contract exactly; turn on
# only if the cloud team confirm they use the field.
PALLET_SEND_LOCATION_NAME = False

# Beer type in the pallet report
# An approved addition to the payload (confirmed 2026-09-21). When the operator
# has selected a beer type it goes with the report; when the feature is off or
# nothing is selected, both keys are omitted, so the body is byte-identical to
# the plain report (same omit-when-blank rule as batchNumber).
#
#   "beerType":   "Wheat beer"                  <- the cloud matches on this
#   "beerTypeId": "6a71e04d3b40d07df208d487"    <- for our own traceability only
#
# The cloud matches the name case-insensitively and ignores a name it does not
# recognise, so a stale or misspelt name is a silent no-op, not a 400. Key names
# live here so they can be renamed in one place.
PALLET_BEER_TYPE_FIELD    = "beerType"     # body key carrying the name
PALLET_BEER_TYPE_ID_FIELD = "beerTypeId"   # body key carrying the catalogue _id

# Operator in the pallet report
# An approved addition to the payload (confirmed 2026-09-21), part of the cloud
# contract ("Camera API update: operator attribution + palletising"): all three
# camera report endpoints accept an optional operator, recorded against the work.
#
#   "operatorId":   "6512a3f09c4e1b0012a4d901"   <- preferred, the id from the list
#   "operatorName": "Ana Rossi"                  <- only when we have no id
#
# Same omit-when-blank rule as beerType/batchNumber: with nobody selected neither
# key is sent. An unrecognised operator is not an error; the report is accepted
# and the kegs updated anyway (operatorMatched=false in the response), so a bad
# operator never costs a pallet.
PALLET_OPERATOR_ID_FIELD   = "operatorId"     # body key carrying the operator _id
PALLET_OPERATOR_NAME_FIELD = "operatorName"   # body key carrying the name

# Already-palletized kegs: MOVE or KEEP
# A keg already on a cloud pallet can be scanned onto a new one. The operator
# chooses:
#   MOVE: the keg joins the new pallet and comes off the pallet it was on.
#   KEEP: the keg joins the new pallet and the old pallet is left as it was.
#
# Neither changes the pallet-report body. The cloud reassigns a re-reported keg
# to the newest pallet on its own, so the choice is settled on this device: MOVE
# makes the old pallet's local record stop claiming the keg
# (DatabaseManager.move_kegs_to_pallet) and logs it in the keg_moves audit table.
# Without that, the local lookup keeps naming the old pallet and the operator
# keeps getting a stale duplicate warning.

# Cloud blacklist
# Kegs the cloud has flagged as not allowed, scoped to this camera by MAC:
#
#     GET {base}/api/camera/{mac_id}/blacklist
#     -> {"macId": ..., "breweryId": ..., "count": N, "items": [...]}
#
# Served by the same API as the pallet report, so it follows PALLET_API_BASE_URL:
# a device on the test box reads the test box's blacklist, and nothing is left
# pointing at prod by accident.
BLACKLIST_BASE_URL = PALLET_API_BASE_URL


def blacklist_url(mac_id=None):
    """Full blacklist URL for a camera MAC.

    The MAC is a path segment, so it is percent-encoded (colons are legal in a
    path and kept; anything else odd is escaped rather than breaking the URL).
    Defaults to this device's CAMERA_MAC_ID, resolved at call time so a MAC
    change needs no restart.
    """
    mac = _normalize_mac(mac_id if mac_id is not None else CAMERA_MAC_ID)
    path = BLACKLIST_PATH.format(mac_id=quote(mac, safe=':'))
    return f"{BLACKLIST_BASE_URL}{API_PREFIX}{path}"


def operators_url(mac_id=None):
    """Full operator-list URL for a camera MAC.

    Same rules as blacklist_url(): the MAC is a path segment, so it is
    percent-encoded and upper-cased. The API is case-sensitive there, and a
    lower-case MAC answers 404 as if the camera were not registered.
    """
    mac = _normalize_mac(mac_id if mac_id is not None else CAMERA_MAC_ID)
    path = OPERATORS_PATH.format(mac_id=quote(mac, safe=':'))
    return f"{BLACKLIST_BASE_URL}{API_PREFIX}{path}"


def qr_prefixes_url(mac_id=None):
    """Full keg-ID prefix URL for a camera MAC (same rules as blacklist_url())."""
    mac = _normalize_mac(mac_id if mac_id is not None else CAMERA_MAC_ID)
    path = QR_PREFIXES_PATH.format(mac_id=quote(mac, safe=':'))
    return f"{BLACKLIST_BASE_URL}{API_PREFIX}{path}"


# Camera + operator login (Checkology "Camera Login API")
# Every camera API call needs a token. There are two logins:
#   camera login   - MAC + camera password -> Bearer token (valid 7 days). The
#                    camera stays signed in around the clock; modules/camera_auth
#                    attaches the token to every cloud call and, on a 401, logs
#                    in once and repeats the call once.
#   operator login - PIN (operators with hasPin) or email + password, for the
#                    length of a shift. The cloud records everything reported
#                    while signed in against that operator.
# Only POST /api/camera/auth/login works without a token.
CAMERA_AUTH_LOGIN_PATH  = "/camera/auth/login"
CAMERA_AUTH_LOGOUT_PATH = "/camera/auth/logout"
CAMERA_AUTH_ME_PATH     = "/camera/auth/me"
OPERATOR_LOGIN_PATH     = "/camera/operator/login"
OPERATOR_LOGOUT_PATH    = "/camera/operator/logout"

# Show the operator sign-in page after the splash (after camera init and
# auto-focus). False = go straight to the operator page; the camera login still
# runs either way.
OPERATOR_LOGIN_REQUIRED = True

# The camera password is generated per camera in Keg Management > Scanner >
# Cameras > (camera) > Camera login, and shown only once. It is kept in its own
# file, readable by this app's user only. Never put it in source code or a log.
# KEG_CAMERA_PASSWORD in the environment overrides the file.
CAMERA_CREDENTIALS_FILE = BASE_DIR / "camera_credentials.json"


# Device state: everything this camera remembers between starts, except the
# camera password (kept apart in CAMERA_CREDENTIALS_FILE) and the cloud caches
# (deleted on every start and exit). One file with a section per kind of data:
#   device             location, line, camera number, beer type, operator
#   ui                 {"theme": ..., "language": ...}
#   operator_languages each operator's own screen language
#   batch_day          {"batch": ..., "day": "YYYY-MM-DD"}
#   last_logins        last sign-in time per operator
#   remembered_emails  emails offered in the sign-in field
#   tour_seen          operators who have seen the app tour
#   camera_light       {"mode": ..., "intensity": ...} last set in Settings
#   camera_exposure    {"ae_min": ..., "ae_max": ..., "auto": ...} last set in Settings
# Writes go through a temporary file and a rename, so a power cut mid-write
# leaves the previous version intact.
DEVICE_STATE_FILE = BASE_DIR / "device_state.json"
_state_lock = threading.RLock()
_state_migrated = False

# The separate files this replaces: (file, section). Read once on the first
# start after the update, then deleted.
_LEGACY_STATE_FILES = (
    ("device_identity.json",   "device"),
    ("ui_theme.json",          "ui"),
    ("ui_language.json",       "ui_language"),
    ("batch_day.json",         "batch_day"),
    ("last_logins.json",       "last_logins"),
    ("remembered_emails.json", "remembered_emails"),
    ("tour_seen.json",         "tour_seen"),
)


def _read_state():
    try:
        if DEVICE_STATE_FILE.exists():
            with open(DEVICE_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[config] Could not read {DEVICE_STATE_FILE.name}: {e}")
    return {}


def _write_state(data):
    tmp = DEVICE_STATE_FILE.with_name(DEVICE_STATE_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DEVICE_STATE_FILE)


def _migrate_legacy_state():
    """Move the old one-file-per-setting data into DEVICE_STATE_FILE (once)."""
    global _state_migrated
    _state_migrated = True
    found = [(BASE_DIR / name, section) for name, section in _LEGACY_STATE_FILES
             if (BASE_DIR / name).exists()]
    if not found:
        return
    data, moved = _read_state(), []
    for path, section in found:
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception as e:
            print(f"[config] Could not migrate {path.name}: {e}")
            continue
        if section == "ui":
            ui = data.setdefault("ui", {})
            if isinstance(old, dict) and "theme" not in ui:
                ui["theme"] = old.get("theme")
        elif section == "ui_language":
            if isinstance(old, dict):
                data.setdefault("ui", {}).setdefault("language", old.get("device"))
                if isinstance(old.get("operators"), dict):
                    data.setdefault("operator_languages", old["operators"])
        else:
            data.setdefault(section, old)
        moved.append(path)
    try:
        _write_state(data)
    except Exception as e:
        print(f"[config] Could not save {DEVICE_STATE_FILE.name}: {e}")
        return                  # keep the old files; try again next start
    for path in moved:
        try:
            path.unlink()
        except OSError:
            pass
    print(f"[config] Moved {', '.join(p.name for p in moved)} into {DEVICE_STATE_FILE.name}")


def _state(section, default):
    """One section of the device state (`default` if missing or the wrong type)."""
    with _state_lock:
        if not _state_migrated:
            _migrate_legacy_state()
        value = _read_state().get(section, default)
    return value if isinstance(value, type(default)) else default


def _save_state(section, value):
    """Replace one section and save. Raises OSError if the file can't be written."""
    with _state_lock:
        if not _state_migrated:
            _migrate_legacy_state()
        data = _read_state()
        data[section] = value
        _write_state(data)


# Emails (never passwords) of operators who signed in with email + password,
# most recent first. Offered as suggestions in the sign-in email field.
REMEMBERED_EMAILS_MAX = 10


def remembered_emails():
    """Emails of past email sign-ins, most recent first."""
    return [str(e) for e in _state("remembered_emails", [])
            if isinstance(e, str) and e.strip()]


def remember_email(email):
    """Put `email` first in the remembered list, lower-cased and de-duplicated.

    The cloud treats emails as case-insensitive.
    """
    email = str(email or "").strip().lower()
    if not email:
        return
    emails = [email] + [e for e in remembered_emails() if e.lower() != email.lower()]
    try:
        _save_state("remembered_emails", emails[:REMEMBERED_EMAILS_MAX])
    except Exception as e:
        print(f"[config] Could not save remembered emails: {e}")


# Application tour: shown once per operator on this device. The keys are the
# operators who finished or skipped it (operator id, else name; "__device__"
# when nobody is signed in). Remove the "tour_seen" section of
# DEVICE_STATE_FILE to show the tour to everyone again.
def tour_seen(key):
    return str(key) in [str(k) for k in _state("tour_seen", [])]


def mark_tour_seen(key):
    seen = [str(k) for k in _state("tour_seen", [])]
    if str(key) not in seen:
        seen.append(str(key))
    try:
        _save_state("tour_seen", seen[-200:])
    except Exception as e:
        print(f"[config] Could not save the tour state: {e}")


# Camera light (Settings -> Camera Light). The values the camera last confirmed
# from this screen, so the sliders start where the operator left them and the
# light is set back to them once the camera has started.
def camera_light():
    """{"mode": int, "intensity": int} with only the values that were set."""
    out = {}
    for key, value in _state("camera_light", {}).items():
        if key in ("mode", "intensity"):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
    return out


def save_camera_light(**values):
    """Save mode= and/or intensity=. Returns False if it could not be saved."""
    try:
        with _state_lock:
            data = camera_light()
            data.update({k: int(v) for k, v in values.items()
                         if k in ("mode", "intensity")})
            _save_state("camera_light", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the camera light: {e}")
        return False


# Camera auto-exposure range (Settings -> Camera Exposure). Same idea as the
# light: the range the camera last confirmed from this screen, set back once
# the camera has started.
def camera_exposure():
    """(ae_min, ae_max) last set from Settings, or None if never set."""
    data = _state("camera_exposure", {})
    try:
        return int(data["ae_min"]), int(data["ae_max"])
    except (KeyError, TypeError, ValueError):
        return None


def camera_auto_exposure():
    """True/False as last set from Settings, or None if never set."""
    value = _state("camera_exposure", {}).get("auto")
    return value if isinstance(value, bool) else None


def save_camera_exposure(ae_min=None, ae_max=None, auto=None):
    """Save the range and/or the auto on/off. Returns False if it could not be saved."""
    try:
        with _state_lock:
            data = _state("camera_exposure", {})
            if ae_min is not None and ae_max is not None:
                data.update(ae_min=int(ae_min), ae_max=int(ae_max))
            if auto is not None:
                data["auto"] = bool(auto)
            _save_state("camera_exposure", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the camera exposure: {e}")
        return False


def camera_white_balance():
    """"fixed" or "auto": as last chosen on the card, else CAMERA_WHITE_BALANCE."""
    value = _state("camera_white_balance", {}).get("mode")
    return value if value in ("fixed", "auto") else CAMERA_WHITE_BALANCE


def save_camera_white_balance(mode):
    """Save "fixed" or "auto". Returns False if it could not be saved."""
    try:
        _save_state("camera_white_balance", {"mode": "fixed" if mode == "fixed" else "auto"})
        return True
    except Exception as e:
        print(f"[config] Could not save the white balance: {e}")
        return False


# Lens position saved by the QR / tap-to-focus search (modules/focus.py). Start-up moves
# the lens there instead of running auto-focus (CAMERA_FOCUS_USE_SAVED_AT_START).
def camera_focus_position():
    """The saved lens position, or None if none is saved."""
    try:
        return int(_state("camera_focus", {})["position"])
    except (KeyError, TypeError, ValueError):
        return None


def save_camera_focus_position(position):
    """Save a lens position, or clear it with None. Returns False if it could
    not be saved."""
    from datetime import datetime
    try:
        _save_state("camera_focus", {} if position is None else
                    {"position": int(position),
                     "saved_at": datetime.now().isoformat(timespec="seconds")})
        return True
    except Exception as e:
        print(f"[config] Could not save the camera focus: {e}")
        return False


# Focus range per layer height, measured on the Camera Focus card.
def focus_range_layers():
    """{layer number: {"band", "points", "measured_at"}} measured so far."""
    out = {}
    for key, value in _state("focus_range", {}).items():
        try:
            out[int(key)] = value
        except (TypeError, ValueError):
            continue
    return out


def save_focus_range_layer(layer, band, points):
    """Keep one layer's measurement, or clear them all with layer None.
    Returns False if it could not be saved."""
    from datetime import datetime
    try:
        data = {} if layer is None else {str(k): v for k, v in focus_range_layers().items()}
        if layer is not None:
            data[str(int(layer))] = {
                "band": band, "points": [list(p) for p in points],
                "measured_at": datetime.now().isoformat(timespec="seconds")}
        _save_state("focus_range", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the focus range: {e}")
        return False


def layer_focus_positions():
    """{layer number: lens position} learned by the automatic layer refocus."""
    out = {}
    for key, value in _state("layer_focus", {}).items():
        try:
            out[int(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def predict_layer_focus(layer):
    """(lens position, how sure) for `layer` from the positions learned:
    the learned one ("learned"), a straight line through the two nearest
    learned layers ("predicted"), one learned layer plus LAYER_FOCUS_STEP per
    layer ("stepped"), or (None, None)."""
    known = layer_focus_positions()
    if layer in known:
        return known[layer], "learned"
    if len(known) >= 2:
        a, b = sorted(known, key=lambda n: (abs(n - layer), n))[:2]
        slope = (known[b] - known[a]) / (b - a)
        guess = round(known[a] + slope * (layer - a))
        return max(CAMERA_FOCUS_POSITION_MIN, min(CAMERA_FOCUS_POSITION_MAX, guess)), "predicted"
    if known:
        k = min(known, key=lambda n: abs(n - layer))
        guess = known[k] + int(LAYER_FOCUS_STEP) * (layer - k)
        return max(CAMERA_FOCUS_POSITION_MIN, min(CAMERA_FOCUS_POSITION_MAX, guess)), "stepped"
    return None, None


def stack_area():
    """(x1, y1, x2, y2) full-frame pixels where the stacks stand, or None."""
    try:
        v = _state("stack_area", {}).get("area")
        return tuple(float(x) for x in v) if v and len(v) == 4 else None
    except (TypeError, ValueError):
        return None


def save_stack_area(area):
    """Keep where the stacks stand (layer 1's area), or forget it with None."""
    try:
        _save_state("stack_area", {"area": [float(x) for x in area]} if area else {})
        return True
    except Exception as e:
        print(f"[config] Could not save the stack area: {e}")
        return False


def save_layer_focus(layer, position):
    """Remember the lens position that reads `layer`, or forget them all with
    layer None. Returns False if it could not be saved."""
    try:
        data = {} if layer is None else {str(k): v for k, v in layer_focus_positions().items()}
        if layer is not None:
            data[str(int(layer))] = int(position)
        _save_state("layer_focus", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the layer focus: {e}")
        return False


# Layers per stack chosen on the operator page.
def layers_per_stack():
    """The saved layers per stack, or LAYERS_PER_STACK if none is saved."""
    try:
        return int(_state("stacking", {})["layers_per_stack"])
    except (KeyError, TypeError, ValueError):
        return LAYERS_PER_STACK


def save_layers_per_stack(layers):
    """Save the layers per stack. Returns False if it could not be saved."""
    try:
        data = dict(_state("stacking", {}))
        data["layers_per_stack"] = int(layers)
        _save_state("stacking", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the layers per stack: {e}")
        return False


# Layer-1 USB webcam on / off, chosen in Settings -> Cameras. Saved, so it
# survives a restart. Off: the USB webcam is not used at all and the iCam reads
# every layer, exactly as before the webcam was added. On: a USB webcam, when
# one is plugged in, reads layer 1. LAYER1_WEBCAM_ENABLED (above) stays the
# master switch: with it False the webcam is never used, whatever is saved here.
def layer1_webcam_use():
    """True if the operator wants the USB webcam to read layer 1 (default on)."""
    value = _state("cameras", {}).get("layer1_webcam")
    return True if value is None else bool(value)


def save_layer1_webcam_use(on):
    """Save the layer-1 webcam choice. Returns False if it could not be saved."""
    try:
        data = dict(_state("cameras", {}))
        data["layer1_webcam"] = bool(on)
        _save_state("cameras", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the layer-1 webcam setting: {e}")
        return False


# Layer-1 webcam focus found in Settings -> USB webcam focus. Saved, and used
# every time the webcam opens (over LAYER1_WEBCAM_CONFIG['focus']).
def layer1_webcam_focus():
    """The saved webcam focus value, or LAYER1_WEBCAM_CONFIG['focus'] (None:
    the lens is left where it is)."""
    value = _state("cameras", {}).get("layer1_webcam_focus")
    try:
        return int(value) if value is not None else LAYER1_WEBCAM_CONFIG.get('focus')
    except (TypeError, ValueError):
        return LAYER1_WEBCAM_CONFIG.get('focus')


def save_layer1_webcam_focus(value):
    """Save the webcam focus (None forgets it). Returns False if it could not be saved."""
    try:
        data = dict(_state("cameras", {}))
        if value is None:
            data.pop("layer1_webcam_focus", None)
        else:
            data["layer1_webcam_focus"] = int(value)
        _save_state("cameras", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the layer-1 webcam focus: {e}")
        return False


# Kegs per layer chosen on the operator page. Saved, so a restart does not fall
# back to DEFAULT_KEG_COUNT (2026-11-03: set to 3, back to 6 after a restart,
# and a 3-keg layer could never complete).
def kegs_per_layer():
    """The saved kegs per layer, or DEFAULT_KEG_COUNT if none is saved."""
    try:
        return max(MIN_KEG_COUNT, min(MAX_KEG_COUNT,
                                      int(_state("stacking", {})["kegs_per_layer"])))
    except (KeyError, TypeError, ValueError):
        return DEFAULT_KEG_COUNT


def save_kegs_per_layer(count):
    """Save the kegs per layer. Returns False if it could not be saved."""
    try:
        data = dict(_state("stacking", {}))
        data["kegs_per_layer"] = int(count)
        _save_state("stacking", data)
        return True
    except Exception as e:
        print(f"[config] Could not save the kegs per layer: {e}")
        return False


# Screen theme chosen with the sidebar button: "light" (default) or "dark".
# Read once at start-up by ui_pyside/theme.py; a change applies on restart.
UI_THEMES = ("light", "dark")


def ui_theme():
    theme = _state("ui", {}).get("theme")
    return theme if theme in UI_THEMES else "light"


def save_ui_theme(theme):
    """Save the theme for the next start. Returns False if it could not be saved."""
    if theme not in UI_THEMES:
        return False
    try:
        with _state_lock:
            ui = _state("ui", {})
            ui["theme"] = theme
            _save_state("ui", ui)
        return True
    except Exception as e:
        print(f"[config] Could not save the theme: {e}")
        return False


# Screen language (ui_pyside/i18n.py). ui.language is the language the screen
# starts in (the last one chosen); operator_languages remembers each operator's
# own choice (operator id, else name), applied when they sign in.
def ui_language(operator_key=None):
    """The device's language, or an operator's own choice ("" if they have none)."""
    if operator_key is None:
        return str(_state("ui", {}).get("language") or "en")
    return str(_state("operator_languages", {}).get(str(operator_key), ""))


def save_ui_language(code, operator_key=None):
    """Save `code` as the device language, and as this operator's if given."""
    try:
        with _state_lock:
            ui = _state("ui", {})
            ui["language"] = code
            _save_state("ui", ui)
            if operator_key:
                ops = _state("operator_languages", {})
                ops[str(operator_key)] = code
                _save_state("operator_languages", dict(list(ops.items())[-200:]))
        return True
    except Exception as e:
        print(f"[config] Could not save the language: {e}")
        return False


# When each operator last signed in on this camera (local time, ISO). Shown as
# a small line on the sign-in page. Keys: "id:<operator id>", "email:<email>",
# "name:<name>". Times only, never a PIN or password.
def last_login(key):
    """ISO time of the last sign-in for `key`, or ""."""
    return str(_state("last_logins", {}).get(str(key), ""))


def record_login(keys):
    """Stamp now as the last sign-in for every key in `keys`."""
    from datetime import datetime
    with _state_lock:
        data = _state("last_logins", {})
        now = datetime.now().isoformat(timespec="seconds")
        for k in keys:
            if k:
                data[str(k)] = now
        if len(data) > 300:                        # keep the file small
            data = dict(sorted(data.items(), key=lambda kv: kv[1])[-300:])
        try:
            _save_state("last_logins", data)
        except Exception as e:
            print(f"[config] Could not save the sign-in time: {e}")


# The day the operator last set or confirmed the batch number. On a new day the
# app suggests the next number (batch step + a bar on the dashboard). The
# operator decides; the batch never changes by itself.
BATCH_DAY_SUGGEST = True        # False = no new-day suggestion at all


def batch_day():
    """Return (batch, "YYYY-MM-DD") last confirmed by the operator, or ("", "")."""
    d = _state("batch_day", {})
    return str(d.get("batch", "")), str(d.get("day", ""))


def save_batch_day(batch):
    """Record that the operator set or confirmed `batch` today."""
    from datetime import date
    try:
        _save_state("batch_day", {"batch": str(batch or "").strip(),
                                  "day": date.today().isoformat()})
    except Exception as e:
        print(f"[config] Could not save the batch day: {e}")


def batch_is_from_earlier_day(batch):
    """Return the "YYYY-MM-DD" `batch` was last confirmed on, if before today.

    Returns "" otherwise, including when nothing is recorded yet.
    """
    from datetime import date
    if not BATCH_DAY_SUGGEST:
        return ""
    saved, day = batch_day()
    if not day or not batch or saved != str(batch).strip():
        return ""
    return day if day < date.today().isoformat() else ""


def auth_url(path):
    """Full URL for a camera-auth path, on the same host as every other call."""
    return f"{PALLET_API_BASE_URL}{API_PREFIX}{path}"


def stack_close_url():
    return f"{PALLET_API_BASE_URL}{API_PREFIX}{STACK_CLOSE_PATH}"


def stacks_url(mac_id=None):
    """This camera's stacks (same MAC rules as blacklist_url())."""
    mac = _normalize_mac(mac_id if mac_id is not None else CAMERA_MAC_ID)
    path = STACKS_PATH.format(mac_id=quote(mac, safe=':'))
    return f"{PALLET_API_BASE_URL}{API_PREFIX}{path}"


def stack_url(stack_id, mac_id=None):
    """One stack with its layers: GET /api/camera/{mac}/stacks/{stackId}."""
    return f"{stacks_url(mac_id)}/{quote(str(stack_id), safe='')}"


def stack_report_url():
    return f"{PALLET_API_BASE_URL}{API_PREFIX}{STACK_REPORT_PATH}"


def camera_password():
    """The stored camera password, or "" when none has been set yet."""
    import os
    env = os.environ.get("KEG_CAMERA_PASSWORD", "").strip()
    if env:
        return env
    try:
        if CAMERA_CREDENTIALS_FILE.exists():
            with open(CAMERA_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # A password belongs to one camera. If it was saved for another
            # MAC (a disk image copied from another device, or the MAC was
            # changed), don't send it: the cloud would only reject it.
            stored = parse_mac(data.get("macId", ""))
            if stored and CAMERA_MAC_ID and stored != CAMERA_MAC_ID:
                print(f"[config] {CAMERA_CREDENTIALS_FILE.name} is for camera {stored}, "
                      f"not {CAMERA_MAC_ID}; ignoring the stored password")
                return ""
            return str(data.get("password", "")).strip()
    except Exception as e:
        print(f"[config] Could not read {CAMERA_CREDENTIALS_FILE.name}: {e}")
    return ""


def save_camera_password(password, mac_id=None):
    """Store the camera password, readable by this user only.

    Saved together with the MAC it belongs to (`mac_id`, default the current
    CAMERA_MAC_ID). Mode 0600 on the device; ignored on the Windows dev box.
    """
    import os
    password = str(password or "").strip()
    mac = parse_mac(mac_id) if mac_id is not None else CAMERA_MAC_ID
    tmp = CAMERA_CREDENTIALS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"macId": mac, "password": password}, f)
    os.replace(tmp, CAMERA_CREDENTIALS_FILE)
    try:
        os.chmod(CAMERA_CREDENTIALS_FILE, 0o600)
    except OSError:
        pass

# Blacklist offline behaviour
# The spec requires the blacklist to work without the cloud, so every successful
# fetch is cached to disk and reused when the cloud is unreachable. The cache
# only ever holds real cloud responses; there is no dummy blacklist source.
BLACKLIST_OFFLINE_FALLBACK = True                               # serve the cache when the cloud fails
BLACKLIST_CACHE_FILE       = BASE_DIR / "blacklist_cache.json"  # last known good cloud response

# Site / line / camera position identity
# Per the Checkology "camera positions in a keg washing line" spec, every read
# position reports its location, line and camera number to the cloud.
# This app is the position 3 (pallet) camera. The same software runs on every
# pallet camera, so LOCATION_NAME and LINE_NUMBER are edited by the operator in
# Settings, saved to the "device" section of DEVICE_STATE_FILE and loaded over
# these defaults on startup. Don't edit them here for a deployment; set them
# once in the UI.

# Code defaults, used only until the operator saves values in Settings.
# By default (AUTO) CAMERA_NUMBER follows LINE_NUMBER using the spec's Cam3.X
# convention: the pallet camera on line N is "Cam3.N". The operator can turn
# AUTO off and type a custom camera number in Settings.
LOCATION_NAME      = "Brewery-Main"   # site / location name (set in Settings)
LINE_NUMBER        = 1                # washing/filling line number (set in Settings)
CAMERA_POSITION    = 3                # 1=infeed · 2=monoblock · 3=pallet position (fixed: this is the pallet app)
CAMERA_NUMBER_AUTO = True             # True → derive camera number from line; False → manual override
CAMERA_NUMBER      = f"Cam{CAMERA_POSITION}.{LINE_NUMBER}"   # → "Cam3.1"

# Beer type: operator preference
# Not identity, but stored in the same file because it is also set in Settings
# and must survive a restart. This reuses the existing load/save code instead of
# adding another state file.
#   BEER_TYPE_ENABLED  False -> no selector, and no beer keys in the payload.
#   BEER_TYPE_SELECTED the chosen name (the cloud matches on name); "" = none.
BEER_TYPE_ENABLED  = False
BEER_TYPE_SELECTED = ""

# Operator: who is on the line
# Set in Settings and stored in the same file as the beer type, since the
# operator picks once a shift and it must survive a restart.
# Both blank = no operator selected, and no operator key is sent.
# The cloud matches on the id. The name is kept too, so the UI and Activity Log
# can show who without a second lookup, and so there is still something to send
# if the cloud only ever gave us a name.
OPERATOR_SELECTED_ID   = ""
OPERATOR_SELECTED_NAME = ""


def _auto_camera_number():
    """Camera number derived from position + line: Cam<pos>.<line> (e.g. Cam3.1)."""
    return f"Cam{CAMERA_POSITION}.{LINE_NUMBER}"


def current_identity():
    """Return the device identity the cloud payload should use now."""
    return {
        "location_name":      LOCATION_NAME,
        "line_number":        LINE_NUMBER,
        "camera_position":    CAMERA_POSITION,
        "camera_number_auto": CAMERA_NUMBER_AUTO,
        "camera_number":      CAMERA_NUMBER,
        "beer_type_enabled":  BEER_TYPE_ENABLED,
        "beer_type_selected": BEER_TYPE_SELECTED,
        "operator_selected_id":   OPERATOR_SELECTED_ID,
        "operator_selected_name": OPERATOR_SELECTED_NAME,
    }


def save_operator_pref(selected_id=None, selected_name=None):
    """Save the operator picked in Settings and update the live values.

    The next pallet report uses it without a restart. Pass both as "" to clear
    the selection (then no operator key is sent). Written through
    save_device_identity() so that function stays the only writer of the file.
    """
    global OPERATOR_SELECTED_ID, OPERATOR_SELECTED_NAME
    if selected_id is not None:
        OPERATOR_SELECTED_ID = str(selected_id).strip()
    if selected_name is not None:
        OPERATOR_SELECTED_NAME = str(selected_name).strip()
    return save_device_identity()


def current_operator():
    """(id, name) for the payload; ("", "") when nobody is selected."""
    return OPERATOR_SELECTED_ID, OPERATOR_SELECTED_NAME


def save_beer_type_pref(enabled=None, selected=None):
    """Save the beer-type toggle and selection and update the live values.

    The next pallet report uses them without a restart. Written through
    save_device_identity() so that function stays the only writer of the file.
    """
    global BEER_TYPE_ENABLED, BEER_TYPE_SELECTED
    if enabled is not None:
        BEER_TYPE_ENABLED = bool(enabled)
    if selected is not None:
        BEER_TYPE_SELECTED = str(selected).strip()
    return save_device_identity()


def load_device_identity():
    """Apply the saved identity (DEVICE_STATE_FILE, "device") over the code defaults.

    Called once at import so saved values survive restarts. Keeps the defaults
    if nothing is saved or the saved values are corrupt.
    """
    global LOCATION_NAME, LINE_NUMBER, CAMERA_POSITION, CAMERA_NUMBER_AUTO, CAMERA_NUMBER
    global BEER_TYPE_ENABLED, BEER_TYPE_SELECTED
    global OPERATOR_SELECTED_ID, OPERATOR_SELECTED_NAME
    try:
        data = _state("device", {})
        if data:
            BEER_TYPE_ENABLED  = bool(data.get("beer_type_enabled", BEER_TYPE_ENABLED))
            BEER_TYPE_SELECTED = str(data.get("beer_type_selected", BEER_TYPE_SELECTED)).strip()
            OPERATOR_SELECTED_ID   = str(data.get("operator_selected_id", OPERATOR_SELECTED_ID)).strip()
            OPERATOR_SELECTED_NAME = str(data.get("operator_selected_name", OPERATOR_SELECTED_NAME)).strip()
            LOCATION_NAME      = str(data.get("location_name", LOCATION_NAME)).strip() or LOCATION_NAME
            LINE_NUMBER        = int(data.get("line_number", LINE_NUMBER))
            CAMERA_POSITION    = int(data.get("camera_position", CAMERA_POSITION))
            CAMERA_NUMBER_AUTO = bool(data.get("camera_number_auto", CAMERA_NUMBER_AUTO))
            if CAMERA_NUMBER_AUTO:
                CAMERA_NUMBER = _auto_camera_number()
            else:
                # manual value; fall back to the auto value if the file omitted it
                CAMERA_NUMBER = str(data.get("camera_number", CAMERA_NUMBER)).strip() or _auto_camera_number()
    except Exception as e:  # never let a bad file stop the app from starting
        print(f"[config] Could not load the device identity: {e}")
    return current_identity()


def save_device_identity(location_name=None, line_number=None,
                         camera_number=None, camera_auto=None,
                         camera_position=None):
    """Save identity changes (DEVICE_STATE_FILE, "device") and update the live values.

    The next cloud payload uses them without a restart.

    Camera number rules:
      - camera_auto=True:  number follows the line number (Cam3.<line>).
      - camera_auto=False: camera_number is used as given (manual override).
    Returns the new identity dict. Raises ValueError on invalid input.
    """
    global LOCATION_NAME, LINE_NUMBER, CAMERA_POSITION, CAMERA_NUMBER_AUTO, CAMERA_NUMBER

    if location_name is not None:
        name = str(location_name).strip()
        if not name:
            raise ValueError("Location name cannot be empty")
        LOCATION_NAME = name
    if line_number is not None:
        LINE_NUMBER = int(line_number)
        if LINE_NUMBER < 1:
            raise ValueError("Line number must be 1 or greater")
    if camera_position is not None:
        CAMERA_POSITION = int(camera_position)
    if camera_auto is not None:
        CAMERA_NUMBER_AUTO = bool(camera_auto)

    if CAMERA_NUMBER_AUTO:
        CAMERA_NUMBER = _auto_camera_number()           # follow the line number
    elif camera_number is not None:
        val = str(camera_number).strip()
        if not val:
            raise ValueError("Camera number cannot be empty (or enable Auto from line)")
        CAMERA_NUMBER = val                              # manual override
    # manual mode with no new value supplied: keep the existing CAMERA_NUMBER

    identity = current_identity()
    _save_state("device", identity)
    return identity


# Camera MAC (set on the Camera Setup screen)
_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def parse_mac(text):
    """A typed MAC in the cloud's form ("3C:6D:66:2C:D8:16"), or "" if invalid.

    Accepts colons, dashes, dots, spaces or no separators, in any case. Rejects
    the all-zero and broadcast addresses.
    """
    hexdigits = re.sub(r"[\s:\-.]", "", str(text or "")).upper()
    if not re.fullmatch(r"[0-9A-F]{12}", hexdigits):
        return ""
    mac = ":".join(hexdigits[i:i + 2] for i in range(0, 12, 2))
    if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
        return ""
    return mac


def is_valid_mac(mac):
    return bool(_MAC_RE.match(str(mac or ""))) and parse_mac(mac) == mac


# Interfaces never offered as the camera's MAC (virtual, wireless, USB gadget).
_SKIP_IFACE_PREFIXES = ("lo", "docker", "veth", "br", "virbr", "l4tbr", "usb",
                        "rndis", "wl", "wlan", "tun", "tap", "can", "dummy")


def detect_device_mac():
    """(mac, iface) of this device's wired Ethernet port, or ("", "").

    Only a SUGGESTION for the setup screen; it is never used without the
    cloud check there. Reads /sys/class/net (Linux / Jetson). Prefers
    CAMERA_MAC_IFACE, then the first physical non-wireless interface.
    Skips locally administered (random) and multicast addresses. Returns
    ("", "") on Windows / macOS: uuid.getnode() is not used because it can
    pick any adapter, which is how the wrong MAC reached the cloud before.
    """
    net = Path("/sys/class/net")
    if not net.is_dir():
        return "", ""

    def _read(iface):
        try:
            mac = parse_mac((net / iface / "address").read_text().strip())
        except OSError:
            return ""
        if not mac:
            return ""
        first = int(mac[:2], 16)
        if first & 0x02 or first & 0x01:     # locally administered / multicast
            return ""
        return mac

    try:
        names = sorted(p.name for p in net.iterdir())
    except OSError:
        return "", ""
    candidates = [CAMERA_MAC_IFACE] if CAMERA_MAC_IFACE in names else []
    candidates += [n for n in names
                   if n != CAMERA_MAC_IFACE
                   and not n.startswith(_SKIP_IFACE_PREFIXES)
                   and (net / n / "device").exists()          # physical hardware
                   and not (net / n / "wireless").exists()]
    for iface in candidates:
        mac = _read(iface)
        if mac:
            return mac, iface
    return "", ""


def _credentials_mac():
    """macId stored next to the camera password (camera_credentials.json), or ""."""
    try:
        if CAMERA_CREDENTIALS_FILE.exists():
            with open(CAMERA_CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                return parse_mac(json.load(f).get("macId", ""))
    except Exception:
        pass
    return ""


def load_camera_mac():
    """Set CAMERA_MAC_ID / CAMERA_MAC_SOURCE. Called once at import.

    Order: KEG_CAMERA_MAC env -> saved ("camera" section of DEVICE_STATE_FILE)
    -> migration from camera_credentials.json (devices set up before the setup
    screen existed: their MAC was saved there with the camera password)
    -> "" (setup screen). Never raises.
    """
    global CAMERA_MAC_ID, CAMERA_MAC_SOURCE
    try:
        env = os.environ.get("KEG_CAMERA_MAC", "").strip()
        if env:
            mac = parse_mac(env)
            if mac:
                CAMERA_MAC_ID, CAMERA_MAC_SOURCE = mac, "env"
                print(f"[config] Camera MAC {mac} (from KEG_CAMERA_MAC)")
                return mac
            print(f"[config] KEG_CAMERA_MAC={env!r} is not a valid MAC; ignored")

        mac = parse_mac(_state("camera", {}).get("mac_id", ""))
        if mac:
            CAMERA_MAC_ID, CAMERA_MAC_SOURCE = mac, "saved"
            print(f"[config] Camera MAC {mac} (saved on this device)")
            return mac

        mac = _credentials_mac()
        if mac:
            CAMERA_MAC_ID, CAMERA_MAC_SOURCE = mac, "migrated"
            try:
                save_camera_mac(mac, source="migrated")
            except Exception as e:
                print(f"[config] Could not save the migrated camera MAC: {e}")
            print(f"[config] Camera MAC {mac} (taken from {CAMERA_CREDENTIALS_FILE.name})")
            return mac
    except Exception as e:  # never let a bad file stop the app from starting
        print(f"[config] Could not load the camera MAC: {e}")
    CAMERA_MAC_ID, CAMERA_MAC_SOURCE = "", ""
    print("[config] Camera MAC not set — the Camera Setup screen will be shown")
    return ""


def save_camera_mac(mac, source="setup"):
    """Save the verified camera MAC to DEVICE_STATE_FILE ("camera" section).

    Only the setup screen calls this, after the cloud accepted the MAC.
    Modules copy CAMERA_MAC_ID at import, so the app restarts after a change.
    Raises ValueError for an invalid MAC, OSError if the file can't be written.
    """
    global CAMERA_MAC_ID, CAMERA_MAC_SOURCE
    mac = parse_mac(mac)
    if not mac:
        raise ValueError("Not a valid MAC address")
    import time as _time
    previous = _state("camera", {}).get("mac_id", "")
    _save_state("camera", {"mac_id": mac, "source": source,
                           "saved_at": _time.strftime("%Y-%m-%d %H:%M:%S")})
    if previous and previous != mac:
        print(f"[config] Camera MAC changed {previous} -> {mac}")
    CAMERA_MAC_ID = mac
    if CAMERA_MAC_SOURCE != "env":
        CAMERA_MAC_SOURCE = "saved" if source == "setup" else source
    return mac


def camera_mac_configured():
    return bool(CAMERA_MAC_ID)


# Apply any saved overrides on top of the code defaults at import time.
load_device_identity()
load_camera_mac()
API_TIMEOUT = 10
API_MAX_RETRIES = 3
# The cloud API has a valid public certificate, so verification is on: keg data
# and the paletteId a label is printed from should not travel over an
# unauthenticated connection. The plain-HTTP test box is unaffected.
SSL_VERIFY = True
# The pallet-report spec defines no 'hash' field. Leave this off unless the
# cloud team adds one.
ENABLE_PAYLOAD_HASH = False

# Cloud keg-count sync
# Pulls the target keg count/type for this camera from the cloud.
#
# Disabled: no current-config route is deployed on any host. /api/current-config
# and every /api/camera/{mac}/config|settings|info variant returns a framework
# 404 page, so leaving it on only floods the log with "Cloud sync failed: HTTP
# 404". The operator's keg count from the UI is used instead.
# Turn back on once the cloud team ships the route (and fix the path below).
CLOUD_SYNC_ENABLED    = False
CLOUD_CONFIG_ENDPOINT = f"{PALLET_API_BASE_URL}{API_PREFIX}{CURRENT_CONFIG_PATH}"

# Cloud data: loaded on the loading screen, then kept fresh
# Local data (DB + caches) is wiped on every exit, so the blacklist, beer types
# and cloud config are fetched on the loading screen alongside the model load
# and are ready when the dashboard opens. (The sign-in page fetches operators
# itself.) This is the longest the loading screen waits for them once the
# camera is up; anything still pending is picked up by the first refresh.
CLOUD_PRELOAD_TIMEOUT_SECONDS = 15
# Blacklist, beer types and cloud config are re-fetched this often, so a cloud
# change mid-shift reaches the line within seconds. Skipped while the network is
# known to be down (the cached copy stays in use) and run at once when it comes
# back. A fetch in flight is never doubled up, and an unchanged result does not
# touch the screen, cache file or log, so a short interval stays cheap.
CLOUD_REFRESH_SECONDS = 3


# Pallet labels under the camera
# A finished pallet carries its printed label, whose QR holds only the pallet
# ID (e.g. 2026-10-000052-pallette). Without this the camera would count that
# label as a keg. A code is treated as a pallet label, drawn grey and never
# counted, when it matches PALLET_ID_PATTERN (case-insensitive) or is a pallet
# ID this camera already knows (Batch History, print queue, confirmed pallets).
# Set PALLET_ID_PATTERN = "" to rely on the known IDs only, and
# IGNORE_PALLET_LABELS = False to count every code as before.
IGNORE_PALLET_LABELS = True
# Stack labels (2026-11-000019-stack) are left out the same way.
PALLET_ID_PATTERN = r"^\d{4}-\d{2}-\d{6}-(pallette|stack)$"

DEFAULT_KEG_COUNT = 6
MAX_KEG_COUNT = 20
MIN_KEG_COUNT = 1
STABILITY_THRESHOLD = 3

# Stacking. 1 = pallets are not stacked, and nothing below applies.
LAYERS_PER_STACK = 1
MAX_LAYERS_PER_STACK = 6
# Layers are counted in the order they are sent: when a layer has its kegs it is
# sent, and the next kegs are the next layer, up to the layers per stack.
# Optional height check: a layer's QR boxes must look at least this much bigger
# than the layer below, or the operator is asked whether it is a new stack. A
# 2-layer stack under a camera 3.2 m up gives about 1.24 (30 L kegs) to 1.44
# (50 L). 0 (the default) turns the check and its question off.
LAYER_SIZE_RATIO = 0
# Sizes varying by more than this over recent rounds mean the pallet is moving.
LAYER_SIZE_TOLERANCE = 0.08
# Longest a moving reading holds up AUTO before the layer is judged anyway.
LAYER_SIZE_SETTLE_SECONDS = 5
# Nothing in view for this long with a stack open: remind the operator to finish
# it. Only a reminder, because a forklift can block the view for a while.
STACK_CLEAR_SECONDS = 15

# Automatic focus per layer. Each layer is a different distance from a camera
# at a fixed height, and one lens position is sharp over only ~20 steps (about
# one layer), so the lens follows the stack:
#   - QR boxes in view but not read for LAYER_FOCUS_AFTER_S while a layer is
#     being scanned: the camera focuses on the codes by itself (~5-15 s), and
#     remembers that lens position for that layer number;
#   - when the next layer is due, the lens goes straight to the position
#     remembered for it (about a second), so from the second stack on there
#     is no search at all.
# The start-up focus saved on the Camera Focus card is not changed.
LAYER_AUTO_FOCUS = True
LAYER_FOCUS_AFTER_S = 1.0
LAYER_FOCUS_RETRY_S = 10      # at most one search this often while codes stay unread
# Each layer up is closer to the camera, so its lens position is lower by about
# the same amount. When a layer is done, the lens moves this many steps for the
# next one at once, even on the first stack (no search). Measured on the can rig
# 2026-11-03: floor 1006-1029, one layer up 992-1015, so about -14. Once two
# layers are learned their real difference is used instead. Measure it again
# for real kegs (CHECK FOCUS FOR LAYERS shows each layer's sharp range).
LAYER_FOCUS_STEP = -14
# A layer search starts at the expected position and glides only this far
# either side of it (the full search glides CAMERA_FOCUS_LOCAL_WINDOW): about
# half the time. Nothing found there: the full sweep runs as before.
CAMERA_LAYER_FOCUS_WINDOW = 60
# Before that search: a quick check around the remembered or expected position,
# CAMERA_LAYER_QUICK_STEP steps from -SPAN to +SPAN, stopping as soon as every
# code reads (about 0.3 s a step: 1-3 s). The motor lands a few steps off and a
# layer reads over only ~10 steps, so a remembered position usually needs just
# this. Keep the step below that ~10.
CAMERA_LAYER_QUICK_SPAN = 20
CAMERA_LAYER_QUICK_STEP = 5
# A layer search looks only at codes inside the area where the last layer
# stood (the next one goes on top of it), widened by this share of its size.
# Floor, crate edges and other kegs outside it are left out. No code inside:
# all codes in view, as before.
LAYER_FOCUS_AREA_MARGIN = 0.3
# A layer with some of its kegs read and no new one for this long: the others
# are too soft for even a QR box to be found, so search then too.
LAYER_FOCUS_STALL_S = 4.0
# At most this many focus searches per layer and keg count, whatever started
# them. More does not help when the codes are sharp but unreadable (glare on a
# shiny label, a damaged code: 2026-11-04) or a keg is missing; the operator is
# told to check the labels instead.
LAYER_FOCUS_STALL_TRIES = 2
# Codes too soft for even a QR box to be found start no trigger above. So when
# the picture changes (something put down: a mean grey-level change of
# SCENE_CHANGE_LEVEL on a 64x36 thumbnail), then holds still (changes below
# SCENE_STILL_LEVEL for 2 s), and nothing reads, one focus search runs for
# that scene. An empty floor has no codes: that search ends at once.
SCENE_CHANGE_LEVEL = 10
SCENE_STILL_LEVEL  = 3
# Area pass: while a layer is incomplete, YOLO also looks at the stack's area
# cut from the full 4K frame, every ZOOM_AREA_EVERY-th detection round, so the
# small labels of the bottom layer (farthest from the camera) are found too.
# 1 = every round (most reads, half the round rate), 0 = off.
ZOOM_AREA_EVERY = 2
# True: an area round looks at the area only, not the whole picture as well, so
# every round is one YOLO pass (~125 ms on the iCam-540) instead of the area
# rounds being two. The rounds in between still see the whole picture, so a keg
# set down outside the area is found within one round. Only with
# ZOOM_AREA_EVERY >= 2. False = whole picture plus area on area rounds.
ZOOM_AREA_ONLY = False          # 2026-11-11: back to the original (upper layers flickered / did not focus)
# The layer search is the QR focus search (glide, then close in until the code
# reads; 6-9 s on the iCam-540), started from the position remembered or
# predicted for the layer. The readable zone of one height is only ~10 lens
# steps wide (log 2026-11-03 16:05: 1011-1020), so a coarse stepped scan misses it.

# Model Configuration
QR_MODEL_PATH = MODELS_DIR / "model_qr" / "best.pt"
# TensorRT version of the same best.pt, converted on the camera (see
# TENSORRT_STEPS.md). The live detector uses it only if the file exists and a
# test run passes at start-up; otherwise it loads best.pt.
# Manual capture (modules/advanced.py) always uses best.pt, because an engine
# accepts only the input size it was built for (YOLO_IMGSZ).
USE_TENSORRT_ENGINE = True
QR_ENGINE_PATH = MODELS_DIR / "model_qr" / "best.engine"

# Live pipeline: 4K decode, downscaled display
# The camera streams 4K for QR decode accuracy. The live preview is rendered
# smaller so the UI thread never uploads a full 4K frame. This keeps the feed
# smooth while decoding stays at full 4K in the background.
PREVIEW_WIDTH  = 1280          # live-preview width (downscaled from 4K just for display)
PREVIEW_HEIGHT = 720           # live-preview height
DETECT_MIN_INTERVAL = 0.10     # min seconds between background detections (~10 Hz); each
                               # detection still runs on the full 4K frame
YOLO_IMGSZ = 1280              # YOLO inference size, large enough to localize small/distant
                               # QR codes in a 4K frame (default 640 loses them)

# Live pipeline: load / heat reduction
# Each switch below can be turned off on its own. None of them touch the
# CUDA / torch / OpenCV install.
#
# Idle mode: after DETECT_IDLE_AFTER seconds with no QR box from YOLO, detect
# every DETECT_IDLE_INTERVAL seconds instead of every DETECT_MIN_INTERVAL. The
# first box seen returns to full rate, so a new layer costs at most one idle
# interval of extra latency.
DETECT_IDLE_ENABLED  = True
DETECT_IDLE_AFTER    = 5.0     # s with no YOLO box before slowing down
DETECT_IDLE_INTERVAL = 0.25    # s between detections while idle (4 Hz). 0.5 made a
                               # newly placed keg wait up to half a second on-device.
# Also idle while nothing changes: every code in view read, the same codes and
# the same number of boxes for DETECT_IDLE_AFTER seconds (a finished layer
# waiting to be taken away, or the robot fetching the next keg). A new box, a
# new code or a label that does not read returns to full rate at once.
# False = idle only when YOLO sees no box at all.
DETECT_IDLE_WHEN_STEADY = False # 2026-11-11: back to the original behaviour
# Full-frame 4K pyzbar + OpenCV scan when YOLO decodes nothing. On the live
# stream it would run on every empty-conveyor round (the most expensive CPU work
# in the app) and almost never finds what YOLO + crop decoding missed. It stays
# on for the saved-frame paths (short-layer advanced detection, manual capture).
LIVE_FULLFRAME_FALLBACK = False
# Hard labels: on the live stream, try the plain decode plus one enhancement per
# round (upscale, CLAHE, sharpen, binary, rotating) instead of all four, so one
# stubborn label cannot stretch every round. Each enhancement is still tried once
# every 4 rounds (~0.4 s at 10 Hz).
# Off: measured on the iCAM-540, crop decoding is ~15 ms of a ~140 ms round (YOLO
# is ~125 ms), so this saved little and made hard labels read slower / flicker.
LIVE_ONE_ENHANCEMENT_PER_ROUND = False

# QR reading on each YOLO box: zxing-cpp first (fast, copes with rotation and
# inverted labels), then the pyzbar chain above if zxing reads nothing.
# False = pyzbar chain only, exactly as before. Needs: pip install zxing-cpp
QR_DECODER_ZXING = True
# zxing again on a 2x enlarged copy when a crop up to this size (px, longer
# side) is not read: small or slightly blurred codes read far more often.
# 0 = off.
QR_ZXING_UPSCALE_MAX_PX = 600
# Serial matching (modules/serial_match.py): a YOLO box that no decoder could
# read is compared with the QR pattern of every serial the brewery's labels can
# carry, and accepted only when one serial fits clearly better than all others.
# The serial shapes (e.g. CHECKOLOGY-2026- + 4 digits) and the label form are
# learned from labels read normally; only stems starting with one of the
# brewery's keg-ID prefixes from the cloud count. Runs only after every decoder
# failed; until a keg label has been read normally it does nothing.
# False = behaviour exactly as before.
QR_SERIAL_MATCH           = True
SERIAL_MATCH_MIN_VISIBLE  = 40     # informative squares visible before a match is tried
SERIAL_MATCH_MAX_MISMATCH = 0.25   # highest share of compared squares the best may disagree on
SERIAL_MATCH_MIN_GAP      = 25     # squares by which the runner-up must be worse
# The three values above are starting points from simulation; tune them only on
# measured real labels, never to make one label pass.
# Live-stream cost control. One attempt takes tens of ms (on a PC ~30-40 ms:
# locating the code is most of it), and an unreadable label stays under the
# camera for many rounds, so:
SERIAL_MATCH_PER_ROUND    = 1      # new attempts per detection round, at most
SERIAL_MATCH_RETRY_S      = 1.0    # a box that did not match is retried after this
SERIAL_MATCH_REVERIFY_S   = 3.0    # a matched box, seen without a gap, is re-checked after this
SERIAL_LIST_REFRESH_S     = 300    # seconds between prefix refreshes
SERIAL_LIST_DB            = BASE_DIR / "serial_list.db"   # candidates + pre-built patterns
SERIAL_MATCH_NUMBER_MARGIN = 2000  # candidates up to the highest number seen + this
SERIAL_MATCH_LEVELS       = ("M", "H")   # error-correction levels of the labels in use
# What the labels carry (plain serial, or a web link ending in it) is learned
# from the labels the camera reads normally, so nothing needs setting. To fix it
# by hand instead, uncomment:
# SERIAL_MATCH_TEXT_FORMS = ("https://dev-keg-management.checkology-cloud.io/s/{serial}",)
# [PERF] log (modules/perf_log.py): measured camera fps (requested / driver /
# delivered), per-frame wait + convert time, frames used vs unused, detection
# rate, CPU / GPU load, CPU clock (throttling), temperatures, fan and memory.
# One block every PERF_LOG_INTERVAL_S seconds in logs/app.log, the first after
# PERF_LOG_FIRST_S. Summarise with: python tools/perf_summary.py logs/app.log
PERF_LOG_ENABLED    = True
PERF_LOG_INTERVAL_S = 60
PERF_LOG_FIRST_S    = 15

# Capture efficiency (modules/camera.py). Measured on the iCAM-540 (2026-10-16):
# converting every 4K frame cost 33.5 ms (~47% of one CPU core at 14 fps), and
# shrinking every frame for the screen preview 19.7 ms (~27% of a core).
#   CAPTURE_PREVIEW_SUBSAMPLE - the screen preview takes the nearest pixel (every
#       3rd pixel of the 4K frame for 1280x720) instead of blending pixels:
#       about half the time. Display only; detection still gets the full 4K frame.
#   CAPTURE_CONVERT_ON_DEMAND - keep the camera's raw YU12 frame and convert it
#       to a colour picture only when detection / a capture takes it (each frame
#       at most once). The preview is made from the raw frame at preview size.
#       If the camera does not deliver raw YU12, the app falls back to converting
#       every frame by itself and logs it.
# Set either to False to go back to the old behaviour.
CAPTURE_PREVIEW_SUBSAMPLE = True
CAPTURE_CONVERT_ON_DEMAND = True
# Live detection straight from the raw YU12 frame (needs CAPTURE_CONVERT_ON_DEMAND):
# YOLO gets a YOLO_IMGSZ-wide colour picture made from every k-th pixel (k = 3
# for 4K at 1280), and the QR crops are cut from the frame's full-size
# black-and-white (Y) plane, so the 4K colour conversion is no longer done on
# every detection round. Boxes are mapped back to 4K (x k). Captured pallet
# photos still use the full colour 4K frame. False = the 4K colour frame as before.
DETECT_FROM_RAW = True

# Screen refresh of the live preview (ms). 33 ≈ 30 fps; 66 ≈ 15 fps is lighter on
# the touchscreen thread if the UI still feels slow.
DISPLAY_INTERVAL_MS = 33
# White flash on screen at a layer change: when the robot arm leaves the view,
# the camera's auto exposure is still set for the dark scene and a few frames
# come out nearly white. Such a frame is not drawn; the previous picture stays,
# for at most DISPLAY_HIDE_WASHED_OUT_MAX_S, then whatever the camera sees is
# shown. Display only: detection and counting are not affected.
# False = every frame drawn, as before. The real cure is a narrower AE range
# (Settings -> Camera Exposure, e.g. AE Min 50 / AE Max 300).
DISPLAY_HIDE_WASHED_OUT = True
DISPLAY_HIDE_WASHED_OUT_MAX_S = 0.8
# Keep a QR box on screen this long after its last successful read, so a single
# missed round does not make it blink. Display only; counting is unaffected.
OVERLAY_HOLD_SECONDS = 0.6
# Red "NOT READ" tag on QR boxes found but not decoded. Off: a label at the edge
# of readability read in one round and not the next, so the tag flickered over
# the read code. Display only; counting is unaffected.
SHOW_NOT_READ_BOXES = False

# Printer configuration
# Any installed printer can print the pallet label (QR + pallet id from cloud).
PRINTER_CONFIG = {
    # "auto"    : any USB printer, nothing to set per printer. For every label the
    #             connected printer is looked up again and the label is built in
    #             the language it reports: ZPL > TSPL > ESC/POS > office printer
    #             (through CUPS). A printer whose language is not supported (EPL
    #             only, for example) is refused, and the label waits. With no USB
    #             printer, the system default printer is used if there is one.
    # "zebra"   : force raw ZPL over USB (Zebra), the behaviour before "auto"
    # "generic" : force the label image through the system/Windows/CUPS printer
    'backend':      'auto',
    # Target printer name. None/"" = use the operating system's DEFAULT printer,
    # so whatever printer the user has connected just works. Set to an exact name
    # (as shown by the OS) to force a specific printer. With "auto" it is used
    # only for labels that go through the system print service.
    'printer_name': None,
    # Labels per pallet ("auto" and "generic"; "zebra" always prints one).
    'copies':       1,
    # Label image for office / system printers (pixels). Tuned for clear scanning.
    'label_size':   (600, 760),
}

# Label print queue (modules/print_queue.py)
# Every label goes through a single print thread, one job at a time, so two
# labels never fight over the USB printer. A label that cannot print yet
# (printer unplugged, out of labels) waits in print_queue.db instead of being skipped.
# Seconds between checks while a label is waiting for the printer.
PRINT_RETRY_SECONDS = 15
# Failed print attempts (printer present but the job errored) before a label is
# marked FAILED and left for the operator to reprint. Waiting for an absent
# printer does not count as an attempt.
PRINT_MAX_ATTEMPTS = 3
# History kept in print_queue.db: printed/failed jobs older than this many days,
# or beyond the newest PRINT_KEEP_ROWS, are deleted. Unprinted jobs are never
# deleted by this.
PRINT_KEEP_DAYS = 7
PRINT_KEEP_ROWS = 500

# Batch History (batch_history.db): confirmed reports older than this many days,
# or beyond the newest BATCH_HISTORY_KEEP_ROWS, are deleted. Keg Management
# keeps the full history; this is the camera's own recent view of it.
BATCH_HISTORY_KEEP_DAYS = 30
BATCH_HISTORY_KEEP_ROWS = 5000

# GPU configuration
GPU_CONFIG = {
    'device': 0,                    # CUDA device ID (0 = first GPU)
    'half_precision': True,         # FP16 YOLO inference for best.pt (GPU only; ignored on
                                    # CPU). The TensorRT engine has its precision built in:
                                    # rebuild it as FP16 with tools/build_trt_engine.py.
                                    # Compare the [TIMING] read lines before and after.
    'memory_fraction': 0.8,         # Max GPU memory fraction to use
    'warmup_enabled': False,        # Warm up GPU at startup
    'force_cpu': False,             # keep the GPU enabled on Jetson
}

# Advanced QR Detection
TILE_SIZE = (1280, 960)
OVERLAP_RATIO = 0.2
SCALE_FACTORS = [1.0, 1.2, 1.5]
MIN_CROP_SIZE = 50
MIN_UPSCALE_SIZE = 100

# Cloud send queue
# A pallet only exists once the cloud has acknowledged it, so an undelivered
# report is never thrown away; it waits in retry_queue until it goes through.
# The numbers below control how often delivery is attempted, not whether:
# nothing here can abandon a queued pallet.
# Backoff ladder between queue sweeps, in minutes. The last entry is the cap, so
# an item that keeps failing settles at that interval instead of backing off
# indefinitely. On a network reconnect the ladder is skipped and the whole queue
# is due at once, so a restored line uploads immediately.
RETRY_BACKOFF_MINUTES = [1, 2, 4, 8, 16]
# A queued report is never dropped; on a production line losing a pallet is far
# worse than retrying forever. After this many failed attempts it is also
# flagged require_attention, so a report stuck for hours is visible to the
# operator while the queue keeps trying.
RETRY_ATTENTION_AFTER = 10
# Max reports delivered per sweep. The queue is always sent one at a time in
# creation order; this only bounds a single sweep so the monitor thread stays
# responsive to shutdown. 0 = drain the whole backlog in one sweep.
RETRY_DRAIN_LIMIT = 0


# Settings check
# A wrong value in this file must not make the line behave strangely without
# anyone knowing. validate_settings() runs once, below, at import (before any
# module copies a value): an out-of-range value is replaced by a safe default
# and the problem is recorded in CONFIG_WARNINGS, which the app writes to its
# log and shows in the Audit Log at start-up. Nothing here stops the app.
CONFIG_WARNINGS = []


def _is_number(value, minimum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    return value >= minimum


def validate_settings():
    """Replace invalid settings with safe defaults; return the warnings."""
    g = globals()
    warnings = []

    def fix(name, ok, default, why):
        if ok:
            return
        warnings.append(f"{name}={g.get(name)!r} is not valid ({why}); using {default!r}")
        g[name] = default

    fix("MIN_KEG_COUNT", _is_number(MIN_KEG_COUNT, 1, integer=True), 1,
        "must be a whole number of at least 1")
    fix("MAX_KEG_COUNT", _is_number(MAX_KEG_COUNT, g["MIN_KEG_COUNT"], integer=True),
        max(20, g["MIN_KEG_COUNT"]), "must be a whole number not below MIN_KEG_COUNT")
    lo, hi = g["MIN_KEG_COUNT"], g["MAX_KEG_COUNT"]
    fix("DEFAULT_KEG_COUNT",
        _is_number(DEFAULT_KEG_COUNT, lo, integer=True) and DEFAULT_KEG_COUNT <= hi,
        min(max(6, lo), hi), f"must be a whole number from {lo} to {hi}")
    fix("STABILITY_THRESHOLD", _is_number(STABILITY_THRESHOLD, 1, integer=True), 3,
        "must be a whole number of at least 1")
    fix("LAYER1_WEBCAM_ROTATE", LAYER1_WEBCAM_ROTATE in (0, 90, 180, 270)
        and not isinstance(LAYER1_WEBCAM_ROTATE, bool), 0, "must be 0, 90, 180 or 270")
    fix("LAYER1_WEBCAM_MIRROR", isinstance(LAYER1_WEBCAM_MIRROR, bool), False,
        "must be True or False")
    fix("API_TIMEOUT", _is_number(API_TIMEOUT, 1), 10, "must be at least 1 second")
    fix("API_MAX_RETRIES", _is_number(API_MAX_RETRIES, 0, integer=True), 3,
        "must be a whole number of at least 0")
    fix("PRINT_RETRY_SECONDS", _is_number(PRINT_RETRY_SECONDS, 1), 15,
        "must be at least 1 second")
    fix("PRINT_MAX_ATTEMPTS", _is_number(PRINT_MAX_ATTEMPTS, 1, integer=True), 3,
        "must be a whole number of at least 1")
    fix("CLOUD_REFRESH_SECONDS", _is_number(CLOUD_REFRESH_SECONDS, 1), 3,
        "must be at least 1 second")
    fix("CLOUD_PRELOAD_TIMEOUT_SECONDS", _is_number(CLOUD_PRELOAD_TIMEOUT_SECONDS, 1), 15,
        "must be at least 1 second")
    fix("CLOUD_CACHE_MAX_AGE_DAYS", _is_number(CLOUD_CACHE_MAX_AGE_DAYS, 0), 3,
        "must be 0 or more days")
    fix("KEG_LEDGER_KEEP_DAYS", _is_number(KEG_LEDGER_KEEP_DAYS, 1), 30,
        "must be at least 1 day")
    fix("RETRY_BACKOFF_MINUTES",
        isinstance(RETRY_BACKOFF_MINUTES, (list, tuple)) and len(RETRY_BACKOFF_MINUTES) > 0
        and all(_is_number(m, 1) for m in RETRY_BACKOFF_MINUTES),
        [1, 2, 4, 8, 16], "must be a list of minutes, each at least 1")

    backend = PRINTER_CONFIG.get("backend", "auto") if isinstance(PRINTER_CONFIG, dict) else None
    if not isinstance(backend, str) or backend.strip().lower() not in ("auto", "zebra", "generic"):
        warnings.append(f"PRINTER_CONFIG['backend']={backend!r} is not valid "
                        "(must be 'auto', 'zebra' or 'generic'); using 'auto'")
        if isinstance(PRINTER_CONFIG, dict):
            PRINTER_CONFIG["backend"] = "auto"

    if CAMERA_SOURCE not in ("icam", "webcam"):
        warnings.append(f"CAMERA_SOURCE={CAMERA_SOURCE!r} is not 'icam' or 'webcam'")

    # Security: these are reported, not changed, so a test set-up keeps working.
    if SSL_VERIFY is not True:
        warnings.append("SSL_VERIFY is off: cloud certificates are not checked")
    from urllib.parse import urlparse
    url = urlparse(PALLET_API_BASE_URL or "")
    if url.scheme != "https" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        warnings.append(f"Cloud API address {PALLET_API_BASE_URL!r} does not use https: "
                        "the camera sign-in token is sent unencrypted")
    return warnings


CONFIG_WARNINGS = validate_settings()
for _w in CONFIG_WARNINGS:
    print(f"[config] WARNING: {_w}")
