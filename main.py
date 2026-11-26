#!/usr/bin/env python3
"""
Keg Palletization HMI: PySide6 desktop application.

The visual style follows the approved VisionCore dashboard demo. The operator
workflow and all backend integrations (camera, YOLO+QR detector, session state
machine, cloud send, Zebra printer, SQLite) live in modules/ and are driven
here through QThread workers. The old Kivy UI is archived as main_kivy_backup.py.

Architecture (3 layers):
    modules/                - headless backend (framework-agnostic)
    ui_pyside/workers.py    - QThreads wrapping the backend (no GUI-thread blocking)
    ui_pyside/*.py + main   - PySide6 shell: top bar + icon sidebar + QStackedWidget

Run:
    pip install PySide6 opencv-python
    python main.py
"""

import os
import sys
import logging
import signal
import time

# Kiosk mode (production on the iCam-540): full-screen and the operator cannot
# reach the OS. The production launcher enables it with KEG_KIOSK=1.
KIOSK = os.environ.get("KEG_KIOSK") == "1"

# Set only by the in-app refresh relaunch (_relaunch below). The camera daemon
# still holds its 4K configuration and focus lock (CameraManager.stop() only
# releases the OpenCV capture), so the REST init and auto-focus are skipped.
# Popped so it applies to this launch only and child processes don't inherit it.
SKIP_CAMERA_INIT = os.environ.pop("KEG_SKIP_CAMERA_INIT", "") == "1"

from PySide6.QtCore import Qt, QTimer, Slot, Signal, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QFrame, QProgressBar,
)

import config
from config import (
    CAMERA_INIT_ENABLED, CAMERA_NUMBER,
    SPLASH_CAMERA_ACTIONS_AFTER, OPERATOR_LOGIN_REQUIRED,
)
from ui_pyside.theme import C, GLOBAL_QSS, IS_DARK, now_time, now_date
from ui_pyside import i18n
from ui_pyside.i18n import tr, N_
from ui_pyside.widgets import (NavButton, vline, ConfirmDialog, ExitChoiceDialog, BrandLogo,
                               svg_icon, svg_pixmap, IconLabel, LanguageDialog,
                               language_button, update_language_button)
from ui_pyside.workers import InitWorker, CameraInitBridge, AuthWorker
from ui_pyside.operator_page import OperatorPage
from ui_pyside.pages import RecordsPage, SettingsPage
from ui_pyside.login_page import LoginPage
from ui_pyside.maintenance import authorize as authorize_maintenance
from ui_pyside.maintenance import set_hooks as set_maintenance_hooks
from modules.camera_auth import get_camera_auth, CREDENTIAL_CODES
from modules.local_data import wipe_local_data, count_unsent_reports
from modules import disk_check

logging.basicConfig(level=logging.INFO,
                    format="[%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("main")


# Top bar
class TopBar(QWidget):
    exit_requested = Signal()
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("topBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(52)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(14)

        lay.addWidget(BrandLogo(height=26, plaque="none",
                                variant="white" if IS_DARK else "black"))
        brand = QLabel("KEG PALLETIZATION")
        brand.setObjectName("brandName")
        lay.addWidget(brand)
        lay.addWidget(vline(color=C["chrome_border"]))

        self._net = IconLabel("wifi", tr("ONLINE"), C["green_dark"], size=11, pad=(0, 0))
        lay.addWidget(self._net)
        lay.addWidget(vline(color=C["chrome_border"]))

        cam = QLabel(CAMERA_NUMBER)
        cam.setObjectName("topMeta")
        lay.addWidget(cam)

        lay.addStretch()

        self._date = QLabel(now_date())
        self._date.setObjectName("topMeta")
        lay.addWidget(self._date)
        lay.addWidget(vline(color=C["chrome_border"]))

        self._mode = QLabel(tr("AUTO MODE"))
        self._mode.setObjectName("modeChip")
        self._mode.setFixedHeight(24)     # a chip, not a block the bar's height
        lay.addWidget(self._mode)
        lay.addWidget(vline(color=C["chrome_border"]))

        self._clock = QLabel(now_time())
        self._clock.setObjectName("clockLbl")
        self._clock.setFixedWidth(84)
        self._clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._clock)

        # Refresh restarts the app (model, camera stream, cloud data) without
        # re-running camera auto-focus, for a long-running kiosk whose focus is
        # already right. See MainWindow.request_refresh.
        refresh_btn = QPushButton(tr("  Restart"))
        refresh_btn.setObjectName("refreshBtn")
        refresh_btn.setIcon(svg_icon("rotate-ccw", "#FFFFFF", 15))
        refresh_btn.setIconSize(QSize(15, 15))
        refresh_btn.setFixedHeight(36)
        refresh_btn.setMinimumWidth(96)
        refresh_btn.setToolTip(tr("Restart the app — the camera focus is kept"))
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.clicked.connect(self.refresh_requested.emit)
        lay.addWidget(refresh_btn)
        self._restart_btn = refresh_btn      # (application tour points at it)

        # Exit control. In kiosk mode it is also the emergency safe exit: the
        # operator can shut the app down cleanly (camera released, upload/print
        # queue flushed) if something goes wrong on the line.
        # A deliberate exit asks systemd to stop the user service, so the app
        # stays down until restarted manually. The service keeps
        # Restart=on-failure for crash recovery.
        exit_btn = QPushButton(tr("  Exit"))
        exit_btn.setObjectName("exitBtn")
        exit_btn.setIcon(svg_icon("power", "#FFFFFF", 15))
        exit_btn.setIconSize(QSize(15, 15))
        exit_btn.setFixedHeight(36)
        exit_btn.setMinimumWidth(80)
        exit_btn.setToolTip(tr("Exit the app or shut down the camera"))
        exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        exit_btn.clicked.connect(self.exit_requested.emit)
        lay.addWidget(exit_btn)
        self._exit_btn = exit_btn

        self._t = QTimer(self)
        self._t.timeout.connect(lambda: self._clock.setText(now_time()))
        self._t.start(1000)

    @Slot(str)
    def set_mode(self, mode):
        if mode == "auto":
            self._mode.setText(tr("AUTO MODE"))
        elif mode == "manual":
            self._mode.setText(tr("MANUAL MODE"))
        else:
            self._mode.setText(tr("{mode} MODE", mode=mode.upper()))
        # Both modes share the same neutral chip styling; only the text differs.
        self._mode.setStyleSheet(
            f"color:{C['slate']}; font-size:11px; font-weight:700; padding:3px 10px;"
            f" border-radius:4px; background-color:{C['chrome2']};")

    @Slot(bool)
    def set_network(self, online):
        self._net.set("wifi" if online else "wifi-off", tr("ONLINE") if online else tr("OFFLINE"),
                      C["green_dark"] if online else C["red"])


# Sidebar (icon rail)
class Sidebar(QFrame):
    """Navigation rail: icon and label per page, with the signed-in operator
    and Sign out at the bottom."""
    sign_out_requested = Signal()     # also "Sign in" when running without one
    tour_requested = Signal()         # "App tour": replay the guided tour
    switch_requested = Signal()       # "Switch operator": next person signs in
    theme_requested = Signal()        # "Dark theme" / "Light theme"
    language_requested = Signal()     # screen language picker

    def __init__(self, items, on_select):
        super().__init__()
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(220)
        lay = QVBoxLayout(self)
        # No side margin: the active item's navy bar sits on the sidebar's edge.
        lay.setContentsMargins(0, 12, 0, 12)
        lay.setSpacing(2)

        # Navy mode bar at the top, as on the Keg Management dashboard.
        mode = QLabel(tr("Mode: Palletization"))
        mode.setObjectName("sideMode")
        mode.setFixedHeight(34)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(8, 0, 8, 10)
        mode_row.addWidget(mode)
        lay.addLayout(mode_row)

        self._buttons = []
        for icon, label, page_idx in items:
            btn = NavButton(icon, label)
            btn.clicked.connect(lambda _=False, i=page_idx: on_select(i))
            lay.addWidget(btn)
            self._buttons.append((btn, page_idx))
        lay.addStretch()

        # Replay the guided tour (it also starts by itself for a new operator).
        self._tour_btn = QPushButton(tr("  App tour"))
        self._tour_btn.setObjectName("headerBtn")
        self._tour_btn.setIcon(svg_icon("circle-help", C["ck_navy"], 16))
        self._tour_btn.setIconSize(QSize(16, 16))
        self._tour_btn.setFixedHeight(34)
        self._tour_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tour_btn.setToolTip(tr("A short guided tour of this screen"))
        self._tour_btn.clicked.connect(self.tour_requested.emit)
        tour_row = QHBoxLayout()
        tour_row.setContentsMargins(12, 0, 12, 4)
        tour_row.addWidget(self._tour_btn)
        lay.addLayout(tour_row)

        # Screen language: shows the current one by its own name.
        self._lang_btn = language_button()
        self._lang_btn.clicked.connect(self.language_requested.emit)
        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(12, 0, 12, 4)
        lang_row.addWidget(self._lang_btn)
        lay.addLayout(lang_row)

        # Light / dark screen theme. The button offers the other theme.
        self._theme_btn = QPushButton(tr("  Light theme") if IS_DARK else tr("  Dark theme"))
        self._theme_btn.setObjectName("headerBtn")
        self._theme_btn.setIcon(svg_icon("sun" if IS_DARK else "moon", C["ck_navy"], 16))
        self._theme_btn.setIconSize(QSize(16, 16))
        self._theme_btn.setFixedHeight(34)
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.setToolTip(tr("Change the screen colours (the app restarts)"))
        self._theme_btn.clicked.connect(self.theme_requested.emit)
        theme_row = QHBoxLayout()
        theme_row.setContentsMargins(12, 0, 12, 4)
        theme_row.addWidget(self._theme_btn)
        lay.addLayout(theme_row)

        # Who is on shift, and Sign out. Always visible at the bottom.
        op = QVBoxLayout()
        op.setContentsMargins(12, 10, 12, 0)
        op.setSpacing(8)
        who = QHBoxLayout()
        who.setSpacing(10)
        avatar = QLabel()
        avatar.setPixmap(svg_pixmap("user", C["ck_navy"], 30))
        avatar.setFixedSize(30, 30)
        who.addWidget(avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        cap = QLabel(tr("SIGNED IN"))
        cap.setObjectName("fieldLabel")
        names.addWidget(cap)
        self._op_name = QLabel("")
        self._op_name.setObjectName("sideOperator")
        self._op_name.setWordWrap(True)
        names.addWidget(self._op_name)
        who.addLayout(names, 1)
        op.addLayout(who)
        # Switch operator: the next person signs in without a sign-out first
        # (shown only while someone is signed in).
        self._switch_btn = QPushButton(tr("  Switch operator"))
        self._switch_btn.setObjectName("ghostBtn")
        self._switch_btn.setIcon(svg_icon("users", C["text2"]))
        self._switch_btn.setIconSize(QSize(16, 16))
        self._switch_btn.setFixedHeight(36)
        self._switch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._switch_btn.setToolTip(tr("Hand over to another operator — they sign in, "
                                       "your shift ends"))
        self._switch_btn.clicked.connect(self.switch_requested.emit)
        op.addWidget(self._switch_btn)
        self._sign_btn = QPushButton(tr("Sign in"))
        self._sign_btn.setObjectName("ghostBtn")
        self._sign_btn.setFixedHeight(36)
        self._sign_btn.setIconSize(QSize(16, 16))
        self._sign_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sign_btn.clicked.connect(self.sign_out_requested.emit)
        op.addWidget(self._sign_btn)
        lay.addLayout(op)
        self.set_operator(None)

    def set_operator(self, name, offline=False):
        """The signed-in name with Sign out, or a "Sign in" prompt."""
        self._switch_btn.setVisible(bool(name))
        if name:
            self._op_name.setText(name)
            self._sign_btn.setText(tr("  Sign out"))
            self._sign_btn.setIcon(svg_icon("log-out", C["text2"]))
            self._sign_btn.setToolTip(tr("End of shift — the camera keeps running"))
        else:
            self._op_name.setText(tr("Nobody (offline)") if offline else tr("Nobody"))
            self._sign_btn.setText(tr("  Sign in"))
            self._sign_btn.setIcon(svg_icon("log-in", C["text2"]))
            self._sign_btn.setToolTip(tr("Operator sign-in"))

    def set_active(self, page_idx):
        for btn, idx in self._buttons:
            btn.set_active(idx == page_idx)

    def setEnabled(self, enabled):  # noqa: N802 (Qt naming)
        for btn, _ in self._buttons:
            btn.setEnabled(enabled)


# Splash
# Step labels from modules/camera_init.py (status_cb(label + "…")), translated in SplashPage.set_status
_BACKEND_STATUS = [N_("Closing camera…"), N_("Disabling timestamp overlay…"),
                   N_("Setting 4K resolution…"), N_("Opening camera…"),
                   N_("Starting video stream…"), N_("Enabling auto-exposure…"),
                   N_("Resetting focus…"), N_("Starting auto-focus…"),
                   N_("Moving lens to the saved QR focus…")]


class SplashPage(QWidget):
    """Start-up screen with an operator escape hatch.

    Start-up is slow: the camera REST settles take 17 s and `autofocus_start`
    blocks until the lens locks (up to 60 s). If the camera stage runs past
    SPLASH_CAMERA_ACTIONS_AFTER, or start-up fails, the operator gets RETRY
    (re-run the stuck stage) and EXIT (close cleanly). In kiosk mode they
    cannot reach the OS, so otherwise the only recovery is a power cycle.
    """
    retry_requested = Signal()
    exit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("splash")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)

        # Checkology brand, as on the Keg Management web app: logo badge,
        # navy title, pink-ruled tagline.
        lay.addWidget(BrandLogo(height=46, plaque="badge", variant="white"),
                      alignment=Qt.AlignmentFlag.AlignCenter)
        lay.addSpacing(14)
        title = QLabel(tr("Keg Palletization"))
        title.setObjectName("splashTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        tag = QLabel(tr("INDUSTRIAL VISION HMI"))
        tag.setObjectName("splashTag")
        tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(tag)
        lay.addSpacing(24)

        # Overall start-up progress in percent. Each stage owns a slice of
        # 0-100 (set_stage). A starting step moves the bar to its start; during
        # a long step (auto-focus, model load) the bar creeps toward, but not
        # past, the step's end so it never looks frozen. Range is 0-1000 so
        # the creep is smooth.
        self._bar = QProgressBar()
        self._bar.setFixedWidth(420)
        self._bar.setFixedHeight(8)
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        lay.addWidget(self._bar, alignment=Qt.AlignmentFlag.AlignCenter)

        self._pct = QLabel("0%")
        self._pct.setObjectName("splashPct")
        self._pct.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._pct)

        self._stage = (0.0, 100.0)
        self._shown = 0.0        # percent currently drawn
        self._target = 0.0       # percent the finished steps have earned
        self._ceiling = 0.0      # how far the creep may go during this step
        self._anim = QTimer(self)
        self._anim.setInterval(100)
        self._anim.timeout.connect(self._animate)
        self._anim.start()

        self._status = QLabel(tr("Starting…"))
        self._status.setObjectName("splashStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._status)

        # Secondary line: why the buttons appeared ("still working" vs "failed").
        self._hint = QLabel("")
        self._hint.setObjectName("splashSub")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setFixedWidth(460)
        self._hint.setVisible(False)
        lay.addWidget(self._hint, alignment=Qt.AlignmentFlag.AlignCenter)

        lay.addSpacing(6)

        # Action row (hidden until auto-focus overruns or start-up fails)
        self._actions = QWidget()
        al = QHBoxLayout(self._actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(12)
        al.addStretch()
        self._retry_btn = QPushButton(tr("RETRY"))
        self._retry_btn.setObjectName("primaryBtn")
        self._retry_btn.setMinimumWidth(140)
        self._retry_btn.setFixedHeight(38)
        self._retry_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._retry_btn.clicked.connect(self.retry_requested.emit)
        al.addWidget(self._retry_btn)
        self._exit_btn = QPushButton(tr("EXIT"))
        self._exit_btn.setObjectName("dangerBtn")
        self._exit_btn.setMinimumWidth(140)
        self._exit_btn.setFixedHeight(38)
        self._exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._exit_btn.clicked.connect(self.exit_requested.emit)
        al.addWidget(self._exit_btn)
        al.addStretch()
        self._actions.setVisible(False)
        lay.addWidget(self._actions)

    @Slot(str)
    def set_status(self, text):
        self._status.setText(tr(text))
        self._status.setStyleSheet("")      # back to the themed navy

    # Percentage progress
    def set_stage(self, start, end):
        """Set this stage's slice of the bar, e.g. camera init 0-60 %.

        The bar jumps to `start`, which may go backwards (e.g. after RETRY)."""
        self._stage = (float(start), float(end))
        self._set_shown(start)
        self._target = self._ceiling = float(start)
        self._bar.setRange(0, 1000)
        if not self._anim.isActive():
            self._anim.start()

    @Slot(int, int)
    def set_step(self, idx, total):
        """Step `idx` of `total` (1-based) has just started within the stage."""
        start, end = self._stage
        total = max(1, int(total))
        done = min(max(int(idx) - 1, 0), total)
        self._target = start + (end - start) * done / total
        self._ceiling = start + (end - start) * min(done + 1, total) / total
        if self._target < self._shown:          # stage restarted (internal retry)
            self._set_shown(self._target)

    def finish(self):
        """Start-up complete: 100 %."""
        self._target = self._ceiling = 100.0
        self._set_shown(100.0)
        self._anim.stop()

    def set_indeterminate(self):
        """Shutdown screen: no meaningful percentage."""
        self._anim.stop()
        self._bar.setRange(0, 0)
        self._pct.setVisible(False)

    def _animate(self):
        if not self.isVisible():
            return
        if self._shown < self._target:
            # Catch up quickly with steps that have finished.
            self._set_shown(min(self._target, self._shown + 2.0))
        elif self._shown < self._ceiling - 1.0:
            # Creep through a long step; slows as it nears the step's end.
            gap = self._ceiling - 1.0 - self._shown
            self._set_shown(self._shown + max(0.02, gap * 0.01))

    def _set_shown(self, pct):
        self._shown = max(0.0, min(100.0, float(pct)))
        self._bar.setValue(int(self._shown * 10))
        self._pct.setVisible(True)
        self._pct.setText(f"{int(self._shown)}%")

    def show_actions(self, hint=""):
        """Reveal RETRY / EXIT. Safe to call repeatedly."""
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))
        self._actions.setVisible(True)
        self._retry_btn.setEnabled(True)

    def hide_actions(self):
        self._actions.setVisible(False)
        self._hint.setVisible(False)

    def set_error(self, message, hint=""):
        """Failed outright: red status, bar frozen where it stopped, offer the buttons."""
        self._target = self._ceiling = self._shown   # no creeping on a failure
        self._status.setText(tr(message))
        self._status.setStyleSheet(
            f"color:{C['red']}; font-size:13px; font-weight:700;")
        self.show_actions(tr(hint))

    def mark_retrying(self, message):
        """Operator pressed RETRY: return to a working state.

        The caller resets the percentage with set_stage."""
        self.hide_actions()
        self.set_status(tr(message))
        self._retry_btn.setEnabled(False)   # re-enabled by the next show_actions


# Toast
class Toast(QFrame):
    """Small feedback card, top-right: SVG icon and one short line, coloured
    by kind. Errors stay up longer than confirmations."""
    KINDS = {
        "success": ("circle-check",   C["green_dark"]),
        "info":    ("info",           C["ck_navy"]),
        "warning": ("triangle-alert", C["amber_dark"]),
        "error":   ("circle-x",       C["red"]),
    }
    DURATION = {"success": 3000, "info": 3000, "warning": 5000, "error": 7000}

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toastCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setVisible(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 9, 14, 9)
        lay.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        self._icon.setStyleSheet("background:transparent;")
        self._text = QLabel()
        self._text.setObjectName("toastText")
        self._text.setWordWrap(True)
        self._text.setMaximumWidth(380)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._text, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.setVisible(False))

    @Slot(str, str)
    def show_toast(self, message, kind="info", duration=None):
        icon, color = self.KINDS.get(kind, self.KINDS["info"])
        self._icon.setPixmap(svg_pixmap(icon, color, 18))
        self.setStyleSheet(
            f"#toastCard {{ background:{C['white']}; border:1px solid {C['border']};"
            f" border-left:4px solid {color}; border-radius:8px; }}")
        self._text.setText(message)
        self.adjustSize()
        self._reposition()
        self.setVisible(True)
        self.raise_()
        self._timer.start(duration or self.DURATION.get(kind, 3000))

    def _reposition(self):
        p = self.parent()
        if p:
            x = p.width() - self.width() - 20
            self.move(max(10, x), 64)      # just under the top bar


# Main window
class MainWindow(QMainWindow):
    # Stack page indices
    PAGE_SPLASH, PAGE_OPERATOR, PAGE_RECORDS, PAGE_SETTINGS = 0, 1, 2, 3
    PAGE_LOGIN = 4
    CAMERA_STAGE_PCT = 60   # splash %: camera init + auto-focus = 0-60, backend = 60-100

    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("Keg Palletization — VisionCore HMI"))
        self.resize(1600, 940)
        self.setMinimumSize(1180, 720)
        self._stopped = False
        self._poweroff = False   # set True when the operator chooses "Power Off Device"
        # Set by refresh: after cleanup the window closes and main() relaunches
        # the app in place instead of exiting.
        self.restart_requested = False
        self._init_stage = "camera"
        self._init_gen = 0
        self._slow_timer = None
        # Superseded init workers stay referenced so Qt doesn't destroy a
        # running QThread (that aborts the process). Drained in _cleanup.
        self._orphaned_workers = []
        # Superseded camera-init bridges stay alive so their uninterruptible
        # daemon threads keep emitting into a live (disconnected) object.
        self._orphaned_bridges = []
        self._build_ui()
        QTimer.singleShot(200, self._start_init)

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("appBg")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.topbar = TopBar()
        # Use a safe exit handler so the app can wait for background tasks
        # to finish (with a timeout) before closing.
        self.topbar.exit_requested.connect(self.request_exit)
        self.topbar.refresh_requested.connect(self.request_refresh)
        self.topbar.hide()          # revealed once the splash finishes loading
        root.addWidget(self.topbar)

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        self.sidebar = Sidebar(
            [("operator",  tr("Operator"),        self.PAGE_OPERATOR),
             ("records",   tr("Pallet Records"),  self.PAGE_RECORDS),
             ("settings",  tr("Settings"),        self.PAGE_SETTINGS)],
            self._switch_page,
        )
        self.sidebar.setEnabled(False)   # locked until init completes
        self.sidebar.hide()              # hidden during splash; revealed when ready
        bl.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.splash = SplashPage()
        self.operator = OperatorPage()
        self.records = RecordsPage()
        self.settings = SettingsPage()
        self.stack.addWidget(self.splash)     # 0
        self.stack.addWidget(self.operator)   # 1
        self.stack.addWidget(self.records)    # 2
        self.stack.addWidget(self.settings)   # 3
        self.login = LoginPage()
        self.stack.addWidget(self.login)      # 4
        bl.addWidget(self.stack, 1)

        root.addWidget(body, 1)

        # Cross-page signal wiring
        self.operator.mode_changed.connect(self.topbar.set_mode)
        self.operator.net_changed.connect(self.topbar.set_network)
        self.operator.net_changed.connect(self.settings.set_network)
        self.operator.activity.connect(self.settings.add_activity)
        # A pallet reaches Records only on a cloud confirmation; a report that
        # could not be delivered goes to the separate pending table instead.
        self.operator.pallet_created.connect(self.records.add_pallet)
        self.operator.pallet_queued.connect(self.records.add_pending)
        # The upload queue changes state on a background thread (delivered,
        # deferred, rejected), so Records re-reads rather than trying to track it.
        self.operator.records_stale.connect(self.records.refresh_all)
        # Removing a pallet frees its kegs on this device, so the operator page
        # has to rebuild its already-palletized map.
        self.records.pallet_removed.connect(self._on_pallet_removed)
        # REPRINT goes through the operator page's print queue like every label.
        self.records.reprint_requested.connect(self.operator.reprint_label)
        self.toast = Toast(central)
        self.operator.toast.connect(self.toast.show_toast)

        # Splash escape hatch (slow / failed start-up)
        self.splash.retry_requested.connect(self._on_splash_retry)
        self.splash.exit_requested.connect(self._on_splash_exit)

        # Settings -> Operator. The only signals flowing this way; everything
        # else above originates on the operator page.
        self.settings.beer_type_changed.connect(self.operator.set_beer_type_enabled)
        self.settings.beer_types_loaded.connect(self.operator.set_beer_types)
        # A shift handover is confirmed over whatever page is showing. The
        # operator page needs no signal: it reads config.current_operator()
        # when it builds the payload.
        self.settings.operator_changed.connect(self._on_operator_changed)
        # Forgot-PIN admin sign-in (ui_pyside/maintenance.py): the cloud has put
        # the admin on shift, so show them; resets go to the activity list.
        set_maintenance_hooks(on_admin_signed_in=self._on_pin_admin_signed_in,
                              on_activity=self.settings.add_activity)
        # New camera MAC saved in Settings: restart so every module uses it.
        self.settings.camera_mac_changed.connect(self._restart_for_new_mac)
        # Camera light / exposure from the operator page: a side panel that
        # borrows the Settings cards and sits over the control column.
        self._camera_panel = None
        self.operator.camera_settings_requested.connect(self._open_camera_settings)
        # Focusing: the operator page asks (first QR codes in view, a
        # double-tap on the feed), the Settings focus card runs it and marks
        # the spot.
        self.operator.qr_focus_requested.connect(self.settings.run_qr_focus)
        self.operator.focus_point_requested.connect(self.settings.run_point_focus)
        self.operator.focus_region_requested.connect(self.settings.run_region_focus)
        self.settings.focus_marker.connect(self.operator.show_focus_marker)
        # Automatic focus per layer of a stack.
        self.operator.layer_focus_requested.connect(self.settings.run_layer_focus)
        self.settings.layer_focus_done.connect(self.operator.on_layer_focus_done)
        self.settings.layer_lens_known.connect(self.operator.on_layer_lens_known)
        self.operator.layer_lens_query.connect(self.settings.report_layer_lens)

        # Operator login (after the splash) and sign-out from the top bar.
        self._operator_signed_in = None      # operator dict while on shift
        self._auth_error_shown = None
        self.login.signed_in.connect(self._on_signed_in)
        self.login.continue_offline.connect(self._on_continue_offline)
        self.login.batch_confirmed.connect(self._on_batch_confirmed)
        self.login.notice.connect(self.toast.show_toast)
        # Batch step "Back to sign in" = sign out (asks first, as the sidebar does).
        self.login.back_requested.connect(self._on_sign_out_requested)
        # Back buttons and action feedback on the secondary pages.
        for page in (self.records, self.settings):
            page.back_requested.connect(self._go_back)
            page.toast.connect(self.toast.show_toast)
        self.login.exit_requested.connect(self.request_exit)
        self.sidebar.sign_out_requested.connect(self._on_sign_out_requested)
        self._tour = None
        self.sidebar.tour_requested.connect(self.start_tour)
        self._switching_from = ""
        self.sidebar.switch_requested.connect(self._on_switch_requested)
        self.sidebar.theme_requested.connect(self.request_theme_change)
        # Screen language: from the sidebar, or on the sign-in page before an
        # operator signs in (that choice then becomes theirs).
        self._lang_picked_at_login = False
        self.sidebar.language_requested.connect(self.request_language)
        self.login.language_requested.connect(lambda: self.request_language(at_login=True))
        i18n.notifier().changed.connect(self._on_language_changed)
        self.login.switch_cancelled.connect(self._on_switch_cancelled)
        # Light watch (no network): a camera re-login ends the operator's
        # shift server-side, and a rejected camera password stops uploads.
        self._auth_watch = QTimer(self)
        self._auth_watch.setInterval(5000)
        self._auth_watch.timeout.connect(self._check_auth_state)
        # Blacklist / beer types / cloud config, kept fresh during the shift.
        # Loaded first on the loading screen (InitWorker), so the first tick
        # is one full interval after the dashboard is ready.
        self._cloud_refresh = QTimer(self)
        self._cloud_refresh.setInterval(int(config.CLOUD_REFRESH_SECONDS * 1000))
        self._cloud_refresh.timeout.connect(self._refresh_cloud_data)
        self.operator.network_restored.connect(self._refresh_cloud_data)
        # Free disk space: warn only, never stops the workflow.
        self._disk_level = disk_check.OK
        self._disk_warned_at = 0.0
        self._disk_timer = QTimer(self)
        self._disk_timer.setInterval(int(config.DISK_CHECK_INTERVAL_S * 1000))
        self._disk_timer.timeout.connect(self._check_disk_space)

    @Slot()
    def _check_disk_space(self):
        """Tell the operator when free disk space runs low. Announced when the
        level changes, and every DISK_CRITICAL_REPEAT_S while critical."""
        try:
            info = disk_check.check()
            level, free_mb = info["level"], info["free_mb"]
            if level is None:
                return
            now = time.monotonic()
            changed = level != self._disk_level
            repeat = (level == disk_check.CRITICAL and now - self._disk_warned_at
                      >= config.DISK_CRITICAL_REPEAT_S)
            self._disk_level = level
            if not (changed or repeat):
                return
            self._disk_warned_at = now
            free = f"{free_mb / 1024:.1f} GB" if free_mb >= 1024 else f"{free_mb:.0f} MB"
            if level == disk_check.OK:
                log.info("[DISK] Free space back to normal: %s free", free)
                self.settings.add_activity("success", tr("Disk space OK: {free} free", free=free))
                return
            if level == disk_check.CRITICAL:
                log.warning("[DISK] Free space critical: %s free (%.0f%% used)",
                            free, info["used_pct"] or 0)
                deleted = disk_check.free_space_now()
                if deleted:
                    log.info("[DISK] %d old captured frame(s) deleted to free space", deleted)
                msg = tr("Disk almost full: {free} left. Call maintenance.", free=free)
                self.settings.add_activity("error", msg)
                self.toast.show_toast(msg, "error")
            else:
                log.warning("[DISK] Free space low: %s free (%.0f%% used)",
                            free, info["used_pct"] or 0)
                msg = tr("Disk space low: {free} left", free=free)
                self.settings.add_activity("warning", msg)
                self.toast.show_toast(msg, "warning")
        except Exception:
            log.exception("[DISK] check failed")

    @Slot()
    def _refresh_cloud_data(self):
        """Re-fetch the blacklist, beer types and cloud config.

        Runs every CLOUD_REFRESH_SECONDS and at once when the network comes
        back. Each fetch runs on its own worker and skips itself if one is
        already in flight. While the network is known to be down nothing is
        fetched: the copy in use is the latest cache, and a fetch would only
        sit on its timeout.
        """
        if self._stopped:
            return
        api = self.operator.api_sender
        if api is None:
            return
        try:
            offline = api.is_known_offline()
        except Exception:
            offline = False
        if offline != getattr(self, "_cloud_refresh_offline", False):
            self._cloud_refresh_offline = offline
            log.info("Cloud refresh paused — offline, using the last fetched data"
                     if offline else "Cloud refresh resumed — back online")
        if offline:
            return
        self.operator.fetch_blacklist()
        if config.BEER_TYPE_ENABLED:
            self.settings.refresh_beer_types_quietly()
        if config.CLOUD_SYNC_ENABLED:
            self.operator.sync_cloud()

    @Slot(str, str)
    def _on_operator_changed(self, operator_id, operator_name):
        """Confirm a shift handover made in Settings."""
        if operator_name or operator_id:
            self.toast.show_toast(tr("Operator: {name}", name=operator_name or operator_id), "success")
        else:
            self.toast.show_toast(tr("Operator cleared"), "info")

    # Operator login
    def _show_chrome(self, visible):
        self.topbar.setVisible(visible)
        self.sidebar.setVisible(visible)
        self.sidebar.setEnabled(visible)

    def _show_login(self, camera_result=None, notice="", switching_from=""):
        """Show the sign-in page: after the splash, after a sign-out, or when
        the cloud ended the shift."""
        if self._tour is not None:          # don't leave the tour over sign-in
            self._tour.close_tour()
        self._show_chrome(False)
        self.stack.setCurrentWidget(self.login)
        self._switching_from = switching_from     # "" on every normal sign-in
        self.login.activate(camera_result, notice, switching_from)

    def _enter_dashboard(self):
        self._show_chrome(True)
        self.sidebar.set_active(self.PAGE_OPERATOR)
        self._switch_page(self.PAGE_OPERATOR)
        # A new operator gets the guided tour once (after the layout settles).
        QTimer.singleShot(700, self._maybe_start_tour)

    # Application tour
    def _tour_key(self):
        """Key the tour is remembered under: the signed-in operator, else the device."""
        op = self._operator_signed_in or {}
        return str(op.get("id") or op.get("name") or "__device__")

    def _maybe_start_tour(self):
        if self._stopped or config.tour_seen(self._tour_key()):
            return
        self.start_tour()

    @Slot()
    def start_tour(self):
        """Open the guided tour on the operator page (also the sidebar's "App tour" button)."""
        if self._tour is not None or self.stack.currentWidget() in (self.login, self.splash):
            return
        from ui_pyside.tour import TourOverlay, build_steps, build_settings_steps
        if self.stack.currentIndex() == self.PAGE_SETTINGS:
            # On Settings, "App tour" is the Settings tour.
            self._tour_kind, steps = "settings", build_settings_steps(self)
        else:
            if self.stack.currentIndex() != self.PAGE_OPERATOR:
                self._switch_page(self.PAGE_OPERATOR)
            self._tour_kind, steps = "app", build_steps(self)
        self._tour = TourOverlay(self.centralWidget(), steps)
        self._tour.finished.connect(self._on_tour_finished)

    def _maybe_start_settings_tour(self):
        """First visit to Settings by this operator: the short Settings tour."""
        if (self._stopped or self._tour is not None
                or self.stack.currentIndex() != self.PAGE_SETTINGS
                or config.tour_seen("settings:" + self._tour_key())):
            return
        self.start_tour()

    @Slot(bool)
    def _on_tour_finished(self, completed):
        self._tour = None
        settings_tour = getattr(self, "_tour_kind", "app") == "settings"
        # finished or skipped: not again (each tour is remembered separately)
        config.mark_tour_seen(("settings:" if settings_tour else "") + self._tour_key())
        if settings_tour:
            text = tr("Settings tour finished") if completed else tr("Settings tour skipped")
        else:
            text = tr("App tour finished") if completed else tr("App tour skipped")
        self.settings.add_activity("info", text)

    # Screen language
    @Slot()
    def request_language(self, at_login=False):
        """Language picker. Applies at once, without a restart, and is saved
        for the device and for the signed-in operator."""
        code = LanguageDialog.ask(self)
        if not code or code == i18n.current():
            return
        if at_login:
            self._lang_picked_at_login = True
        key = self._tour_key() if self._operator_signed_in and not at_login else None
        self._set_language(code, key)

    def _set_language(self, code, operator_key=None):
        code = i18n.set_language(code)
        if not config.save_ui_language(code, operator_key):
            self.toast.show_toast(i18n.tr("Could not save the language choice"), "error")
        log.info("Screen language: %s", code)
        self.settings.append_log(f"[language] Screen language set to {code}")

    def _apply_operator_language(self):
        """At sign-in: the operator's own language, or keep the one just chosen.

        A language picked on the sign-in page becomes the operator's choice. If
        they picked none, the language they used last time comes back."""
        key = self._tour_key()
        if self._lang_picked_at_login:
            config.save_ui_language(i18n.current(), key)
        else:
            own = config.ui_language(key)
            if own and own != i18n.current():
                self._set_language(own)
        self._lang_picked_at_login = False

    @Slot(str)
    def _on_language_changed(self, _code):
        update_language_button(self.sidebar._lang_btn)
        update_language_button(self.login._lang_btn)
        self.topbar._date.setText(now_date())

    # Switch operator
    @Slot()
    def _on_switch_requested(self):
        """Hand over to the next operator.

        The sign-in page opens while the current operator stays signed in. The
        next operator's sign-in ends the previous shift (the cloud does that on
        an operator login). Cancel returns with nothing changed."""
        if self._operator_signed_in is None:
            self._show_login()
            return
        self._switching_from = self._operator_signed_in.get("name", "")
        self._show_login(switching_from=self._switching_from)

    @Slot()
    def _on_switch_cancelled(self):
        self._switching_from = ""
        if self._operator_signed_in is None:      # shift ended meanwhile
            self._show_login()
            return
        self._enter_dashboard()

    @Slot(object)
    def _on_signed_in(self, operator):
        """Start of shift.

        The cloud records every report against this operator. The report's own
        operator fields are set to match, so the Settings page, the Activity
        Log and the payload all agree."""
        self._operator_signed_in = operator
        self._apply_operator_language()
        name = operator.get("name", "")
        try:
            config.save_operator_pref(selected_id=operator.get("id", ""),
                                      selected_name=name)
        except Exception:
            log.exception("Could not save the signed-in operator")
        self.settings.set_signed_in_operator(operator)
        self.sidebar.set_operator(name)
        self.settings.append_log(
            f"[login] {name} signed in"
            + (" (local — cloud login not enabled on this server)" if operator.get("local") else ""))
        self.toast.show_toast(tr("Welcome, {name}", name=name), "success")
        prev, self._switching_from = self._switching_from, ""
        if prev and prev != name:
            self.settings.add_activity("info", tr("Operator switched: {previous} → {name}",
                                                   previous=prev, name=name))
        else:
            self.settings.add_activity("success", tr("{name} signed in", name=name))
        self.login.show_batch(name, self.operator.current_batch())

    def _on_pin_admin_signed_in(self, operator):
        """Someone signed in from a maintenance approval or Forgot PIN. The cloud
        has ended the previous shift, so show them as the operator, without
        leaving the screen the approval was asked on (unlike _on_signed_in,
        which opens the batch step)."""
        if not operator:
            return
        prev = (self._operator_signed_in or {}).get("name", "")
        self._operator_signed_in = operator
        name = operator.get("name", "")
        try:
            config.save_operator_pref(selected_id=operator.get("id", ""),
                                      selected_name=name)
        except Exception:
            log.exception("Could not save the signed-in operator")
        self.settings.set_signed_in_operator(operator)
        self.sidebar.set_operator(name)
        self.settings.append_log(f"[login] {name} signed in (maintenance approval)")
        if prev and prev != name:
            self.settings.add_activity("info", tr("Operator switched: {previous} → {name}",
                                                   previous=prev, name=name))

    @Slot()
    def _on_continue_offline(self):
        """Cloud unreachable: run without a signed-in operator.

        Reports queue up and are sent once the cloud is back."""
        self._operator_signed_in = None
        self.settings.set_signed_in_operator(None)
        self.sidebar.set_operator(None, offline=True)
        self.settings.append_log("[login] Continued without sign-in (cloud unreachable)")
        self.settings.add_activity("warning", tr("Started without sign-in (offline)"))
        self.toast.show_toast(tr("Running without operator sign-in"), "warning")
        self.login.show_batch("", self.operator.current_batch())

    @Slot(str)
    def _on_batch_confirmed(self, batch):
        """Batch chosen on the step after sign-in; go on to the dashboard."""
        self.operator.set_batch(batch)
        self.settings.append_log(f"[login] Batch selected: {batch}")
        self._enter_dashboard()

    @Slot()
    def _on_sign_out_requested(self):
        if self._operator_signed_in is None:
            self._show_login()               # "Sign in" from offline mode
            return
        name = self._operator_signed_in.get("name", "")
        if not ConfirmDialog.ask(
                self, tr("Sign Out"), tr("Sign out {name}?", name=name),
                tr("End of shift. The camera keeps running and reporting with no "
                   "operator until the next sign-in."), confirm_text=tr("SIGN OUT")):
            return
        if self._operator_signed_in.get("local"):
            self._on_signed_out(name, {"ok": True})   # nothing to tell the cloud
            return
        self._sign_out_worker = AuthWorker(get_camera_auth().operator_logout, parent=self)
        self._sign_out_worker.done.connect(
            lambda res, n=name: self._on_signed_out(n, res))
        self._sign_out_worker.start()

    def _on_signed_out(self, name, res):
        self._clear_operator()
        if res.get("ok"):
            self.settings.append_log(f"[login] {name} signed out")
            self.settings.add_activity("info", tr("{name} signed out", name=name))
            notice = ""
        else:
            # Still signed out here; the cloud ends the shift at the next
            # operator or camera login.
            self.settings.append_log(f"[login] Sign-out of {name} not confirmed by "
                                     f"the cloud: {res.get('message')}")
            notice = tr("Signed out on this device — the cloud could not be reached.")
        self._show_login(notice=notice)

    def _clear_operator(self):
        self._operator_signed_in = None
        try:
            config.save_operator_pref(selected_id="", selected_name="")
        except Exception:
            log.exception("Could not clear the operator")
        self.settings.set_signed_in_operator(None)
        self.sidebar.set_operator(None)

    def _check_auth_state(self):
        auth = get_camera_auth()
        if auth.operator_session_lost and self._operator_signed_in is not None:
            auth.operator_session_lost = False
            name = self._operator_signed_in.get("name", "")
            self.settings.append_log(f"[login] Shift of {name} ended by the cloud "
                                     f"(camera signed in again)")
            self._clear_operator()
            if self.stack.currentWidget() is not self.splash:
                self._show_login(notice=tr("Your shift was ended by the cloud.") + " "
                                 + tr("Please sign in again."))
        err = auth.last_error
        if self.stack.currentWidget() in (self.login, self.splash):
            self._auth_error_shown = err     # the sign-in page shows it itself
        elif err and err.get("error") in CREDENTIAL_CODES and err is not self._auth_error_shown:
            self._auth_error_shown = err
            self.settings.append_log(f"[login] Camera sign-in failed: {err.get('message')}")
            self.settings.add_activity("error", tr("Camera lost its connection to Keg Management "
                                                   "— pallets are saved.") + " "
                                       + tr("Call your supervisor."))
            self.toast.show_toast(tr("Camera not signed in to the cloud — reports are "
                                     "queued.") + " "
                                  + tr("Sign out and fix the camera password."),
                                  "error", 8000)

    @Slot(str)
    def _on_pallet_removed(self, session_id):
        """A pallet record was deleted from this device.

        Its kegs are no longer on any local pallet, so the operator page's
        already-palletized map is rebuilt. Otherwise those kegs would keep
        raising the duplicate warning against a pallet that is gone.
        """
        self.settings.append_log(f"[records] Pallet record {session_id} removed "
                                 f"from this device (cloud copy unaffected)")
        self.operator.reload_used_kegs()
        self.toast.show_toast(tr("Pallet record removed from this device"), "info")

    def _switch_page(self, page_idx):
        # Remember where the operator came from, for the pages' Back button.
        cur = self.stack.currentIndex()
        if cur != page_idx and cur in (self.PAGE_OPERATOR, self.PAGE_RECORDS,
                                       self.PAGE_SETTINGS):
            self._page_history = (getattr(self, "_page_history", []) + [cur])[-10:]
        self.stack.setCurrentIndex(page_idx)
        self.sidebar.set_active(page_idx)
        if page_idx == self.PAGE_SETTINGS:
            QTimer.singleShot(400, self._maybe_start_settings_tour)

    @Slot()
    @Slot()
    def _open_camera_settings(self):
        """Open the camera settings panel; the icon closes it again."""
        if self._camera_panel is not None:
            self._camera_panel.close_panel()
            return
        from ui_pyside.camera_controls_dialog import CameraControlsPanel
        self._camera_panel = CameraControlsPanel(
            self.settings, self.operator.control_column(), self.operator)
        self._camera_panel.closed.connect(self._on_camera_panel_closed)

    @Slot()
    def _on_camera_panel_closed(self):
        self._camera_panel = None

    def _go_back(self):
        """Back button on Records / Settings: go to the previous page, or the
        operator page when there is none."""
        hist = getattr(self, "_page_history", [])
        target = hist.pop() if hist else self.PAGE_OPERATOR
        self.stack.setCurrentIndex(target)
        self.sidebar.set_active(target)

    # Startup orchestration
    def _arm_slow_timer(self, hint):
        """Reveal RETRY/EXIT if the camera stage is still running after
        SPLASH_CAMERA_ACTIONS_AFTER seconds, so a slow or hung camera start
        doesn't leave the operator stuck."""
        self._cancel_slow_timer()
        self._slow_timer = QTimer(self)
        self._slow_timer.setSingleShot(True)
        self._slow_timer.timeout.connect(lambda: self.splash.show_actions(hint))
        self._slow_timer.start(int(SPLASH_CAMERA_ACTIONS_AFTER * 1000))

    def _cancel_slow_timer(self):
        t = getattr(self, "_slow_timer", None)
        if t is not None:
            t.stop()
            self._slow_timer = None

    def _start_init(self):
        # After a refresh the camera is already configured and focused, so go
        # straight to the backend (model, camera stream, cloud data).
        run_camera_init = CAMERA_INIT_ENABLED and not SKIP_CAMERA_INIT
        self._init_stage = "camera" if run_camera_init else "backend"
        # Splash %: camera init + auto-focus take the first CAMERA_STAGE_PCT,
        # the backend the rest. Without camera init the backend is 0-100 %.
        self._backend_start_pct = self.CAMERA_STAGE_PCT if run_camera_init else 0
        if run_camera_init:
            self.splash.set_stage(0, self.CAMERA_STAGE_PCT)
        # Bumped on every retry. Callbacks from a superseded run carry an old
        # generation and are ignored. Otherwise, pressing RETRY during a 60 s
        # autofocus would let the abandoned run drive the splash (and advance
        # the app) underneath the new one.
        self._init_gen = getattr(self, "_init_gen", 0) + 1
        gen = self._init_gen

        if SKIP_CAMERA_INIT and CAMERA_INIT_ENABLED:
            log.info("Refresh launch — camera REST init / auto-focus skipped")
            self.settings.append_log(
                "[init] Refreshed — camera auto-focus skipped (focus kept)")

        if run_camera_init:
            self.splash.set_status(tr("Initialising camera hardware…"))
            # The RETRY / EXIT clock starts now with the camera splash, not at
            # auto-focus, so the buttons appear in under 2 minutes. The camera
            # keeps retrying on its own behind them.
            self._arm_slow_timer(
                tr("The camera is taking longer than usual — still retrying by itself.")
                + " " + tr("Wait, or RETRY / EXIT."))
            # A superseded bridge must be kept alive. Its init runs on an
            # uninterruptible daemon thread that keeps calling
            # status_cb -> self.status.emit. If the QObject is collected first
            # (reassigning _cam_bridge would do that), the thread dies with
            # "RuntimeError: Signal source has been deleted". Disconnect it so it
            # can't drive the UI, but keep the reference so its emits stay
            # harmless no-ops until the thread finishes.
            old = getattr(self, "_cam_bridge", None)
            if old is not None:
                try:
                    old.status.disconnect()
                    old.step.disconnect()
                    old.done.disconnect()
                except (RuntimeError, TypeError):
                    pass
                self._orphaned_bridges.append(old)
            self._cam_bridge = CameraInitBridge()
            self._cam_bridge.status.connect(self.splash.set_status)
            self._cam_bridge.step.connect(self.splash.set_step)
            self._cam_bridge.done.connect(
                lambda ok, err, g=gen: self._on_camera_init_done(ok, err, g))
            self._cam_bridge.start()
        else:
            self._run_backend_init()

    @Slot(bool, str)
    def _on_camera_init_done(self, ok, err, gen=None):
        if gen is not None and gen != self._init_gen:
            log.info("Ignoring camera-init result from superseded attempt %s", gen)
            return
        self._cancel_slow_timer()
        if ok:
            self.settings.append_log("[init] Camera hardware initialised")
        else:
            # Not fatal: log and continue so the app stays usable (e.g. dev box,
            # camera daemon down). InitWorker retries the camera open anyway.
            log.warning("Camera REST init failed: %s", err)
            self.settings.append_log(f"[init] Camera REST init skipped: {err}")
        self._run_backend_init()

    def _run_backend_init(self):
        self._init_stage = "backend"
        # The buttons are for a long auto-focus only. During backend load they
        # appear only on an actual failure (_on_backend_failed).
        self._cancel_slow_timer()
        self.splash.hide_actions()
        self.splash.set_status(tr("Loading backend…"))
        self.splash.set_stage(getattr(self, "_backend_start_pct", 0), 100)
        self._init_worker = InitWorker()
        self._init_worker.progress.connect(self.splash.set_status)
        self._init_worker.step.connect(self.splash.set_step)
        self._init_worker.ready.connect(self._on_backend_ready)
        self._init_worker.failed.connect(self._on_backend_failed)
        self._init_worker.start()

    # Splash escape hatch
    @Slot()
    def _on_splash_retry(self):
        """Re-run whichever stage is stuck."""
        stage = getattr(self, "_init_stage", "camera")
        log.info("Operator pressed RETRY on the splash (stage=%s)", stage)
        self.settings.append_log(f"[init] Operator retried start-up ({stage})")
        if stage == "camera":
            self.splash.mark_retrying(tr("Retrying camera initialisation…"))
            self.splash.set_step(0, 1)
            self._start_init()          # bumps _init_gen, orphaning the old run
        else:
            self.splash.mark_retrying(tr("Retrying backend load…"))
            prev = getattr(self, "_init_worker", None)
            if prev is not None and prev.isRunning():
                # Don't stack two model loads: let the old one finish, but stop
                # it driving the UI.
                try:
                    prev.progress.disconnect()
                    prev.step.disconnect()
                    prev.ready.disconnect()
                    prev.failed.disconnect()
                except (RuntimeError, TypeError):
                    pass
                self._orphaned_workers.append(prev)
            self._run_backend_init()

    @Slot()
    def _on_splash_exit(self):
        """Quit cleanly from the splash.

        No confirm dialog: the operator already chose EXIT and there is no
        pallet work in flight this early. Goes through _finish_shutdown so the
        kiosk service is stopped explicitly. A bare close() under
        `Restart=always` would be relaunched three seconds later.
        """
        log.info("Operator pressed EXIT on the splash")
        # Leaving the app opens the desktop, so it needs the maintenance PIN
        # when one is set. Power is still the operator's way out of a hung start.
        if not authorize_maintenance(self, tr("Exit the app")):
            return
        self._cancel_slow_timer()
        self.splash.hide_actions()
        self.splash.set_status(tr("Exiting…"))
        QApplication.processEvents()
        self._finish_shutdown()

    @Slot()
    def request_exit(self):
        """Initiate a safe, confirmed shutdown from the Exit button.

        Offers two outcomes plus Cancel: quit the app only, or power off the
        whole device (the iCam-540, before mains power is cut). Both run the
        same graceful cleanup first. Running background tasks are polled to
        completion (with a timeout) before the window closes or the machine
        halts.
        """
        if self._stopped:
            self.close()
            return
        pending = self._pending_tasks()
        detail = tr("The camera will be released and services closed first.")
        if pending:
            detail = (tr("{n} background task(s) are still finishing (uploads / printing).",
                         n=pending) + " "
                      + tr("They'll be completed or safely queued first."))
        detail += "\n\n" + self._wipe_warning()
        choice = ExitChoiceDialog.ask(self, detail)
        if choice == ExitChoiceDialog.CANCEL:
            return
        # Second confirmation: either choice stops the line.
        if choice == ExitChoiceDialog.POWEROFF:
            sure = ConfirmDialog.ask(
                self, tr("Shut Down Camera"), tr("Shut down the camera now?"),
                tr("The line stops until someone switches the camera back on."),
                confirm_text=tr("YES, SHUT DOWN"), danger=True)
        else:
            # Exit App leaves the kiosk for the desktop: maintenance only.
            # Power Off stays open to every operator.
            if not authorize_maintenance(self, tr("Exit the app")):
                return
            sure = ConfirmDialog.ask(
                self, tr("Exit App"), tr("Close the app now?"),
                tr("The line stops until the app is started again."),
                confirm_text=tr("YES, EXIT"), danger=True)
        if not sure:
            return
        self._poweroff = (choice == ExitChoiceDialog.POWEROFF)
        self._begin_shutdown(pending)

    @Slot()
    def request_refresh(self):
        """Restart the app without re-running camera auto-focus.

        For a long-running kiosk whose focus is already correct. Runs the same
        graceful cleanup as Exit (uploads / prints drained or safely queued,
        camera stream released), then main() relaunches the process in place.
        The new process reloads the model and camera stream, reconnects to the
        cloud and fetches fresh beer types, operators and blacklist. Only the
        camera REST init is skipped, because the daemon still holds its
        configuration and focus. Device settings (beer type, device identity)
        carry over. The local DB and cloud caches are wiped as on every start,
        and unsent reports are restored from the offline outbox
        (modules/outbox.py).
        """
        if self._stopped:
            return
        pending = self._pending_tasks()
        detail = (tr("The app restarts fresh and reloads all data from the cloud.") + " "
                  + tr("Camera focus is kept — auto-focus is skipped.") + " "
                  + tr("Kegs in view are counted again after the restart."))
        if pending:
            detail += ("\n\n" + tr("{n} background task(s) are still finishing "
                                    "(uploads / printing).", n=pending) + " "
                       + tr("They'll be completed or safely queued first."))
        detail += "\n\n" + self._wipe_warning()
        if not ConfirmDialog.ask(self, tr("Restart App"),
                                 tr("Restart the app now?"), detail,
                                 confirm_text=tr("RESTART")):
            return
        log.info("Operator requested REFRESH")
        self.settings.append_log("[refresh] Operator requested a fresh restart")
        self.restart_requested = True
        self._poweroff = False
        self._begin_shutdown(pending)

    @Slot(str)
    def _restart_for_new_mac(self, mac):
        """Restart after Camera Setup saved a new MAC (no second question: the
        change was confirmed twice and verified). Same graceful path as REFRESH:
        uploads and prints are drained or queued first. Camera focus is kept."""
        if self._stopped:
            return
        log.info("Camera MAC changed to %s — restarting", mac)
        self.restart_requested = True
        self._poweroff = False
        self._begin_shutdown(self._pending_tasks())

    @Slot()
    def request_theme_change(self):
        """Switch between the light and dark screen theme.

        Colours are applied when the widgets are built, so the new theme is
        saved and the app restarts the same way as the Restart button (camera
        focus kept, uploads and prints finished or safely queued first).
        """
        if self._stopped:
            return
        new_theme = "light" if IS_DARK else "dark"
        pending = self._pending_tasks()
        detail = (tr("The app restarts to apply the new colours.") + " "
                  + tr("Camera focus is kept.") + " "
                  + tr("Kegs in view are counted again after the restart.") + " "
                  + tr("Best done between pallets."))
        if pending:
            detail += ("\n\n" + tr("{n} background task(s) are still finishing "
                                    "(uploads / printing).", n=pending) + " "
                       + tr("They'll be completed or safely queued first."))
        detail += "\n\n" + self._wipe_warning()
        question = (tr("Switch to the light theme?") if new_theme == "light"
                    else tr("Switch to the dark theme?"))
        if not ConfirmDialog.ask(self, tr("Change Theme"), question, detail,
                                 confirm_text=tr("SWITCH THEME")):
            return
        if not config.save_ui_theme(new_theme):
            self.toast.show_toast(tr("Could not save the theme choice"), "error")
            return
        log.info("Operator switched theme to %s", new_theme)
        self.settings.append_log(f"[theme] Switched to the {new_theme} theme, restarting")
        self.restart_requested = True
        self._poweroff = False
        self._begin_shutdown(pending)

    def _begin_shutdown(self, pending):
        """Shared by Exit and Refresh: show the shutdown screen, wait (bounded)
        for background tasks, then call _finish_shutdown."""
        self._show_shutdown_screen(pending)
        if pending:
            # Poll for pending tasks, but don't wait forever.
            self._exit_poll = QTimer(self)
            self._exit_poll.setInterval(500)
            self._exit_poll.timeout.connect(self._check_pending_and_close)
            self._exit_timeout = QTimer(self)
            self._exit_timeout.setSingleShot(True)
            self._exit_timeout.timeout.connect(self._force_close)
            self._exit_timeout.start(15000)  # 15s max wait
            self._exit_poll.start()
        else:
            self._finish_shutdown()

    def _check_pending_and_close(self):
        try:
            pending = self._pending_tasks()
        except Exception:
            pending = 0
        if pending == 0:
            if getattr(self, "_exit_poll", None):
                self._exit_poll.stop()
            if getattr(self, "_exit_timeout", None):
                self._exit_timeout.stop()
            self._finish_shutdown()
        else:
            # keep the splash message informative while we wait
            self.splash.set_status(tr("Finishing {n} background task(s) — please wait…", n=pending))

    def _force_close(self):
        # Timeout reached: force cleanup and exit
        if getattr(self, "_exit_poll", None):
            self._exit_poll.stop()
        self._finish_shutdown()

    def _finish_shutdown(self):
        """Run cleanup, then either restart fresh, power off the device, or just
        close the app."""
        self._cleanup()
        if self.restart_requested:
            # Don't call stop_kiosk_service: the relaunch happens in place (same
            # PID, see _relaunch), so systemd never sees the app exit.
            self.splash.set_status(tr("Restarting — loading fresh…"))
            QApplication.processEvents()
            self.close()
            return
        if self._poweroff:
            self.splash.set_status(tr("Powering off the device — you may cut power once the screen goes dark…"))
            QApplication.processEvents()   # paint the message before we halt
            try:
                from modules.system_power import poweroff
                if not poweroff():
                    # Couldn't halt (e.g. missing permission). Say so and fall
                    # back to exiting the app so the operator isn't stuck.
                    self.splash.set_status(tr("Could not power off automatically — please shut down from the OS."))
                    QApplication.processEvents()
                    log.error("Automatic power-off failed; exiting app only.")
            except Exception:
                log.exception("power-off error")
        else:
            try:
                from modules.system_power import stop_kiosk_service
                if stop_kiosk_service():
                    self.splash.set_status(tr("Exiting — returning to desktop…"))
                    QApplication.processEvents()
                    QTimer.singleShot(8000, self.close)
                    return
            except Exception:
                log.exception("service-stop error")
        self.close()

    @Slot(object)
    def _on_backend_ready(self, components):
        self._cancel_slow_timer()
        self.splash.hide_actions()
        self.operator.attach_backend(components)
        # Records needs the DB to show keg moves recorded before this session.
        self.records.attach_backend(components.get("database"),
                                    components.get("print_queue"))
        # Settings needs the camera for the focus control (a webcam drives the
        # lens through the live capture; the iCam-540 always goes over REST).
        cam_login = components.get("camera_auth") or {}
        if cam_login.get("ok"):
            self.settings.append_log(
                f"[login] Camera signed in to the cloud as {get_camera_auth().camera_label}")
        else:
            self.settings.append_log(
                f"[login] Camera sign-in failed: {cam_login.get('message')}")
        if OPERATOR_LOGIN_REQUIRED:
            # A camera login ends any shift left over from before the restart,
            # so the saved operator is stale until the sign-in page says
            # otherwise. Cleared before Settings attaches so it doesn't announce
            # last shift's operator.
            try:
                config.save_operator_pref(selected_id="", selected_name="")
            except Exception:
                log.exception("Could not clear the previous operator")
        self.settings.attach_backend(components)
        self.login.attach(components.get("api_sender"), get_camera_auth())
        self._auth_watch.start()
        self._cloud_refresh.start()
        self._disk_timer.start()
        self._check_disk_space()
        for warning in config.CONFIG_WARNINGS:
            log.warning("[config] %s", warning)
            self.settings.append_log(f"[config] {warning}")
            self.settings.add_activity("warning", tr("Settings problem: {msg}", msg=warning))
        self.settings.append_log("[init] System ready")
        self.settings.add_activity("success", tr("System ready"))
        # Splash is done (camera init + auto-focus + backend): show 100 % for
        # a moment, then operator sign-in, then the dashboard.
        self.splash.finish()
        self.splash.set_status(tr("Ready"))

        def _leave_splash():
            if self._stopped or self.stack.currentWidget() is not self.splash:
                return
            if OPERATOR_LOGIN_REQUIRED:
                self._show_login(cam_login)
            else:
                self._enter_dashboard()
        QTimer.singleShot(500, _leave_splash)

    @Slot(str)
    def _on_backend_failed(self, err):
        # Show the failure and offer RETRY / EXIT. In kiosk mode the operator
        # has no other way out short of a power cycle.
        self._cancel_slow_timer()
        log.error("Backend init failed: %s", err)
        self.settings.append_log(f"[init] Initialisation failed: {err}")
        self.splash.set_error(
            tr("Initialisation failed: {error}", error=str(err)[:120]),
            tr("RETRY reloads the detection model and reopens the camera.") + " "
            + tr("If it keeps failing, EXIT and check the camera daemon."))

    # Shutdown
    def resizeEvent(self, e):
        super().resizeEvent(e)
        if getattr(self, "toast", None) and self.toast.isVisible():
            self.toast._reposition()

    def _pending_tasks(self):
        """Best-effort count of background work still running (uploads / prints)."""
        n = 0
        try:
            from modules.process_worker import get_active_tasks
            n += int(get_active_tasks())
        except Exception:
            pass
        try:
            n += sum(1 for w in getattr(self.operator, "_workers", []) if w.isRunning())
        except Exception:
            pass
        return n

    def _show_shutdown_screen(self, pending):
        """Show the shutdown screen while the backend (camera, uploads,
        printer) is being closed."""
        self.topbar.hide()
        self.sidebar.hide()
        self._cancel_slow_timer()
        self.splash.hide_actions()   # no RETRY/EXIT while we are shutting down
        self.splash.set_indeterminate()
        if pending:
            self.splash.set_status(
                tr("Finishing {n} background task(s) — please wait…", n=pending))
        elif self.restart_requested:
            self.splash.set_status(tr("Refreshing — closing camera & services…"))
        elif self._poweroff:
            self.splash.set_status(tr("Shutting down — then powering off the device…"))
        else:
            self.splash.set_status(tr("Shutting down — closing camera & services…"))
        self.stack.setCurrentWidget(self.splash)
        QApplication.processEvents()   # paint the message before _cleanup blocks

    def closeEvent(self, e):
        # Confirm before exiting (covers both the Exit button and the window X).
        # If we've already begun shutdown, don't prompt again.
        if not self._stopped:
            if not authorize_maintenance(self, tr("Exit the app")):
                e.ignore()
                return
            pending = self._pending_tasks()
            detail = tr("The camera will be released and services closed.")
            if pending:
                detail = (tr("{n} background task(s) are still finishing "
                             "(uploads / printing).", n=pending) + " "
                          + tr("They'll be completed or safely queued before exit."))
            detail += "\n\n" + self._wipe_warning()
            if not ConfirmDialog.ask(
                    self, tr("Exit Application"), tr("Exit Keg Palletization?"),
                    detail, confirm_text=tr("EXIT")):
                e.ignore()
                return
            self._show_shutdown_screen(pending)
        self._cleanup()
        super().closeEvent(e)

    def _cleanup(self):
        if self._stopped:
            return
        self._stopped = True
        self._cancel_slow_timer()
        self._auth_watch.stop()
        self._cloud_refresh.stop()
        self._disk_timer.stop()
        try:
            self.login.shutdown()
            w = getattr(self, "_sign_out_worker", None)
            if w is not None and w.isRunning():
                w.wait(3000)
        except Exception:
            log.exception("login shutdown error")
        try:
            self.operator.shutdown()
        except Exception:
            log.exception("operator shutdown error")
        try:
            self.settings.shutdown()
        except Exception:
            log.exception("settings shutdown error")
        # Join the init worker (and any superseded by a RETRY) before Qt tears
        # down: destroying a running QThread aborts the process.
        for w in [getattr(self, "_init_worker", None)] + list(self._orphaned_workers):
            try:
                if w is not None and w.isRunning():
                    w.wait(5000)
            except Exception:
                log.exception("init worker join error")
        self._orphaned_workers.clear()
        try:
            from modules.process_worker import shutdown as worker_shutdown
            worker_shutdown()
        except Exception:
            log.exception("process_worker shutdown error")
        # The print thread stopped before the uploads finished. Save any label
        # for a pallet confirmed since then; it prints on the next start.
        pq = getattr(self.operator, "print_queue", None)
        if pq is not None:
            pq.flush()
        # Last: every worker that writes to the DB has stopped by now.
        wipe_local_data("exit")

    def _wipe_warning(self):
        """The line every exit / refresh dialog carries about the local wipe."""
        msg = tr("Note: the local database and cached cloud data are cleared "
                 "automatically.")
        unsent = count_unsent_reports()
        if unsent:
            msg += " " + tr("{n} report(s) not yet uploaded are kept and sent "
                            "when the network is back.", n=unsent)
        return msg


# Entry point
def _relaunch(skip_camera_init=True):
    """Refresh: start the app again with camera auto-focus skipped.

    skip_camera_init=False after the first-run Camera Setup: the camera REST
    init and auto-focus have not run yet in this process, so the new one must
    do them.

    On the device (POSIX) the process is replaced in place with os.execve and
    keeps its PID. start_prod.sh keeps waiting on it, log redirection is
    inherited, and systemd never sees an exit, so there is no Restart=always
    cycle and no camera warm-up / preflight in start_prod.sh. KEG_KIOSK and
    systemd's INVOCATION_ID carry over in the environment, so kiosk mode and
    the Exit button's service stop still work. On the Windows dev box execv is
    not a true in-place replace, so a child is spawned and this one exits.
    """
    env = dict(os.environ)
    if skip_camera_init:
        env["KEG_SKIP_CAMERA_INIT"] = "1"
    else:
        env.pop("KEG_SKIP_CAMERA_INIT", None)
    args = [sys.executable] + sys.argv
    log.info("Relaunching for REFRESH: %s", " ".join(args))
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if os.name == "posix":
            os.execve(sys.executable, args, env)   # does not return
        import subprocess
        subprocess.Popen(args, env=env)
    except OSError:
        # Fall back to a plain exit. Under systemd, Restart=always brings the
        # app back (with a full camera init).
        log.exception("REFRESH relaunch failed — exiting instead")


def main():
    # The startup wipe runs in the first DatabaseManager() (modules/database.py).
    app = QApplication(sys.argv)
    app.setApplicationName("Keg Palletization")
    app.setStyle("Fusion")
    # One standard UI font everywhere, with fallbacks for the Linux camera
    # (Segoe UI exists only on Windows).
    font = QFont()
    font.setFamilies(["Segoe UI", "Inter", "Roboto", "Noto Sans", "DejaVu Sans"])
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(GLOBAL_QSS)
    # Screen language before any widget is built (the last one chosen here).
    i18n.set_language(config.ui_language())

    # First start on a new camera: no MAC saved yet. Set it on the Camera
    # Setup screen (checked with the cloud), then restart so every module
    # imports the saved value. The main window is not built without a MAC:
    # every cloud call is addressed by it.
    if not config.CAMERA_MAC_ID:
        from ui_pyside.setup_page import CameraSetupDialog
        log.info("No camera MAC saved — showing Camera Setup")
        setup = CameraSetupDialog(first_run=True)
        if KIOSK:
            setup.showFullScreen()
        if setup.exec() == CameraSetupDialog.DialogCode.Accepted:
            log.info("Camera Setup done — restarting with MAC %s", config.CAMERA_MAC_ID)
            _relaunch(skip_camera_init=SKIP_CAMERA_INIT)
        else:
            log.info("Camera Setup closed without saving — exiting")
        sys.exit(0)

    # [PERF] efficiency log (camera fps, frame use, CPU/GPU, temperatures).
    try:
        from modules import perf_log
        perf_log.start()
    except Exception:
        log.exception("[PERF] log could not start")

    # Old captured frames (keg_frames/) left by failed or cancelled sends.
    try:
        from modules import frame_store
        frame_store.prune_soon("startup")
    except Exception:
        log.exception("keg_frames cleanup could not start")

    win = MainWindow()
    app.aboutToQuit.connect(win._cleanup)
    if KIOSK:
        # Production: full-screen kiosk with no window chrome. The service uses
        # Restart=on-failure for crash recovery; a deliberate Exit requests an
        # explicit service stop so the app stays down until started manually.
        win.showFullScreen()

    # Graceful termination signals. On the iCam-540 the app runs as a systemd
    # user service, which stops it with SIGTERM. Without a handler Python is
    # killed with no cleanup (camera left open, uploads dropped, DB possibly
    # mid-write). SIGTERM/SIGINT/SIGHUP go through app.quit(), which fires
    # QApplication.aboutToQuit -> win._cleanup() to release the camera and
    # drain uploads/prints. SIGHUP is POSIX-only, hence the guard.
    def _handle_signal(signum, _frame):
        log.info("Received signal %s — shutting down gracefully", signum)
        win.restart_requested = False   # a stop from systemd beats a pending refresh
        app.quit()

    for _sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _handle_signal)
            except (ValueError, OSError):
                # e.g. not on the main thread, or unsupported on this platform
                pass
    # Qt's event loop sits in C and won't run Python signal handlers until
    # control returns to the interpreter, so a no-op timer gives it frequent
    # chances.
    _t = QTimer()
    _t.start(300)
    _t.timeout.connect(lambda: None)

    if not KIOSK:
        win.show()
    code = app.exec()
    if win.restart_requested:
        _relaunch()
    sys.exit(code)


if __name__ == "__main__":
    main()
