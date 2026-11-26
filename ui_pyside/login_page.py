"""
Operator sign-in page, shown after the splash (camera init, auto-focus,
backend load) and before the dashboard.

Follows the Checkology Camera Login API:
  * the operator list (GET /api/camera/{mac}/operators) says per operator
    whether they have a PIN (`hasPin`): true -> PIN pad, false -> email +
    password;
  * POST /api/camera/operator/login with one of {operatorId, pin} or
    {email, password};
  * 423 OPERATOR_LOCKED -> wait `retryAfterSeconds`, shown as a countdown.

If the camera itself is not signed in (missing or wrong camera password,
locked, cloud unreachable) the card shows a camera-connection panel instead,
since an operator cannot sign in on a camera the cloud does not trust. When
only the network is missing the operator may continue without signing in, so
an outage does not stop the line. Reports then carry the Settings operator
fields, which the cloud accepts when nobody is signed in.
"""

import logging

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QEvent, QSize
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtCore import QRegularExpression
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QFrame,
)

from modules.camera_auth import CREDENTIAL_CODES, NETWORK
from ui_pyside.theme import C
from ui_pyside.i18n import tr, qlocale
import config
from ui_pyside.widgets import (BrandLogo, SearchableCombo, EmailField, field_label, svg_icon,
                               svg_pixmap, IconLabel, caution_note,
                               add_password_toggle, set_password_visible, language_button)
from ui_pyside.workers import AuthWorker, OperatorFetcher
from ui_pyside.operator_page import validate_batch, next_batch

log = logging.getLogger("ui_pyside.login")

BTN_H = 40          # touch-sized controls (fits a 1366×768 panel)
CARD_W = 460


def _show_page(pages, index):
    """Show one of `pages` (sibling widgets in one layout) and hide the rest.

    Uses plain show/hide instead of a QStackedWidget: a stack is as tall as its
    tallest page and ignores word-wrapped label heights, which leaves big gaps
    in the card."""
    for i, w in enumerate(pages):
        w.setVisible(i == index)


def _button(text, name, height=BTN_H):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setFixedHeight(height)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class LoginPage(QWidget):
    signed_in = Signal(object)        # operator dict {id, name, role, loggedInAt}
    continue_offline = Signal()       # cloud unreachable, run without an operator
    batch_confirmed = Signal(str)     # batch picked on the step after sign-in
    notice = Signal(str, str)         # (message, kind) for a short toast
    back_requested = Signal()         # batch step: back to sign-in (signs the operator out)
    switch_cancelled = Signal()       # switch operator: cancel, keep the current one
    exit_requested = Signal()
    language_requested = Signal()     # language picker, before signing in

    def __init__(self):
        super().__init__()
        self.setObjectName("splash")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._api = None
        self._auth = None
        self._operators = []
        self._workers = []
        self._busy = False
        self._lock_left = 0
        self._lock_btn = None
        self._cam_connected_announced = False
        self._lock_timer = QTimer(self)
        self._lock_timer.setInterval(1000)
        self._lock_timer.timeout.connect(self._tick_lock)
        self._build()

    # Layout
    def _build(self):
        """Split screen: branded navy panel on the left, sign-in box on the right."""
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_hero(), 11)

        side = QFrame()
        side.setObjectName("loginSide")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(24, 16, 20, 12)
        sl.setSpacing(0)
        outer.addWidget(side, 9)

        # Exit button top right, shown on every step (camera connection,
        # operator sign-in, batch selection).
        exit_btn = QPushButton(tr("  Exit"))
        exit_btn.setObjectName("loginExit")
        exit_btn.setIcon(svg_icon("power", C['ck_pink_dark'], 16))
        exit_btn.setIconSize(QSize(16, 16))
        exit_btn.setFixedHeight(36)
        exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        exit_btn.setToolTip(tr("Exit or shut down"))
        exit_btn.clicked.connect(self.exit_requested.emit)
        # Language, top left: an operator can switch before reading the card.
        self._lang_btn = language_button(height=36)
        self._lang_btn.clicked.connect(self.language_requested.emit)
        top = QHBoxLayout()
        top.addWidget(self._lang_btn)
        top.addStretch()
        top.addWidget(exit_btn)
        sl.addLayout(top)
        sl.addStretch(1)

        card = QFrame()
        card.setObjectName("loginCard")
        card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        card.setFixedWidth(CARD_W)
        # Word-wrapped labels leave a centred card taller than its content
        # after a panel switch, so re-fit it whenever its layout changes.
        self._card = card
        card.installEventFilter(self)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(1, 1, 1, 1)
        card_lay.setSpacing(0)
        stripe = QFrame()                  # navy → pink, as on the web sign-in
        stripe.setObjectName("loginStripe")
        stripe.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        stripe.setFixedHeight(5)
        card_lay.addWidget(stripe)
        cl = QVBoxLayout()
        cl.setContentsMargins(28, 18, 28, 18)
        cl.setSpacing(8)
        card_lay.addLayout(cl)

        self._title = QLabel(tr("Operator sign in"))
        self._title.setObjectName("loginTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        cl.addWidget(self._title)
        self._subtitle = QLabel(tr("Start your shift on this camera."))
        self._subtitle.setObjectName("loginSub")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setWordWrap(True)
        cl.addWidget(self._subtitle)

        self._cam_status = IconLabel(size=12, icon_size=16, radius=6, pad=(10, 7), wrap=True)
        self._cam_status.setVisible(False)     # shown only for a problem (see _set_cam_status)
        cl.addWidget(self._cam_status)
        cl.addSpacing(2)

        self._panels = [self._build_operator_panel(),   # 0
                        self._build_camera_panel(),     # 1
                        self._build_batch_panel()]      # 2
        for p in self._panels:
            cl.addWidget(p)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error.setStyleSheet(f"color:{C['red']}; font-size:12px; font-weight:700;")
        self._error.setVisible(False)
        cl.addWidget(self._error)

        self._offline_btn = _button(tr("CONTINUE WITHOUT SIGN-IN"), "ghostBtn", 38)
        self._offline_btn.setToolTip(tr(
            "The cloud cannot be reached. The line keeps running; pallets are "
            "queued and sent with no signed-in operator once it is back."))
        self._offline_btn.clicked.connect(self.continue_offline.emit)
        self._offline_btn.setVisible(False)
        cl.addWidget(self._offline_btn)

        # Switch operator: go back to the dashboard, current operator unchanged.
        self._switch_cancel_btn = QPushButton(tr("  Cancel"))
        self._switch_cancel_btn.setObjectName("headerBtn")
        self._switch_cancel_btn.setIcon(svg_icon("arrow-left", C["ck_navy"], 15))
        self._switch_cancel_btn.setIconSize(QSize(15, 15))
        self._switch_cancel_btn.setFixedHeight(32)
        self._switch_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._switch_cancel_btn.clicked.connect(self.switch_cancelled.emit)
        self._switch_cancel_btn.setVisible(False)
        self._switching_from = ""
        cl.addWidget(self._switch_cancel_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        sl.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
        sl.addStretch(1)
        sl.addSpacing(36)     # balances the Exit row, so the card stays centred
        _show_page(self._modes, 0)
        _show_page(self._panels, 0)

    def _build_hero(self):
        """Left panel in the Checkology web look: logo badge and headline."""
        hero = QFrame()
        hero.setObjectName("loginHero")
        hero.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        v = QVBoxLayout(hero)
        v.setContentsMargins(56, 24, 56, 40)
        v.setSpacing(0)
        v.addStretch(2)

        v.addWidget(BrandLogo(height=38, plaque="badge", variant="white"),
                    alignment=Qt.AlignmentFlag.AlignLeft)
        v.addSpacing(26)

        title = QLabel(tr("<span style='color:{color}'>Keg</span> Palletization.",
                      color=C['ck_navy']))
        title.setObjectName("heroTitle")
        v.addWidget(title)
        v.addSpacing(12)
        tag = QHBoxLayout()
        tag.setSpacing(14)
        rule = QFrame()
        rule.setObjectName("heroRule")
        rule.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rule.setFixedSize(36, 2)
        tag.addWidget(rule, alignment=Qt.AlignmentFlag.AlignVCenter)
        sub = QLabel(tr("INDUSTRIAL VISION HMI"))
        sub.setObjectName("heroSub")
        tag.addWidget(sub)
        tag.addStretch()
        v.addLayout(tag)
        v.addStretch(3)
        return hero

    def _build_operator_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # PIN | EMAIL segmented toggle
        self._seg_w = QWidget()
        seg = QHBoxLayout(self._seg_w)
        seg.setContentsMargins(0, 0, 0, 0)
        seg.setSpacing(0)
        self._seg_pin = _button(tr("PIN"), "segLeftOn", 38)
        self._seg_email = _button(tr("EMAIL && PASSWORD"), "segRightOff", 38)
        self._seg_pin.clicked.connect(lambda: self._set_mode("pin"))
        self._seg_email.clicked.connect(lambda: self._set_mode("email"))
        seg.addWidget(self._seg_pin, 1)
        seg.addWidget(self._seg_email, 1)
        v.addWidget(self._seg_w)

        # PIN mode: operator picker and PIN pad
        pin_w = QWidget()
        pv = QVBoxLayout(pin_w)
        pv.setContentsMargins(0, 4, 0, 0)
        pv.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(field_label(tr("OPERATOR")))
        row.addStretch()
        self._refresh_btn = QPushButton(tr("Refresh list"))
        self._refresh_btn.setObjectName("headerBtn")
        self._refresh_btn.setFixedHeight(32)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.clicked.connect(self._fetch_operators)
        row.addWidget(self._refresh_btn)
        pv.addLayout(row)
        self._combo = SearchableCombo(placeholder=tr("Search your name…"))
        self._combo.setFixedHeight(BTN_H)
        self._combo.currentTextChanged.connect(self._on_operator_picked)
        pv.addWidget(self._combo)
        # "Last sign-in" line, shown only once someone is picked.
        self._last_pin = IconLabel("clock", "", C["slate"], size=11, weight=500,
                                   icon_size=12, pad=(2, 0))
        self._last_pin.setVisible(False)
        pv.addWidget(self._last_pin)

        # PIN field and pad. Hidden in local sign-in (no cloud login).
        self._pin_box = QWidget()
        pbv = QVBoxLayout(self._pin_box)
        pbv.setContentsMargins(0, 0, 0, 0)
        pbv.setSpacing(6)
        pbv.addWidget(field_label(tr("PIN")))
        self._pin = QLineEdit()
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin.setMaxLength(6)
        self._pin.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}")))
        self._pin.setPlaceholderText(tr("4–6 digits"))
        self._pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin.setFixedHeight(BTN_H)
        self._pin.setStyleSheet("font-size:20px; letter-spacing:6px;")
        self._pin.returnPressed.connect(self._submit)
        pbv.addWidget(self._pin)

        pad = QGridLayout()
        pad.setSpacing(6)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "CLR", "0", "DEL"]
        for i, k in enumerate(keys):
            b = _button("" if k == "DEL" else tr("CLR") if k == "CLR" else k, "ghostBtn", 40)
            b.setStyleSheet("font-size:18px;")
            if k == "DEL":
                b.setIcon(svg_icon("delete", C["ck_navy_dark"], 22))
                b.setIconSize(QSize(22, 22))
                b.setToolTip(tr("Delete last digit"))
            b.clicked.connect(lambda _=False, key=k: self._pad(key))
            pad.addWidget(b, i // 3, i % 3)
        pbv.addLayout(pad)
        pv.addWidget(self._pin_box)
        self._local_note = QLabel(tr(
            "Cloud sign-in is not enabled on this server yet. Pick your name — "
            "your pallets are recorded against you in the report."))
        self._local_note.setObjectName("splashSub")
        self._local_note.setStyleSheet("font-size:11px;")
        self._local_note.setWordWrap(True)
        pv.addWidget(self._local_note)

        # Email mode
        em_w = QWidget()
        ev = QVBoxLayout(em_w)
        ev.setContentsMargins(0, 4, 0, 0)
        ev.setSpacing(6)
        self._email_hint = QLabel("")
        self._email_hint.setObjectName("splashSub")
        self._email_hint.setWordWrap(True)
        self._email_hint.setVisible(False)
        ev.addWidget(self._email_hint)
        ev.addWidget(field_label(tr("EMAIL (USERNAME)")))
        self._email = EmailField()      # remembered emails as suggestions
        self._email.setPlaceholderText("name@brewery.com")
        self._email.setFixedHeight(BTN_H)
        self._email.textChanged.connect(self._update_last_email)
        ev.addWidget(self._email)
        self._last_email = IconLabel("clock", "", C["slate"], size=11, weight=500,
                                     icon_size=12, pad=(2, 0))
        self._last_email.setVisible(False)
        ev.addWidget(self._last_email)
        ev.addWidget(field_label(tr("PASSWORD")))
        self._password = QLineEdit()
        self._password.addAction(svg_icon("lock", C["slate"]),
                                 QLineEdit.ActionPosition.LeadingPosition)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        add_password_toggle(self._password)
        self._password.setPlaceholderText(tr("Your Keg Management password"))
        self._password.setFixedHeight(BTN_H)
        self._password.returnPressed.connect(self._submit)
        ev.addWidget(self._password)

        self._modes = [pin_w, em_w]
        v.addWidget(pin_w)
        v.addWidget(em_w)

        self._sign_in_btn = _button(tr("SIGN IN"), "primaryBtn")
        self._sign_in_btn.clicked.connect(self._submit)
        v.addWidget(self._sign_in_btn)
        self._mode = "pin"
        self._local = False
        return w

    def _build_camera_panel(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        self._cam_reason = QLabel("")
        self._cam_reason.setWordWrap(True)
        self._cam_reason.setStyleSheet(f"color:{C['text2']}; font-size:13px;")
        v.addWidget(self._cam_reason)

        self._cam_pw_box = QWidget()
        pv = QVBoxLayout(self._cam_pw_box)
        pv.setContentsMargins(0, 4, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(field_label(tr("CAMERA PASSWORD")))
        self._cam_pw = QLineEdit()
        self._cam_pw.setEchoMode(QLineEdit.EchoMode.Password)
        add_password_toggle(self._cam_pw)
        self._cam_pw.setPlaceholderText(tr("24-character camera password"))
        self._cam_pw.setFixedHeight(BTN_H)
        self._cam_pw.returnPressed.connect(self._connect_camera)
        pv.addWidget(self._cam_pw)
        where = QLabel(tr("Keg Management → Scanner → Cameras → this camera → "
                          "Camera login → Generate password. It is shown once; it is "
                          "stored on this device only."))
        where.setObjectName("splashSub")
        where.setStyleSheet("font-size:11px;")
        where.setWordWrap(True)
        pv.addWidget(where)
        v.addWidget(self._cam_pw_box)

        self._cam_btn = _button(tr("CONNECT CAMERA"), "primaryBtn")
        self._cam_btn.clicked.connect(self._connect_camera)
        v.addWidget(self._cam_btn)
        return w

    def _build_batch_panel(self):
        """Step after sign-in where the operator chooses the production batch.

        The batch is stamped on every pallet and does not advance by itself."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 2, 0, 0)
        v.setSpacing(8)

        # Who is signing in. The name wraps instead of being cut off.
        self._batch_who = QFrame()
        self._batch_who.setObjectName("batchWho")
        self._batch_who.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        wl = QHBoxLayout(self._batch_who)
        wl.setContentsMargins(12, 10, 12, 10)
        wl.setSpacing(10)
        avatar = QLabel()
        avatar.setPixmap(svg_pixmap("user", C["ck_navy"], 28))
        avatar.setFixedSize(28, 28)
        avatar.setStyleSheet("background:transparent;")
        wl.addWidget(avatar, 0, Qt.AlignmentFlag.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(1)
        cap = QLabel(tr("SIGNED IN AS"))
        cap.setObjectName("fieldLabel")
        names.addWidget(cap)
        self._batch_who_name = QLabel("")
        self._batch_who_name.setObjectName("batchWhoName")
        self._batch_who_name.setWordWrap(True)
        names.addWidget(self._batch_who_name)
        wl.addLayout(names, 1)
        v.addWidget(self._batch_who)
        v.addSpacing(4)

        v.addWidget(field_label(tr("BATCH NUMBER")))
        self._batch_edit = QLineEdit()
        self._batch_edit.setMaxLength(32)
        self._batch_edit.setPlaceholderText(tr("e.g. BATCH-001"))
        self._batch_edit.addAction(svg_icon("package", C["slate"]),
                                   QLineEdit.ActionPosition.LeadingPosition)
        self._batch_edit.setFixedHeight(BTN_H)
        self._batch_edit.setStyleSheet("font-size:15px; font-weight:600;")
        self._batch_edit.textChanged.connect(self._update_batch_next)
        self._batch_edit.returnPressed.connect(self._confirm_batch)
        v.addWidget(self._batch_edit)

        self._batch_next_btn = _button(tr("Next batch"), "ghostBtn", 38)
        self._batch_next_btn.setIcon(svg_icon("arrow-right", C["ck_navy"], 16))
        self._batch_next_btn.setIconSize(QSize(16, 16))
        self._batch_next_btn.clicked.connect(self._batch_step_next)
        v.addWidget(self._batch_next_btn)
        # Shown only on a new day: suggests the next number, changes nothing.
        self._batch_day_note = caution_note("")
        self._batch_day_note.setVisible(False)
        v.addWidget(self._batch_day_note)
        v.addSpacing(4)

        self._batch_ok_btn = _button(tr("START WITH THIS BATCH"), "primaryBtn")
        self._batch_ok_btn.clicked.connect(self._confirm_batch)
        v.addWidget(self._batch_ok_btn)

        back = QPushButton(tr("  Back to sign in"))
        back.setObjectName("headerBtn")
        back.setIcon(svg_icon("arrow-left", C["ck_navy"], 15))
        back.setIconSize(QSize(15, 15))
        back.setFixedHeight(32)
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setToolTip(tr("Wrong person? Go back and sign in again"))
        back.clicked.connect(self.back_requested.emit)
        v.addWidget(back, 0, Qt.AlignmentFlag.AlignHCenter)
        return w

    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        if obj is self._card and event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._fit_card)
        return super().eventFilter(obj, event)

    def _fit_card(self):
        h = self._card.layout().heightForWidth(CARD_W)
        if h <= 0:
            h = self._card.layout().sizeHint().height()
        # Add the card's own border (4 px accent on top, 1 px elsewhere).
        h += self._card.height() - self._card.contentsRect().height()
        if self._card.height() != h:
            self._card.setFixedHeight(h)

    # Entry point
    def attach(self, api_sender, camera_auth):
        self._api = api_sender
        self._auth = camera_auth

    def activate(self, camera_result=None, notice="", switching_from=""):
        """Show the page in a fresh state.

        `camera_result` is the start-up camera login result. `switching_from`
        is the name of the operator still signed in during a switch; the card
        mentions it and offers Cancel."""
        self._switching_from = switching_from or ""
        self._switch_cancel_btn.setText(
            tr("  Cancel — stay signed in as {name}", name=self._switching_from))
        self._switch_cancel_btn.setVisible(bool(self._switching_from))
        self._pin.clear()
        self._password.clear()
        self._cam_pw.clear()
        # Don't leave a revealed password on screen for the next person.
        set_password_visible(self._password, False)
        set_password_visible(self._cam_pw, False)
        self._email.set_suggestions(config.remembered_emails())
        self._show_error(notice)
        self._offline_btn.setVisible(False)
        if self._auth is not None and self._auth.signed_in:
            self._show_operator_panel()
        elif self._auth is not None and not self._auth.cloud_login_available:
            self._show_operator_panel(local=True)
        else:
            res = camera_result or (self._auth.last_error if self._auth else None)
            self._show_camera_panel(res or {"error": "NO_PASSWORD",
                                            "message": tr("The camera is not signed in.")})
            if notice:
                self._show_error(notice)

    # Camera panel
    def _show_camera_panel(self, res):
        code = res.get("error")
        self._title.setText(tr("Camera connection"))
        self._subtitle.setText(tr("Connect this camera to the Checkology cloud."))
        self._set_cam_status(False, tr("Camera not signed in to the cloud"))
        self._cam_reason.setText(res.get("message") or code or "")
        needs_pw = code in CREDENTIAL_CODES
        self._cam_pw_box.setVisible(needs_pw)
        self._cam_btn.setText(tr("SAVE && CONNECT") if needs_pw else tr("RETRY"))
        # Not offered for a wrong or missing password: that needs fixing, not skipping.
        self._offline_btn.setVisible(code not in CREDENTIAL_CODES)
        if code == "CAMERA_LOCKED" and res.get("retry_after"):
            self._start_lock(int(res["retry_after"]), self._cam_btn)
        _show_page(self._panels, 1)
        if needs_pw:
            self._cam_pw.setFocus()

    @Slot()
    def _connect_camera(self):
        if self._busy or self._auth is None:
            return
        password = None
        if self._cam_pw_box.isVisible():
            password = self._cam_pw.text().strip()
            if not password:
                self._show_error(tr("Enter the camera password."))
                return
        self._show_error("")
        self._run(self._auth.login, self._on_camera_login, self._cam_btn,
                  tr("CONNECTING…"), password)

    def _on_camera_login(self, res):
        self._cam_pw.clear()
        if res.get("ok"):
            self._show_error("")
            self._offline_btn.setVisible(False)
            self._show_operator_panel()
        elif res.get("error") == "AUTH_NOT_DEPLOYED":
            self._show_operator_panel(local=True)
        else:
            self._show_camera_panel(res)

    # Operator panel
    def _show_operator_panel(self, local=False):
        """Show operator sign-in.

        `local` means the server has no Camera Login API: the operator only
        picks a name, which goes into the report's operator fields."""
        self._local = local
        if self._switching_from:
            self._title.setText(tr("Switch operator"))
            self._subtitle.setText(tr("The next operator signs in here."))
        else:
            self._title.setText(tr("Operator sign in"))
            self._subtitle.setText(tr("Start your shift on this camera."))
        if local:
            self._set_cam_status(None, tr("Local sign-in — cloud login not enabled yet"))
        else:
            self._set_cam_status(True, tr("Camera {camera} connected to the cloud",
                                          camera=self._auth.camera_label))
        self._seg_w.setVisible(not local)
        self._pin_box.setVisible(not local)
        self._local_note.setVisible(local)
        _show_page(self._panels, 0)
        self._set_mode("pin")
        self._fetch_operators()

    def _set_cam_status(self, ok, text):
        """Status pill: green = signed in, red = not, amber (None) = local."""
        # When connected, show a short toast instead of a box, and only on the
        # first connect (or a reconnect after a failure). The box is used for
        # problems only: not connected (red) or local sign-in (amber).
        if ok is True:
            self._cam_status.setVisible(False)
            if not self._cam_connected_announced:
                self._cam_connected_announced = True
                self.notice.emit(text, "success")
            return
        self._cam_connected_announced = False
        icon, fg, bg = {False: ("circle-x",     C['red_dark'],   "rgba(239,68,68,0.08)"),
                        None:  ("circle-alert", C['amber_dark'], "rgba(245,158,11,0.12)")}[ok]
        self._cam_status.set(icon, text, fg, bg)
        self._cam_status.setVisible(True)

    def _set_mode(self, mode):
        self._mode = mode
        pin = mode == "pin"
        self._seg_pin.setObjectName("segLeftOn" if pin else "segLeftOff")
        self._seg_email.setObjectName("segRightOff" if pin else "segRightOn")
        for b in (self._seg_pin, self._seg_email):
            b.style().unpolish(b)
            b.style().polish(b)
        _show_page(self._modes, 0 if pin else 1)
        (self._pin if pin else self._email).setFocus()

    @Slot()
    def _fetch_operators(self):
        if self._api is None:
            return
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText(tr("Loading…"))
        w = OperatorFetcher(self._api, self)
        w.result_ready.connect(self._on_operators)
        w.failed.connect(lambda err: self._on_operators([], err))
        self._keep(w)
        w.start()

    def _on_operators(self, operators, warning):
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText(tr("Refresh list"))
        self._operators = list(operators or [])
        self._combo.set_items([o["name"] for o in self._operators])
        if not self._operators and self._local:
            self._show_error(tr("No operator list available.")
                             + (f"\n{warning}" if warning else ""))
            self._offline_btn.setVisible(True)
        elif not self._operators:
            self._show_error(tr("No operator list available — sign in with email and "
                                "password.") + (f"\n{warning}" if warning else ""))
            self._set_mode("email")
        elif warning:
            log.warning("Operator list: %s", warning)

    def _selected_operator(self):
        text = self._combo.currentText().strip().lower()
        for o in self._operators:
            if o["name"].strip().lower() == text:
                return o
        return None

    # Last sign-in (stored on this camera, times only)
    @staticmethod
    def _last_text(*keys):
        """"Last sign-in: 29 Sep 2026, 06:00" for the first key that has one."""
        from datetime import datetime
        for key in keys:
            iso = config.last_login(key) if key else ""
            if iso:
                try:
                    when = qlocale().toString(datetime.fromisoformat(iso),
                                              "dd MMM yyyy, HH:mm")
                except ValueError:
                    continue
                return tr("Last sign-in: {when}", when=when)
        return ""

    def _update_last_email(self, text=None):
        email = (self._email.text() if text is None else text).strip().lower()
        msg = self._last_text(f"email:{email}") if email else ""
        self._last_email.setText(msg)
        self._last_email.setVisible(bool(msg))

    def _on_operator_picked(self, _text):
        op = self._selected_operator()
        msg = ""
        if op is not None:
            msg = self._last_text(f"id:{op['id']}" if op.get("id") else "",
                                  f"name:{op['name'].strip().lower()}")
        self._last_pin.setText(msg)
        self._last_pin.setVisible(bool(msg))
        if op is None:
            return
        self._show_error("")
        if self._local:
            return
        if op.get("hasPin"):
            self._email_hint.setVisible(False)
            self._set_mode("pin")
            self._pin.clear()
            self._pin.setFocus()
        else:
            # Per the spec, the PIN pad is offered only when hasPin is true.
            self._email_hint.setText(tr("{name} has no PIN — sign in with the "
                                        "Keg Management email and password.",
                                        name=op['name']))
            self._email_hint.setVisible(True)
            self._set_mode("email")

    def _pad(self, key):
        if key == "CLR":
            self._pin.clear()
        elif key == "DEL":
            self._pin.backspace()
        elif len(self._pin.text()) < 6:
            self._pin.insert(key)

    @Slot()
    def _submit(self):
        if self._busy or self._auth is None or self._lock_left > 0:
            return
        if self._local:
            op = self._selected_operator()
            if op is None:
                self._show_error(tr("Pick your name from the list."))
                return
            config.record_login([f"id:{op['id']}" if op.get("id") else "",
                                 f"name:{op['name'].strip().lower()}"])
            self.signed_in.emit({"id": op.get("id", ""), "name": op["name"],
                                 "role": op.get("role", ""), "local": True})
            return
        if self._mode == "pin":
            op = self._selected_operator()
            pin = self._pin.text().strip()
            if op is None:
                self._show_error(tr("Pick your name from the list."))
                return
            if not op.get("id"):
                self._show_error(tr("This operator has no cloud id — use email and password."))
                self._set_mode("email")
                return
            if not (4 <= len(pin) <= 6):
                self._show_error(tr("The PIN is 4 to 6 digits."))
                return
            kwargs = {"operator_id": op["id"], "pin": pin}
        else:
            email, pw = self._email.text().strip(), self._password.text()
            if not email or not pw:
                self._show_error(tr("Enter your email and password."))
                return
            kwargs = {"email": email, "password": pw}
        self._show_error("")
        self._run(self._auth.operator_login, self._on_operator_login,
                  self._sign_in_btn, tr("SIGNING IN…"), **kwargs)

    def _on_operator_login(self, res):
        self._pin.clear()
        self._password.clear()
        if res.get("ok"):
            self._email_hint.setVisible(False)
            if self._mode == "email":
                # Remember the email for next time, never the password.
                config.remember_email(self._email.text())
                self._email.set_suggestions(config.remembered_emails())
            # Remember when (not how) this operator signed in.
            who = res.get("operator") or {}
            keys = [f"id:{who['id']}" if who.get("id") else "",
                    f"name:{str(who.get('name', '')).strip().lower()}" if who.get("name") else ""]
            if self._mode == "email":
                keys.append(f"email:{self._email.text().strip().lower()}")
            config.record_login(keys)
            self.signed_in.emit(who)
            return
        code = res.get("error")
        if code == "OPERATOR_LOCKED":
            self._start_lock(int(res.get("retry_after") or 300), self._sign_in_btn)
            return
        if code == "PIN_NOT_SET":
            self._set_mode("email")
        if code == NETWORK:
            self._offline_btn.setVisible(True)
        # The camera token was refused and the single re-login failed, so the
        # camera needs attention before any operator can sign in.
        if code in ("TOKEN_EXPIRED", "SESSION_ENDED", "INVALID_TOKEN",
                    "CAMERA_AUTH_REQUIRED") and not self._auth.signed_in:
            self._show_camera_panel(self._auth.last_error or res)
            return
        self._show_error(res.get("message") or code or tr("Sign-in failed"))

    # Batch step (after sign-in)
    def show_batch(self, operator_name, current_batch):
        """Ask for the batch before showing the dashboard.

        Pre-filled with the last batch used on this camera. The operator keeps
        it, types another, or takes the next number."""
        self._title.setText(tr("Batch selection"))
        self._subtitle.setText(tr("Choose the production batch for this shift."))
        self._switch_cancel_btn.setVisible(False)    # the new operator is signed in
        self._batch_who_name.setText(operator_name or "")
        self._batch_who.setVisible(bool(operator_name))
        self._offline_btn.setVisible(False)
        self._show_error("")
        self._batch_edit.setText((current_batch or "").strip())
        self._update_batch_next()
        # On a new day, suggest the next number. The operator taps Next batch or keeps it.
        cur = (current_batch or "").strip()
        day = config.batch_is_from_earlier_day(cur) if next_batch(cur) else ""
        if day:
            from datetime import date
            when = qlocale().toString(date.fromisoformat(day), "dd MMM")
            self._batch_day_note.setText(tr(
                "New day — {batch} was last used on {date}. If today is a new batch, "
                "tap Next batch.", batch=cur, date=when))
        self._batch_day_note.setVisible(bool(day))
        _show_page(self._panels, 2)
        self._batch_edit.setFocus()
        self._batch_edit.selectAll()

    def _update_batch_next(self, _text=None):
        nxt = next_batch(self._batch_edit.text())
        self._batch_next_btn.setText(tr("  Next batch:  {batch}", batch=nxt) if nxt
                                     else tr("  Next batch"))
        self._batch_next_btn.setEnabled(bool(nxt))
        self._show_error("")

    @Slot()
    def _batch_step_next(self):
        nxt = next_batch(self._batch_edit.text())
        if nxt:
            self._batch_edit.setText(nxt)
            self._batch_day_note.setVisible(False)

    @Slot()
    def _confirm_batch(self):
        batch = self._batch_edit.text().strip()
        ok, msg = validate_batch(batch)
        if not ok:
            self._show_error(msg)
            self._batch_edit.setFocus()
            return
        self.batch_confirmed.emit(batch)

    # Lockout countdown
    def _start_lock(self, seconds, button):
        self._lock_left = max(1, seconds)
        self._lock_btn = button
        button.setEnabled(False)
        self._tick_lock()
        self._lock_timer.start()

    def _tick_lock(self):
        if self._lock_left <= 0:
            self._lock_timer.stop()
            self._lock_btn.setEnabled(True)
            self._show_error("")
            return
        m, s = divmod(self._lock_left, 60)
        self._show_error(tr("Too many wrong attempts — try again in {time}.",
                            time=f"{m}:{s:02d}"))
        self._lock_left -= 1

    # Helpers
    def _run(self, fn, on_done, button, busy_text, *args, **kwargs):
        self._busy = True
        label = button.text()
        button.setEnabled(False)
        button.setText(busy_text)

        def finished(res):
            self._busy = False
            button.setText(label)
            button.setEnabled(self._lock_left <= 0)
            on_done(res)

        w = AuthWorker(fn, *args, parent=self, **kwargs)
        w.done.connect(finished)
        self._keep(w)
        w.start()

    def _keep(self, worker):
        """Hold a reference until the thread ends.

        Qt aborts if a running QThread is destroyed."""
        self._workers = [x for x in self._workers if x.isRunning()]
        self._workers.append(worker)

    def _show_error(self, text):
        self._error.setText(text or "")
        self._error.setVisible(bool(text))

    def shutdown(self):
        self._lock_timer.stop()
        for w in self._workers:
            if w.isRunning():
                w.wait(3000)
