"""
Operator page: the keg-counting workflow (originally from the Kivy SimpleKegHMI).

    AUTO   : detect -> accumulate new kegs -> at target and stable -> capture ->
             send -> print -> reset (continuous flow, ignores sent kegs).
    MANUAL : operator presses CAPTURE, then SEND TO SERVER.

AUTO is the default and the app does not leave it by itself. MANUAL is entered
only by pressing MANUAL and left only by pressing AUTO.

"No QR decoded for 7 s" and "kegs from an existing pallet reappeared" are
reported to the operator without changing the mode. An automatic switch to
MANUAL would quietly stop auto-palletising and wait for CAPTURE presses nobody
knew were needed, possibly for the rest of the shift.

Repeat kegs are still safe in AUTO: _check_auto_trigger refuses to send a layer
holding an already-palletized keg until the operator decides what happens to it
(it pauses on `has_flagged() and not duplicate_action`, regardless of mode).
"""

import logging
import re
import threading
import time
from datetime import datetime

import cv2

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QLineEdit, QScrollArea, QFrame, QSizePolicy,
)

import config   # read/write the beer-type preference live (no restart needed)
from config import (
    MIN_KEG_COUNT, MAX_KEG_COUNT, STABILITY_THRESHOLD,
    SAVE_FOLDER, PREVIEW_WIDTH, PREVIEW_HEIGHT,
)
from modules.session_store import SessionStore, ScanState
from modules.stack_tracker import StackTracker, HEIGHT_OK, HEIGHT_ASK
from modules import frame_store, keg_layout, stack_store
from modules.utils import create_timestamp, save_last_batch, load_last_batch
# Event kinds published by the retry monitor; see APISender.drain_events().
from modules.api_sender import (EV_NETWORK_DOWN, EV_NETWORK_UP, EV_DRAIN_START,
                                EV_ITEM_SENDING, EV_ITEM_CONFIRMED,
                                EV_ITEM_REJECTED, EV_ITEM_DEFERRED,
                                EV_DRAIN_DONE, EV_STACK_SENT, EV_STACK_REJECTED,
                                EV_STACK_DEFERRED, build_stack_report)
# Event kinds published by the label print thread; see PrintQueue.drain_events().
from modules.print_queue import (EV_LABEL_PRINTED, EV_LABEL_RETRY, EV_LABEL_FAILED,
                                 EV_PRINTER_WAIT, EV_PRINTER_BACK, EV_LABEL_CHECK)

from .theme import C
from .i18n import tr, N_, notifier
from .widgets import (
    SectionHeader, StatCell, QRItem, CameraFeedWidget, FocusZonePanel, StackMap, NextStepCard,
    Card, CountCombo, ConfirmDialog, IconLabel, svg_pixmap, svg_icon, caution_note, DuplicateKegDialog, SearchableCombo,
    HeaderMeta, field_label, header_button, CONTROL_H, KegRegisterDialog, chip_button,
)
from .workers import (
    DetectWorker, BatchSubmitWorker, CloudSyncWorker,
    BlacklistFetcher, AdvancedDecodeWorker, save_frame_async, wait_for_frame_save,
    KegCheckWorker, KegFormFetcher, KegRegisterWorker, KegBulkRegisterWorker,
)

# What the duplicate-keg memory calls a keg kept on a layer of the stack being
# built (whole-stack mode): not on any pallet yet, so it can be forgotten.
ON_STACK_LABEL = "this stack (not sent yet)"

DISPLAY_INTERVAL_MS = int(getattr(config, "DISPLAY_INTERVAL_MS", 33))
# How long a detected QR box stays drawn after its last read (display only).
OVERLAY_HOLD_SECONDS = float(getattr(config, "OVERLAY_HOLD_SECONDS", 0.6))
# Preview box colours (OpenCV BGR). A keg the cloud does not know yet is drawn
# in the same fuchsia as its row in the list, so it can be found on the pallet.
BOX_BGR = (63, 185, 80)
UNREG_BOX_BGR = tuple(int(C['unreg'][i:i + 2], 16) for i in (5, 3, 1))
# A pallet label (not a keg) is drawn grey: seen, but never counted.
PALLET_BOX_BGR = tuple(int(C['slate'][i:i + 2], 16) for i in (5, 3, 1))
# A QR box YOLO found but no reader could decode: the keg the operator has to fix.
NOT_READ_BOX_BGR = tuple(int(C['red'][i:i + 2], 16) for i in (5, 3, 1))

IGNORE_PALLET_LABELS = bool(getattr(config, "IGNORE_PALLET_LABELS", True))
_pattern = getattr(config, "PALLET_ID_PATTERN", "") or ""
PALLET_ID_RE = re.compile(_pattern, re.IGNORECASE) if _pattern else None

log = logging.getLogger("ui_pyside.operator")

QR_FRESH_S = 2.5     # a keg just scanned stays highlighted in the list this long
BANNER_H = 40        # alert banners: one line, grows for a second
BANNER_BTN = 32      # their close buttons (same as the small header buttons)
# The "N kegs from previous pallets ignored" note is normal after every pallet;
# shown all the time it was one more line to read. The DO NOW card says it when
# it matters (already-sent kegs put back under the camera).
SHOW_IGNORED_NOTE = False
# A scene search (something new under the camera, no QR box found) that did
# not focus is tried again for the same scene: up to this many searches,
# SCENE_RETRY_S apart. With codes too soft for a box nothing else can start a
# focus, so one failed search left the kegs unread for good (2026-11-05,
# after a restart).
SCENE_SEARCH_TRIES = 3
SCENE_RETRY_S = 4.0
# When the lens already stands where the expected layer reads, a new scene
# with no QR box waits this long before a search: the empty pallet goes down
# first and the kegs a few seconds later, and once they are there the normal
# trigger (codes boxed, not read) focuses in 3-6 s. 2026-11-05: searches on
# the empty pallet ran 21-24 s each, three times, while the kegs arrived.
SCENE_WAIT_S = 5.0
WAITING_FOR_KEGS_TEXT = N_("Waiting for kegs...")
PLACE_KEGS_TEXT = N_("Place kegs under camera")
SEND_TO_SERVER_TEXT = N_("SEND TO SERVER")

# Seconds an already-palletized keg may sit under the camera before the inline
# flag escalates to a prominent "these kegs already made a pallet" warning.
DUP_WARN_SECONDS = 6.0

# Seconds of AUTO with nothing decoded before the operator is told. This only
# raises a banner; it does not change mode (see the module docstring).
NO_DETECT_WARN_SECONDS = 7.0

# How long a keg must be absent from the live detections before it is dropped
# from the pallet being built (SessionStore.prune_absent). Long enough to ride
# out a label that is unreadable for a frame or two (glare, shadow, partial
# occlusion), short enough that the operator doesn't notice the lag when lifting
# a keg off. Dropping on the first missed frame makes the count oscillate and
# keeps resetting the stability counter, so AUTO could capture mid-swap.
ABSENT_PRUNE_SECONDS = 2.5

# Batch number format: alphanumeric with optional dashes/underscores,
# 3-32 chars, at least one digit (e.g. BATCH-001, B2024-12).
BATCH_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9_-]{3,32}$")


def validate_batch(text):
    """Validate a batch number. Returns (ok, message).

    message is empty when ok, otherwise a readable reason for the inline
    error and the toast.
    """
    batch = (text or "").strip()
    if not batch or batch == "BATCH-":
        return False, tr("Enter Batch Number")
    if not BATCH_RE.match(batch):
        return False, tr("Invalid batch number format (e.g. BATCH-001)")
    return True, ""


# The last run of digits in a batch number (every valid one has at least one).
_LAST_NUMBER_RE = re.compile(r"(\d+)(?!.*\d)")


def next_batch(text):
    """Return the batch number after `text`.

    Adds one to the last number and keeps the zero padding: BATCH-001 ->
    BATCH-002, B2024-12 -> B2024-13, BATCH-099 -> BATCH-100.

    Returns None when there is no number, or when the result would break the
    batch format (e.g. go past 32 characters).
    """
    batch = (text or "").strip()
    m = _LAST_NUMBER_RE.search(batch)
    if not m:
        return None
    digits = m.group(1)
    bumped = str(int(digits) + 1).zfill(len(digits))
    nxt = batch[:m.start(1)] + bumped + batch[m.end(1):]
    return nxt if validate_batch(nxt)[0] else None


# Hint under CAPTURE / SEND, per mode.
AUTO_NOTE = N_("AUTO sends each pallet by itself. Use SEND only if a pallet was not "
               "sent.")
MANUAL_NOTE = N_("MANUAL: press CAPTURE, check the kegs, then SEND.")


class OperatorPage(QWidget):
    # Signals out to the main shell
    mode_changed   = Signal(str)    # "auto" | "manual"  -> TopBar
    net_changed    = Signal(bool)   # online?            -> TopBar
    activity       = Signal(str, str)  # (kind, plain text) -> Settings Recent activity
    toast          = Signal(str, str)  # (message, kind: info|success|error|warning)
    pallet_created = Signal(dict)   # cloud-confirmed pallet      -> Records page
    pallet_queued  = Signal(dict)   # captured, waiting to upload -> Records page
    records_stale  = Signal()       # queue state moved; re-read it -> Records page
    network_restored  = Signal()        # back online after an outage -> refresh cloud data
    camera_settings_requested = Signal()  # camera header icon -> light/exposure dialog
    # Focusing (Settings camera focus card runs it, modules/focus.py)
    focus_point_requested = Signal(float, float)  # tap on the feed, full-frame pixels
    # FOCUS button: the default region as fractions, and in full-frame pixels
    focus_region_requested = Signal(object, object)
    qr_focus_requested    = Signal()              # first QR codes in view since start-up
    _stack_checked        = Signal(object)        # start-up stack check result (thread)
    # layer, lens position to go to (or None), predicted position to scan around (or None)
    # layer, lens position to go to ({"step": n} = n steps from where it is) or
    # None, predicted position to search from or None, the stack's area or None
    layer_focus_requested = Signal(int, object, object, object, bool)
    # A layer was read and sent: ask where the lens is, to remember it for
    # that layer (the last stack's x, y, z), answered by layer_lens_known.
    layer_lens_query = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        # Backend components (injected after InitWorker finishes)
        self.camera = None
        self.qr_detector = None
        self.database = None
        self.api_sender = None
        self.printer = None
        self.print_queue = None          # the single label print thread
        self._labels_shown = None        # last badge count drawn
        self._detect_worker = None
        self._workers = []   # keep refs so QThreads aren't GC'd mid-run

        # Session state machine
        # The last kegs per layer the operator chose, kept across restarts.
        self.required_keg_count = config.kegs_per_layer()
        self.session = SessionStore(target_count=self.required_keg_count)
        self.is_auto_mode = True
        # The stack being built. An unfinished stack comes back after a restart.
        self.stack_tracker = StackTracker(config.layers_per_stack())
        self.session.stack_qrs = self.stack_tracker.codes()
        self._height_dialog_open = False # guards re-entry from the detection stream
        self._stack_empty_start = None   # when the view went empty with a stack open
        self._stack_empty_warned = False # reminder shown once per empty spell
        self._lower_in_view = set()      # lower-layer kegs seen beside a new layer
        self._old_in_view = set()        # kegs just sent, read again, nothing counted
        self._keg_boxes = {}             # code -> last QR box, for its cell in the layer
        self._last_stack = None          # the stack just finished, still drawn
        # Automatic focus per layer (see config.LAYER_AUTO_FOCUS)
        self._layer_focus_busy = False
        self._unread_since = None        # when boxes were first seen but not read
        self._last_layer_search = -1e9   # monotonic time of the last focus search
        self._focused_layer = None       # the layer the lens is set for
        self._focused_pos = None         # ... and the lens position it was set to
        # The stack's area (where layer 1 stands, full-frame px), kept across
        # stacks and restarts: every focus looks only there.
        self._stack_area = config.stack_area()
        self._await_layer = False        # waiting for the next layer (see _awaiting_layer)
        self._await_rebase = False
        self._await_clear = False        # a finished stack is still to be taken away
        self._layer_focus_kind = "scan"  # the request in flight: "move" or "scan"
        self._layer_focus_startup = False  # the search in flight is the start-up focus
        self._prev_layer_search = -1e9   # _last_layer_search before the one in flight
        self._search_key = None          # its (stack, layer, count) try, when counted
        self._search_tries = {}

        self.latest_qr_results = []
        self._overlay_boxes = {}         # code -> (bbox, monotonic time) for drawing
        self._keg_box_cam = {}           # code -> "icam" / "webcam": which camera gave its box
        # Pallet labels: IDs known from Batch History / confirmed pallets (the
        # print queue keeps its own), and the labels seen, to draw them grey and
        # log each one once.
        self._pallet_ids = set()
        self._pallet_labels_seen = set()
        self._no_detect_start = None
        self._no_detect_warned = False   # banner shown once until decoding resumes
        self._dup_seen_start = None      # when already-palletized kegs first appeared
        self._dup_warned = False         # escalated time-based warning already shown
        self._repeat_prompted = False    # "these kegs already made a pallet" asked once
        self._repeat_dialog_open = False # guards re-entry from the detection stream
        self.blacklist = set()           # codes the cloud flagged
        self._warned_blacklist = set()   # codes we've already toasted about
        self._net_seen_down = False      # suppresses a "reconnected" on first probe
        self.last_batch_number = load_last_batch()
        self._syncing = False
        self._blacklist_busy = False     # a blacklist fetch is in flight
        self._blacklist_entries = None   # last list applied (None = never loaded)
        self._cloud_keg_count = None     # last keg count the CLOUD gave us
        self._advanced_busy = False     # MANUAL-mode tiled decode in progress
        self._frame_saves = {}          # frame_path -> background save thread
        self._status_shown = None       # last (main, sub, color) drawn by _set_status
        # Beer type. Seeded from the saved preference so a restart comes back in
        # the same state; the catalogue itself arrives from Settings.
        self._beer_enabled = bool(config.BEER_TYPE_ENABLED)
        self._beer_types = {}           # {name: _id}
        self._beer_missing_shown = False
        # Keg registration. Each new code is checked with the cloud once, off
        # the GUI thread; an unknown keg gets a REGISTER button in the list.
        # A layer holding an unregistered keg is not sent (see
        # _registration_hold): the cloud only palletises kegs it knows, so it
        # would create the pallet short.
        self._keg_reg_enabled = bool(getattr(config, "KEG_REGISTRATION_ENABLED", False))
        self._keg_reg = {}               # code -> "registered" | "unknown" | "blocked"
        self._keg_check_queue = []       # codes waiting for a check
        self._keg_checking = set()       # codes the running check is asking about
        self._reg_hold_key = None        # the hold last announced, so it is said once
        self._keg_check_busy = False
        self._keg_check_err_logged = False
        self._keg_form_busy = False      # form fetch for the REGISTER button
        self._keg_form_codes = []        # the kegs that form fetch is for
        self._keg_reg_dialog = None      # the open registration form, if any
        self._keg_selected = set()       # unknown kegs ticked for bulk registration
        self._matched_codes = set()      # kegs found by serial matching (damaged label)
        self._unreg_codes = []           # listed kegs that can be registered, in list order
        self._keg_bulk = None            # progress of a running bulk registration
        self._gui_stats = self._new_gui_stats()   # [TIMING] GUI log line

        self._build_ui()
        # AUTO is the default on every start; only the MANUAL button leaves it.
        self._set_auto_mode()
        self._refresh_stack()
        notifier().changed.connect(lambda _c: self._rebuild_list())
        notifier().changed.connect(lambda _c: self._refresh_stack())
        # Test mode (Settings): applied now and on every change.
        try:
            from modules import test_mode
            test_mode.add_listener(self.set_test_mode)
            self.set_test_mode(test_mode.enabled())
        except Exception:
            log.exception("test mode not available")

    # UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Warning and info banners (hidden by default). Touch-sized (44 px) on
        # the HMI, an icon on every one, and the text wraps onto a second line
        # instead of being cut off.
        self._warn = QWidget()
        self._warn.setObjectName("warnBanner")
        self._warn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._warn.setMinimumHeight(BANNER_H)
        wl = QHBoxLayout(self._warn)
        wl.setContentsMargins(16, 4, 8, 4)
        wl.setSpacing(10)
        warn_icon = QLabel()
        warn_icon.setPixmap(svg_pixmap("triangle-alert", C["amber_dark"], 20))
        warn_icon.setStyleSheet("background:transparent;")
        wl.addWidget(warn_icon)
        self._warn_lbl = QLabel(tr("No QR detected — reposition the kegs, or press "
                                   "MANUAL to capture and decode a frame yourself."))
        self._warn_lbl.setObjectName("warnText")
        self._warn_lbl.setWordWrap(True)
        wl.addWidget(self._warn_lbl, 1)
        wl.addWidget(self._banner_close(lambda: self._warn.setVisible(False),
                                        C["amber_dark"], tr("Hide this warning")))
        self._warn.setVisible(False)
        root.addWidget(self._warn)

        self._info = QWidget()
        self._info.setObjectName("infoBanner")
        self._info.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._info.setMinimumHeight(BANNER_H)
        il = QHBoxLayout(self._info)
        il.setContentsMargins(16, 4, 8, 4)
        il.setSpacing(10)
        info_icon = QLabel()
        info_icon.setPixmap(svg_pixmap("info", C["ck_navy"], 20))
        info_icon.setStyleSheet("background:transparent;")
        il.addWidget(info_icon)
        self._info_lbl = QLabel(tr("Previous kegs ignored — detecting new kegs only."))
        self._info_lbl.setObjectName("infoText")
        self._info_lbl.setWordWrap(True)
        il.addWidget(self._info_lbl, 1)
        # Close hides the notice. The ignored-kegs note comes back only when
        # the number of ignored kegs changes (next pallet, history cleared).
        info_close = QPushButton()
        info_close.setIcon(svg_icon("x", C["ck_navy"], 18))
        info_close.setIconSize(QSize(18, 18))
        info_close.setFixedSize(BANNER_BTN, BANNER_BTN)
        info_close.setCursor(Qt.CursorShape.PointingHandCursor)
        info_close.setToolTip(tr("Hide this note — it shows again when more kegs are ignored"))
        info_close.setStyleSheet(
            "QPushButton { background:transparent; border:none; border-radius:4px; padding:0; }"
            f"QPushButton:hover {{ background:{C['blue_border']}; }}")
        info_close.clicked.connect(self._dismiss_info)
        il.addWidget(info_close)
        self._info_dismissed_at = None     # ignored-keg count when closed
        self._info_text = ""
        self._info.setVisible(False)
        root.addWidget(self._info)

        # New-day batch suggestion, shown when the day changes while the app is
        # running. The operator chooses; nothing changes by itself.
        self._day_bar = QWidget()
        self._day_bar.setObjectName("infoBanner")
        self._day_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._day_bar.setMinimumHeight(BANNER_H)
        dl = QHBoxLayout(self._day_bar)
        dl.setContentsMargins(16, 4, 12, 4)
        dl.setSpacing(10)
        day_icon = QLabel()
        day_icon.setPixmap(svg_pixmap("clock", C["ck_navy"], 20))
        day_icon.setStyleSheet("background:transparent;")
        dl.addWidget(day_icon)
        self._day_lbl = QLabel("")
        self._day_lbl.setObjectName("infoText")
        self._day_lbl.setWordWrap(True)
        dl.addWidget(self._day_lbl, 1)
        self._day_next_btn = header_button(tr("Change"), tr("Use the next batch number from now on"))
        self._day_next_btn.clicked.connect(self._day_use_next)
        dl.addWidget(self._day_next_btn)
        self._day_keep_btn = header_button(tr("Keep"), tr("Stay on the current batch number today"))
        self._day_keep_btn.clicked.connect(self._day_keep)
        dl.addWidget(self._day_keep_btn)
        self._day_bar.setVisible(False)
        root.addWidget(self._day_bar)
        self._day_timer = QTimer(self)             # one cheap date check a minute
        self._day_timer.setInterval(60_000)
        self._day_timer.timeout.connect(self._check_batch_day)
        self._day_timer.start()

        # Duplicate-keg banner: a keg already in a cloud-sent pallet was detected.
        self._dup = QWidget()
        self._dup.setObjectName("dupBanner")
        self._dup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._dup.setMinimumHeight(BANNER_H)
        dl = QHBoxLayout(self._dup)
        dl.setContentsMargins(16, 4, 12, 4)
        dl.setSpacing(10)
        dup_icon = QLabel()
        dup_icon.setPixmap(svg_pixmap("circle-alert", C["amber_dark"], 20))
        dup_icon.setStyleSheet("background:transparent;")
        dl.addWidget(dup_icon)
        self._dup_lbl = QLabel(tr("Keg already in another pallet — operator confirmation required."))
        self._dup_lbl.setObjectName("dupText")
        self._dup_lbl.setWordWrap(True)
        dl.addWidget(self._dup_lbl, 1)
        # Shortcut for the usual choice when a keg is re-stacked: put it on this
        # pallet and take it off the old one. The full choice (every keg named)
        # is offered again at send time.
        self._dup_move_btn = QPushButton(tr("MOVE HERE"))
        self._dup_move_btn.setObjectName("dupBtn")
        self._dup_move_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dup_move_btn.setToolTip(
            tr("Move these kegs onto the pallet being built — they are removed from "
               "their previous pallet when this one is sent"))
        self._dup_move_btn.clicked.connect(self._choose_move_duplicates)
        dl.addWidget(self._dup_move_btn)
        # Only hides the bar; it decides nothing ("OK" read like "keep them").
        self._dup_dismiss_all = QPushButton(tr("HIDE"))
        self._dup_dismiss_all.setObjectName("dupBtn")
        self._dup_dismiss_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dup_dismiss_all.setToolTip(tr("Hide this bar. Nothing is decided: the choice "
                                            "is still asked before the pallet is sent."))
        self._dup_dismiss_all.clicked.connect(self._acknowledge_dup_reminder)
        dl.addWidget(self._dup_dismiss_all)
        self._dup.setVisible(False)
        root.addWidget(self._dup)

        # Test mode banner: the whole time test mode is on, so the line is
        # never left in it by mistake. No close button on purpose.
        self._test_bar = QWidget()
        self._test_bar.setObjectName("testModeBanner")
        self._test_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._test_bar.setMinimumHeight(BANNER_H)
        self._test_bar.setStyleSheet(
            f"#testModeBanner {{ background-color: rgba(192,38,211,0.10);"
            f" border-left: 6px solid {C['unreg']}; border-bottom: 1px solid rgba(192,38,211,0.4); }}")
        tbl = QHBoxLayout(self._test_bar)
        tbl.setContentsMargins(16, 4, 12, 4)
        tbl.setSpacing(10)
        test_icon = QLabel()
        test_icon.setPixmap(svg_pixmap("sliders", C["unreg"], 20))
        test_icon.setStyleSheet("background:transparent;")
        tbl.addWidget(test_icon)
        test_lbl = QLabel(tr("TEST MODE — kegs already on a pallet are not checked. Turn it "
                             "off in Settings → Line setup before production."))
        test_lbl.setWordWrap(True)
        test_lbl.setStyleSheet(f"color:{C['unreg']}; font-size:14px; font-weight:700;"
                               f" background:transparent;")
        tbl.addWidget(test_lbl, 1)
        self._test_bar.setVisible(False)
        root.addWidget(self._test_bar)

        # Printer-not-ready banner, shown whenever the label printer is missing or not ready.
        self._printer_warn = QWidget()
        self._printer_warn.setObjectName("printerWarnBanner")
        self._printer_warn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Grows to two lines when the reason and what to do don't fit on one.
        self._printer_warn.setMinimumHeight(BANNER_H)
        pwl = QHBoxLayout(self._printer_warn)
        pwl.setContentsMargins(16, 4, 12, 4)
        pwl.setSpacing(10)
        pw_icon = QLabel()
        pw_icon.setPixmap(svg_pixmap("printer", "#EA580C", 20))
        pw_icon.setStyleSheet("background:transparent;")
        pw_icon.setFixedWidth(22)
        self._printer_warn_lbl = QLabel(tr("No printer detected — pallet labels will NOT be printed."))
        self._printer_warn_lbl.setObjectName("printerWarnText")
        self._printer_warn_lbl.setWordWrap(True)
        pw_dismiss = QPushButton(tr("Dismiss"))
        pw_dismiss.setObjectName("dupBtn")
        pw_dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        pw_dismiss.setToolTip(tr("Dismiss printer warning (it will reappear after the next pallet)"))
        pw_dismiss.clicked.connect(lambda: self._printer_warn.setVisible(False))
        pwl.addWidget(pw_icon)
        pwl.addWidget(self._printer_warn_lbl, 1)
        pwl.addWidget(pw_dismiss)
        self._printer_warn.setVisible(False)
        root.addWidget(self._printer_warn)

        # Content area: camera card (62%) | control column (38%)
        content = QWidget()
        content.setObjectName("appBg")
        cl = QHBoxLayout(content)
        cl.setContentsMargins(16, 16, 16, 16)   # same page padding as every page
        cl.setSpacing(12)
        # Left: the picture, with the batch and CAPTURE / SEND right under it
        # (the 16:9 picture leaves that height unused anyway). Right: the
        # scanned list and the stack, which on a 1280 x 800 HMI otherwise got
        # only a sliver of height.
        cam_col = QWidget()
        cam_lay = QVBoxLayout(cam_col)
        cam_lay.setContentsMargins(0, 0, 0, 0)
        cam_lay.setSpacing(12)
        cam_lay.addWidget(self._build_camera_card(), 1)
        cam_lay.addWidget(self._build_config_card(), 0)
        cl.addWidget(cam_col, 58)
        cl.addWidget(self._build_control_column(), 42)
        root.addWidget(content, 1)

        # Timers
        self._display_timer = QTimer(self)
        self._display_timer.timeout.connect(self._update_frame)

        self._net_timer = QTimer(self)
        self._net_timer.timeout.connect(self._check_network)

        # Drains the APISender event bus. Much faster than _net_timer (30 s)
        # because these are acknowledgements the operator waits on (a queued
        # pallet confirmed, the network back). The drain is a lock plus a deque
        # swap, so polling often costs nothing.
        self._cloud_timer = QTimer(self)
        self._cloud_timer.timeout.connect(self._drain_cloud_events)

        # Keeps the cached keg registration form fresh (the spec asks for at
        # least one fetch an hour), so the form still opens when offline.
        self._keg_form_timer = QTimer(self)
        self._keg_form_timer.timeout.connect(self._refresh_keg_form)

    @staticmethod
    def _banner_close(slot, color, tooltip):
        """The x at the end of an alert banner."""
        btn = QPushButton()
        btn.setIcon(svg_icon("x", color, 18))
        btn.setIconSize(QSize(18, 18))
        btn.setFixedSize(BANNER_BTN, BANNER_BTN)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            "QPushButton { background:transparent; border:none; border-radius:6px; padding:0; }"
            "QPushButton:hover { background:rgba(0,0,0,0.06); }")
        btn.clicked.connect(slot)
        return btn

    def _build_camera_card(self):
        card = Card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = SectionHeader(tr("Live Camera Feed"))
        self._live = IconLabel("dot", tr("LIVE"), C["green_dark"], size=11, icon_size=10,
                               bg="rgba(34,197,94,0.15)")
        self._live.setFixedHeight(22)     # keep it a pill, not the header's full height
        # Labels still to print; hidden while there are none.
        self._labels_badge = HeaderMeta()
        self._labels_badge.setVisible(False)
        hdr.add_right(self._labels_badge)
        hdr.add_right(self._live)
        # Camera light and exposure, in a side panel over the control column so
        # the preview stays in view (ui_pyside/camera_controls_dialog.py).
        from .camera_controls_dialog import camera_settings_icon
        self._cam_settings_btn = QPushButton()
        self._cam_settings_btn.setObjectName("headerBtn")
        icon, icon_size = camera_settings_icon()
        self._cam_settings_btn.setIcon(icon)
        self._cam_settings_btn.setIconSize(icon_size)
        self._cam_settings_btn.setFixedSize(30, 26)
        self._cam_settings_btn.setStyleSheet("padding: 0;")
        self._cam_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cam_settings_btn.setToolTip(tr("Camera light and exposure"))
        self._cam_settings_btn.clicked.connect(self.camera_settings_requested.emit)
        # FOCUS: focus on the QR codes in the default region (or the region
        # itself). Double-tapping the feed focuses on a chosen spot instead.
        self._focus_btn = header_button(
            tr("Focus"), tr("Focus the camera on the QR codes in view. Double-tap the "
                            "picture to focus on one spot instead."))
        self._focus_btn.clicked.connect(self._focus_clicked)
        hdr.add_right(self._focus_btn)
        hdr.add_right(self._cam_settings_btn)
        lay.addWidget(hdr)

        self._cam = CameraFeedWidget()
        self._cam.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._cam.setMinimumHeight(360)
        self._cam.double_tapped.connect(self._on_feed_double_tapped)
        lay.addWidget(self._cam, 1)
        # Focus zone: which heights are sharp and how many layers that covers,
        # opened and closed from the corner of the preview. Closed by default so
        # it never hides QR boxes unless asked for.
        self._zone_panel = FocusZonePanel(self._cam)
        self._zone_panel.setVisible(False)
        self._zone_btn = QPushButton(tr("Focus zone"), self._cam)
        self._zone_btn.setCheckable(True)
        self._zone_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._zone_btn.setToolTip(tr("Show or hide which heights are in focus"))
        self._zone_btn.setStyleSheet(
            "QPushButton { color:white; background:rgba(15,23,42,0.80); border:1px solid "
            "rgba(148,163,184,0.5); border-radius:6px; padding:5px 10px; font-size:11px;"
            " font-weight:700; }"
            "QPushButton:checked { background:rgba(21,128,61,0.90); border-color:#22C55E; }")
        self._zone_btn.toggled.connect(self._toggle_focus_zone)
        self._cam.set_corner_widgets(self._zone_btn, self._zone_panel)
        # Manual focus: double-tap the feed. Full-frame size of the picture on
        # screen, to turn a tap into frame pixels.
        self._frame_size = None
        # Focus on the QR codes the first time YOLO sees any (once per run).
        self._auto_focus_pending = bool(config.CAMERA_FOCUS_AUTO_QR_AT_START)
        self._marker_timer = QTimer(self)
        self._marker_timer.setSingleShot(True)
        self._marker_timer.timeout.connect(
            lambda: self._cam.set_focus_marker(None, ""))

        footer = QWidget()
        footer.setObjectName("camFooter")
        footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(10, 8, 10, 8)
        fl.setSpacing(10)
        self._stat_target = StatCell(tr("Target"), str(self.required_keg_count))
        self._stat_detected = StatCell(tr("Detected"), "0")
        self._stat_stable = StatCell(tr("Stability"), "0")
        for cell in (self._stat_target, self._stat_detected):
            fl.addWidget(cell, 1)
        self._stat_stable.setVisible(False)   # still updated, not shown
        badge_cell = QWidget()
        badge_cell.setObjectName("statTile")
        badge_cell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bcl = QVBoxLayout(badge_cell)
        bcl.setContentsMargins(12, 6, 12, 6)
        bcl.setSpacing(2)
        badge_caption = QLabel(tr("STATUS"))
        badge_caption.setObjectName("statLabel")
        badge_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_badge = QLabel(tr("Auto Mode"))
        self._cam_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_badge.setStyleSheet("color:white; font-size:15px; font-weight:700; background:transparent;")
        bcl.addWidget(badge_caption)
        bcl.addWidget(self._cam_badge)
        fl.addWidget(badge_cell, 1)
        lay.addWidget(footer)
        return card

    @Slot(bool)
    def _toggle_focus_zone(self, shown):
        if shown:
            self._refresh_focus_zone()
        self._zone_panel.setVisible(shown)
        self._zone_btn.setText(tr("Close focus zone") if shown else tr("Focus zone"))
        self._cam.set_corner_widgets(self._zone_btn, self._zone_panel)

    def _refresh_focus_zone(self):
        try:
            from modules.focus_zone import focus_zone
            self._zone_panel.set_zone(focus_zone(self.stack_tracker.layers_target))
        except Exception:
            log.exception("focus zone could not be worked out")

    def control_column(self):
        """The right-hand control column: the camera settings panel is laid
        over it, leaving the preview visible."""
        return self._control_col

    @Slot()
    def _focus_clicked(self):
        """FOCUS button: the QR codes in CAMERA_FOCUS_DEFAULT_REGION."""
        if self._frame_size is None or self._webcam_focus_blocked():
            return
        w, h = self._frame_size
        x1, y1, x2, y2 = config.CAMERA_FOCUS_DEFAULT_REGION
        self.focus_region_requested.emit((x1, y1, x2, y2), (x1 * w, y1 * h, x2 * w, y2 * h))

    # Manual focus: double-tap on the live feed
    @Slot(float, float)
    def _on_feed_double_tapped(self, fx, fy):
        if self._frame_size is None or self._webcam_focus_blocked():
            return
        w, h = self._frame_size
        self.focus_point_requested.emit(fx * w, fy * h)

    @Slot(float, float, str, object)
    def show_focus_marker(self, x, y, state, box=None):
        """Draw the manual-focus marker (Settings' focus_marker): amber around
        the double-tap while focusing, then green or red for a moment around
        what was actually focused on (the QR code's own box, or the area)."""
        if self._frame_size is None:
            return
        w, h = self._frame_size
        if box is not None:
            x1, y1, x2, y2 = box
        else:
            half = config.CAMERA_TAP_FOCUS_AREA * w / 2
            x1, y1, x2, y2 = x - half, y - half, x + half, y + half
        self._cam.set_focus_marker((x1 / w, y1 / h, x2 / w, y2 / h), state)
        if state == "busy":
            self._marker_timer.stop()
        else:
            self._marker_timer.start(1500)

    def _build_control_column(self):
        col = QWidget()
        self._control_col = col
        cv = QVBoxLayout(col)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(12)
        cv.addWidget(self._build_qr_card(), 2)
        cv.addWidget(self._build_stack_card(), 5)
        return col

    def _build_stack_card(self):
        """Everything about the stack in one place.

        The operator sets two numbers: kegs per layer and layers per stack.
        When a layer has its kegs it is sent and the next kegs are the next
        layer; when the last layer is sent the stack is complete and the next
        kegs start a new one. One line says what to do now, and the picture
        shows layers done, kegs read, and every keg in its cell.
        """
        card = Card()
        self._stack_card = card
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        hdr = SectionHeader(tr("Stack"))
        self._stack_id_lbl = QLabel("")
        self._stack_id_lbl.setStyleSheet(f"color:{C['text2']}; font-size:11px;"
                                         f" font-weight:600; background:transparent;")
        hdr.add_right(self._stack_id_lbl)
        self._clear_all_btn = header_button(
            tr("CLEAR ALL"), tr("Empty the scanned list and forget every keg remembered "
                                "from earlier layers and pallets"))
        self._clear_all_btn.clicked.connect(self._clear_all_clicked)
        self._clear_stack_btn = header_button(
            tr("CLEAR STACK"), tr("Remove every layer of this stack and start again at "
                                  "layer 1 (only before the stack is sent)"))
        self._clear_stack_btn.clicked.connect(self._clear_stack_clicked)
        hdr.add_right(self._clear_stack_btn)
        hdr.add_right(self._clear_all_btn)
        lay.addWidget(hdr)

        body = QVBoxLayout()
        body.setContentsMargins(12, 10, 12, 10)
        body.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(10)
        kcol = QVBoxLayout()
        kcol.setSpacing(4)
        kcol.addWidget(field_label(tr("Kegs per layer")))
        self._count = CountCombo(self.required_keg_count, MIN_KEG_COUNT, MAX_KEG_COUNT)
        self._count.setFixedHeight(CONTROL_H)
        self._count.setToolTip(tr("How many kegs make one layer. When this many are "
                                  "scanned the layer is sent."))
        self._count.valueChanged.connect(self._on_count_changed)
        kcol.addWidget(self._count)
        row.addLayout(kcol, 1)
        lcol = QVBoxLayout()
        lcol.setSpacing(4)
        lcol.addWidget(field_label(tr("Layers per stack")))
        self._layers = CountCombo(self.stack_tracker.layers_target, 1,
                                  config.MAX_LAYERS_PER_STACK)
        self._layers.setFixedHeight(CONTROL_H)
        self._layers.setToolTip(tr("How many layers make one stack. Choose 1 if pallets "
                                   "are not stacked."))
        self._layers.valueChanged.connect(self._on_layers_changed)
        lcol.addWidget(self._layers)
        row.addLayout(lcol, 1)
        ncol = QVBoxLayout()
        ncol.setSpacing(4)
        self._stack_lbl = field_label("")
        ncol.addWidget(self._stack_lbl)
        self._finish_btn = QPushButton(tr("NEW STACK"))
        self._finish_btn.setObjectName("ghostBtn")
        self._finish_btn.setFixedHeight(CONTROL_H)
        self._finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._finish_btn.setToolTip(tr("End this stack with the layers it has now. The "
                                       "next kegs are layer 1 of a new stack."))
        self._finish_btn.clicked.connect(self._finish_stack_clicked)
        ncol.addWidget(self._finish_btn)
        row.addLayout(ncol, 1)
        body.addLayout(row)

        # What to do now, in big words, and what comes after it.
        self._stack_hint = NextStepCard()
        body.addWidget(self._stack_hint)

        self._stack_map = StackMap()
        self._stack_map.setToolTip(tr("Left: the stack, layer 1 at the bottom. Right: the "
                                      "layer being scanned, every keg in its numbered place.")
                                   + " " + tr("Tap a layer to look at it.")
                                   + " " + tr("Tap a keg to remove it from its layer."))
        self._stack_map.set_editable(True)
        self._stack_map.keg_clicked.connect(self._stack_keg_clicked)
        # Rows keep a readable height; a tall stack scrolls instead of
        # squeezing its layers together.
        map_scroll = QScrollArea()
        map_scroll.setWidgetResizable(True)
        map_scroll.setFrameShape(QFrame.Shape.NoFrame)
        map_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        map_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        map_scroll.setWidget(self._stack_map)
        body.addWidget(map_scroll, 1)
        lay.addLayout(body, 1)
        return card

    def _build_qr_card(self):
        card = Card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        hdr = SectionHeader(tr("Scanned QR Codes"))
        self._qr_count = HeaderMeta()
        self._qr_count.set_status(tr("{n} scanned", n=0), C['blue'])
        copy_btn = header_button(tr("Copy"), tr("Copy the scanned keg IDs, one per line"))
        copy_btn.clicked.connect(self._copy_clicked)
        clr = header_button(tr("Clear"), tr("Clear scanned QR codes"))
        clr.clicked.connect(self._clear_clicked)
        hdr.add_right(self._qr_count)
        hdr.add_right(copy_btn)
        hdr.add_right(clr)
        lay.addWidget(hdr)

        # Bulk registration bar: shown only while unregistered kegs are listed.
        self._reg_bar = QFrame()
        self._reg_bar.setFixedHeight(36)
        self._reg_bar.setStyleSheet(f"QFrame {{ background:{C['pink_pale']}; }}")
        rb = QHBoxLayout(self._reg_bar)
        rb.setContentsMargins(10, 0, 10, 0)
        rb.setSpacing(8)
        self._reg_all_btn = chip_button(tr("Select all"), "square", C['unreg'])
        self._reg_all_btn.setToolTip(tr("Select every keg that is not registered"))
        self._reg_all_btn.clicked.connect(self._toggle_select_all)
        self._reg_info = QLabel()
        self._reg_info.setStyleSheet(f"color:{C['unreg']}; font-size:11px; font-weight:600;"
                                     f" background:transparent;")
        self._reg_sel_btn = chip_button(tr("Register selected"), "circle-plus", C['blue'])
        self._reg_sel_btn.setToolTip(tr("Register the selected kegs with the same details"))
        self._reg_sel_btn.clicked.connect(self._register_selected)
        rb.addWidget(self._reg_all_btn)
        rb.addWidget(self._reg_info)
        rb.addStretch()
        rb.addWidget(self._reg_sel_btn)
        self._reg_bar.setVisible(False)
        lay.addWidget(self._reg_bar)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._list_w = QWidget()
        self._list_lay = QVBoxLayout(self._list_w)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(0)
        self._list_lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._empty = QLabel(tr("No QR codes scanned yet"))
        self._empty.setObjectName("emptyLabel")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setFixedHeight(70)
        self._list_lay.addWidget(self._empty)
        self._scroll.setWidget(self._list_w)
        lay.addWidget(self._scroll, 1)
        return card

    def _build_config_card(self):
        card = Card()
        outer = QVBoxLayout(card)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Under the camera, two rows and no header, so the picture keeps its
        # height: status | mode | batch, then CAPTURE | SEND. Natural height,
        # never scrolled: the buttons are always in view.
        body = QWidget()
        cl = QVBoxLayout(body)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setSpacing(10)
        # Two equal columns in both rows: AUTO / MANUAL sits right above
        # CAPTURE, the batch right above SEND.
        top_row = QHBoxLayout()
        top_row.setSpacing(10)

        # Status card, as tall as a caption plus a control beside it.
        sc = QFrame()
        sc.setObjectName("statusCard")
        sc.setMinimumHeight(CONTROL_H + 22)
        scl = QHBoxLayout(sc)
        scl.setContentsMargins(14, 6, 14, 6)
        scl.setSpacing(12)
        self._st_icon = QLabel()
        self._st_icon.setPixmap(svg_pixmap("dot", C["text1"], 14))
        self._st_icon.setStyleSheet("background:transparent;")
        self._st_icon.setFixedWidth(20)
        self._st_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        st_box = QVBoxLayout()
        st_box.setSpacing(1)
        self._st_main = QLabel(tr(WAITING_FOR_KEGS_TEXT))
        self._st_main.setStyleSheet(f"color:{C['text1']}; font-size:14px; font-weight:700; background:transparent;")
        self._st_sub = QLabel(tr(PLACE_KEGS_TEXT))
        self._st_sub.setStyleSheet(f"color:{C['text2']}; font-size:12px; background:transparent;")
        st_box.addWidget(self._st_main)
        st_box.addWidget(self._st_sub)
        self._st_main.setWordWrap(True)
        self._st_sub.setWordWrap(True)
        scl.addWidget(self._st_icon)
        scl.addLayout(st_box, 1)
        sc.setVisible(False)
        self._status_box = sc

        # Operation mode
        mode_col = QVBoxLayout()
        mode_col.setSpacing(6)
        mode_col.addWidget(field_label(tr("Operation mode")))
        trow = QHBoxLayout()
        trow.setSpacing(0)
        self._auto_btn = QPushButton(tr("AUTO"))
        self._auto_btn.setObjectName("segAuto")
        self._auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._auto_btn.clicked.connect(self._set_auto_mode)
        self._manual_btn = QPushButton(tr("MANUAL"))
        self._manual_btn.setObjectName("segManualOff")
        self._manual_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_btn.clicked.connect(lambda: self._set_manual_mode())
        trow.addWidget(self._auto_btn, 1)
        trow.addWidget(self._manual_btn, 1)
        mode_col.addLayout(trow)
        mode_col.addStretch(1)

        # Batch number. It is stamped onto every keg in the pallet report, so the
        # operator must get it right. Kegs per layer and layers per stack are
        # on the Stack card. Sits beside the mode toggle to save a row.
        top_row.addLayout(mode_col, 1)
        bcol = QVBoxLayout()
        bcol.setSpacing(6)
        bcol.addWidget(field_label(tr("Batch number")))
        self._batch = QLineEdit(self.last_batch_number)
        self._batch.setPlaceholderText(tr("e.g. BATCH-001"))
        self._batch.setMaxLength(32)
        self._batch.setFixedHeight(CONTROL_H)
        self._batch.setToolTip(tr("Production batch applied to every keg on this pallet"))
        self._batch.textChanged.connect(self._on_batch_changed)
        brow = QHBoxLayout()
        brow.setSpacing(6)
        brow.addWidget(self._batch, 1)
        # The batch does not advance by itself; the operator moves it on here.
        self._next_batch_btn = QPushButton(tr("NEXT"))
        self._next_batch_btn.setObjectName("ghostBtn")
        self._next_batch_btn.setFixedHeight(CONTROL_H)
        self._next_batch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_batch_btn.setToolTip(tr("Move to the next batch number (e.g. BATCH-001 → BATCH-002)"))
        self._next_batch_btn.clicked.connect(self._next_batch_clicked)
        brow.addWidget(self._next_batch_btn)
        bcol.addLayout(brow)
        self._batch_err = QLabel("")
        self._batch_err.setStyleSheet(
            f"color:{C['red']}; font-size:11px; background:transparent;")
        self._batch_err.setWordWrap(True)
        self._batch_err.hide()
        bcol.addWidget(self._batch_err)
        bcol.addStretch(1)
        top_row.addLayout(bcol, 1)
        cl.addLayout(top_row)

        # Beer type: a searchable dropdown, shown only while the feature is
        # enabled in Settings. Wrapped in its own widget so toggling is one
        # setVisible() and the row leaves no gap when off.
        self._beer_wrap = QWidget()
        bl = QVBoxLayout(self._beer_wrap)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)
        bl.addWidget(field_label(tr("Beer type")))
        self._beer = SearchableCombo(placeholder=tr("Search beer type…"))
        self._beer.setFixedHeight(CONTROL_H)
        self._beer.setToolTip(" ".join((
            tr("Type to filter."),
            tr("The selected beer type is sent to the cloud with this pallet."),
            tr("Turn the feature off in Settings if this line does not report a "
               "beer type."))))
        self._beer.currentTextChanged.connect(self._on_beer_changed)
        bl.addWidget(self._beer)
        self._beer_wrap.setVisible(False)
        cl.addWidget(self._beer_wrap)

        # Capture and Send, side by side in one row (CAPTURE left, SEND right).
        btn_col = QVBoxLayout()
        btn_col.setSpacing(8)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._capture_btn = QPushButton(tr("CAPTURE"))
        self._capture_btn.setObjectName("captureBtn")
        self._capture_btn.setEnabled(False)
        self._capture_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._capture_btn.clicked.connect(self._force_capture)
        btn_row.addWidget(self._capture_btn, 1)
        self._send_btn = QPushButton(tr(SEND_TO_SERVER_TEXT))
        self._send_btn.setObjectName("sendBtn")
        self._send_btn.setEnabled(False)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # The button asks first; AUTO mode calls _send_to_server() directly.
        self._send_btn.clicked.connect(self._send_clicked)
        btn_row.addWidget(self._send_btn, 1)
        btn_col.addLayout(btn_row)
        # Small hint under the two buttons; the text follows AUTO/MANUAL.
        self._mode_note = caution_note(tr(AUTO_NOTE), kind="info")
        self._mode_note.setVisible(False)      # the DO NOW card says when to press SEND
        btn_col.addWidget(self._mode_note)
        cl.addLayout(btn_col)

        outer.addWidget(body)
        return card

    # Backend wiring, called by MainWindow once InitWorker finishes
    def attach_backend(self, components):
        self.camera = components.get("camera")
        if hasattr(self.camera, "on_change"):
            # Layer 1 from the USB webcam (modules/camera_select.py)
            self.camera.on_change(self._on_camera_switched)
        self.qr_detector = components.get("qr_detector")
        self.database = components.get("database")
        self.api_sender = components.get("api_sender")
        self.printer = components.get("printer")
        self.print_queue = components.get("print_queue")

        # Check the label printer now, so the operator is warned up front and not
        # after a pallet is created and its label silently fails to print.
        self._check_printer_ready()
        # Labels left unprinted by the last run start printing now.
        if self.print_queue is not None:
            self.print_queue.start()

        # Load the DB-backed set of kegs already in a cloud-sent pallet so the
        # already-palletized warning works immediately and across restarts.
        self._load_used_kegs()
        self._load_pallet_ids()
        self._check_open_stack()
        # The boot put the lens at the saved start-up focus, which is whatever
        # layer was focused last (often a top layer). Send it now to where the
        # layer expected next reads (layer 1 of a new stack, or the next layer
        # of the open one), before any kegs arrive: one move, no search.
        # 2026-11-05: after a restart layer 1 was too soft for even a QR box.
        # Shortly after, so the focus control (Settings page) is attached.
        QTimer.singleShot(1500, self._focus_for_next_layer)

        if components.get("camera_ok"):
            self._log("Camera started successfully")
        else:
            self._log("Camera failed to start — feed unavailable")
            self._live.set("dot", tr("NO CAMERA"), C["red"], "rgba(239,68,68,0.12)")

        # Lane 2: detection worker
        if self.camera is not None and self.qr_detector is not None:
            # Paused while a send is in flight. _on_detections discards those
            # results, so running YOLO for them only wastes CPU. Also paused
            # while a QR / tap-to-focus search moves the lens: those frames are
            # out of focus on purpose, and kegs already counted would drop out
            # and come back, making the count jump.
            # Not paused for a scene search (something put down, no QR box,
            # nothing counted yet): there is no count to protect, and pausing
            # only kept the camera blind to kegs put down meanwhile. A keg read
            # while the lens passes is counted at once. (The detector's model
            # lock keeps the two YOLO users apart.)
            try:
                from modules.focus import qr_focus_running
            except Exception:
                def qr_focus_running():
                    return False
            self._detect_worker = DetectWorker(
                self.camera, self.qr_detector, self,
                paused=lambda: (self.session.state in (ScanState.SENDING, ScanState.SENT)
                                or (qr_focus_running() and not self._detect_during_focus())))
            self._detect_worker.detections_ready.connect(self._on_detections)
            self._detect_worker.error.connect(lambda e: log.debug("detect: %s", e))
            self._detect_worker.start()

        # Lane 3: display and periodic timers
        if self.camera is not None and hasattr(self.camera, "set_preview_size"):
            # The capture thread builds the preview; the GUI thread only draws it.
            self.camera.set_preview_size(PREVIEW_WIDTH, PREVIEW_HEIGHT,
                                         min_interval=DISPLAY_INTERVAL_MS / 1000.0 * 0.9)
        self._display_timer.start(DISPLAY_INTERVAL_MS)   # 33 ms ≈ 30 fps
        self._net_timer.start(30000)         # every 30 s
        self._cloud_timer.start(1500)        # queue/network acknowledgements
        # Blacklist and cloud config were fetched on the loading screen; use
        # them now. Anything that did not finish in time is fetched here.
        cloud = components.get("cloud_data") or {}
        if "blacklist" in cloud:
            self._update_blacklist(cloud["blacklist"])
        else:
            QTimer.singleShot(500, self.fetch_blacklist)
        if "cloud_config" in cloud:
            count, keg_type, reason = cloud["cloud_config"]
            if reason is None:
                self._on_sync_success(count, keg_type)
            else:
                self._on_sync_fail(reason)
        else:
            QTimer.singleShot(800, self.sync_cloud)
        if self._keg_reg_enabled and self.api_sender is not None:
            QTimer.singleShot(5000, self._refresh_keg_form)
            self._keg_form_timer.start(int(config.KEG_REG_FORM_REFRESH_S * 1000))
        QTimer.singleShot(1500, self._recover_batches)
        # Tell the operator right away about any backlog from a previous run,
        # before they start building the next pallet.
        QTimer.singleShot(1800, self._report_startup_backlog)

    # Lane 3: display tick
    def _update_frame(self):
        if self.camera is None:
            return
        try:
            preview, src = (self.camera.get_preview()
                            if hasattr(self.camera, "get_preview") else (None, None))
            if preview is not None:
                # Shared with the capture thread, so copy before drawing on it
                # (a 1280x720 copy, not a 4K one).
                disp = preview.copy()
                src_w, src_h = src
            else:
                # No preview yet (first frames): resize on this thread.
                ret, frame = self.camera.get_frame_no_copy()
                if not ret or frame is None:
                    return
                disp = cv2.resize(frame, (PREVIEW_WIDTH, PREVIEW_HEIGHT),
                                  interpolation=cv2.INTER_AREA)
                src_w, src_h = frame.shape[1], frame.shape[0]
            self._frame_size = (src_w, src_h)
            if not self._washed_out(disp):
                self._draw_overlays_scaled(disp, src_w, src_h)
                rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
                self._cam.set_frame_rgb(rgb)
        except Exception:
            pass
        self._process_frame()

    def _washed_out(self, disp):
        """True for a preview that suddenly turned (nearly) white, e.g. the
        camera's auto exposure overshooting when the robot arm leaves the view
        at a layer change. The previous picture then stays on screen, for at
        most DISPLAY_HIDE_WASHED_OUT_MAX_S, so a scene that really is bright
        is shown after that. Display only: detection and counting use the
        camera frames as before."""
        if not getattr(config, "DISPLAY_HIDE_WASHED_OUT", True):
            return False
        try:
            mean = float(disp[::8, ::8].mean())     # 1/64 of the pixels: ~0.1 ms
        except Exception:
            return False
        now = time.monotonic()
        last = getattr(self, "_last_good_mean", None)
        since = getattr(self, "_washed_since", None)
        max_s = float(getattr(config, "DISPLAY_HIDE_WASHED_OUT_MAX_S", 0.8))
        if (last is not None and mean >= 200 and mean - last >= 60
                and (since is None or now - since < max_s)):
            if since is None:
                self._washed_since = now
            return True
        self._washed_since = None
        self._last_good_mean = mean
        return False

    def _draw_overlays_scaled(self, disp, src_w, src_h):
        try:
            sx = disp.shape[1] / float(src_w)
            sy = disp.shape[0] / float(src_h)
            disp_w = disp.shape[1]
            font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
            keg_reg = self._keg_reg
            for qr in self._overlay_items():
                x1, y1, x2, y2 = qr["bbox"]
                x1, y1 = int(x1 * sx), int(y1 * sy)
                x2, y2 = int(x2 * sx), int(y2 * sy)
                code = qr.get("data")
                color = (PALLET_BOX_BGR if code in self._pallet_labels_seen
                         else UNREG_BOX_BGR if keg_reg.get(code) == "unknown"
                         else BOX_BGR)
                cv2.rectangle(disp, (x1, y1), (x2, y2), color, 2)
                if qr.get("data"):
                    # The full code, drawn inside its own box so labels of
                    # neighbouring kegs don't overlap. Shrunk to the box width,
                    # and wrapped onto more lines when it would get too small.
                    self._draw_label_in_box(disp, str(qr["data"]), x1, y1, x2, y2,
                                            disp_w, font, scale, thick, color)
            # Labels seen but not decoded: the operator sees which keg to fix.
            text = tr("NOT READ")
            text = text if text.isascii() else "NOT READ"   # cv2 draws ASCII only
            for bx1, by1, bx2, by2 in self._unread_overlay_boxes():
                x1, y1 = int(bx1 * sx), int(by1 * sy)
                x2, y2 = int(bx2 * sx), int(by2 * sy)
                cv2.rectangle(disp, (x1, y1), (x2, y2), NOT_READ_BOX_BGR, 3)
                self._draw_label_in_box(disp, text, x1, y1, x2, y2, disp_w, font, scale,
                                        thick, NOT_READ_BOX_BGR)
        except Exception:
            pass

    @staticmethod
    def _fit_label(text, max_w, font, max_scale, thick, min_scale=0.32):
        """Return (scale, lines) so every line of `text` fits in `max_w` px.

        Shrinks first. Below `min_scale` it wraps at '-', '_', '/' or spaces when
        possible, otherwise mid-word. Nothing is cut off."""
        def width(t, s):
            return cv2.getTextSize(t, font, s, thick)[0][0]
        tw = width(text, max_scale)
        if tw <= max_w:
            return max_scale, [text]
        s = max_scale * max_w / float(tw)
        if s >= min_scale:
            return s, [text]
        lines, cur = [], ""
        for tok in re.split(r"(?=[-_/ ])", text):
            if cur and width(cur + tok, min_scale) > max_w:
                lines.append(cur)
                cur = tok
            else:
                cur += tok
            while len(cur) > 1 and width(cur, min_scale) > max_w:
                n = len(cur) - 1
                while n > 1 and width(cur[:n], min_scale) > max_w:
                    n -= 1
                lines.append(cur[:n])
                cur = cur[n:]
        if cur:
            lines.append(cur)
        return min_scale, lines

    def _draw_label_in_box(self, disp, text, x1, y1, x2, y2, disp_w,
                           font, max_scale, thick, color):
        pad = 3
        # A very small (far-away) box still gets two readable lines instead of
        # a column of single characters; the label is centred on it.
        box_w = max(70, x2 - x1 - 2 * pad)
        scale, lines = self._fit_label(text, box_w, font, max_scale, thick)
        sizes = [cv2.getTextSize(ln, font, scale, thick) for ln in lines]
        line_h = max(th + base for (_, th), base in sizes) + 2
        block_w = max(tw for (tw, _), _ in sizes) + 2 * pad
        block_h = len(lines) * line_h + 2 * pad
        # Top of the box, centred across it; kept inside the frame.
        bx = max(0, min(x1 + (x2 - x1 - block_w) // 2, disp_w - block_w))
        by = max(0, min(y1 + 2, disp.shape[0] - block_h))
        # A solid strip in the box colour behind the text, so the code reads on
        # any background (bright keg tops, glare). Black text on a light strip,
        # white on a dark one.
        cv2.rectangle(disp, (bx, by), (bx + block_w, by + block_h), color, cv2.FILLED)
        b, g, r = color
        text_color = (0, 0, 0) if 0.299 * r + 0.587 * g + 0.114 * b > 128 else (255, 255, 255)
        for i, (ln, ((tw, th), _base)) in enumerate(zip(lines, sizes)):
            ty = by + pad + i * line_h + th
            tx = bx + (block_w - tw) // 2
            cv2.putText(disp, ln, (tx, ty), font, scale, text_color, thick, cv2.LINE_AA)

    def _overlay_items(self):
        """Boxes to draw: every code seen within OVERLAY_HOLD_SECONDS.

        Display only: holding boxes briefly stops them blinking when a round
        misses a label. Counting, pruning and sending use each round's real
        result, not this list.
        """
        now = time.monotonic()
        self._overlay_boxes = {code: (bbox, t) for code, (bbox, t)
                               in self._overlay_boxes.items()
                               if now - t <= OVERLAY_HOLD_SECONDS}
        return [{"data": code, "bbox": bbox}
                for code, (bbox, _) in self._overlay_boxes.items()]

    def _unread_overlay_boxes(self):
        """The last round's unread QR boxes, held OVERLAY_HOLD_SECONDS like the
        read ones. Not while a layer is being sent (nothing left to fix)."""
        if not getattr(config, "SHOW_NOT_READ_BOXES", False):
            return ()
        boxes, t = getattr(self, "_unread_overlay", ((), 0.0))
        if (time.monotonic() - t > OVERLAY_HOLD_SECONDS
                or self.session.state in (ScanState.SENDING, ScanState.SENT)):
            return ()
        return boxes

    # Lane 2 result handler (runs on the GUI thread)
    @Slot(object)
    def _on_detections(self, qr_list):
        t0 = time.perf_counter()
        try:
            self._handle_detections(qr_list)
        finally:
            self._count_gui_tick((time.perf_counter() - t0) * 1000, len(qr_list or []))

    def _count_gui_tick(self, ms, codes):
        """[TIMING] GUI line every 30 detection results: how long the screen
        spends on each result, and how often the scanned list is rebuilt. Also
        reports any stray top-level window (anything but the main window)."""
        g = self._gui_stats
        g['n'] += 1
        g['ms'] += ms
        g['max'] = max(g['max'], ms)
        g['codes'] += codes
        if g['n'] < 30:
            return
        n = g['n']
        wall = max(time.perf_counter() - g['started'], 0.001)
        log.info("[TIMING] GUI: %d results in %.1f s = %.1f/s | handling %.1f ms avg, "
                 "%.0f ms max | %.1f codes/result | list rebuilt %dx, %.1f ms avg",
                 n, wall, n / wall, g['ms'] / n, g['max'], g['codes'] / n,
                 g['rebuilds'], g['rebuild_ms'] / max(g['rebuilds'], 1))
        # Dialogs (ConfirmDialog etc.) are meant to be their own windows.
        main = self.window()
        stray = [w for w in QApplication.topLevelWidgets()
                 if w.isVisible() and w is not main and not isinstance(w, QDialog)]
        if stray:
            log.warning("[TIMING] GUI: %d stray window(s) open: %s", len(stray),
                        ", ".join(f"{type(w).__name__}({w.objectName() or '-'}, "
                                  f"{w.width()}x{w.height()})" for w in stray[:10]))
        self._gui_stats = self._new_gui_stats()

    @staticmethod
    def _new_gui_stats():
        return {'n': 0, 'ms': 0.0, 'max': 0.0, 'codes': 0, 'rebuilds': 0,
                'rebuild_ms': 0.0, 'started': time.perf_counter()}

    def _load_pallet_ids(self):
        """Pallet IDs from Batch History and the confirmed-pallet table."""
        from modules import batch_history
        ids = batch_history.pallet_ids()
        if self.database:
            try:
                ids |= {str(p["pallet_id"]) for p in self.database.get_pallets()
                        if p.get("pallet_id")}
            except Exception as e:
                log.warning("get_pallets for pallet IDs failed: %s", e)
        self._pallet_ids = ids
        self._log(f"Loaded {len(ids)} known pallet ID(s) — their labels are not "
                  f"counted as kegs")

    def _is_pallet_label(self, code):
        """True if `code` is a pallet label rather than a keg: it has the pallet
        ID format, or it is a pallet ID this camera already knows."""
        if not IGNORE_PALLET_LABELS or not code:
            return False
        if PALLET_ID_RE is not None and PALLET_ID_RE.match(code):
            return True
        if code in self._pallet_ids:
            return True
        pq = self.print_queue
        return bool(pq is not None and pq.knows_pallet(code))

    def _drop_pallet_labels(self, qr_list):
        """The keg codes in `qr_list`; pallet labels are left out (logged once)."""
        kegs = []
        for qr in qr_list or []:
            code = qr.get("data")
            if self._is_pallet_label(code):
                if code not in self._pallet_labels_seen:
                    if len(self._pallet_labels_seen) > 1000:
                        self._pallet_labels_seen.clear()
                    self._pallet_labels_seen.add(code)
                    self._log(f"PALLET LABEL: {code} seen under the camera — not counted")
            else:
                kegs.append(qr)
        return kegs

    def _note_matched(self, qr_list):
        """Track which kegs were found by serial matching, for the MATCHED tag
        that lets the operator check them by eye. A keg read normally later
        loses the tag. Returns True when a keg already listed changed."""
        before = set(self._matched_codes)
        for qr in qr_list or []:
            code = qr.get("data")
            if not code:
                continue
            if qr.get("source") == "serial_match":
                if code not in self._matched_codes:
                    self._log(f"MATCHED: {code} identified from the serial list "
                              f"(damaged label) — check it by eye")
                self._matched_codes.add(code)
            else:
                self._matched_codes.discard(code)
        changed = before ^ self._matched_codes
        return bool(changed & set(self.session.qr_list()))

    def _handle_detections(self, qr_list):
        self._select_layer_camera()
        self._check_start_focus(qr_list)
        # Don't mutate the session while a send is in flight
        if self.session.state in (ScanState.SENDING, ScanState.SENT):
            return
        self._check_layer_focus(qr_list)
        now = time.monotonic()
        for qr in qr_list or []:
            if qr.get("data") and qr.get("bbox"):
                self._overlay_boxes[qr["data"]] = (qr["bbox"], now)
        unread = getattr(qr_list, "unread", ())
        if unread:
            self._unread_overlay = (unread, now)
        # Everything is drawn (pallet labels in grey); only kegs go further.
        qr_list = self._drop_pallet_labels(qr_list)
        self.latest_qr_results = qr_list
        # Mark everything visible as seen first. prune_absent() measures absence
        # against these stamps, so a code reported on this tick could otherwise
        # be pruned by its own tick.
        codes = [qr.get("data") for qr in (qr_list or []) if qr.get("data")]
        self.session.touch_seen(codes)
        matched_changed = self._note_matched(qr_list)

        if qr_list:
            for code in codes:
                self._add_qr(code)
            if matched_changed:
                self._rebuild_list()
            self._note_keg_boxes(qr_list)
            self._update_zoom_area()
            self._note_stack_view(qr_list)
            # Prune before the auto trigger, not after. The trigger compares the
            # count to the target, so it must see only the kegs under the camera
            # now. A count still padded by a layer already carried away stalls
            # AUTO after every pallet.
            self._prune_absent_kegs()
            self._update_stack_map()
            # The old layer may still decode (its codes are ignored) while the
            # next one, put on top, does not: watch the area for it too.
            self._check_scene_focus()
            self._check_auto_trigger()
            self._no_detect_start = None
            self._no_detect_warned = False      # decoding is alive again
        else:
            # An empty tick is the ordinary way a layer leaves the camera, so
            # this is the branch that most needs the prune.
            self._old_in_view = set()
            self._prune_absent_kegs()
            self._check_stack_left()
            self._update_zoom_area()
            self._check_scene_focus()
            # Warn once per dry spell. Otherwise the warning would re-fire every
            # 7 s for as long as the conveyor is empty, which is most of a shift.
            if self.is_auto_mode and not self._no_detect_warned:
                if self._no_detect_start is None:
                    self._no_detect_start = datetime.now()
                elif ((datetime.now() - self._no_detect_start).total_seconds()
                      > NO_DETECT_WARN_SECONDS):
                    self._warn_no_detect()
        # Time-based "same kegs already made a pallet" warning: if
        # already-palletized kegs stay under the camera, escalate to a prominent
        # warning so the operator removes them (or accepts reusing them).
        self._update_dup_dwell()

    # Beer type
    @Slot(bool)
    def set_beer_type_enabled(self, enabled):
        """Show or hide the selector (driven by the Settings toggle)."""
        self._beer_enabled = bool(enabled)
        self._beer_wrap.setVisible(self._beer_enabled)
        if not self._beer_enabled:
            self._beer_missing_shown = False
            self._info.setVisible(False)
        self._update_beer_gate()

    @Slot(object)
    def set_beer_types(self, mapping):
        """Populate the dropdown from the catalogue, restoring the saved choice."""
        self._beer_types = dict(mapping or {})
        names = sorted(self._beer_types.keys(), key=lambda s: s.lower())
        self._beer.set_items(names)
        want = (config.BEER_TYPE_SELECTED or "").strip()
        if want and want in self._beer_types:
            self._beer.setCurrentText(want)
        self._update_beer_gate()

    def _on_beer_changed(self, text):
        """Persist the selection so it survives a restart."""
        text = (text or "").strip()
        if text and text not in self._beer_types:
            return                      # mid-typing, not a real choice yet
        if text != (config.BEER_TYPE_SELECTED or ""):
            try:
                config.save_beer_type_pref(selected=text)
            except Exception as e:
                log.warning("could not save beer type: %s", e)
        self._update_beer_gate()

    def _beer_ready(self):
        """True when the pallet may be sent as far as the beer type is concerned.

        Two "no beer type chosen" cases, and only one of them blocks:

        A catalogue is loaded but nothing is picked -> blocked. The operator can
        fix it in one click, and carrying the beer type is the point of the
        feature.

        No catalogue at all -> not blocked. Nothing can be picked, and blocking
        would stop the line over a cloud read the operator can't fix from here.
        This matches how other cloud reads degrade (blacklist uses its cache,
        operator list keeps its last copy). The report just goes without the
        beer keys, as with the feature off.

        If this blocked, a broken catalogue fetch with the toggle on would leave
        the selector empty, SEND would refuse every layer and AUTO would never
        advance: the line could not build any pallet.
        """
        if not getattr(self, "_beer_enabled", False):
            return True                 # feature off -> no block
        if not self._beer_types:
            return True                 # nothing to pick -> no block
        return bool(self._selected_beer()[0])

    def _selected_beer(self):
        """(name, id) for the payload; ('','') when the feature is off or unset."""
        if not getattr(self, "_beer_enabled", False):
            return "", ""
        name = (self._beer.currentText() or "").strip()
        if not name or name not in self._beer_types:
            return "", ""
        return name, self._beer_types.get(name, "")

    def _update_beer_gate(self):
        """Show a static banner when a beer type is required but not chosen.

        Intentionally not a toast and not setFocus(): this runs on every
        detection tick, and either would fire several times a second (toast spam
        plus focus stealing the operator can't type through).

        Driven by the catalogue and selection directly, not by _beer_ready(). A
        missing catalogue doesn't block the send, but the operator still needs
        to know pallets are going out without a beer type; keying off
        _beer_ready() would make that case silent.
        """
        if not getattr(self, "_beer_enabled", False):
            return

        if not self._beer_types:
            # Degraded, not blocked: warn, and let the line run.
            parts = (N_("No beer types loaded — pallets will be sent WITHOUT a beer "
                        "type."),
                     N_("Press REFRESH LIST under Beer Type in Settings, or disable the "
                        "beer type there."))
        elif not self._selected_beer()[0]:
            # Blocked: there is something to pick and the operator must pick it.
            parts = (N_("Select a beer type to send this pallet"),)
        else:
            if getattr(self, "_beer_missing_shown", False):
                self._beer_missing_shown = False
                self._info.setVisible(False)
            return

        if not getattr(self, "_beer_missing_shown", False):
            self._beer_missing_shown = True
            msg = " ".join(parts)
            self._info_text = msg
            self._info_lbl.setText(" ".join(tr(p) for p in parts))
            self._info.setVisible(True)
            self._log(f"Beer type: {msg}")

    def _check_auto_trigger(self):
        """Continuous auto-pallet flow.

        If any detected keg is already in a cloud-sent pallet, auto-send pauses
        until the operator confirms in a dialog. Only an all-new keg set is sent
        without interaction.
        """
        if not self.is_auto_mode:
            return
        if self.session.state != ScanState.SCANNING:
            return
        # Beer-type gate, checked before the stability loop. Inside
        # _trigger_capture (where batch validation lives) AUTO would re-enter it
        # about 3x a second; here the stability counter just doesn't advance, so
        # the operator gets one static banner and a still UI.
        if not self._beer_ready():
            self.session.stability_counter = 0
            self._update_beer_gate()
            self._stat_stable.set_value(0)
            return
        # Registration gate, same place and reason as the beer gate: the layer
        # waits, uncaptured, until every keg is registered, then AUTO carries on
        # by itself. Sending now would create the pallet without those kegs.
        if self.session.target_reached() and self._registration_hold():
            self.session.stability_counter = 0
            self._stat_stable.set_value(0)
            self._announce_registration_hold()
            return
        self._reg_hold_key = None
        # Height gate, same place again: a pallet still being lowered is not
        # captured, and one that is not above the layer below needs a decision.
        if self.session.target_reached():
            height = self.stack_tracker.check_height()
            if height != HEIGHT_OK or self._height_dialog_open:
                self.session.stability_counter = 0
                self._stat_stable.set_value(0)
                if height == HEIGHT_ASK:
                    QTimer.singleShot(0, self._ask_layer_height)
                return
        if self.session.target_reached():
            self.session.stability_counter += 1
            if self.session.stability_counter >= STABILITY_THRESHOLD:
                self.session.stability_counter = 0
                ret, frame = (self.camera.get_frame() if self.camera else (False, None))
                if ret and frame is not None and self._trigger_capture(frame):
                    if self.session.has_flagged() and not self.session.duplicate_action:
                        # Kegs are already in a cloud pallet and the operator has
                        # not decided yet. Don't auto-send; wait for sign-off.
                        self._update_dup_banner()
                        flagged = self.session.flagged_list()
                        pallets = ", ".join(sorted({p for _, p in flagged}))
                        self._log(f"AUTO PAUSED: {len(flagged)} keg(s) already in "
                                  f"pallet(s) {pallets} — awaiting operator decision")
                        self._set_status(
                            "",
                            tr("{n} keg(s) already palletized — decision needed",
                               n=len(flagged)),
                            tr("MOVE HERE on the banner, or SEND TO SERVER to choose"),
                            C["amber_dark"],
                        )
                        self.toast.emit(
                            tr("{n} keg(s) already in another pallet — "
                               "move them here or keep them on both", n=len(flagged)),
                            "warning")
                        # Leave state=READY so the operator can press SEND TO
                        # SERVER, which calls _resolve_duplicates() for the decision.
                        self._send_btn.setEnabled(True)
                    else:
                        # All kegs new, or the operator already decided what
                        # happens to the duplicates (MOVE HERE on the banner).
                        # Asking again would stall the line, so AUTO keeps going.
                        if self.session.has_flagged():
                            self._log(f"AUTO: proceeding under the operator's "
                                      f"'{self.session.duplicate_action}' decision for "
                                      f"{len(self.session.flagged_list())} keg(s)")
                        QTimer.singleShot(500, self._auto_send_and_print)
        else:
            self.session.stability_counter = 0
        self._stat_stable.set_value(self.session.stability_counter)

    # Stacking
    def _refresh_stack(self):
        """Redraw the stack controls and keep the session's stack kegs in step."""
        st = self.stack_tracker
        self.session.stack_qrs = st.codes()
        self._update_match_exclusions()
        # The layer number is in the tower and the DO NOW card; this caption
        # only says what the button does.
        text = tr("End stack early") if st.active else tr("Single pallet")
        self._stack_lbl.setText(text.upper())    # field_label captions are uppercase
        self._finish_btn.setEnabled(st.open)
        self._load_stack_record()

    def _load_stack_record(self):
        """Re-read the stack on screen from stack_history.db: on layer and
        upload events only, never per detection round."""
        st = self.stack_tracker
        sid = (st.stack_id or self._last_stack) if st.active else None
        self._stack_rec = stack_store.get_stack(sid) if sid else None
        self._update_stack_map()

    def _update_stack_map(self):
        """Redraw the stack card: the line saying what to do now, and the
        picture. Cheap: called on every detection round, from memory only."""
        st = self.stack_tracker
        s = self.session
        rec = getattr(self, "_stack_rec", None)
        finished = rec is not None and rec["status"] != stack_store.STATUS_OPEN
        if finished and (s.count() or getattr(self, "_stack_gone", False)):
            # Kegs of the next stack are coming in, or the finished stack has
            # been taken away: show layer 1 of the next one.
            rec, finished = None, False
        scanning = s.state in (ScanState.SCANNING, ScanState.READY)
        building = None
        if scanning and not finished:
            codes = s.qr_list()
            building = {"codes": codes, "expected": s.target_count,
                        "boxes": {c: self._keg_boxes[c] for c in codes if c in self._keg_boxes},
                        "flagged": set(s.flagged_used)}
        self._stack_map.set_stack(keg_layout.stack_map_data(rec, st.layers_target, building))

        cloud = (rec or {}).get("cloud_stack_id")
        report = (rec or {}).get("report_status")
        self._stack_id_lbl.setText(
            "" if not st.active else
            tr("Stack {id}", id=cloud) if cloud else
            tr("Sending to Keg Management…") if report == stack_store.REPORT_PENDING else
            tr("Refused by Keg Management") if report == stack_store.REPORT_REJECTED else
            tr("New stack") if rec is None else
            tr("Kept on the camera") if self._stack_mode() else tr("Stack id on first upload"))
        self._clear_stack_btn.setEnabled(bool(st.open and st.editable))
        self._stack_hint.set_step(*self._next_step(rec, finished))

    def _next_step(self, rec, finished):
        """(kind, title, detail) for the DO NOW card: the one thing the
        operator does now, and one short line with the step after it. kind:
        "action", "busy", "done", "warn" or "error" (NextStepCard). The one
        place on the screen that says what to do."""
        st, s = self.stack_tracker, self.session
        n, target = s.count(), s.target_count
        layer = st.next_layer
        stacked = st.active
        what = tr("Layer {n}", n=layer) if stacked else tr("Pallet")

        if self._advanced_busy:
            return "busy", tr("Decoding the picture…"), tr("Please wait.")
        if s.state == ScanState.SENDING:
            return "busy", tr("Sending {what}…", what=what.lower()), tr("Keep the kegs still.")
        if s.state == ScanState.SENT:
            if stacked and st.open:
                return "done", tr("Sent"), tr("Next: place layer {n} on top.", n=layer)
            if stacked:
                return "done", tr("Sent"), tr("Next: take the stack away.")
            return "done", tr("Sent"), tr("Next: place the next pallet.")
        hold = getattr(self, "_hold_msg", None)
        if hold:
            return hold
        if (s.has_flagged() and not s.duplicate_action and n
                and (s.state == ScanState.READY or n >= target)):
            # AUTO does not send a layer with kegs already on another pallet
            # until the operator chooses (_check_auto_trigger): say so here,
            # not "sending by itself".
            pallets = ", ".join(sorted({p for _, p in s.flagged_list()}))
            return ("warn", tr("{k} keg(s) already on pallet {p} — choose first",
                               k=len(s.flagged_list()), p=pallets),
                    tr("Press MOVE HERE on the yellow bar, or SEND TO SERVER to choose."))
        if self._old_in_view and not finished:
            return ("warn", tr("These kegs were already sent"),
                    tr("Put down new kegs, or press CLEAR ALL to count them again."))
        if finished:
            return ("done", tr("Stack complete"),
                    tr("Take the stack away, then place layer 1 of the next stack."))
        if n == 0:
            if stacked and st.open:
                return "action", tr("Place layer {n} on top", n=layer), tr(
                    "{k} kegs, labels facing up.", k=target)
            if stacked:
                return "action", tr("Place layer 1"), tr(
                    "{k} kegs on an empty pallet, labels facing up.", k=target)
            return "action", tr("Place the pallet"), tr("{k} kegs, labels facing up.",
                                                         k=target)
        if n > target:
            return ("error", tr("Too many kegs: {n} of {k}", n=n, k=target),
                    tr("Take {x} keg(s) off.", x=n - target))
        if n < target:
            return ("action", tr("{what}: {n} of {k} kegs", what=what, n=n, k=target),
                    tr("{x} more to read — check the empty places.", x=target - n))
        if self.is_auto_mode:
            return "busy", tr("{what} complete", what=what), tr("Sending by itself.")
        return "action", tr("{what} complete", what=what), tr("Press SEND.")

    # Whole-stack mode (config.PALLET_REPORT_MODE = "stack")
    def _stack_mode(self):
        """Layers are kept on the camera and the stack is sent whole."""
        return (str(getattr(config, "PALLET_REPORT_MODE", "layer")).lower() == "stack"
                and self.stack_tracker.active)

    def _hold_layer(self, codes, positions, layout):
        """The layer has its kegs: keep it on the camera, move on. Nothing goes
        to the cloud until the whole stack is done, so until then a keg can be
        removed from any layer, or the stack cleared."""
        s, st = self.session, self.stack_tracker
        st.begin_layer(s.session_id, codes, positions, keg_layout.layout_text(layout),
                       s.target_count, held=True,
                       scanned_time=s.captured_at.isoformat() if s.captured_at else None,
                       batch=s.batch_number)
        layer, sid = st.pending_layer, st.stack_id
        self._log(f"LAYER KEPT: layer {layer} of {st.layers_target} on stack {sid}: "
                  f"{len(codes)} keg(s) — sent with the whole stack: {sorted(codes)}")
        self._discard_frame(s.frame_path)
        self._stack_layer_sent()
        if not st.open and self._last_stack == sid:
            self._queue_stack_report(sid)
        else:
            self.toast.emit(tr("Layer {n} kept — place layer {next} on top",
                               n=layer, next=layer + 1), "success")
        s.mark_sent(pallet_label=ON_STACK_LABEL)
        QTimer.singleShot(1000, self._reset_session)

    def _discard_frame(self, path):
        """A kept layer needs no saved picture: delete it once it is written."""
        if not path:
            return
        save = self._frame_saves.get(path)

        def drop():
            try:
                wait_for_frame_save(save)
                import os
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                log.debug("frame %s not removed", path, exc_info=True)
        threading.Thread(target=drop, name="FrameDiscard", daemon=True).start()

    def _queue_stack_report(self, stack_id, finished_early=False):
        """The stack is done: build its report and hand it to the upload thread
        (kept in stack_history.db, so being offline or a restart loses nothing)."""
        from uuid import uuid4
        rec = stack_store.get_stack(stack_id)
        if not rec or rec.get("report_status") or not any(
                l["status"] == stack_store.LAYER_HELD for l in rec["layers"]):
            return
        beer, beer_id = self._selected_beer()
        op_id, op_name = config.current_operator()
        report = build_stack_report(rec, uuid4(), finished_early, beer, beer_id, op_id, op_name)
        codes = sum(len(l["kegIds"]) for l in report["layers"])
        if (len(report["layers"]) > int(config.STACK_REPORT_MAX_LAYERS)
                or codes > int(config.STACK_REPORT_MAX_CODES)):
            self._log(f"STACK TOO BIG: {len(report['layers'])} layer(s), {codes} code(s) — "
                      f"Keg Management takes at most {config.STACK_REPORT_MAX_LAYERS} layers "
                      f"and {config.STACK_REPORT_MAX_CODES} codes in one stack")
        stack_store.queue_report(stack_id, report["reportId"], report)
        self._log(f"STACK REPORT: {stack_id} queued — {len(report['layers'])} layer(s), "
                  f"{codes} keg(s), report {report['reportId']}"
                  f"{', finished early' if report.get('stackComplete') else ''}")
        self.toast.emit(tr("Stack complete — sending all {n} layer(s) to Keg Management",
                           n=len(report["layers"])), "info")
        self._activity("info", tr("Stack complete — sending all {n} layer(s) to Keg "
                                  "Management", n=len(report["layers"])))
        if self.api_sender:
            self.api_sender.wake_retry_monitor()
        self._load_stack_record()

    def _on_stack_sent(self, ev):
        """The cloud stored a whole stack: one pallet per layer."""
        pallets = ev.get("pallets") or []
        cloud = ev.get("cloud_id") or "?"
        rec = stack_store.get_stack(ev.get("stack_id")) or {"layers": []}
        sessions = {l["layer"]: l for l in rec["layers"]}
        self._log(f"STACK STORED BY CLOUD: {cloud} — " + ", ".join(
            f"layer {p['layer']} = {p['pallet_id']}" for p in pallets)
            + (" (resend: nothing written again)" if ev.get("duplicate") else ""))
        for p in pallets:
            l = sessions.get(p["layer"], {})
            for code in l.get("keg_ids") or []:
                # From now on a repeat of these kegs names their real pallet.
                if self.session.used_kegs.get(code) in (None, ON_STACK_LABEL):
                    self.session.used_kegs[code] = p["pallet_id"]
            self.pallet_created.emit({
                "session_id": l.get("session_id", ""),
                "pallet_id": p["pallet_id"],
                "created_at": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
                "keg_count": len(l.get("keg_ids") or []),
                "batch": l.get("batch", ""),
                "status": "CREATED",
            })
        self.toast.emit(tr("Stack {stack} stored: {n} pallet(s)", stack=cloud,
                           n=len(pallets)), "success")
        self._activity("success", tr("Stack {stack} stored in Keg Management: {n} pallet(s)",
                                     stack=cloud, n=len(pallets)))
        self.records_stale.emit()
        self._load_stack_record()

    def _forget_codes(self, codes):
        """Kegs taken off the stack before it was sent: no longer ignored or
        flagged as already on a pallet."""
        s = self.session
        codes = set(codes or ())
        s.sent_qr_history -= codes
        s.last_sent_qr_set -= codes
        for c in codes:
            if s.used_kegs.get(c) == ON_STACK_LABEL:
                s.used_kegs.pop(c, None)
            s.flagged_used.pop(c, None)

    @Slot(int, str)
    def _stack_keg_clicked(self, layer, code):
        """A keg tapped in the stack picture: remove it from its layer."""
        QTimer.singleShot(0, lambda: self._ask_remove_stack_keg(layer, code))

    def _ask_remove_stack_keg(self, layer_no, code):
        st, s = self.stack_tracker, self.session
        if layer_no == st.next_layer and code in s.qr_codes:
            self._ask_remove_qr(code)           # the layer being scanned
            return
        layer = next((l for l in st.layers if l["layer"] == layer_no), None)
        if layer is None:
            self.toast.emit(tr("This stack is finished — it can't be changed here."), "info")
            return
        if layer.get("status") != stack_store.LAYER_HELD:
            self.toast.emit(tr("Layer {n} is already in Keg Management — it can't be "
                               "changed here.", n=layer_no), "warning")
            return
        last = len(layer["keg_ids"]) == 1
        if last and layer is not st.layers[-1]:
            self.toast.emit(tr("Layer {n} needs at least one keg.", n=layer_no) + " " +
                            tr("To redo the stack, press CLEAR STACK."), "warning")
            return
        detail = (tr("Layer {n} is then empty and is scanned again.", n=layer_no) if last
                  else tr("Layer {n} keeps {k} keg(s).", n=layer_no,
                          k=len(layer["keg_ids"]) - 1))
        if not ConfirmDialog.ask(
                self, tr("Remove Keg"),
                tr("Remove keg {code} from layer {n}?", code=code, n=layer_no),
                detail + " " + tr("Take it off the pallet too, or it is not in the stack "
                                  "report."),
                confirm_text=tr("REMOVE"), danger=True):
            return
        ok, why = st.remove_keg(layer_no, code)
        if not ok:
            self.toast.emit(tr("Keg not removed — layer {n} is already in Keg Management.",
                               n=layer_no) if why == "sent" else tr("Keg not removed."),
                            "warning")
            return
        self._forget_codes({code})
        self._log(f"STACK EDIT: keg {code} removed from layer {layer_no} by the operator"
                  f"{' — the layer is gone and is scanned again' if why == 'layer' else ''}")
        self.toast.emit(tr("Keg {code} removed from layer {n}", code=code, n=layer_no),
                        "success")
        self._refresh_stack()

    @Slot()
    def _clear_stack_clicked(self):
        """CLEAR STACK: drop every layer kept on the camera, start at layer 1."""
        st = self.stack_tracker
        if not st.open:
            self.toast.emit(tr("There is no stack to clear."), "info")
            return
        if not st.editable:
            self.toast.emit(tr("Layers of this stack are already in Keg Management — they "
                               "can't be cleared here.") + " " +
                            tr("Press NEW STACK to end the stack."), "warning")
            return
        if self.session.state == ScanState.SENDING:
            return
        n, codes = len(st.layers), st.codes()
        if not ConfirmDialog.ask(
                self, tr("Clear Stack"),
                tr("Remove all {n} layer(s) and their kegs from this stack?", n=n),
                tr("Nothing of this stack has been sent yet.") + " " +
                tr("The scanned list is emptied too, and the next kegs are layer 1."),
                confirm_text=tr("CLEAR STACK"), danger=True):
            return
        sid = st.stack_id
        if not st.discard():
            self.toast.emit(tr("The stack could not be cleared."), "error")
            return
        self._forget_codes(codes | set(self.session.qr_codes))
        self.session.reset()
        self._keg_boxes = {}
        self._keg_box_cam = {}
        self._send_btn.setEnabled(False)
        self._log(f"CLEAR STACK by the operator: stack {sid} with {n} layer(s) removed "
                  f"(nothing had been sent)")
        self.toast.emit(tr("Stack cleared — place layer 1"), "success")
        self._refresh_stack()
        self._rebuild_list()


    def _check_open_stack(self):
        """A stack left open by the last run: ask the cloud whether it is still
        building (GET .../stacks?status=building&mine=true), off the GUI
        thread. If the cloud closed it meanwhile, it is closed here too."""
        st = self.stack_tracker
        if not (st.open and self.api_sender and config.PALLET_SEND_STACKS):
            return
        stack = stack_store.open_stack()
        sender = self.api_sender
        if not getattr(self, "_stack_check_wired", False):
            self._stack_checked.connect(self._on_stack_checked)
            self._stack_check_wired = True

        def run():
            try:
                status = sender.check_open_stack(stack)
            except Exception:
                log.exception("open stack check failed")
                status = None
            self._stack_checked.emit((stack["stack_id"], status))
        threading.Thread(target=run, name="Stack_Check", daemon=True).start()

    @Slot(object)
    def _on_stack_checked(self, result):
        stack_id, status = result
        st = self.stack_tracker
        if not status or st.stack_id != stack_id:
            return
        self._log(f"Stack {stack_id} was closed in Keg Management since the last run "
                  f"— closed here too; the next pallet starts a new stack")
        self._last_stack = stack_id
        st.closed_elsewhere(status)
        self.toast.emit(tr("The open stack was closed in Keg Management — the next "
                           "pallet starts a new stack"), "info")
        self._refresh_stack()

    def set_test_mode(self, on):
        """Test mode on: kegs already on a pallet are taken as new ones (no
        reminder, no MOVE / KEEP question, AUTO does not wait). Off: checked
        again from the next keg read."""
        on = bool(on)
        s = self.session
        s.ignore_repeats = on
        if on and s.flagged_used:
            # The ones already flagged count as plain kegs from now on.
            s.flagged_used.clear()
            s.move_kegs.clear()
            s.duplicate_action = ""
            self._repeat_prompted = False
            self._update_dup_banner()
            self._rebuild_list()
        self._test_bar.setVisible(on)
        self._log(f"TEST MODE {'ON' if on else 'OFF'}: kegs already on a pallet are "
                  f"{'not checked' if on else 'checked again'}")

    def _detect_during_focus(self):
        """True while the running focus search is a scene search with nothing
        counted yet: detection keeps reading then (see the DetectWorker)."""
        return (self._layer_focus_busy and getattr(self, "_layer_focus_scene", False)
                and not self.session.count())

    def _check_start_focus(self, qr_list):
        """The start-up focus, once, on the first QR codes in view.

        Waits until the area holds still: fired on the first box, it focused
        on the one keg put down first while the rest were still being placed,
        and those then needed a second search. Codes that already read need
        no focus at all (the boot put the lens at the saved focus). With stack
        tracking on it runs as the layer search, so the layer triggers know
        about it and do not collide with it."""
        if not self._auto_focus_pending or self._webcam_owns_layer():
            return
        boxes = int(getattr(self.qr_detector, "last_box_count", 0) or 0)
        if not boxes or not self._area_still() or self._layer_focus_busy:
            return
        if self.session.state != ScanState.SCANNING:
            return
        self._auto_focus_pending = False
        read = sum(1 for qr in qr_list or [] if qr.get("data"))
        if read >= boxes:
            self._log(f"START-UP FOCUS: all {boxes} code(s) in view read at the saved "
                      f"focus — no search needed")
            return
        if self._layer_focus_on():
            self._start_layer_search(f"start-up: {boxes - read} of {boxes} code(s) in view "
                                     f"not reading", limited=False, startup=True)
        else:
            self.qr_focus_requested.emit()

    # Automatic focus per layer
    def _layer_focus_on(self):
        return (bool(getattr(config, "LAYER_AUTO_FOCUS", False)) and self.stack_tracker.active
                and not self._webcam_owns_layer())

    # Layer 1 from the USB webcam (modules/camera_select.py)
    def _webcam_reads(self):
        """True while the USB webcam, not the iCam, reads the layer. Always
        False without a webcam: everything then runs exactly as before."""
        return bool(getattr(self.camera, "webcam_active", False))

    def _webcam_owns_layer(self):
        """True while the webcam reads and the layer expected next is its
        layer. The iCam's focus rules ask this, not _webcam_reads(): right
        after layer 1 is sent the layer number is already 2 while the webcam
        is still the source until the next round, and the iCam's lens must
        move ahead for layer 2 then, as it always has."""
        if not self._webcam_reads():
            return False
        st = self.stack_tracker
        return bool(self.camera.wants_webcam(st.next_layer, stacking=st.active))

    def _webcam_will_read(self, layer):
        """True when the USB webcam is there, turned on, and reads `layer`.
        False without a webcam: the iCam then reads every layer as before."""
        cam = self.camera
        if getattr(cam, "webcam", None) is None or not getattr(cam, "enabled", False):
            return False
        return bool(cam.wants_webcam(layer, stacking=self.stack_tracker.active))

    def _select_layer_camera(self):
        """Pick the camera for the layer expected now. It only changes
        between layers (no keg counted yet), so one layer's boxes all come
        from one camera."""
        if not hasattr(self.camera, "select_for_layer"):
            return
        st = self.stack_tracker
        self.camera.select_for_layer(st.next_layer, stacking=st.active,
                                     kegs_counted=self.session.count())

    def _webcam_focus_blocked(self):
        """FOCUS and double-tap drive the iCam's lens. While the USB webcam
        reads, the picture on screen is the webcam's and its lens is fixed,
        so they would focus the wrong camera on the wrong spot."""
        if not self._webcam_owns_layer():
            return False
        self._activity("info", tr("Layer 1 is read by the USB webcam, whose focus is "
                                  "fixed. Focus works again from layer 2."))
        return True

    def _on_camera_switched(self, which, reason):
        """The other camera reads now: boxes drawn from the old one are in
        its pixels, so they go; the detector looks at the whole new picture."""
        self._overlay_boxes = {}
        self._unread_since = None
        self._update_zoom_area()
        if which == "webcam":
            self._log(f"CAMERA: the USB webcam reads now ({reason})")
            self._activity("info", tr("Layer 1 is read by the USB webcam"))
        else:
            self._log(f"CAMERA: the iCam reads now ({reason})")
            if "stopped" in reason:
                self._activity("warning", tr("USB webcam stopped — the iCam reads this "
                                             "layer"))

    def _check_layer_focus(self, qr_list):
        """The layer under the camera is out of focus: the camera focuses on
        the codes by itself, at most once per LAYER_FOCUS_RETRY_S. Two signs:

          - QR boxes in view but not all read, for LAYER_FOCUS_AFTER_S;
          - the layer has some of its kegs but no new one has been read for
            LAYER_FOCUS_STALL_S. The others are there but too soft for even a
            QR box to be found, which the first sign needs (2026-11-03 16:36:
            layer 1 at 1 of 3, no box on the other two cans, nothing started).
            At most LAYER_FOCUS_STALL_TRIES searches per layer and count, so a
            can that is really missing does not keep the lens hunting.
        """
        if not self._layer_focus_on() or self._layer_focus_busy:
            self._unread_since = None
            return
        if self._awaiting_layer():
            self._unread_since = None
            return
        if not self._area_still():
            # Kegs are being put down or someone reaches in: what does not
            # read now may read in a moment. No focusing while it moves.
            self._unread_since = None
            self._stall_since = time.monotonic()
            return
        boxes = int(getattr(self.qr_detector, "last_box_count", 0) or 0)
        read = sum(1 for qr in qr_list or [] if qr.get("data"))
        n, target = self.session.count(), self.session.target_count
        short = n < target
        now = time.monotonic()
        last_tick, self._stall_tick = getattr(self, "_stall_tick", None), now
        paused = last_tick is not None and now - last_tick > 1.0   # a focus ran
        layer_key = (self.stack_tracker.stack_id, self.stack_tracker.next_layer)
        if layer_key != getattr(self, "_stall_layer", None):
            self._stall_layer, self._stall_best = layer_key, 0
            self._stall_since = now
        # Only more kegs than this layer has had so far restart the wait. The
        # count also drops and comes back as a keg flickers in and out of view
        # (log 16:06), which must not.
        if paused or n > self._stall_best:
            self._stall_since = now
        self._stall_best = max(self._stall_best, n)
        if self.session.state != ScanState.SCANNING:
            self._unread_since = None
            return
        if (0 < n < target and now - self._stall_since >= float(config.LAYER_FOCUS_STALL_S)
                and now - self._last_layer_search >= float(config.LAYER_FOCUS_RETRY_S)):
            if self._start_layer_search(
                    f"layer stuck at {n} of {target} keg(s) for "
                    f"{now - self._stall_since:.0f} s, the rest not reading"):
                return
        if not (boxes > read and short):
            self._unread_since = None
            return
        now = time.monotonic()
        last, self._unread_tick = getattr(self, "_unread_tick", None), now
        if last is not None and now - last > 1.0:
            # Detection was paused meanwhile (a focus moved the lens), so what
            # was unread before says nothing about now. 2026-11-03 16:05: the
            # start-up focus had just read the code, and a timer started
            # before it fired a second, needless 15 s search.
            self._unread_since = None
        if self._unread_since is None:
            self._unread_since = now
            layer = self.stack_tracker.next_layer
            if getattr(self, "_unread_logged", None) != layer:
                self._unread_logged = layer
                self._log(f"LAYER FOCUS: layer {layer}: {boxes - read} of {boxes} QR box(es) "
                          f"not reading — focus search in {config.LAYER_FOCUS_AFTER_S:g} s "
                          f"if it stays so")
            return
        if (now - self._unread_since < float(config.LAYER_FOCUS_AFTER_S)
                or now - self._last_layer_search < float(config.LAYER_FOCUS_RETRY_S)):
            return
        self._start_layer_search(f"{boxes - read} of {boxes} code(s) in view not reading")

    def _scene_thumb(self):
        """A tiny grey picture of the stack's area (the whole view until the
        area is known), or None. Changes elsewhere (people, a forklift passing)
        do not count as something put down."""
        try:
            cam = getattr(self.camera, "primary", self.camera)
            preview, src = (cam.get_preview() if cam is not None else (None, None))
            if preview is None:
                return None
            if self._stack_area and src:
                ph, pw = preview.shape[:2]
                sx, sy = pw / float(src[0]), ph / float(src[1])
                x1, y1, x2, y2 = self._stack_area
                x1, x2 = max(0, int(x1 * sx)), min(pw, int(x2 * sx))
                y1, y2 = max(0, int(y1 * sy)), min(ph, int(y2 * sy))
                if x2 - x1 >= 8 and y2 - y1 >= 8:
                    preview = preview[y1:y2, x1:x2]
            small = cv2.resize(preview, (64, 36), interpolation=cv2.INTER_AREA)
            return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype("int16")
        except Exception:
            return None

    def _watch_area(self):
        """Twice a second: a small grey picture of the stack's area, and how
        many checks in a row it has held still. Returns the picture when a new
        one was taken, else None. No picture available: counted as still, so
        the focus triggers are never blocked by a missing preview."""
        now = time.monotonic()
        # Twice a second: two still checks in a row (about 1 s) mean the kegs
        # are down, so a search starts about a second after they stop moving.
        if now - getattr(self, "_scene_checked", 0.0) < 0.5:
            return None
        self._scene_checked = now
        thumb = self._scene_thumb()
        if thumb is None:
            self._scene_still = 99
            return None
        last, self._scene_last = getattr(self, "_scene_last", None), thumb
        still = last is not None and float(abs(thumb - last).mean()) < float(
            config.SCENE_STILL_LEVEL)
        self._scene_still = (getattr(self, "_scene_still", 0) + 1) if still else 0
        if self._scene_still == 2:
            self._still_since = now
        elif self._scene_still < 2:
            self._still_since = None
        return thumb

    def _area_still(self):
        """True when the stack's area has not changed for 2 checks: nobody is
        putting kegs down or reaching in. The focus triggers wait for it, so a
        keg being placed (unreadable for a moment) does not start a search."""
        return getattr(self, "_scene_still", 99) >= 2

    def _check_scene_focus(self):
        """Something new was put under the camera, it has stopped moving, and
        no QR code is found at all: the codes may be too soft for even a QR
        box to be found, which every other focus trigger needs. One focus
        search per new scene (it tries the remembered lens positions, then
        nearby ones, then far ones). 2026-11-04 11:53: layer 1 on the crate,
        codes visibly soft, no box drawn, nothing ever started a focus.

        Runs every detection round; looks at the area at most once a second."""
        if not getattr(config, "LAYER_AUTO_FOCUS", False):
            return
        thumb = self._watch_area()
        if thumb is None:
            return
        if self._await_layer and getattr(self, "_await_rebase", False):
            # The scene to compare with is the finished layer as it is now
            # (blurred, the lens already moved ahead), not what came before.
            self._await_rebase = False
            self._scene_ref = thumb
            # The stack is complete and layer 1 of a new one comes next: the
            # finished stack has to be taken away first (stacks stand in the
            # same place), so the next change is that, not a layer put down.
            self._await_clear = self.stack_tracker.next_layer == 1
            return
        if (self._layer_focus_busy or self.session.state != ScanState.SCANNING
                or self.session.count()):
            return
        ref = getattr(self, "_scene_ref", None)
        # The first scene counts as new: kegs may already stand there at start-up.
        changed = ref is None or float(abs(thumb - ref).mean()) >= float(
            config.SCENE_CHANGE_LEVEL)
        if not changed or not self._area_still():
            return
        st = self.stack_tracker
        since = getattr(self, "_still_since", None)
        if (self._focused_layer == st.next_layer and since is not None
                and time.monotonic() - since < SCENE_WAIT_S):
            return        # the lens is where this layer reads: give the kegs time
        if self._await_layer and getattr(self, "_await_clear", False):
            # The finished stack has gone (stacks stand in the same place, so
            # the first change after one is complete is it being taken away):
            # its kegs stop being ignored and the wait ends. The search below
            # still runs. "No QR box" does not mean an empty floor: 2026-11-05
            # the next stack's layer 1 was put down with its codes too soft for
            # even a box, was taken for the empty floor, the search was skipped
            # and nothing ever focused. On a really empty floor the search finds
            # nothing, puts the lens back and does not hold the next one back
            # (_forgive_failed_search).
            self._await_clear = False
            self._await_layer = False
            self._log("LAYER FOCUS: the finished stack has left — looking for layer 1 "
                      "of the next stack")
            self._release_finished_stack()
        self._await_clear = False
        if time.monotonic() - self._last_layer_search < float(config.LAYER_FOCUS_RETRY_S):
            return
        # This scene gets a search, tried again if it does not focus
        # (_forgive_failed_search puts the old reference back).
        self._scene_tries = (getattr(self, "_scene_tries", 0) + 1
                             if getattr(self, "_scene_rearmed", False) else 1)
        self._scene_rearmed = False
        self._scene_prev_ref = ref
        self._scene_ref = thumb
        if self._await_layer:
            self._await_layer = False
            self._log("LAYER FOCUS: the next layer was put down — waiting over")
        self._start_layer_search("something new is under the camera and has stopped "
                                 "moving, but no QR code is found"
                                 + (f" (try {self._scene_tries} of {SCENE_SEARCH_TRIES})"
                                    if self._scene_tries > 1 else ""),
                                 limited=False, scene=True)

    def _release_finished_stack(self):
        """The finished stack has left the camera: its kegs are no longer
        silently ignored. They were ignored only because the stack usually
        still stands under the camera; once it is gone, one of them put down
        again is a real repeat. It is then counted with the amber "IN
        <pallet>" reminder like any keg already on a pallet, instead of
        being left out with nothing on screen (2026-11-05: the same four
        kegs put back as layer 1 of the next stack, 0 counted)."""
        self.stack_tracker.forget_previous()
        self.session.last_sent_qr_set.clear()
        self.session.stack_qrs = self.stack_tracker.codes()
        self._update_match_exclusions()
        self._stack_gone = True

    def _zoom_area(self):
        """Where the detector should also look closely (full-frame pixels), or
        None: while a layer is incomplete, the stack's area; on the very first
        stack, before it is known, the kegs read so far on this layer, widened
        (the missing ones stand next to them)."""
        s = self.session
        if s.state != ScanState.SCANNING or s.count() >= s.target_count:
            return None
        if self._stack_area and not self._webcam_reads():
            return self._stack_area
        boxes = [self._keg_boxes[c] for c in s.qr_codes if c in self._keg_boxes]
        if not boxes:
            return None
        x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        w, h = (x2 - x1) + 200, (y2 - y1) + 200      # at least a keg's width around
        return (x1 - 0.6 * w, y1 - 0.6 * h, x2 + 0.6 * w, y2 + 0.6 * h)

    def _update_zoom_area(self):
        if self.qr_detector is None:
            return
        area = self._zoom_area()
        if area != getattr(self.qr_detector, "zoom_area", None):
            try:
                self.qr_detector.zoom_area = area
            except Exception:
                pass

    def _awaiting_layer(self):
        """True while the camera waits for the next layer to be put down: the
        lens has already moved ahead for it, and the layer just finished, still
        under the camera, is blurred on purpose. Its codes must not start a
        search: that focused the lens back on the old layer, again and again
        while the operator fetched the next kegs (2026-11-04).

        Ends when a new keg is read, or when the stack's area changes and
        holds still (the next layer is down: _check_scene_focus). Never by
        time: however long the operator takes, nothing is searched."""
        if self._await_layer and self.session.count():
            self._await_layer = False
            self._log("LAYER FOCUS: a keg of the next layer is read — waiting over")
        return self._await_layer

    def _start_awaiting_layer(self):
        self._await_layer = True
        self._await_rebase = True
        self._await_clear = False        # set at the rebase, once the next layer is known
        self._stack_gone = False         # the finished stack still stands there
        self._unread_since = None

    def _start_layer_search(self, why, limited=True, startup=False, scene=False):
        """Ask for the focus search for the layer expected now. False when
        it has already been tried LAYER_FOCUS_STALL_TRIES times for this layer
        and keg count: focusing again does not help then.

        2026-11-04: two cans had their code printed on shiny gold foil; the
        lens was sharp (the two paper labels next to them read at 1016), the
        foil codes were washed out by glare, and the camera kept searching
        every 10 s. After the tries it stops and says what to do instead."""
        if self._webcam_owns_layer():
            return False
        n, target = self.session.count(), self.session.target_count
        key = (self.stack_tracker.stack_id, self.stack_tracker.next_layer, n)
        tries = getattr(self, "_search_tries", {})
        if limited and tries.get(key, 0) >= int(config.LAYER_FOCUS_STALL_TRIES):
            self._unread_since = None
            self._last_layer_search = time.monotonic()
            if getattr(self, "_focus_gave_up", None) != key:
                self._focus_gave_up = key
                self._log(f"LAYER FOCUS: layer {key[1]}: still {n} of {target} after "
                          f"{tries[key]} focus search(es) — focus is not the problem "
                          f"(glare on shiny labels, a damaged or covered code). Not "
                          f"searching again until a keg is read or the layer changes.")
                self._warn_lbl.setText(tr(
                    "{k} keg(s) on this layer can't be read, and focusing again does not "
                    "help.", k=max(0, target - n)) + " " + tr(
                    "Check those labels: shine or glare on the label, damaged or covered "
                    "code. Turn the keg, shade the light, or remove and register it."))
                self._warn.setVisible(True)
                QTimer.singleShot(15000, lambda: self._warn.setVisible(False))
                self._activity("warning", tr("Layer {n}: {k} keg(s) can't be read — check "
                                             "their labels", n=key[1], k=max(0, target - n)))
            return False
        self._search_tries = {k: v for k, v in tries.items() if k == key}
        self._search_tries[key] = tries.get(key, 0) + 1
        layer = self.stack_tracker.next_layer
        guess, how = config.predict_layer_focus(layer)
        if guess is None and layer == 1 and config.camera_focus_position() is not None:
            # First stack, nothing learned yet: the saved start-up focus is
            # the best known start for the quick check.
            guess, how = config.camera_focus_position(), "start-up"
        self._unread_since = None
        self._prev_layer_search = self._last_layer_search
        self._search_key = key if limited else None
        self._last_layer_search = time.monotonic()
        self._layer_focus_busy = True
        self._layer_focus_kind = "scan"
        self._layer_focus_startup = startup
        self._layer_focus_scene = scene
        self._log(f"LAYER FOCUS: layer {layer}: {why} — focus search from "
                  f"{f'lens {guess} ({how})' if guess is not None else 'the lens now'}")
        self._activity("info", tr("Focusing on layer {n}…", n=layer))
        # The remembered / expected position: a quick check around it first.
        # A scene search (no QR box in view: maybe only an empty pallet so
        # far) is short: quick check and area glide, never the 6-15 s search.
        self.layer_focus_requested.emit(layer, None, guess, self._stack_area, bool(scene))
        return True

    def _focus_for_next_layer(self):
        """The next layer is due: send the lens where it reads, before the
        layer is even put down, so no search is needed:
          the position learned for it on an earlier stack;
          else one predicted from the layers learned (two: their real step;
            one: LAYER_FOCUS_STEP per layer);
          else, on the very first stack, LAYER_FOCUS_STEP from where the lens
            is now (the layer just read there), since each layer up is closer.
        If it still does not read there, the usual triggers start a search.

        When the USB webcam will read the next layer (layer 1), the iCam's
        lens goes to the layer after it instead: it is then already sharp when
        the screen switches to the iCam, rather than moving at that moment
        (2026-11-16: a blink at layer 2 from lens 1000 -> 985 during the
        switch). Only a learned or predicted position is used for that; a
        step from where the lens is now (the previous stack's top layer)
        would be wrong, so without one the lens waits as before."""
        if not self._layer_focus_on() or self._layer_focus_busy:
            return
        layer = self.stack_tracker.next_layer
        ahead = self.stack_tracker.active and self._webcam_will_read(layer)
        if ahead:
            layer += 1
        position, how = config.predict_layer_focus(layer)
        if ahead:
            if position is None:
                return
            how = f"{how}; layer {layer - 1} is read by the USB webcam"
        elif position is None and layer > 1 and int(config.LAYER_FOCUS_STEP):
            position, how = {"step": int(config.LAYER_FOCUS_STEP)}, "one layer up"
        if layer == 1 and position is None and config.camera_focus_position() is not None:
            # Last resort only: the start-up focus is whatever the codes in view
            # were when it was saved (the start-up QR focus, USE FOCUS), which
            # may be a top layer. A prediction from the layers above is better.
            position, how = config.camera_focus_position(), "start-up"
        if position is None or layer == self._focused_layer:
            return
        self._layer_focus_busy = True
        self._layer_focus_kind = "move"
        shown = (f"{position['step']:+d} steps" if isinstance(position, dict)
                 else str(position))
        self._log(f"LAYER FOCUS: layer {layer} is next — lens {shown} ({how})")
        self.layer_focus_requested.emit(layer, position, None, self._stack_area, False)

    @Slot(int, int)
    def on_layer_lens_known(self, layer, position):
        """Settings page: the lens position `layer` was read and sent at (read
        just before stepping to the next layer). Proven, so remembered."""
        if layer >= 1 and position >= 0:
            self._remember_layer_focus(layer, position)

    def _note_stack_area(self):
        """The stack's area, full-frame pixels: where layer 1 stood, widened.
        Every layer goes straight on top of layer 1 and the next stack goes in
        the same place, so every focus looks only there (quick check, search,
        new-scene check) and the rest of the picture is ignored.

        Layer 1 sets it (a new stack's layer 1 replaces it, in case the pallet
        moved); higher layers, closer and so a little bigger in the picture,
        can only widen it. Saved, so it is known again after a restart."""
        boxes = [b for c, b in self._keg_boxes.items()
                 if b and self._keg_box_cam.get(c, "icam") == "icam"]
        if not boxes:
            return
        x1, y1 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x2, y2 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        m = float(config.LAYER_FOCUS_AREA_MARGIN)
        w, h = (x2 - x1) or 1, (y2 - y1) or 1
        area = (x1 - m * w, y1 - m * h, x2 + m * w, y2 + m * h)
        old = self._stack_area
        if self.stack_tracker.next_layer == 1 or not old:
            self._stack_area_base = area
        else:
            area = (min(old[0], area[0]), min(old[1], area[1]),
                    max(old[2], area[2]), max(old[3], area[3]))
            base = getattr(self, "_stack_area_base", None)
            if base:
                # Higher layers are closer and look a little bigger, but one
                # stray box must not stretch the area over the whole picture
                # (2026-11-05: 137-3824 x 40-2090 after layer 3, so every
                # area focus measured the floor). At most a quarter of layer
                # 1's size more on each side.
                bw, bh = base[2] - base[0], base[3] - base[1]
                area = (max(area[0], base[0] - 0.25 * bw), max(area[1], base[1] - 0.25 * bh),
                        min(area[2], base[2] + 0.25 * bw), min(area[3], base[3] + 0.25 * bh))
        if area != old:
            self._stack_area = area
            config.save_stack_area(area)
            self._log(f"LAYER FOCUS: stack area {tuple(int(v) for v in area)} "
                      f"(from layer {self.stack_tracker.next_layer})")

    @Slot(int, bool, int, str)
    def on_layer_focus_done(self, layer, ok, position, message):
        """Settings page answered a layer focus request."""
        self._layer_focus_busy = False
        self._unread_since = None
        kind = getattr(self, "_layer_focus_kind", "scan")
        startup, self._layer_focus_startup = getattr(self, "_layer_focus_startup", False), False
        if ok and position >= 0:
            self._layer_focus_scene = False
            self._scene_tries, self._scene_rearmed = 0, False
            self._focused_layer, self._focused_pos = layer, position
            if kind == "scan":
                # The scan saw the codes read here: remember it at once. A
                # move is only remembered once its layer is read and sent.
                self._remember_layer_focus(layer, position)
                self._activity("success", tr("Camera focused for layer {n} · lens {pos}",
                                             n=layer, pos=position))
                if startup and config.save_camera_focus_position(position):
                    self._log(f"START-UP FOCUS: lens {position} saved for start-up")
        else:
            self._focused_layer = self._focused_pos = None
            self._log(f"LAYER FOCUS: layer {layer} — {message}")
            if kind == "scan":
                self._forgive_failed_search(message, startup)
            elif message.startswith("Layer focus not started"):
                # The lens move for the next layer came while a search was still
                # running: send it again once that is over.
                QTimer.singleShot(1500, self._focus_for_next_layer)

    def _forgive_failed_search(self, message, startup):
        """A search that never ran (another focus was busy) or found no QR code
        at all says nothing about the layer, so it must not hold the next one
        back for LAYER_FOCUS_RETRY_S. 2026-11: the finished stack was taken
        away, the empty floor started a search that found nothing, and the
        next stack's layer 1, put down a few seconds later, waited out the
        10 s before any focus started."""
        not_started = message.startswith("Layer focus not started")
        scene, self._layer_focus_scene = getattr(self, "_layer_focus_scene", False), False
        if scene:
            # A scene search that did not focus: the scene is tried again.
            tries = getattr(self, "_scene_tries", 1)
            if not_started or tries < SCENE_SEARCH_TRIES:
                self._scene_ref = getattr(self, "_scene_prev_ref", None)
                self._scene_rearmed = not not_started    # a refused one is not a try
                if not not_started:
                    self._log(f"LAYER FOCUS: search {tries} of {SCENE_SEARCH_TRIES} did not "
                              f"focus — trying again in {SCENE_RETRY_S:g} s")
            else:
                self._log(f"LAYER FOCUS: {tries} searches did not focus on this scene — "
                          f"double-tap the kegs on the picture or press FOCUS")
                self._warn_lbl.setText(tr(
                    "The camera could not focus on the kegs. Double-tap a keg on the "
                    "picture, or press Focus."))
                self._warn.setVisible(True)
            if not not_started:
                # The next try a few seconds on, not after the full 10 s.
                self._last_layer_search = (time.monotonic()
                                           - float(config.LAYER_FOCUS_RETRY_S) + SCENE_RETRY_S)
        if not (not_started or "No QR code in view" in message):
            return
        if not (scene and not not_started):
            self._last_layer_search = getattr(self, "_prev_layer_search", -1e9)
        key = getattr(self, "_search_key", None)
        if not_started:
            if key is not None and self._search_tries.get(key, 0) > 0:
                self._search_tries[key] -= 1      # it did not run: not a try
            if startup:
                self._auto_focus_pending = True   # try the start-up focus again

    def _remember_layer_focus(self, layer, position):
        if config.layer_focus_positions().get(layer) != position:
            config.save_layer_focus(layer, position)
            self._log(f"LAYER FOCUS: layer {layer} reads at lens {position} — "
                      f"remembered for the next stacks")

    def _note_keg_boxes(self, qr_list):
        """Keep the latest box of every keg on the layer, for its cell."""
        for qr in qr_list or []:
            code, bbox = qr.get("data"), qr.get("bbox")
            if code and bbox and code in self.session.qr_codes:
                self._keg_boxes[code] = bbox
                self._keg_box_cam[code] = "webcam" if self._webcam_reads() else "icam"

    def _layer_positions(self, codes, expected):
        """(positions {code: (row, col)}, (rows, cols)) of the layer to send."""
        boxes = {c: self._keg_boxes[c] for c in codes if c in self._keg_boxes}
        boxes = self._boxes_in_icam_orientation(boxes)
        layout = keg_layout.grid_for(expected, boxes)
        return keg_layout.assign_positions(boxes, layout), layout

    def _boxes_in_icam_orientation(self, boxes):
        """Boxes as the iCam would lay them out. A layer is read by one
        camera; if the webcam dropped out part-way, the camera that read most
        of the kegs keeps its boxes and the rest get no cell (like a keg read
        without a box) rather than a cell from the wrong picture."""
        cams = {c: self._keg_box_cam.get(c, "icam") for c in boxes}
        if "webcam" not in cams.values():
            return boxes
        webcam = sum(1 for v in cams.values() if v == "webcam")
        keep = "webcam" if webcam * 2 >= len(cams) else "icam"
        turn = getattr(self.camera, "to_primary_orientation", None)
        out = {}
        for code, bbox in boxes.items():
            if cams[code] != keep:
                continue
            out[code] = turn(bbox) if keep == "webcam" and turn else bbox
        return out

    def _close_stack(self, status=stack_store.STATUS_SHORT, reason=None):
        """Close the stack and keep it drawn until the next one starts. The
        cloud is told once its layers are delivered (stack-close)."""
        st = self.stack_tracker
        sid, kept = st.stack_id, self._stack_mode() and st.editable
        self._last_stack = sid
        st.finish(status, reason)
        self._start_awaiting_layer()
        if kept:
            self._queue_stack_report(sid, finished_early=True)
        if self.api_sender:
            self.api_sender.wake_retry_monitor()
        QTimer.singleShot(0, self._focus_for_next_layer)     # back to layer 1

    def _on_layers_changed(self, v):
        sid = self.stack_tracker.stack_id
        kept = self._stack_mode() and self.stack_tracker.editable
        finished = self.stack_tracker.set_layers_target(v)
        if finished:
            self._last_stack = sid
            if kept:
                self._queue_stack_report(sid)
            if self.api_sender:
                self.api_sender.wake_retry_monitor()
        config.save_layers_per_stack(v)
        self._log(f"Layers per stack updated to {v}")
        if finished:
            self._log("Stack closed: it already has that many layers")
            self.toast.emit(tr("Stack finished"), "success")
        self._refresh_stack()
        if self._zone_panel.isVisible():
            self._refresh_focus_zone()

    @Slot()
    def _finish_stack_clicked(self):
        st = self.stack_tracker
        if not st.open:
            return
        n = len(st.layers)
        if not ConfirmDialog.ask(self, tr("New Stack"),
                                 tr("End this stack with {n} of {total} layer(s)?",
                                    n=n, total=st.layers_target),
                                 (tr("The {n} layer(s) on the camera are sent now as one "
                                     "stack.", n=n) if self._stack_mode() and st.editable
                                  else tr("The layers already sent stay as they are.")) + " " +
                                 tr("The next kegs are layer 1 of a new stack."),
                                 confirm_text=tr("NEW STACK")):
            return
        self._log(f"Stack {st.stack_id} finished by the operator with {n} layer(s)")
        self._close_stack(reason="operator-finish")
        self.toast.emit(tr("Stack finished"), "success")
        self._refresh_stack()

    def _note_stack_view(self, qr_list):
        """Feed the height check and notice lower-layer kegs beside a new layer."""
        self._stack_empty_start = None
        self._stack_empty_warned = False
        s = self.session
        self._old_in_view = ({qr.get("data") for qr in qr_list
                              if qr.get("data") in s.stack_qrs
                              or qr.get("data") in s.last_sent_qr_set}
                             if not s.count() else set())
        st = self.stack_tracker
        if not st.active:
            return
        sizes = []
        lower = set()
        for qr in qr_list:
            code, bbox = qr.get("data"), qr.get("bbox")
            if code in self.session.qr_codes and bbox:
                sizes.append(max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
            elif code in self.session.stack_qrs:
                lower.add(code)
        st.note_sizes([] if self._webcam_reads() else sizes)
        # Normal right after a send, so only worth showing once a new layer
        # has started: a keg moved up a layer would otherwise stall the count
        # with nothing on screen to explain it.
        lower = lower if self.session.count() else set()
        if lower != self._lower_in_view:
            self._lower_in_view = lower
            self._rebuild_list()

    def _check_stack_left(self):
        """A stack was open and the view went empty: it was probably taken away."""
        if not self.stack_tracker.open or self._stack_empty_warned:
            return
        now = time.monotonic()
        if self._stack_empty_start is None:
            self._stack_empty_start = now
        elif now - self._stack_empty_start > config.STACK_CLEAR_SECONDS:
            self._stack_empty_warned = True
            n = len(self.stack_tracker.layers)
            self._log(f"Stack {self.stack_tracker.stack_id} is out of view with "
                      f"{n} of {self.stack_tracker.layers_target} layer(s)")
            self._warn_lbl.setText(tr("Stack not in view with {n} of {total} layer(s) — "
                                      "press NEW STACK if it was taken away.",
                                      n=n, total=self.stack_tracker.layers_target))
            self._warn.setVisible(True)
            QTimer.singleShot(10000, lambda: self._warn.setVisible(False))

    def _ask_layer_height(self):
        """This layer does not look higher than the one below: ask, once.

        It may be a new stack on the floor. It may also be the next layer with
        an unusual label, so the operator decides and nothing is sent until then.
        """
        st = self.stack_tracker
        if self._height_dialog_open or st.check_height() != HEIGHT_ASK:
            return
        n = len(st.layers)
        self._height_dialog_open = True
        try:
            self._log(f"HEIGHT CHECK: this layer is not above layer {n} — asking")
            new_stack = ConfirmDialog.ask(
                self, tr("Check This Layer"), tr("Is this a new stack?"),
                tr("These kegs do not look higher than layer {n}.", n=n) + " " +
                tr("NEW STACK closes the current stack with {n} layer(s).", n=n) + " " +
                tr("CANCEL keeps these kegs as layer {layer}.", layer=st.next_layer),
                confirm_text=tr("NEW STACK"))
        finally:
            self._height_dialog_open = False
        if new_stack:
            self._log(f"Operator closed stack {st.stack_id} with {n} layer(s): new stack")
            self._close_stack(reason="new-stack")
            self.toast.emit(tr("Stack finished"), "success")
        else:
            self._log(f"Operator kept this layer as layer {st.next_layer}")
            st.accept_height()
        self._refresh_stack()

    def _stack_layer_sent(self):
        """This layer is delivered or safely queued: move on, or close the stack."""
        st = self.stack_tracker
        layer = st.pending_layer
        if layer is None:
            return
        stack_id = st.stack_id
        if self._focused_layer == layer and getattr(self, "_focused_pos", None) is not None:
            # This layer was read and sent at that lens position: proven.
            self._remember_layer_focus(layer, self._focused_pos)
        self._note_stack_area()
        self._start_awaiting_layer()
        if self._layer_focus_on():
            # Remember where this layer read, every time: the next stack goes
            # straight there.
            self.layer_lens_query.emit(int(layer))
        sent = st.layer_sent()
        # The lens goes to where the next layer reads (learned on earlier stacks).
        QTimer.singleShot(0, self._focus_for_next_layer)
        if sent:
            self._last_stack = stack_id
            self._log(f"Stack {stack_id} complete with {layer} layer(s)")
            self.toast.emit(tr("Stack complete: {n} layer(s)", n=layer), "success")
            self._activity("success", tr("Stack complete: {n} layer(s)", n=layer))
        else:
            self._log(f"Layer {layer} of {st.layers_target} done on stack {stack_id}")
            self._activity("info", tr("Layer {n} done — place layer {next} on top",
                                      n=layer, next=layer + 1))
        self._refresh_stack()

    def _registration_hold(self):
        """Why this layer can't be sent yet as far as keg registration goes, or
        None when it can.

        Returns (kind, codes): "unknown" kegs can be registered from the list;
        "blocked" ones can't be registered on this camera and must come off the
        pallet; "checking" means the cloud has not answered for a keg yet.

        A keg whose check failed, or that was never checked because the network
        is down, is not held: nothing can be confirmed then, and the offline
        queue must keep working.
        """
        if not self._keg_reg_enabled:
            return None
        codes = self.session.qr_list()
        unknown = [c for c in codes if self._keg_reg.get(c) == "unknown"]
        if unknown:
            return "unknown", unknown
        blocked = [c for c in codes if self._keg_reg.get(c) == "blocked"]
        if blocked:
            return "blocked", blocked
        waiting = set(self._keg_check_queue) | self._keg_checking
        checking = [c for c in codes if c in waiting]
        if checking:
            return "checking", checking
        return None

    def _registration_hold_text(self, hold):
        """(main, sub) for the status card while a layer is held."""
        kind, codes = hold
        n = len(codes)
        if kind == "unknown":
            return (tr("{n} keg(s) not registered", n=n),
                    tr("Register them to send this pallet"))
        if kind == "blocked":
            return (tr("{n} keg(s) cannot be registered here", n=n),
                    tr("Take them off the pallet and remove them from the list"))
        return (tr("Checking keg registration…"),
                tr("The pallet is sent once every keg is confirmed"))

    def _announce_registration_hold(self):
        """Toast and log a hold once, not on every detection tick."""
        hold = self._registration_hold()
        key = (hold[0], tuple(sorted(hold[1]))) if hold else None
        if key == self._reg_hold_key:
            return
        self._reg_hold_key = key
        if not hold or hold[0] == "checking":
            return
        kind, codes = hold
        what = "not registered" if kind == "unknown" else "cannot be registered here"
        self._log(f"PALLET HELD: {len(codes)} keg(s) {what} — {', '.join(codes)}. "
                  f"Not sent until resolved.")
        main, sub = self._registration_hold_text(hold)
        self.toast.emit(f"{main}. {sub}.", "warning")
        self._activity("warning", f"{main}. {sub}.")

    def _resume_after_registration(self):
        """A registration finished. In AUTO, a layer already captured and waiting
        only on registration is sent now; one still scanning carries on through
        _check_auto_trigger by itself."""
        if (self.is_auto_mode and self.session.state == ScanState.READY
                and self.session.count() == self.session.target_count
                and self._registration_hold() is None):
            self._log("All kegs registered — sending the pallet")
            QTimer.singleShot(500, self._auto_send_and_print)

    def _warn_no_detect(self):
        """Nothing has decoded for a while: say so and stay in AUTO.

        The mode is left alone on purpose. An empty conveyor decodes nothing and
        is not a fault. AUTO keeps running, so the line carries on as soon as
        kegs appear.
        """
        self._no_detect_start = None
        self._no_detect_warned = True
        self._log("No QR detected — still in AUTO (press MANUAL if you want to "
                  "capture and decode a frame yourself)")
        # Set the text explicitly: the repeat-keg warning shares this banner and
        # may have left its own message.
        self._warn_lbl.setText(tr("No QR detected — reposition the kegs, or press "
                                  "MANUAL to capture and decode a frame yourself."))
        self._warn.setVisible(True)
        QTimer.singleShot(6000, lambda: self._warn.setVisible(False))
        self._blind_layer_focus()

    def _blind_layer_focus(self):
        if self._awaiting_layer():
            return
        self._blind_layer_focus_now()

    def _blind_layer_focus_now(self):
        """Nothing decodes at all while a stack is being built: the codes may be
        too soft for even a QR box to be found, which _check_layer_focus needs.
        One search for the expected layer (it looks at spread lens positions
        when no box is found). Only in the middle of a stack, where the layer
        below is under the camera and should at least decode; between stacks
        an empty floor decodes nothing and is no reason to move the lens. Once
        per layer."""
        st = self.stack_tracker
        if (not self._layer_focus_on() or self._layer_focus_busy
                or self.session.state != ScanState.SCANNING or not st.open):
            return
        layer = st.next_layer
        if getattr(self, "_blind_focus_layer", None) == (st.stack_id, layer):
            return
        if time.monotonic() - self._last_layer_search < float(config.LAYER_FOCUS_RETRY_S):
            return
        self._blind_focus_layer = (st.stack_id, layer)
        guess, how = config.predict_layer_focus(layer)
        self._prev_layer_search, self._search_key = self._last_layer_search, None
        self._last_layer_search = time.monotonic()
        self._layer_focus_busy = True
        self._layer_focus_kind = "scan"
        self._log(f"LAYER FOCUS: nothing decoding while layer {layer} is expected — "
                  f"searching once (codes may be too soft to find)")
        # The remembered / expected position: a quick check around it first.
        self.layer_focus_requested.emit(layer, None, guess, self._stack_area, False)

    def _prune_absent_kegs(self):
        """Drop kegs that have left the camera view, and redraw.

        This keeps AUTO continuous. Without pruning, kegs still in frame after a
        send stay counted, the next layer pushes the count past the target,
        over_target() goes true and AUTO never fires again. Pruning keeps the
        count equal to what is under the camera, so the cycle repeats: target
        reached, capture, send, layer carried away, next layer.

        An already-palletized keg also clears its own banner by being removed,
        so there is nothing to dismiss.

        Only runs in SCANNING. After a capture the layer is fixed and under
        review (especially in MANUAL, where the tiled decode adds codes the live
        detector may never report); changing it would alter what the operator
        is about to send.
        """
        if self.session.state != ScanState.SCANNING:
            return
        removed = self.session.prune_absent(ABSENT_PRUNE_SECONDS)
        if not removed:
            return

        self._rebuild_list()
        # Clears the amber banner by itself once nothing is flagged any more.
        self._update_dup_banner()
        if not self.session.has_flagged():
            # Re-arm the once-per-pallet repeat prompt: the kegs that raised it
            # are gone, so a new repeat later gets its own prompt.
            self._repeat_prompted = False

        shown = ", ".join(sorted(removed)[:4])
        more = "…" if len(removed) > 4 else ""
        self._log(f"{len(removed)} keg(s) left the camera view — removed from the "
                  f"pallet being built: {shown}{more} (now {self.session.count()}/"
                  f"{self.session.target_count})")

    # QR list
    def _add_qr(self, code):
        if not code:
            return
        status = self.session.add_qr(code)
        if status == "new":
            if code in self.blacklist and code not in self._warned_blacklist:
                self._warned_blacklist.add(code)
                self.toast.emit(tr("Blocked keg: {code} — set it aside", code=code), "warning")
                self._log(f"BLACKLIST: flagged keg {code} detected")
                self._activity("warning", tr("Blocked keg seen: {code} — set it aside",
                                             code=code))
            else:
                self._queue_keg_check(code)
            self._rebuild_list()
        elif status == "flagged":
            self._on_duplicate_keg(code)

    def _on_duplicate_keg(self, code):
        """An already-palletized keg was detected; it is still counted and sent.

        Every such keg is logged, but the operator is interrupted only once per
        pallet: the first repeat raises the warning and prompt (see
        _handle_repeat_kegs), and further kegs in the same load are added quietly
        to the banner and the list.
        """
        pallet = self.session.flagged_used.get(code, "another pallet")
        if self.session.duplicate_action == "move":
            self._log(f"MOVE: keg {code} joins this pallet and leaves pallet {pallet}")
        else:
            self._log(f"REPEAT: keg {code} already in pallet {pallet} — counted & sent")
        self._update_dup_banner()
        self._rebuild_list()
        self._handle_repeat_kegs()

    # Repeat detection: same kegs, already palletized
    def _handle_repeat_kegs(self):
        """First repeat of this pallet: warn once, then ask.

        Kegs that already made a pallet are usually a load that hasn't been
        taken off the camera yet. AUTO won't build a second pallet from them
        because _check_auto_trigger pauses on flagged kegs until a decision is
        made.

        The dialog is deferred slightly so it doesn't open from inside the
        detection handler.
        """
        if self._repeat_prompted or self.session.duplicate_action:
            return
        self._repeat_prompted = True

        flagged = self.session.flagged_list()
        pallets = ", ".join(sorted({p for _, p in flagged}))
        self._log(f"REPEAT DETECTED: keg(s) from pallet(s) {pallets} are under the "
                  f"camera again")

        if self.is_auto_mode:
            # Mode is left alone: _check_auto_trigger refuses to send a layer
            # with a flagged keg until duplicate_action is set.
            self._warn_lbl.setText(
                tr("These kegs already made pallet {pallets} — AUTO is paused for "
                   "them until you choose what happens to them.", pallets=pallets))
            self._warn.setVisible(True)
            QTimer.singleShot(10000, lambda: self._warn.setVisible(False))

        self.toast.emit(tr("These kegs already made pallet {pallets}", pallets=pallets),
                        "warning")
        QTimer.singleShot(400, self._ask_repeat_pallet)

    def _ask_repeat_pallet(self):
        """Ask once whether another pallet is wanted from these same kegs.

        The answer applies to the whole pallet; the send-time dialog reads the
        stored decision instead of asking again.
        """
        if self._repeat_dialog_open:
            return
        flagged = self.session.flagged_list()
        if not flagged or self.session.duplicate_action:
            return

        pallets = ", ".join(sorted({p for _, p in flagged}))
        self._repeat_dialog_open = True
        try:
            choice = DuplicateKegDialog.ask(self, flagged, repeat=True)
        finally:
            self._repeat_dialog_open = False

        if choice == DuplicateKegDialog.MOVE:
            codes = self.session.choose_move()
            self._log(f"Operator: create a NEW pallet and MOVE {len(codes)} keg(s) off "
                      f"pallet(s) {pallets}")
            self.toast.emit(
                tr("{n} keg(s) will move to a new pallet — press CAPTURE", n=len(codes))
                if not self.is_auto_mode else
                tr("{n} keg(s) will move to a new pallet — it is sent automatically",
                   n=len(codes)), "info")
        elif choice == DuplicateKegDialog.KEEP:
            self.session.choose_keep()
            self._log(f"Operator: create ANOTHER pallet with these kegs — pallet(s) "
                      f"{pallets} left unchanged")
            self.toast.emit(
                tr("New pallet approved for these kegs — press CAPTURE")
                if not self.is_auto_mode else
                tr("New pallet approved for these kegs — it is sent automatically"),
                "info")
        else:
            # No new pallet wanted. Nothing is decided, so a later SEND still asks;
            # this just keeps quiet until the kegs are removed.
            self._log("Operator: no new pallet needed for the repeated kegs")
            self.toast.emit(tr("No new pallet — remove these kegs from the camera"), "info")

        self._update_dup_banner()
        self._rebuild_list()

    # Operator decisions on already-palletized kegs
    def _update_dup_banner(self):
        """Show or refresh the amber duplicate banner from the flagged set."""
        flagged = self.session.flagged_list()
        if not flagged:
            self._dup.setVisible(False)
            self._dup_warned = False
            self._dup_seen_start = None
            if self.camera is not None:
                self._cam.set_alert(False)
            return
        codes = ", ".join(c for c, _ in flagged[:4])
        more = "…" if len(flagged) > 4 else ""
        pallets = ", ".join(sorted({p for _, p in flagged}))
        action = self.session.duplicate_action
        # Once the operator has answered, the banner states what will happen
        # instead of warning.
        if action == "move":
            self._dup_lbl.setText(
                tr("{n} keg(s) will MOVE to a new pallet and be removed "
                   "from {pallets}: {codes}", n=len(flagged), pallets=pallets,
                   codes=codes + more))
        elif action == "keep":
            self._dup_lbl.setText(
                tr("New pallet approved for {n} keg(s) — {pallets} stays "
                   "unchanged: {codes}", n=len(flagged), pallets=pallets,
                   codes=codes + more))
        else:
            # Before a choice the layer is not sent (AUTO waits), so the bar
            # says what to do, not "counted & sent".
            self._dup_lbl.setText(
                tr("{n} keg(s) already on pallet {pallets}: {codes}. Press MOVE HERE to "
                   "take them off that pallet, or choose when sending.",
                   n=len(flagged), pallets=pallets, codes=codes + more))
        self._dup_move_btn.setVisible(not action)
        self._dup.setVisible(True)

    def _choose_move_duplicates(self):
        """Banner shortcut: move every flagged keg onto the pallet being built.

        Only records the decision. The kegs come off their old pallet when this
        pallet is actually created, not before.
        """
        codes = self.session.choose_move()
        if not codes:
            return
        pallets = ", ".join(sorted({p for _, p in self.session.flagged_list()}))
        self._log(f"Operator chose MOVE for {len(codes)} keg(s) — they will be removed "
                  f"from pallet(s) {pallets} once this pallet is created")
        self.toast.emit(tr("{n} keg(s) set to move to this pallet", n=len(codes)), "info")
        self._update_dup_banner()
        self._rebuild_list()

    def _acknowledge_dup_reminder(self):
        """Operator dismisses the duplicate reminder; kegs stay counted and sent.

        Does not undo a MOVE already chosen: dismissing a banner is not a
        decision about where a keg belongs.
        """
        self._dup.setVisible(False)
        self._dup_warned = False
        self._dup_seen_start = None
        if self.camera is not None:
            self._cam.set_alert(False)

    def _update_dup_dwell(self):
        """Escalate to a prominent warning if duplicate kegs linger under the camera.

        The detection-time prompt normally handles repeats, so this only fires
        for a repeat that never reached the prompt.
        """
        if not self.session.has_flagged():
            self._dup_seen_start = None
            self._dup_warned = False
            return
        if self.session.duplicate_action or self._repeat_prompted:
            # Already raised or answered for this pallet; don't nag again.
            return
        if self._dup_seen_start is None:
            self._dup_seen_start = datetime.now()
        elif (not self._dup_warned and
              (datetime.now() - self._dup_seen_start).total_seconds() > DUP_WARN_SECONDS):
            self._dup_warned = True
            flagged = self.session.flagged_list()
            pallets = ", ".join(sorted({p for _, p in flagged}))
            self._dup_lbl.setText(
                tr("These kegs already made a pallet ({pallets}) — still counted & sent.",
                   pallets=pallets) + " " +
                tr("Remove them from the camera if this is unintended."))
            self._dup.setVisible(True)
            self.toast.emit(tr("Same kegs still under camera — they already made a pallet"),
                            "warning")
            self._log(f"WARNING: already-palletized kegs lingering ({pallets})")
            if self.camera is not None:
                self._cam.set_alert(True, int(DUP_WARN_SECONDS))

    @Slot(str)
    def _remove_qr(self, code):
        """Remove (x) on a keg row: ask first, then take it off the list.

        The question opens after the row's click has returned: a new keg can
        rebuild the list while it is open, and that deletes this row's button."""
        QTimer.singleShot(0, lambda: self._ask_remove_qr(code))

    def _ask_remove_qr(self, code):
        if not ConfirmDialog.ask(
                self, tr("Remove Keg"), tr("Remove keg {code} from the list?", code=code),
                tr("If it is still under the camera, it is detected and added again."),
                confirm_text=tr("REMOVE"), danger=True):
            return
        self.session.qr_codes.discard(code)
        self._rebuild_list()
        self._log(f"Keg {code} removed from the list by the operator")

    def _dismiss_info(self):
        """Close button on the notice bar."""
        self._info_dismissed_at = self.session.ignored_count()
        self._info.setVisible(False)

    def _copy_clicked(self):
        """COPY button: put the scanned keg IDs on the clipboard, one per line."""
        codes = self.session.qr_list()
        if not codes:
            self.toast.emit(tr("No keg IDs to copy yet"), "info")
            return
        QGuiApplication.clipboard().setText("\n".join(codes))
        self.toast.emit(tr("Copied {n} keg ID(s)", n=len(codes)), "success")

    def _copy_one(self, code):
        """Copy icon on a row: put that one keg ID on the clipboard."""
        QGuiApplication.clipboard().setText(code)
        self.toast.emit(tr("Copied {code}", code=code), "success")

    def _clear_clicked(self):
        """CLEAR button: ask first, then empty the scanned list."""
        n = self.session.count()
        if not n:
            self.toast.emit(tr("The scanned list is already empty"), "info")
            return
        if not ConfirmDialog.ask(
                self, tr("Clear Scanned Kegs"), tr("Clear all {n} scanned keg(s)?", n=n),
                tr("The list is emptied.") + " " +
                tr("Kegs still under the camera are detected again."),
                confirm_text=tr("CLEAR"), danger=True):
            return
        self._clear_all_qr()
        self.toast.emit(tr("Scanned list cleared"), "success")

    def _clear_all_qr(self):
        self.session.qr_codes.clear()
        self._rebuild_list()
        self._log("QR list cleared")

    def _rebuild_list(self):
        t0 = time.perf_counter()
        try:
            self._rebuild_list_now()
        finally:
            self._gui_stats['rebuilds'] += 1
            self._gui_stats['rebuild_ms'] += (time.perf_counter() - t0) * 1000

    def _rebuild_list_now(self):
        for i in reversed(range(self._list_lay.count())):
            w = self._list_lay.itemAt(i).widget()
            if w is not None and w is not self._empty:
                w.setParent(None)
                w.deleteLater()
        qr_list = self.session.qr_list()
        flagged_map = dict(self.session.flagged_list())   # code -> pallet (subset of qr_list)
        moving = set(self.session.move_list())            # flagged kegs leaving their old pallet
        # Kegs that get a REGISTER button; a tick only survives while it has one.
        unreg_codes = [c for c in qr_list if self._keg_reg.get(c) == "unknown"
                       and c not in self.blacklist and c not in flagged_map]
        self._keg_selected &= set(unreg_codes)
        self._matched_codes &= set(qr_list)
        if not qr_list:
            self._empty.setVisible(True)
            self._qr_added = {}
            self._list_lay.addWidget(self._empty)
        else:
            self._empty.setVisible(False)
            # When each keg first appeared, so the one just read is highlighted.
            now = time.monotonic()
            seen = getattr(self, "_qr_added", {})
            self._qr_added = {c: seen.get(c, now) for c in qr_list}
            fresh = {c for c, t in self._qr_added.items() if now - t < QR_FRESH_S}
            if fresh:
                # Redraw once the highlight is over (nothing else may change).
                QTimer.singleShot(int(QR_FRESH_S * 1000) + 100, self._rebuild_list)
            # Every detected keg is counted. Kegs already in another pallet also
            # get an amber "IN <pallet>" reminder tag, but are still counted and sent.
            for idx, code in enumerate(qr_list, 1):
                item = QRItem(code, idx,
                              blacklisted=code in self.blacklist,
                              reminder_pallet=flagged_map.get(code),
                              moving=code in moving,
                              unregistered=self._keg_reg.get(code) == "unknown",
                              selected=code in self._keg_selected,
                              matched=code in self._matched_codes,
                              fresh=code in fresh)
                item.remove_requested.connect(self._remove_qr)
                item.copy_requested.connect(self._copy_one)
                item.register_requested.connect(self._register_keg)
                item.select_toggled.connect(self._toggle_keg_selected)
                self._list_lay.addWidget(item)
        self._unreg_codes = unreg_codes
        self._update_reg_bar()
        n = len(qr_list)
        blk = sum(1 for c in qr_list if c in self.blacklist)
        dup = len(flagged_map)
        unreg = sum(1 for c in qr_list if self._keg_reg.get(c) == "unknown")
        parts = [tr("{n} scanned", n=n)]
        if blk:
            parts.append(tr("{n} flagged", n=blk))
        if unreg:
            parts.append(tr("{n} not registered", n=unreg))
        if dup:
            parts.append(tr("{n} moving here", n=len(moving)) if moving
                         else tr("{n} in other pallet (counted)", n=dup))
        if self._lower_in_view:
            parts.append(tr("{n} on a lower layer (not counted)", n=len(self._lower_in_view)))
        self._qr_count.set_status(
            " · ".join(parts),
            C['red'] if blk else (C['amber_dark'] if dup else C['blue']))
        self._stat_detected.set_value(n)

    # Status card and banners
    def _set_status(self, icon, main, sub, color=None):
        # `icon` is unused (kept for existing callers); status is shown as a
        # colour-coded dot.
        # Called on every display tick and setStyleSheet re-polishes the widget,
        # so only touch the labels when something changed. Nothing else writes
        # these three labels, so the cached value stays accurate.
        shown = (main, sub, color)
        if shown == self._status_shown:
            return
        self._status_shown = shown
        self._st_icon.setPixmap(svg_pixmap("dot", color or C["text1"], 14))
        self._st_main.setText(main)
        self._st_main.setStyleSheet(
            f"color:{color or C['text1']}; font-size:14px; font-weight:700; background:transparent;")
        self._st_sub.setText(sub)

    def _process_frame(self):
        # While the MANUAL-mode tiled decode runs, keep the "Decoding…" status
        # set by _run_advanced_decode instead of overwriting it each frame.
        if self._advanced_busy:
            return
        qr_count = self.session.count()
        ignored = self.session.ignored_count()
        self._stat_detected.set_value(qr_count)
        self._stat_stable.set_value(self.session.stability_counter)

        has_ignored, _, ign_msg = self.session.get_ignored_info()
        if SHOW_IGNORED_NOTE and has_ignored and ignored != self._info_dismissed_at:
            if ign_msg != self._info_text:          # only touch the label on change
                self._info_text = ign_msg
                self._info_lbl.setText(tr(
                    "{ignored} keg(s) from previous pallet(s) ignored — "
                    "detecting new kegs only ({new} new so far)",
                    ignored=ignored, new=len(self.session.qr_codes)))
            self._info.setVisible(True)
        elif self._info_text == ign_msg:
            self._info.setVisible(False)

        state = self.session.state
        self._hold_msg = None
        # Only worked out when the layer is complete, which is when it matters.
        reg_hold = (self._registration_hold()
                    if state == ScanState.READY
                    or (state == ScanState.SCANNING and qr_count == self.required_keg_count
                        and qr_count > 0)
                    else None)
        if state == ScanState.SENDING:
            self._set_status("", tr("Sending pallet…"),
                             tr("Please wait — this takes a few seconds"), C["blue"])
            self._cam_badge.setText(tr("Sending…"))
        elif reg_hold:
            main, sub = self._registration_hold_text(reg_hold)
            self._hold_msg = ("busy" if reg_hold[0] == "checking" else "warn", main, sub)
            self._set_status("", main, sub,
                             C["blue"] if reg_hold[0] == "checking" else C["unreg"])
            self._cam_badge.setText(tr("Not registered") if reg_hold[0] != "checking"
                                    else tr("Checking…"))
        elif state == ScanState.READY:
            self._set_status("", tr("Ready to Send!"), tr("Tap SEND to upload"),
                             C["green_dark"])
            self._cam_badge.setText(tr("Ready"))
        elif self.session.over_target():
            over = qr_count - self.required_keg_count
            self._set_status("", tr("OVER TARGET — {count} new ({over} extra)",
                                    count=qr_count, over=over),
                             tr("Remove {over} keg(s) to match {target}", over=over,
                                target=self.required_keg_count), C["red"])
            self._cam_badge.setText(tr("Over"))
        elif qr_count == self.required_keg_count and qr_count > 0:
            self._set_status("", tr("Target Achieved"),
                             tr("{n} new kegs ({ignored} ignored)", n=qr_count, ignored=ignored)
                             if ignored else tr("{n} new kegs", n=qr_count),
                             C["green_dark"])
            self._cam_badge.setText(tr("Target Met"))
        elif qr_count > 0:
            remaining = self.required_keg_count - qr_count
            self._set_status("", tr("Detecting — {count} of {target}", count=qr_count,
                                    target=self.required_keg_count),
                             tr("{n} more needed ({ignored} ignored)", n=remaining,
                                ignored=ignored)
                             if ignored else tr("{n} more needed", n=remaining),
                             C["blue"])
            self._cam_badge.setText(tr("Detecting"))
        elif self._old_in_view:
            # Codes read, none counted: they are the kegs just sent. Said on
            # screen, or the operator sees green boxes and "0" with no reason.
            self._set_status("", tr("{n} keg(s) in view were already sent",
                                    n=len(self._old_in_view)),
                             tr("From the stack or layer just finished — not counted "
                                "again. Put down new kegs, or press CLEAR ALL to count "
                                "these again."), C["amber_dark"])
            self._cam_badge.setText(tr("Already sent"))
        else:
            # Same words as the DO NOW card on the Stack card, never a
            # different instruction in two places.
            st, rec = self.stack_tracker, getattr(self, "_stack_rec", None)
            if st.active and rec is not None and rec.get("status") != stack_store.STATUS_OPEN:
                self._set_status("", tr("Stack complete"),
                                 tr("Take it away, then place layer 1 of the next stack"),
                                 C["green_dark"])
                self._cam_badge.setText(tr("Stack done"))
            elif st.active and st.open:
                self._set_status("", tr("Waiting for layer {n}", n=st.next_layer),
                                 tr("Place it on top of layer {n}", n=st.next_layer - 1),
                                 C["text1"])
                self._cam_badge.setText(tr("Waiting"))
            else:
                self._set_status("", tr(WAITING_FOR_KEGS_TEXT), tr(PLACE_KEGS_TEXT), C["text1"])
                self._cam_badge.setText(tr("Waiting"))

    # Mode switching
    def _set_mode_note(self, text):
        note = getattr(self, "_mode_note", None)
        if note is not None:
            note.setText(text)

    def _set_auto_mode(self):
        if not self.is_auto_mode:
            self._activity("info", tr("Switched to AUTO — pallets are sent automatically"))
        self.is_auto_mode = True
        self.session.stability_counter = 0
        self._auto_btn.setObjectName("segAuto")
        self._manual_btn.setObjectName("segManualOff")
        self._restyle(self._auto_btn, self._manual_btn)
        self._capture_btn.setEnabled(False)
        self._cam_badge.setText(tr("Auto Mode"))
        self._cam_badge.setStyleSheet(
            "color:white; font-size:15px; font-weight:700; background:transparent;")
        self.mode_changed.emit("auto")
        self._set_mode_note(tr(AUTO_NOTE))
        self._log("Switched to AUTO")
        self._update_stack_map()

    def _set_manual_mode(self):
        """Switch to MANUAL. Only called from the MANUAL button."""
        if self.is_auto_mode:
            self._activity("info", tr("Switched to MANUAL — press CAPTURE for each pallet"))
        self.is_auto_mode = False
        self.session.stability_counter = 0
        self._auto_btn.setObjectName("segAutoOff")
        self._manual_btn.setObjectName("segManual")
        self._restyle(self._auto_btn, self._manual_btn)
        self._capture_btn.setEnabled(True)
        self._cam_badge.setText(tr("Manual"))
        self._cam_badge.setStyleSheet(
            "color:white; font-size:15px; font-weight:700; background:transparent;")
        self.mode_changed.emit("manual")
        self._set_mode_note(tr(MANUAL_NOTE))
        self._log("Switched to MANUAL by the operator")
        self._update_stack_map()

    @staticmethod
    def _restyle(*widgets):
        for w in widgets:
            w.style().unpolish(w)
            w.style().polish(w)

    # Capture
    def _force_capture(self):
        if self._advanced_busy:
            return
        if ConfirmDialog.ask(self, tr("Capture Pallet"),
                             tr("Take a photo and read every keg label?"),
                             tr("This takes a few seconds.") + " " +
                             tr("Keep the pallet still under the camera."),
                             confirm_text=tr("CAPTURE")):
            if self.camera:
                ret, frame = self.camera.get_frame()
                if ret and frame is not None:
                    # MANUAL capture: run the heavy tiled decode on the frame.
                    self._trigger_capture(frame, advanced=True)

    def _trigger_capture(self, frame, advanced=False):
        if self.session.state != ScanState.SCANNING:
            return False
        batch = self._batch.text().strip()
        ok, message = validate_batch(batch)
        if not ok:
            self._update_batch_validation(batch)
            self._batch.setFocus()
            self.toast.emit(f"{message}!", "error")
            return False
        session_id = f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        image_name = f"batch_{create_timestamp()}.jpg"
        frame_path = str(SAVE_FOLDER / image_name)
        # Encoded on a background thread, since a 4K JPEG would freeze the screen.
        # Both readers of frame_path (AdvancedDecodeWorker, BatchSubmitWorker)
        # wait for this thread before touching the file. `frame` is a private
        # copy from camera.get_frame(), so it is safe to hand over.
        self._frame_saves = {p: t for p, t in self._frame_saves.items() if t.is_alive()}
        self._frame_saves[frame_path] = save_frame_async(frame, frame_path)
        frame_store.prune_soon("capture")

        s = self.session
        s.batch_number = batch
        s.frame_path = frame_path
        s.image_name = image_name
        # Timezone-aware: this becomes the payload's scannedTime, which the cloud
        # writes onto each keg. A naive stamp would be ambiguous once stored.
        s.captured_at = datetime.now().astimezone()
        s.session_id = session_id
        s.target_count = self.required_keg_count

        if advanced:
            # MANUAL mode: don't go READY yet. Tile the captured frame and decode
            # every tile first; this full-resolution re-decode is the main
            # difference from AUTO. State stays SCANNING so the live detector can
            # still merge results in meanwhile.
            self._log(f"Captured {session_id} — running advanced tiled decode…")
            self._run_advanced_decode(frame_path)
            return True

        s.state = ScanState.READY
        self._send_btn.setEnabled(True)
        self.toast.emit(tr("Capture complete — ready to send"), "success")
        self._log(f"Captured {session_id} | {s.count()} QRs")
        return True

    # MANUAL-mode advanced tiled decode
    def _run_advanced_decode(self, frame_path):
        """Tile the captured frame and decode each tile (modules/advanced.py).

        Runs off the GUI thread; _on_advanced_decoded merges the result and goes
        READY. On error it falls back to whatever the live detector collected.
        """
        self._advanced_busy = True
        self._set_status("", tr("Advanced decoding…"),
                         tr("Tiling captured frame & decoding QR codes"), C["blue"])
        self._cam_badge.setText(tr("Decoding…"))
        self._capture_btn.setEnabled(False)
        self._send_btn.setEnabled(False)
        worker = AdvancedDecodeWorker(frame_path, self,
                                      frame_save=self._frame_saves.get(frame_path))
        worker.result_ready.connect(self._on_advanced_decoded)
        worker.failed.connect(self._on_advanced_failed)
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    @Slot(object)
    def _on_advanced_decoded(self, codes):
        added = flagged = 0
        codes = [q["data"] for q in self._drop_pallet_labels(
            [{"data": c} for c in codes or []])]
        for code in codes:
            status = self.session.add_qr(code)
            if status == "new":
                added += 1
                if code not in self.blacklist:
                    self._queue_keg_check(code)
            elif status == "flagged":
                flagged += 1
        if flagged:
            self._update_dup_banner()
        self._rebuild_list()
        self._log(f"Advanced decode returned {len(codes or [])} code(s)")
        self._finalize_manual_capture(added, flagged)

    @Slot(str)
    def _on_advanced_failed(self, err):
        self._log(f"Advanced decode failed: {str(err)[:80]}")
        self.toast.emit(tr("Photo check failed — using the kegs already seen"), "warning")
        self._rebuild_list()
        self._finalize_manual_capture(0, 0)

    def _finalize_manual_capture(self, added, flagged=0):
        self._advanced_busy = False
        self.session.state = ScanState.READY
        self._capture_btn.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._cam_badge.setText(tr("Ready"))
        total = self.session.count()
        extra = f" · {flagged} already in another pallet (counted)" if flagged else ""
        parts = [tr("Photo checked: {n} keg(s) ({added} more found)", n=total, added=added)
                 if added else tr("Photo checked: {n} keg(s)", n=total)]
        if flagged:
            parts.append(tr("{n} already in another pallet (counted)", n=flagged))
        self.toast.emit(" · ".join(parts), "success")
        self._log(f"Advanced tiled decode complete — {total} keg(s), {added} new{extra}")

    def _resolve_duplicates(self):
        """Settle what happens to kegs already belonging to a cloud-sent pallet.

        No pallet is created until this is answered. The dialog names every
        duplicate keg and its pallet, and offers:

            MOVE   - add here and remove from the previous pallet
            KEEP   - add here, leave the previous pallet as it is
            CANCEL - create nothing

        Returns "move" or "keep" to proceed, or None if the operator cancelled.
        A batch with no duplicates returns "keep" without asking.
        """
        flagged = self.session.flagged_list()
        if not flagged:
            return "keep"

        # Already answered on the banner; don't ask twice.
        if self.session.duplicate_action:
            return self.session.duplicate_action

        pallet_labels = ", ".join(sorted({p for _, p in flagged}))
        choice = DuplicateKegDialog.ask(self, flagged)

        if choice == DuplicateKegDialog.CANCEL:
            # Revert to SCANNING so the session stays alive and the operator can
            # pull the duplicate kegs off the camera.
            self.session.state = ScanState.SCANNING
            self._send_btn.setEnabled(False)
            self.toast.emit(tr("Pallet creation cancelled — remove duplicate kegs or "
                               "confirm again"), "warning")
            self._log("Pallet creation cancelled by operator (duplicate kegs)")
            return None

        if choice == DuplicateKegDialog.MOVE:
            codes = self.session.choose_move()
            self._log(f"Operator chose MOVE for {len(codes)} keg(s) — they will be "
                      f"removed from pallet(s) {pallet_labels}")
        else:
            self.session.choose_keep()
            self._log(f"Operator chose KEEP for {len(flagged)} already-palletized "
                      f"keg(s) — pallet(s) {pallet_labels} left unchanged")

        self._update_dup_banner()
        self._rebuild_list()
        return choice

    # Send to server
    def _send_clicked(self):
        """SEND pressed by hand (rare, AUTO sends by itself): confirm first."""
        if self.session.state != ScanState.READY:
            return
        n = self.session.count()
        batch = self._batch.text().strip() or "—"
        detail = tr("Batch {batch}.", batch=batch) + " " + (
            tr("AUTO mode sends pallets by itself — only send by hand if this pallet "
               "was not sent.") if self.is_auto_mode else
            tr("Check the kegs on the list first."))
        if not ConfirmDialog.ask(self, tr("Send Pallet"),
                                 tr("Send this pallet now ({n} kegs)?", n=n), detail,
                                 confirm_text=tr("SEND")):
            return
        self._send_to_server()

    def _send_to_server(self):
        if self.session.state != ScanState.READY:
            return
        # No pallet is created until the operator has said what happens to any
        # keg that is already on another pallet.
        if self._resolve_duplicates() is None:
            return
        # MANUAL reaches this without the height gate in _check_auto_trigger.
        if self.stack_tracker.check_height() == HEIGHT_ASK:
            self._ask_layer_height()
        qr_count = self.session.count()
        if qr_count != self.session.target_count:
            if qr_count > self.session.target_count:
                self.toast.emit(
                    tr("{n} kegs seen — pallet needs {target}.", n=qr_count,
                       target=self.session.target_count) + " " +
                    tr("Remove {n}.", n=qr_count - self.session.target_count), "error")
            else:
                self.toast.emit(
                    tr("Only {n} kegs seen — pallet needs {target}.", n=qr_count,
                       target=self.session.target_count) + " " +
                    tr("Add kegs or turn labels to the camera."), "error")
            return
        # Every keg must be registered first: the cloud only palletises kegs it
        # knows, so sending now would create the pallet short. Stays READY; the
        # operator registers them and presses SEND (AUTO resends by itself).
        hold = self._registration_hold()
        if hold:
            main, sub = self._registration_hold_text(hold)
            self._log(f"Not sent — {main}: {', '.join(hold[1])}")
            self.toast.emit(f"{main}. {sub}.",
                            "info" if hold[0] == "checking" else "error")
            self._send_btn.setEnabled(True)
            return
        # Safe to toast here, unlike the AUTO path: this runs once per button
        # press, not on every detection tick.
        if not self._beer_ready():
            self._update_beer_gate()
            self.toast.emit(tr("Select a beer type before sending"), "error")
            return
        # Details that don't block a send but shouldn't go missing silently.
        # In AUTO this pauses this pallet, like the duplicate-keg question;
        # Cancel leaves it READY for the operator to fill in and press SEND.
        if not self._confirm_missing_details():
            self._send_btn.setEnabled(True)
            return

        self.session.state = ScanState.SENDING
        self._send_btn.setEnabled(False)

        s = self.session
        codes = s.qr_list()
        positions, layout = self._layer_positions(codes, s.target_count)
        self._log(f"Layer layout {keg_layout.layout_text(layout)}: "
                  f"{len(positions)} of {len(codes)} keg(s) placed in a cell")
        if self._stack_mode():
            self._hold_layer(codes, positions, layout)
            return
        # On disk before the send, so a restart mid-send still knows this layer.
        stacked = self.stack_tracker.begin_layer(
            s.session_id, codes, positions, keg_layout.layout_text(layout),
            s.target_count)
        if stacked:
            self._load_stack_record()
        payload = {
            "frame_path":     s.frame_path,
            "image_name":     s.image_name,
            "session_id":     s.session_id,
            "qr_codes":       s.qr_list(),
            "required_count": s.target_count,
            "batch":          s.batch_number,
            # When the layer was read (payload "scannedTime"), not when it is sent.
            "scanned_time":   s.captured_at.isoformat() if s.captured_at else None,
            # Kegs the operator chose to move: they go onto this pallet and come
            # off their old one once this send succeeds.
            "reassign_kegs":  s.move_list(),
            # None when pallets are not stacked: the report is then layer 1.
            "layer":          stacked.get("layer") if stacked else None,
            "stack":          stacked,
            # Where each keg stood: its cell in the layer's grid.
            "positions":      positions,
            "layout":         layout,
        }
        # Beer type: both are "" when the feature is off or nothing is chosen;
        # build_pallet_report() then omits the keys.
        payload["beer_type"], payload["beer_type_id"] = self._selected_beer()
        # Operator: read live from config, not cached here, so a handover made in
        # Settings applies to the next send. Both are "" when nobody is picked and
        # the keys are then omitted. This never blocks the send: an unattributed
        # pallet is valid, and the cloud accepts unknown names.
        payload["operator_id"], payload["operator_name"] = config.current_operator()
        # Not a submit_batch argument: BatchSubmitWorker waits on it so the frame
        # is on disk before submit_batch can read it.
        payload["frame_save"] = self._frame_saves.get(s.frame_path)
        worker = BatchSubmitWorker(payload, self)
        worker.success.connect(self._on_send_complete)
        # The worker itself failed, so submit_batch never ran: no DB row and
        # nothing in the upload queue. Report it as not queued so the operator
        # isn't promised a delivery that won't happen.
        worker.failed.connect(lambda e: self._on_send_complete(
            {"success": False, "error": f"Could not start the send: {e}",
             "retryable": False, "queued": False}))
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    def _missing_details(self):
        """Details that are empty on this pallet, as (field, consequence) pairs.

        Only fields that can be empty here are listed. The batch number is
        validated at capture, and a beer type with a catalogue loaded is a hard
        gate (_beer_ready).
        """
        missing = []
        if not any(config.current_operator()):
            missing.append((N_("Operator"), N_("nobody signed in or selected in Settings — "
                                              "the pallet is reported without an operator")))
        if self._beer_enabled and not self._selected_beer()[0]:
            # Reachable only with no beer list loaded (see _beer_ready).
            missing.append((N_("Beer type"), N_("no beer list loaded — the pallet goes "
                                               "without a beer type")))
        return missing

    def _confirm_missing_details(self):
        """Warn before a pallet goes to the cloud with details missing.

        Returns True to send, False if the operator went back to fill them in."""
        missing = self._missing_details()
        if not missing:
            return True
        names = ", ".join(f for f, _ in missing)
        detail = "\n".join(f"• {tr(f)}: {tr(why)}" for f, why in missing)
        self._log(f"Missing before send: {names}")
        if ConfirmDialog.ask(self, tr("Missing Details"),
                             tr("Send this pallet with details missing?"),
                             detail, confirm_text=tr("SEND ANYWAY")):
            self._log(f"Operator chose to send without: {names}")
            return True
        self.toast.emit(tr("Not sent — fill in {names}, then press SEND",
                           names=", ".join(tr(f) for f, _ in missing)), "warning")
        return False

    # Batch number (changed only by the operator, not automatically)
    def current_batch(self):
        return self._batch.text().strip()

    def set_batch(self, batch):
        """Batch chosen on the sign-in batch step. Saved by _on_batch_changed."""
        batch = (batch or "").strip()
        if batch != self.current_batch():
            self._batch.setText(batch)
        config.save_batch_day(batch)           # confirmed today, even if unchanged
        self._day_bar.setVisible(False)
        self._log(f"Batch number set by operator: {batch or '—'}")
        if batch:
            self._activity("info", tr("Batch set to {batch}", batch=batch))

    # New-day suggestion (the operator decides)
    def _check_batch_day(self):
        """Once a minute, offer the next number if the batch is from an earlier day.

        Only shows a bar; it doesn't change anything."""
        cur = self.current_batch()
        nxt = next_batch(cur) if cur else None
        day = config.batch_is_from_earlier_day(cur) if nxt else ""
        if not day:
            self._day_bar.setVisible(False)
            return
        if not self._day_bar.isVisible():
            self._day_lbl.setText(tr("New day — still on batch {batch}.", batch=cur) + " " +
                                  tr("Is today a new batch?"))
            self._day_next_btn.setText(tr("Change to {batch}", batch=nxt))
            self._day_keep_btn.setText(tr("Keep {batch}", batch=cur))
            self._day_bar.setVisible(True)
            self._activity("info", tr("New day — batch {batch} is from an earlier day",
                                      batch=cur))

    @Slot()
    def _day_use_next(self):
        cur = self.current_batch()
        nxt = next_batch(cur)
        if not nxt:
            return
        self._batch.setText(nxt)               # _on_batch_changed records today
        self._log(f"New day: batch changed by operator: {cur} → {nxt}")
        self._activity("info", tr("Batch changed to {batch} (new day)", batch=nxt))
        self.toast.emit(tr("Batch changed to {batch}", batch=nxt), "success")

    @Slot()
    def _day_keep(self):
        cur = self.current_batch()
        config.save_batch_day(cur)             # asked once per day
        self._day_bar.setVisible(False)
        self._log(f"New day: operator kept batch {cur}")
        self._activity("info", tr("Batch {batch} kept for today", batch=cur))

    @Slot()
    def _next_batch_clicked(self):
        """Operator moves on to the next batch: BATCH-001 -> BATCH-002."""
        cur = self.current_batch()
        nxt = next_batch(cur)
        if not nxt:
            self.toast.emit(tr("Enter a valid batch number first (e.g. BATCH-001)"), "warning")
            return
        if not ConfirmDialog.ask(self, tr("Next Batch"), tr("Move to batch {batch}?", batch=nxt),
                                 tr("Pallets from now on are recorded as {new} "
                                    "instead of {old}.", new=nxt, old=cur),
                                 confirm_text=tr("NEXT BATCH")):
            return
        self._batch.setText(nxt)
        self._log(f"Batch number advanced by operator: {cur} → {nxt}")
        self._activity("info", tr("Batch changed to {batch}", batch=nxt))

    @Slot(object)
    def _on_send_complete(self, result):
        if result.get("success"):
            pallet_id = result.get("pallet_id")
            self._report_unmatched(result)
            self._report_moved(result, pallet_id)
            if pallet_id:
                self.toast.emit(tr("Pallet {pallet} created!", pallet=pallet_id), "success")
                n = result.get('palletised_count') or self.session.count()
                self._activity("success", tr("Pallet {pallet} sent · {n} kegs · batch {batch}",
                                             pallet=pallet_id, n=n,
                                             batch=self.session.batch_number or '—'))
                self._log(f"CONFIRMED BY CLOUD: pallet {pallet_id} created "
                          f"({result.get('palletised_count') or self.session.count()} kegs, "
                          f"batch {self.session.batch_number or '—'})")
                self.pallet_created.emit({
                    "session_id": result.get("session_id") or self.session.session_id,
                    "pallet_id": pallet_id,
                    "created_at": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
                    "keg_count": result.get("palletised_count") or self.session.count(),
                    "batch": self.session.batch_number,
                    "status": "CREATED",
                })
                self._queue_label(
                    pallet_id,
                    session_id=result.get("session_id") or self.session.session_id,
                    batch=self.session.batch_number,
                    keg_count=result.get("palletised_count") or self.session.count())
            else:
                self.toast.emit(tr("Pallet sent"), "success")
                self._log("SUCCESS: Batch sent")
                self._activity("success", tr("Pallet sent · {n} kegs", n=self.session.count()))
            self._stack_layer_sent()
            self.session.mark_sent(pallet_label=str(pallet_id) if pallet_id else None)
            QTimer.singleShot(1000, self._reset_session)
            # Refresh the DB-backed used-keg map so the just-created pallet's kegs
            # are recognised (with their real cloud pallet_id) going forward.
            QTimer.singleShot(1200, self._load_used_kegs)
        else:
            self._on_send_not_delivered(result)

    def _on_send_not_delivered(self, result):
        """The report did not reach the cloud, so no pallet was created.

        The two cases need opposite things from the operator. A queued report is
        safe: it is on disk and in the upload queue and will go out by itself, so
        the operator must not re-scan those kegs and the layer can come off. A
        rejected report won't go out on its own and needs someone to act.

        Either way the operator is not left stuck. Nothing is added to the
        confirmed pallet table here; that only happens on a cloud acknowledgement.
        """
        error = result.get("error", "Unknown error")
        session_id = result.get("session_id") or self.session.session_id

        if result.get("queued") or result.get("retryable", True):
            waiting = self._pending_count()
            backlog = f" ({waiting} waiting)" if waiting else ""
            offline = not (self.api_sender and not self.api_sender.is_known_offline())
            if result.get("held") and not offline:
                # Network is fine, but earlier pallets are still uploading. This
                # one goes after them so pallets and labels stay in order.
                self.toast.emit(
                    tr("Pallet queued behind earlier pallets ({n} waiting) — it is sent "
                       "and its label printed in order.", n=waiting) if waiting else
                    tr("Pallet queued behind earlier pallets — it is sent "
                       "and its label printed in order."), "info")
                self._activity("info", (
                    tr("Pallet saved and queued behind earlier pallets ({n} waiting).",
                       n=waiting) if waiting else
                    tr("Pallet saved and queued behind earlier pallets.")) + " " +
                    tr("It will be sent in order."))
            else:
                saved = (tr("No internet — pallet saved on this device ({n} waiting).",
                            n=waiting) if waiting else
                         tr("No internet — pallet saved on this device."))
                self.toast.emit(
                    saved + " " + tr("It is sent automatically when the connection is back."),
                    "warning")
                self._activity("warning", saved + " " + tr("It will be sent and its label "
                                                           "printed automatically."))
            self._log(f"QUEUED: {session_id} is waiting in the upload queue"
                      f"{backlog} — {error}")
            self._log("NOTE: no pallet exists and no label is printed until the "
                      "cloud accepts this report. The kegs stay reserved for it.")
            self.pallet_queued.emit({
                "session_id": session_id,
                "created_at": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
                "keg_count": result.get("qr_count") or self.session.count(),
                "batch": self.session.batch_number,
                "queued": True,
                "attempts": 0,
                "error": error,
            })
            # The layer is done for the operator, so release the session for the
            # next one. Leaving it READY would invite pressing SEND on a report
            # that is already queued, which just re-queues the same payload.
            #
            # The kegs are held in memory under a PENDING label so the
            # duplicate-keg warning still covers them this shift. They are not in
            # the DB's used-keg map (that is cloud-confirmed only), so this guard
            # ends on restart, by which time the queue has usually delivered them.
            self._stack_layer_sent()
            self.session.mark_sent(pallet_label="PENDING UPLOAD")
            QTimer.singleShot(1000, self._reset_session)
        else:
            self.toast.emit(tr("Pallet refused by Keg Management — do not send again.") +
                            " " + tr("Call your supervisor."), "error")
            self._activity("error", tr("Pallet refused by Keg Management — do not send it "
                                       "again, call your supervisor"))
            self._log(f"REJECTED: {session_id} was refused by the cloud and is NOT "
                      f"being retried — {error}")
            self.records_stale.emit()
            # A rejection needs the operator to change something (registration,
            # beer type, a bad code) and try again, so the layer stays loaded.
            self.stack_tracker.layer_refused()
            self.session.state = ScanState.READY
            self._send_btn.setEnabled(True)

    def _pending_count(self):
        """How many reports are waiting to upload (0 if unknown)."""
        try:
            return int(self.api_sender.pending_count()) if self.api_sender else 0
        except Exception:
            return 0

    def _report_startup_backlog(self):
        """Announce a queue that survived the last shutdown."""
        waiting = self._pending_count()
        if not waiting:
            return
        self._log(f"UPLOAD QUEUE: {waiting} report(s) from a previous run are still "
                  f"waiting for the cloud. They will be delivered in capture order "
                  f"as soon as the network allows.")
        self.toast.emit(tr("{n} saved pallet(s) waiting to be sent — see "
                           "Pallet Records", n=waiting), "warning")
        self._activity("info", tr("{n} saved pallet(s) from before are waiting to "
                                  "be sent", n=waiting))

    # Cloud queue acknowledgements
    def _drain_cloud_events(self):
        """Report what the upload queue has been doing, on the GUI thread.

        The retry monitor is a plain background thread with no Qt access, so it
        publishes dicts and this drains them. All operator feedback about delayed
        delivery comes from here: the network going down and back up, the queue
        clearing one report at a time, and each confirmation. A confirmation is
        when a pallet becomes real, gets its row in Pallet Records and gets its
        label printed.
        """
        # Same tick drains the label print thread's events.
        self._drain_print_events()
        if not self.api_sender:
            return
        try:
            events = self.api_sender.drain_events()
        except Exception:
            log.debug("drain_events failed", exc_info=True)
            return

        for ev in events:
            try:
                self._handle_cloud_event(ev)
            except Exception:
                log.exception("cloud event handling failed: %s", ev)

    def _handle_cloud_event(self, ev):
        kind = ev.get("kind")

        if kind == EV_NETWORK_DOWN:
            waiting = ev.get("pending") or 0
            self._log(f"NETWORK DOWN — {ev.get('error', 'no connection')}. New "
                      f"pallets will be held in the upload queue"
                      f"{f' ({waiting} already waiting)' if waiting else ''}.")
            offline = (tr("Offline — keep working.") + " " +
                       tr("Pallets are saved and sent when the connection is back."))
            self.toast.emit(offline, "warning")
            self._activity("warning", offline)
            self._net_seen_down = True

        elif kind == EV_NETWORK_UP:
            waiting = ev.get("pending") or 0
            # Don't announce "reconnected" on the first probe of a healthy
            # start-up. Only say it after a seen failure or when there is a
            # backlog to clear.
            if getattr(self, "_net_seen_down", False):
                # Cloud data may have changed while offline.
                self.network_restored.emit()
            if getattr(self, "_net_seen_down", False) or waiting:
                self._log(f"NETWORK RESTORED. {waiting or 'No'} queued report(s) "
                          f"to deliver.")
                back = (tr("Back online — sending {n} saved pallet(s)", n=waiting)
                        if waiting else tr("Back online"))
                self.toast.emit(back, "success")
                self._activity("success", back)
            self._net_seen_down = False

        elif kind == EV_DRAIN_START:
            total = ev.get("total") or 0
            self._log(f"UPLOAD QUEUE: delivering {total} report(s), oldest first.")
            if total > 1:
                self.toast.emit(tr("Sending {n} saved pallets…", n=total), "info")

        elif kind == EV_ITEM_SENDING:
            self._log(f"UPLOAD QUEUE [{ev.get('index')}/{ev.get('total')}]: sending "
                      f"{ev.get('session_id')}…")

        elif kind == EV_ITEM_CONFIRMED:
            self._on_queued_pallet_confirmed(ev)
            self._load_stack_record()       # a queued layer turns green

        elif kind == EV_STACK_SENT:
            self._on_stack_sent(ev)

        elif kind == EV_STACK_REJECTED:
            self._log(f"STACK REFUSED: {ev.get('stack_id')} — {ev.get('error')}. Nothing of "
                      f"it was written in Keg Management.")
            self.toast.emit(tr("The stack was refused by Keg Management — call your "
                               "supervisor"), "error")
            self._activity("error", tr("Stack refused by Keg Management: {error}",
                                       error=ev.get("error") or "?"))
            self._load_stack_record()
            self.records_stale.emit()

        elif kind == EV_STACK_DEFERRED:
            if ev.get("stack_id") != getattr(self, "_stack_wait_told", None):
                self._stack_wait_told = ev.get("stack_id")
                self._activity("warning", tr("Stack saved on the camera — it is sent when "
                                             "Keg Management can be reached."))
            self._log(f"STACK WAITING: {ev.get('stack_id')} — {ev.get('error')}")
            self._load_stack_record()

        elif kind == EV_ITEM_REJECTED:
            self._load_stack_record()
            self._log(f"UPLOAD QUEUE: {ev.get('session_id')} was REJECTED by the "
                      f"cloud and removed from the queue — {ev.get('error')}. No "
                      f"pallet was created; this needs attention.")
            self.toast.emit(tr("A saved pallet was refused by Keg Management — call "
                               "your supervisor"), "error")
            self._activity("error", tr("A saved pallet was refused by Keg Management — "
                                       "call your supervisor"))
            self.records_stale.emit()

        elif kind == EV_ITEM_DEFERRED:
            self._log(f"UPLOAD QUEUE: {ev.get('session_id')} still undelivered "
                      f"(attempt {ev.get('attempts')}) — {ev.get('error')}. It stays "
                      f"in the queue and will be retried.")
            self.records_stale.emit()

        elif kind == EV_DRAIN_DONE:
            confirmed, remaining = ev.get("confirmed") or 0, ev.get("remaining") or 0
            if confirmed or remaining:
                self._log(f"UPLOAD QUEUE: {confirmed} delivered this pass, "
                          f"{remaining} still waiting.")
            if confirmed and not remaining:
                self.toast.emit(tr("All saved pallets have been sent"), "success")
                self._activity("success", tr("All saved pallets have been sent"))
            self.records_stale.emit()

    def _on_queued_pallet_confirmed(self, ev):
        """A queued report just became a real pallet: acknowledge and print.

        The cloud issues the pallet ID, so this is the earliest the label for a
        pallet built while offline can be printed.
        """
        session_id = ev.get("session_id")
        pallet_id = ev.get("pallet_id")
        index, total = ev.get("index"), ev.get("total")
        position = f" [{index}/{total}]" if total else ""

        if not pallet_id:
            # A 2xx with no paletteId: accepted, but there is nothing to print.
            self._log(f"UPLOAD QUEUE{position}: {session_id} was accepted by the "
                      f"cloud but no pallet ID came back — no label can be printed. "
                      f"Check the pallet in the cloud.")
            self.toast.emit(tr("Saved pallet sent, but no pallet number came back — no "
                               "label.") + " " + tr("Tell your supervisor."), "warning")
            self._activity("warning", tr("A saved pallet was sent but no pallet number came "
                                         "back — no label printed.") + " " +
                           tr("Tell your supervisor."))
            self.records_stale.emit()
            return

        self._log(f"CONFIRMED BY CLOUD{position}: queued report {session_id} is now "
                  f"pallet {pallet_id}")
        self.toast.emit(tr("Saved pallet {pallet} sent", pallet=pallet_id), "success")
        self._activity("success", tr("Saved pallet {pallet} sent", pallet=pallet_id))

        self.pallet_created.emit({
            "session_id": session_id,
            "pallet_id": pallet_id,
            "created_at": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
            "keg_count": "",        # filled in by the Records refresh below
            "batch": "",
            "status": "CREATED",
        })
        # Re-read from the DB so the row shows its real keg count and batch
        # instead of the blanks above, and the pending table drops it.
        self.records_stale.emit()

        # Its kegs are only now in a cloud-sent pallet, so reload them for the
        # already-palletized warning.
        self._load_used_kegs()

        summary = {}
        try:
            if self.database:
                summary = self.database.get_session_summary(session_id)
        except Exception:
            log.debug("get_session_summary failed", exc_info=True)
        self._queue_label(pallet_id, session_id=session_id,
                          batch=summary.get("batch", ""),
                          keg_count=summary.get("keg_count", ""))

    # Label printing (everything goes through the single print thread)
    def _queue_label(self, pallet_id, session_id="", batch="", keg_count="",
                     reprint=False):
        """Hand a label to the print queue and return immediately.

        The print thread saves it and prints it in turn, or keeps it until the
        printer is back."""
        if self.print_queue is None:
            self._log(f"No print queue — label for {pallet_id} not printed")
            return
        self.print_queue.enqueue(pallet_id, session_id=session_id, batch=batch,
                                 keg_count=keg_count, reprint=reprint)
        if self.print_queue.is_waiting_for_printer():
            self.toast.emit(tr("Printer not ready — label for {pallet} saved, it "
                               "prints when the printer is back", pallet=pallet_id), "warning")
            self._log(f"Label for {pallet_id} queued — waiting for the printer")
        else:
            self.toast.emit(tr("Reprinting label for {pallet}…", pallet=pallet_id) if reprint
                            else tr("Printing label for {pallet}…", pallet=pallet_id), "info")
            self._log(f"Label for {pallet_id} queued{' (reprint)' if reprint else ''}")
        self._update_labels_badge()

    @Slot(str, str, str, str)
    def reprint_label(self, pallet_id, session_id="", batch="", keg_count=""):
        """REPRINT from Pallet Records."""
        self._activity("info", tr("Reprint requested for pallet {pallet}", pallet=pallet_id))
        self._queue_label(pallet_id, session_id=session_id, batch=batch,
                          keg_count=keg_count, reprint=True)

    def _drain_print_events(self):
        if self.print_queue is None:
            return
        try:
            events = self.print_queue.drain_events()
        except Exception:
            log.debug("print drain_events failed", exc_info=True)
            events = []
        for ev in events:
            try:
                self._handle_print_event(ev)
            except Exception:
                log.exception("print event handling failed: %s", ev)
        self._update_labels_badge()

    def _handle_print_event(self, ev):
        kind = ev.get("kind")
        pallet_id = ev.get("pallet_id")

        if kind == EV_LABEL_PRINTED:
            self.toast.emit(tr("Label printed for pallet {pallet}", pallet=pallet_id), "success")
            self._activity("success", tr("Label printed for pallet {pallet}", pallet=pallet_id))
            self._printer_warn.setVisible(False)
            self.records_stale.emit()

        elif kind == EV_LABEL_RETRY:
            self._log(f"LABEL {pallet_id}: print attempt {ev.get('attempts')} failed — "
                      f"{ev.get('error')}. Retrying.")
            self._show_printer_banner(str(ev.get("error") or tr("Print job failed"))[:80])

        elif kind == EV_LABEL_FAILED:
            self.toast.emit(tr("Label for {pallet} did NOT print — use REPRINT in "
                               "Pallet Records", pallet=pallet_id), "error")
            self._activity("error", tr("Label for pallet {pallet} did NOT print — check "
                                       "the printer, then press REPRINT in Pallet Records",
                                       pallet=pallet_id))
            self._log(f"LABEL FAILED: {pallet_id} — {ev.get('error')}")
            self._show_printer_banner(str(ev.get("error") or tr("Print job failed"))[:80])
            self.records_stale.emit()

        elif kind == EV_PRINTER_WAIT:
            waiting = ev.get("pending") or 0
            reason = str(ev.get("reason") or tr("Printer not ready"))
            self._log(f"PRINTER NOT READY: {reason} — {ev.get('detail', '')} — "
                      f"{waiting} label(s) waiting")
            self._show_printer_banner(reason, ev.get("hint", ""), ev.get("detail", ""),
                                      ev.get("supported", ""))
            self.toast.emit(tr("Printer problem — {reason}.", reason=tr(reason)) + " " +
                            tr(ev.get("hint") or ""), "warning")
            if waiting:
                self._activity("warning", tr("Printer not ready — {n} label(s) waiting.",
                                             n=waiting) + " " +
                               tr("They print by themselves when it is back."))

        elif kind == EV_PRINTER_BACK:
            waiting = ev.get("pending") or 0
            self._printer_warn.setVisible(False)
            self._log(f"PRINTER BACK — printing {waiting} waiting label(s)")
            if waiting:
                self.toast.emit(tr("Printer back — printing {n} waiting "
                                   "label(s)", n=waiting), "success")

        elif kind == EV_LABEL_CHECK:
            ids = ev.get("pallet_ids") or []
            listed = ", ".join(str(i) for i in ids[:4]) + ("…" if len(ids) > 4 else "")
            self.toast.emit(tr("{n} label(s) were cut off by the last shutdown — "
                               "check them in Pallet Records", n=len(ids)), "warning")
            self._activity("warning", tr("Check label(s) for pallet {pallets}: printing was "
                                         "interrupted.", pallets=listed) + " " +
                           tr("REPRINT or mark LABEL OK in Pallet Records."))
            self.records_stale.emit()

    def _update_labels_badge(self):
        n = self.print_queue.pending_count() if self.print_queue is not None else 0
        if n == self._labels_shown:
            return
        self._labels_shown = n
        if n:
            self._labels_badge.set_status(
                tr("{n} label to print", n=n) if n == 1 else
                tr("{n} labels to print", n=n), C["amber_dark"],
                tr("Pallet labels waiting for the printer — they print one at a time, "
                   "in order"))
        self._labels_badge.setVisible(bool(n))

    def _report_unmatched(self, result):
        """Surface codes the cloud could not match to a keg.

        A short layer still counts as success: the cloud palletises what it
        matched and records the rest. The operator still needs to know a keg is
        not on the pallet.

        The log line is written immediately, but the toast is deferred. A
        successful send fires "Pallet created" then "Printing label…" quickly,
        and there is only one Toast widget, so an immediate warning would be
        overwritten before it could be read.
        """
        unmatched = result.get("unmatched") or []
        if not unmatched:
            return
        palletised = result.get("palletised_count")
        sent = result.get("qr_count")
        self._log(f"UNMATCHED: sent {sent}, palletised {palletised} — "
                  f"{len(unmatched)} code(s) matched no keg: "
                  f"{', '.join(str(u) for u in unmatched)}")

        self._activity("warning", tr("{n} keg(s) not accepted by Keg "
                                     "Management — they are NOT on the pallet",
                                     n=len(unmatched)))
        codes = ", ".join(str(u) for u in unmatched[:4])
        more = "…" if len(unmatched) > 4 else ""
        message = tr("{n} keg(s) not matched by cloud — NOT on the "
                     "pallet: {codes}", n=len(unmatched), codes=codes + more)
        # Land after the created/printing toasts, and hold longer than the 3 s
        # default so it is not missed.
        QTimer.singleShot(4000, lambda: self.toast.emit(message, "warning"))

    def _report_moved(self, result, pallet_id):
        """Say exactly which kegs left which pallet.

        A move is the only action here that edits an existing pallet's record,
        so it is logged in full. Otherwise a keg would quietly vanish from an old
        pallet with no way to account for it.

        The toast is deferred for the same reason as _report_unmatched: the
        created/printing toasts fire first and there is only one Toast widget.
        """
        moved = result.get("moved") or []
        if not moved:
            # No local record held these kegs (the cloud may already have moved
            # them). Only log it if a move was actually requested.
            requested = self.session.move_list()
            if requested:
                self._log(f"MOVE: {len(requested)} keg(s) approved to move, but no local "
                          f"pallet record still held them — cloud assignment stands")
            return

        to_label = pallet_id or "the new pallet"
        pairs = ", ".join(f"{m['code']} (was on {m['from_pallet']})" for m in moved[:4])
        more = "…" if len(moved) > 4 else ""
        self._log(f"MOVED: {len(moved)} keg(s) removed from their previous pallet and "
                  f"placed on {to_label}: {pairs}{more}")

        shown_label = pallet_id or tr("the new pallet")
        self._activity("info", tr("{n} keg(s) moved onto pallet {pallet}",
                                  n=len(moved), pallet=shown_label))
        message = tr("{n} keg(s) moved off their previous pallet onto {pallet}",
                     n=len(moved), pallet=shown_label)
        QTimer.singleShot(5500, lambda: self.toast.emit(message, "success"))

    def _check_printer_ready(self):
        """Check whether the label printer is ready and update the warning banner.

        The amber banner stays visible (unlike a toast) so the operator always
        knows labels are waiting for the printer. Returns the status dict.
        """
        try:
            from modules.printer import probe_printer
            st = probe_printer()
        except Exception as e:
            self._log(f"Printer check failed: {e}")
            self._last_printer_status = {"ready": False, "summary": f"Probe failed: {e}"}
            self._show_printer_banner(tr("Probe failed: {error}", error=e))
            return self._last_printer_status

        self._last_printer_status = st
        if st.get("ready"):
            # Printer connected: hide the warning banner.
            self._printer_warn.setVisible(False)
            self._log(f"Printer ready: {st.get('printer_name', 'unknown')} "
                      f"({st.get('backend', 'unknown')})")
        else:
            detail = (st.get("device_msg", "") if not st.get("device_ok")
                      else st.get("render_msg", ""))
            summary = st.get("summary", "Printer unavailable")
            self._log(f"PRINTER NOT READY: {summary} — {detail}")
            self._show_printer_banner(summary, st.get("hint", ""), detail,
                                      st.get("supported", ""))
            self.toast.emit(tr("Printer problem — {reason}.", reason=tr(summary)) + " " +
                            tr(st.get("hint") or ""), "warning")
        return st

    def _show_printer_banner(self, reason: str, hint="", detail="", supported=""):
        """Display the persistent amber printer-not-ready banner: what is wrong
        and what to do. The technical detail and the supported printers are in
        its tooltip."""
        text = tr("Printer problem — {reason}.", reason=tr(reason))
        if hint:
            text += " " + tr(hint)
        self._printer_warn_lbl.setText(
            text + " " +
            tr("Labels are kept and print by themselves once the printer is ready."))
        self._printer_warn.setToolTip("\n\n".join(t for t in (detail, supported) if t))
        self._printer_warn.setVisible(True)

    def _reset_session(self):
        self.session.reset()
        self.stack_tracker.clear_samples()
        self._lower_in_view = set()
        self._keg_boxes = {}
        self._keg_box_cam = {}
        # The next layer goes down where this one stood: no serial remembered
        # for a box of this layer may be given to a keg of the next, and none
        # of its kegs may be guessed for a blurred label of the next.
        self._forget_matched_boxes()
        self._update_match_exclusions()
        self._update_stack_map()
        # Each pallet starts fresh: the next load of repeated kegs raises its own
        # warning instead of inheriting this one's "already asked".
        self._repeat_prompted = False
        self._update_dup_banner()   # flagged set cleared by reset()
        self._rebuild_list()
        self._send_btn.setEnabled(False)
        ignored = self.session.ignored_count()
        if ignored:
            self._log(f"Ready for next pallet ({ignored} previous keg(s) ignored)")

    def _auto_send_and_print(self):
        if self.session.state != ScanState.READY:
            return
        self._log("Auto-sending to server...")
        self._send_to_server()

    # Clear everything remembered
    @Slot()
    def _clear_all_clicked(self):
        """CLEAR ALL: start counting from what is under the camera now.

        Empties the scanned list and forgets every keg remembered from before:
        kegs of pallets and layers already sent (normally ignored or flagged
        as repeats), the stack just finished, and boxes serial matching
        remembered (it can hand a remembered serial to a keg put down in the
        same place). The stack's progress is kept: the layers sent are real.
        NEW STACK starts the stack again.
        """
        if self.session.state == ScanState.SENDING:
            self.toast.emit(tr("Wait until the layer is sent, then clear."), "warning")
            return
        st = self.stack_tracker
        keep = (tr("The stack keeps its {n} layer(s) already sent — press NEW STACK to "
                   "start the stack again.", n=len(st.layers)) if st.open else "")
        if not ConfirmDialog.ask(
                self, tr("Clear All"), tr("Clear the list and everything remembered?"),
                (tr("The scanned list is emptied and kegs remembered from earlier layers "
                    "and pallets are forgotten, so only kegs read from now on count.") +
                 (" " + keep if keep else "")),
                confirm_text=tr("CLEAR ALL")):
            return
        self.session.reset()
        self.session.clear_history()
        st.forget_previous()
        self._keg_boxes = {}
        self._keg_box_cam = {}
        self._overlay_boxes = {}
        self._matched_codes = set()
        self._lower_in_view = set()
        self._repeat_prompted = False
        self._forget_matched_boxes()
        self._info.setVisible(False)
        self._update_dup_banner()
        self._send_btn.setEnabled(False)
        self._refresh_stack()       # the stack's own lower layers stay ignored
        self._rebuild_list()
        self._log("CLEAR ALL by the operator: list emptied, remembered kegs forgotten")
        self.toast.emit(tr("Cleared — only kegs read from now on count"), "success")

    def _update_match_exclusions(self):
        """Serials that serial matching may never give as a match: blacklisted
        kegs, kegs on the layers of this stack, and kegs already sent.

        A blurred label is matched to the nearest known serial. On 2026-11-03
        (14:51) blurred layer-3 labels were "identified" as layer-2 kegs
        (04984, 04181) that sat right under them. A keg already sent is still
        counted when its label really reads; it just cannot be guessed."""
        s = self.session
        codes = (set(self.blacklist) | set(s.stack_qrs) | set(s.last_sent_qr_set)
                 | set(s.sent_qr_history))
        if codes == getattr(self, "_match_excluded", None):
            return
        self._match_excluded = codes
        try:
            from modules import serial_match
            serial_match.set_excluded(codes)
        except Exception:
            log.exception("serial list exclusion update failed")

    @staticmethod
    def _forget_matched_boxes():
        try:
            from modules import serial_match
            serial_match.forget_boxes()
        except Exception:
            log.debug("serial match boxes not cleared", exc_info=True)

    # Keg registration (Camera Keg Registration API)
    def _start_worker(self, worker):
        worker.finished.connect(
            lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    def _queue_keg_check(self, code):
        """Ask the cloud once whether a newly seen keg is registered.

        The check runs on a worker thread and the keg is counted at once. A
        complete layer waits for the answer before it is sent, and is held
        while any keg is unregistered (_registration_hold)."""
        if (not self._keg_reg_enabled or self.api_sender is None or not code
                or code in self._keg_reg or code in self._keg_check_queue):
            return
        if not getattr(self.api_sender, "network_online", True):
            return   # checked again the next time it is detected as new
        if len(self._keg_reg) > 5000:
            self._keg_reg.clear()   # keep the memory use bounded on long shifts
        self._keg_check_queue.append(code)
        self._run_keg_checks()

    def _run_keg_checks(self):
        if self._keg_check_busy or not self._keg_check_queue:
            return
        codes, self._keg_check_queue = self._keg_check_queue, []
        self._keg_checking = set(codes)
        self._keg_check_busy = True
        worker = KegCheckWorker(self.api_sender, codes, self)
        worker.checked.connect(self._on_keg_checked)
        worker.finished.connect(self._on_keg_checks_done)
        self._start_worker(worker)

    def _on_keg_checks_done(self):
        self._keg_check_busy = False
        self._keg_checking = set()
        self._run_keg_checks()   # codes that arrived while this batch ran

    @Slot(str, object)
    def _on_keg_checked(self, code, res):
        state = (res or {}).get("state")
        if state == "error":
            # Not stored, so the keg is checked again the next time it is seen.
            if not self._keg_check_err_logged:
                self._keg_check_err_logged = True
                self._log(f"Keg check unavailable: {str(res.get('reason'))[:120]}")
            return
        self._keg_check_err_logged = False
        self._keg_reg[code] = state
        if state == "unknown":
            self._log(f"KEG REGISTRATION: {code} is not registered (can be registered)")
            if code in self.session.qr_codes:
                self._rebuild_list()
        elif state == "blocked":
            reason = res.get("reason") or ""
            self._log(f"KEG REGISTRATION: {code} cannot be registered: {reason}")
            self.toast.emit(tr("Unknown keg {code}: {reason}", code=code, reason=reason),
                            "warning")

    def _refresh_keg_form(self):
        """Background refresh of the cached form. The result only updates the
        cache file; the REGISTER button always asks for a fresh copy."""
        if (not self._keg_reg_enabled or self.api_sender is None or self._keg_form_busy
                or not getattr(self.api_sender, "network_online", True)):
            return
        worker = KegFormFetcher(self.api_sender, self)
        self._start_worker(worker)

    # Bulk selection (tick boxes on unregistered rows + the bar above the list)
    def _update_reg_bar(self):
        unreg = self._unreg_codes
        self._reg_bar.setVisible(self._keg_reg_enabled and bool(unreg))
        if not unreg:
            return
        n_sel = len(self._keg_selected)
        all_on = n_sel == len(unreg)
        self._reg_all_btn.setIcon(svg_icon("square-check" if all_on else "square",
                                           C['unreg'], 12))
        self._reg_all_btn.setText(" " + tr("Select all"))
        info = tr("{n} not registered", n=len(unreg))
        if n_sel:
            info += " · " + tr("{n} selected", n=n_sel)
        self._reg_info.setText(info)
        self._reg_sel_btn.setText(" " + tr("Register selected ({n})", n=n_sel))
        self._reg_sel_btn.setEnabled(n_sel > 0 and self._keg_bulk is None)

    @Slot(str, bool)
    def _toggle_keg_selected(self, code, on):
        if on:
            self._keg_selected.add(code)
        else:
            self._keg_selected.discard(code)
        self._update_reg_bar()

    def _toggle_select_all(self):
        """Select every unregistered keg, or clear the selection if all are."""
        unreg = set(self._unreg_codes)
        self._keg_selected = set() if self._keg_selected >= unreg else unreg
        self._rebuild_list()

    def _register_selected(self):
        codes = [c for c in self._unreg_codes if c in self._keg_selected]
        if not codes:
            self.toast.emit(tr("Select the kegs to register first"), "info")
            return
        self._open_registration(codes)

    def _register_keg(self, code):
        """REGISTER on a list row: fetch the form, then show it."""
        self._open_registration([code])

    def _open_registration(self, codes):
        """Fetch the form, then show it for one keg or several."""
        if self._keg_reg_dialog is not None or self._keg_form_busy:
            return
        if self._keg_bulk is not None:
            self.toast.emit(tr("Kegs are still being registered. Please wait."), "info")
            return
        if self.api_sender is None:
            self.toast.emit(tr("Cloud connection is not ready yet"), "error")
            return
        self._keg_form_busy = True
        self._keg_form_codes = list(codes)
        worker = KegFormFetcher(self.api_sender, self)
        worker.result_ready.connect(self._open_keg_form)
        self._start_worker(worker)

    @Slot(object, object)
    def _open_keg_form(self, form, warning):
        self._keg_form_busy = False
        codes = self._keg_form_codes
        if not codes or self._keg_reg_dialog is not None:
            return
        if not form or not form.get("fields"):
            self._log(f"Keg form unavailable: {warning}")
            self.toast.emit(tr("Could not load the registration form. Try again "
                               "when the cloud is reachable."), "error")
            return
        if warning:
            self._log(f"Keg form: using the cached copy ({str(warning)[:120]})")
        dlg = KegRegisterDialog(codes[0] if len(codes) == 1 else codes, form, self)
        dlg.submitted.connect(self._send_keg_registration)
        dlg.finished.connect(self._on_keg_form_closed)
        self._keg_reg_dialog = dlg
        dlg.open()

    @Slot(int)
    def _on_keg_form_closed(self, _result):
        self._keg_reg_dialog = None

    @Slot(object)
    def _send_keg_registration(self, values):
        dlg = self._keg_reg_dialog
        if dlg is None:
            return
        if len(dlg.codes) > 1:
            self._send_bulk_registration(dlg, values)
            return
        worker = KegRegisterWorker(self.api_sender, dlg.code, values, dlg.form_version, self)
        worker.done.connect(self._on_keg_registered)
        self._start_worker(worker)

    @Slot(str, object)
    def _on_keg_registered(self, code, res):
        dlg = self._keg_reg_dialog
        if dlg is not None and dlg.code != code:
            dlg = None
        if res.get("ok"):
            self._keg_reg[code] = "registered"
            self._keg_selected.discard(code)
            self._log(f"KEG REGISTRATION: {code} registered (HTTP {res.get('status')})")
            self.toast.emit(tr("Keg {code} registered", code=code), "success")
            self._activity("success", tr("Keg registered: {code}", code=code))
            if dlg is not None:
                dlg.finish()
            self._rebuild_list()
            self._resume_after_registration()
            return
        message = res.get("message") or tr("Registration failed. Try again.")
        if res.get("status") == 422 and res.get("error") == "PREFIX_MISMATCH":
            self._keg_reg[code] = "blocked"
            self._rebuild_list()
        if dlg is not None:
            dlg.show_errors(message, res.get("fieldErrors"))
        else:
            # The operator closed the form while it was sending.
            self.toast.emit(tr("Keg {code} not registered: {msg}", code=code, msg=message),
                            "error")

    def _send_bulk_registration(self, dlg, values):
        """Register every keg in the form with the same answers, one at a time."""
        codes = list(dlg.codes)
        self._keg_bulk = {"dlg": dlg, "codes": codes, "done": 0,
                          "ok": [], "blocked": [], "failed": None}
        dlg.set_progress(0, len(codes))
        self._update_reg_bar()
        self._log(f"KEG REGISTRATION: registering {len(codes)} kegs")
        worker = KegBulkRegisterWorker(self.api_sender, codes, values, dlg.form_version, self)
        worker.progress.connect(self._on_bulk_keg_registered)
        worker.finished.connect(self._on_bulk_registration_done)
        self._start_worker(worker)

    @Slot(str, object)
    def _on_bulk_keg_registered(self, code, res):
        bulk = self._keg_bulk
        if bulk is None:
            return
        bulk["done"] += 1
        if res.get("ok"):
            self._keg_reg[code] = "registered"
            self._keg_selected.discard(code)
            bulk["ok"].append(code)
            self._log(f"KEG REGISTRATION: {code} registered (HTTP {res.get('status')})")
        elif res.get("error") == "PREFIX_MISMATCH":
            self._keg_reg[code] = "blocked"
            self._keg_selected.discard(code)
            bulk["blocked"].append(code)
            self._log(f"KEG REGISTRATION: {code} cannot be registered: {res.get('message')}")
        else:
            bulk["failed"] = (code, res)
        if self._keg_reg_dialog is bulk["dlg"]:
            bulk["dlg"].set_progress(bulk["done"], len(bulk["codes"]))

    def _on_bulk_registration_done(self):
        bulk, self._keg_bulk = self._keg_bulk, None
        if bulk is None:
            return
        n_ok, total = len(bulk["ok"]), len(bulk["codes"])
        if n_ok:
            self._activity("success", tr("{n} keg(s) registered", n=n_ok))
        for code in bulk["blocked"]:
            self._activity("warning", tr("Keg {code} cannot be registered here", code=code))
        self._rebuild_list()
        self._resume_after_registration()
        dlg = bulk["dlg"] if self._keg_reg_dialog is bulk["dlg"] else None
        done = set(bulk["ok"]) | set(bulk["blocked"])
        remaining = [c for c in bulk["codes"] if c not in done]
        if bulk["failed"] is None or not remaining:
            if dlg is not None:
                dlg.finish()
            if bulk["blocked"]:
                self.toast.emit(tr("{ok} of {n} keg(s) registered. {b} cannot be registered here.",
                                   ok=n_ok, n=total, b=len(bulk["blocked"])), "warning")
            else:
                self.toast.emit(tr("{n} keg(s) registered", n=n_ok), "success")
            return
        # Stopped on a refusal that would hit the rest too (bad answers,
        # sign-in, network). Keep the form open on the kegs still to do.
        code, res = bulk["failed"]
        message = res.get("message") or tr("Registration failed. Try again.")
        self._log(f"KEG REGISTRATION: bulk stopped at {code}: {message}")
        summary = tr("{ok} of {n} registered. Stopped at {code}: {msg}",
                     ok=n_ok, n=total, code=code, msg=message)
        if dlg is not None:
            dlg.set_codes(remaining)
            dlg.show_errors(summary, res.get("fieldErrors"))
        else:
            self.toast.emit(summary, "error")

    # Cloud blacklist
    def fetch_blacklist(self):
        if not self.api_sender or self._blacklist_busy:
            return
        self._blacklist_busy = True
        worker = BlacklistFetcher(self.api_sender, self)
        worker.result_ready.connect(self._update_blacklist)
        worker.failed.connect(lambda e: self._log(f"Blacklist error: {str(e)[:50]}"))
        worker.finished.connect(lambda: setattr(self, "_blacklist_busy", False))
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        self._workers.append(worker)
        worker.start()

    @Slot(object)
    def _update_blacklist(self, entries):
        """Apply a blacklist from the loading screen, a periodic refresh, or the
        Blacklist page.

        When the cloud is unreachable, get_blacklist() already serves the last
        fetched copy, so this is always the latest available."""
        # Use api_sender.blacklist_codes() for key extraction. It knows about
        # "serialNo", the field the live cloud returns; missing it means every
        # flagged keg is silently ignored.
        from modules.api_sender import blacklist_codes
        entries = list(entries or [])
        if entries == self._blacklist_entries:
            return                      # periodic refresh, nothing changed
        self._blacklist_entries = entries
        codes = blacklist_codes(entries)
        if codes == self.blacklist:
            return                      # details changed, flagged codes did not
        added = codes - self.blacklist
        self.blacklist = codes
        # A blacklisted keg can never be the result of serial matching.
        self._update_match_exclusions()
        self._log(f"Blacklist loaded: {len(codes)} flagged code(s)")
        # A keg already on the pallet being built can become blocked by a
        # refresh, so warn now instead of waiting for it to be newly detected.
        for code in self.session.qr_list():
            if code in added and code not in self._warned_blacklist:
                self._warned_blacklist.add(code)
                self.toast.emit(tr("Blocked keg: {code} — set it aside", code=code), "warning")
                self._log(f"BLACKLIST: keg {code} on this pallet was just flagged")
                self._activity("warning", tr("Blocked keg on this pallet: {code} — "
                                             "set it aside", code=code))
        self._rebuild_list()

    def _on_count_changed(self, v):
        self.required_keg_count = v
        self.session.target_count = v
        self._stat_target.set_value(v)
        config.save_kegs_per_layer(v)
        self._log(f"Kegs per layer updated to {v}")
        self._update_stack_map()

    def _on_batch_changed(self, text):
        self.last_batch_number = text
        try:
            save_last_batch(text)
            config.save_batch_day(text)        # the operator set it today
        except Exception:
            pass
        self._day_bar.setVisible(False)
        self._update_batch_validation(text)

    def _update_batch_validation(self, text):
        """Show inline red feedback when the batch number is malformed.

        Stays quiet while the field is empty (the placeholder is enough) and
        shows a message once the operator has typed something invalid.
        """
        ok, message = validate_batch(text)
        show_err = (not ok) and bool((text or "").strip())
        self._batch_err.setText(message if show_err else "")
        self._batch_err.setVisible(show_err)
        self._batch.setProperty("invalid", show_err)
        self._restyle(self._batch)

    # Cloud sync, network, recovery
    def sync_cloud(self):
        if self._syncing:
            return
        if not self.is_auto_mode:
            self._log("Manual mode — using local config")
            return
        self._syncing = True
        worker = CloudSyncWorker(self)
        worker.success.connect(self._on_sync_success)
        worker.failed.connect(self._on_sync_fail)
        worker.finished.connect(lambda: setattr(self, "_syncing", False))
        worker.finished.connect(lambda *_: self._workers.remove(worker) if worker in self._workers else None)
        self._workers.append(worker)
        worker.start()

    @Slot(int, str)
    def _on_sync_success(self, count, keg_type):
        # Refreshed every few minutes. Apply only when the cloud's value
        # changed, so a refresh doesn't undo the operator's own target change
        # with the same old cloud number.
        self._last_sync_fail = None
        if count == self._cloud_keg_count:
            return
        self._cloud_keg_count = count
        self.required_keg_count = count
        self.session.target_count = count
        self._count.setValue(count)
        self._stat_target.set_value(count)
        self._log(f"Cloud sync: {count} {keg_type} kegs")
        self.toast.emit(tr("Pallet size set to {n} kegs by Keg Management", n=count), "info")

    @Slot(str)
    def _on_sync_fail(self, msg):
        # Refreshed often, so log a given failure once, not every time.
        if msg != getattr(self, "_last_sync_fail", None):
            self._log(msg)
        self._last_sync_fail = msg

    def _check_network(self):
        if not self.api_sender:
            return
        try:
            online = bool(self.api_sender.get_network_status())
        except Exception:
            online = False
        self.net_changed.emit(online)

    def _recover_batches(self):
        if not self.database:
            return
        try:
            stuck = self.database.get_stuck_batches(timeout_minutes=5)
            if stuck:
                self._log(f"Recovered {len(stuck)} stuck batches")
        except Exception:
            pass

    @Slot()
    def reload_used_kegs(self):
        """Public entry point for _load_used_kegs.

        Used when a pallet record is removed elsewhere in the app and its kegs
        become free again."""
        self._load_used_kegs()

    def _load_used_kegs(self):
        """Refresh the DB-backed map of kegs already in a cloud-sent pallet.

        Runs at startup, after every confirmed send (including ones delivered
        from the upload queue) and after a pallet record is removed, so the
        already-palletized warning matches the DB, also across restarts.
        """
        if not self.database:
            return
        try:
            used = self.database.get_used_kegs()
            self.session.set_used_kegs(used)
            self._log(f"Loaded {len(used)} keg(s) already in pallets")
        except Exception as e:
            log.warning("get_used_kegs failed: %s", e)

    # Logging and shutdown
    def _log(self, msg):
        """Technical log line to stdout (ends up in logs/app.log)."""
        print(msg)

    def _activity(self, kind, text):
        """Add a plain-language event to the operator's Recent activity.

        kind is success / info / warning / error. Keep the text free of codes
        and jargon."""
        self.activity.emit(kind, text)

    def shutdown(self):
        # Stop creating new work first: UI timers, the detection loop, the
        # detector and the camera. The APISender is not closed here on purpose:
        # process_worker.shutdown() drains any in-flight upload (using the
        # still-open session) and then closes the sender in the right order.
        self._display_timer.stop()
        self._net_timer.stop()
        self._cloud_timer.stop()
        self._keg_form_timer.stop()
        if self._detect_worker:
            self._detect_worker.stop()
        # The serial-list refresh uses the APISender session, so it stops
        # before process_worker closes that session.
        try:
            from modules import serial_match
            serial_match.stop()
        except Exception:
            log.exception("serial list stop error")
        # Finish the label in hand; anything unprinted stays in print_queue.db
        # and prints on the next start.
        if self.print_queue is not None:
            try:
                self.print_queue.stop()
            except Exception:
                log.exception("print queue stop error")

        # Wait for the short-lived worker QThreads (batch submit, print, cloud
        # sync, blacklist fetch, advanced decode) before tearing anything down.
        # Otherwise Qt can destroy a running QThread on exit ("QThread: Destroyed
        # while thread is still running", then abort) and drop an in-flight
        # print or upload. Iterate over a copy because each worker's finished
        # handler mutates self._workers.
        for w in list(self._workers):
            try:
                if w.isRunning():
                    w.quit()          # ask its event loop (if any) to stop
                    if not w.wait(5000):
                        log.warning("worker %s did not finish in time; terminating",
                                    w.__class__.__name__)
                        w.terminate()
                        w.wait(500)
            except Exception:
                log.exception("worker shutdown error")
        self._workers.clear()

        try:
            if self.qr_detector:
                self.qr_detector.shutdown()
        except Exception:
            pass
        try:
            if self.camera:
                self.camera.stop()
        except Exception:
            pass
