# ui_pyside/maintenance.py
"""
Approval for maintenance actions (see modules/maintenance_pin.py).

    if not authorize(self, tr("Change the device identity")):
        return

authorize() asks for an approval every time (no standing permission), in this
order of preference:

  * Administrator: a named Checkology admin proves who they are to the cloud
    for this one action. An admin already on shift confirms with their own
    PIN; anyone else signs in with email + password. The cloud checks the
    credentials and the role, and the audit log records who approved. Note:
    the cloud has no "check only" call, so this is a real operator sign-in:
    whoever signs in becomes the operator on shift.
  * Maintenance PIN (break-glass): the shared PIN. With
    config.MAINTENANCE_PIN_OFFLINE_ONLY it is offered only when the cloud
    cannot check an administrator (camera not signed in, cloud unreachable).
    "Forgot PIN?" resets it after an admin sign-in or a support code.
  * Support code: a one-time challenge-response code from support.

Nothing is asked when maintenance_pin.required() is False (laptop test path
with no PIN set). Wrong PINs and codes lock out across restarts; the dialog
closes itself after config.MAINTENANCE_DIALOG_IDLE_S seconds without input.

main.py calls set_hooks() so a sign-in here also updates the operator shown in
the app, and approvals appear in the Settings activity list.
"""

import logging

from PySide6.QtCore import QRegularExpression, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QRegularExpressionValidator
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QGridLayout,
                               QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton,
                               QVBoxLayout)

import config
from modules import maintenance_pin, pin_unlock
from ui_pyside.i18n import tr
from ui_pyside.theme import C

log = logging.getLogger("maintenance")

_KEY_H = 40

# Set by main.py (set_hooks). Both are optional.
_on_admin_signed_in = None     # fn(operator dict) after a cloud admin sign-in here
_on_activity = None            # fn(kind, text) for the Settings activity list


def set_hooks(on_admin_signed_in=None, on_activity=None):
    global _on_admin_signed_in, _on_activity
    _on_admin_signed_in = on_admin_signed_in
    _on_activity = on_activity


def _activity(kind, text):
    if _on_activity:
        try:
            _on_activity(kind, text)
        except Exception:
            log.exception("Activity hook failed")


def _signed_in_hook(operator):
    """The cloud switched the shift to `operator`: let the app show that."""
    if _on_admin_signed_in and operator:
        try:
            _on_admin_signed_in(operator)
        except Exception:
            log.exception("Sign-in hook failed")


def _camera_auth():
    try:
        from modules.camera_auth import get_camera_auth
        return get_camera_auth()
    except Exception:
        log.exception("Camera sign-in unavailable")
        return None


def _is_admin(operator) -> bool:
    return str((operator or {}).get("role", "")).strip().lower() == "admin"


def authorize(parent, action: str) -> bool:
    """True when `action` may go ahead (see the module docstring)."""
    if not maintenance_pin.required():
        return True
    dlg = AuthorizeDialog(action, parent)
    approved = dlg.exec() == QDialog.DialogCode.Accepted and bool(dlg.approved_by)
    if approved:
        who = dlg.approved_by
        maintenance_pin.audit("approved", action=action, **who)
        if who["method"] == "admin":
            text = tr("{action}: approved by admin {name}", action=action,
                      name=who.get("name") or "?")
        elif who["method"] == "support":
            text = tr("{action}: approved with a support code", action=action)
        else:
            text = tr("{action}: approved with the maintenance PIN", action=action)
        _activity("info" if who["method"] == "admin" else "warning", text)
    else:
        maintenance_pin.audit("not_approved", action=action, reason=dlg.end_reason)
    log.info("Maintenance action %s: %s", "approved" if approved else "refused", action)
    return approved


# Shared building blocks (same look for the PIN and the Forgot PIN dialogs)

def _card(dialog, title_text):
    """Frameless themed card on `dialog`. Returns the body layout."""
    dialog.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    dialog.setModal(True)

    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    # Resize to the content of each step (Forgot PIN shows different parts).
    outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

    root = QFrame()
    root.setObjectName("confirmRoot")
    root.setFixedWidth(380)
    shadow = QGraphicsDropShadowEffect(root)
    shadow.setBlurRadius(60)
    shadow.setOffset(0, 10)
    shadow.setColor(QColor(0, 0, 0, 170))
    root.setGraphicsEffect(shadow)

    vl = QVBoxLayout(root)
    vl.setContentsMargins(0, 0, 0, 0)
    vl.setSpacing(0)

    header = QFrame()
    header.setObjectName("confirmHeader")
    hl = QHBoxLayout(header)
    hl.setContentsMargins(20, 0, 20, 0)
    title = QLabel(title_text)
    title.setObjectName("confirmTitle")
    hl.addWidget(title)
    hl.addStretch()
    vl.addWidget(header)

    body = QVBoxLayout()
    body.setContentsMargins(22, 18, 22, 18)
    body.setSpacing(8)
    vl.addLayout(body)

    outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)
    return body


def _label(name, text=""):
    lbl = QLabel(text)
    lbl.setObjectName(name)
    lbl.setWordWrap(True)
    return lbl


def _digit_field(max_len, on_enter):
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setMaxLength(max_len)
    field.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,%d}" % max_len)))
    field.setAlignment(Qt.AlignmentFlag.AlignCenter)
    field.setFixedHeight(_KEY_H)
    field.setStyleSheet("font-size:20px; letter-spacing:6px;")
    field.returnPressed.connect(on_enter)
    return field


def _text_field(placeholder, on_enter, password=False):
    field = QLineEdit()
    field.setPlaceholderText(placeholder)
    field.setFixedHeight(_KEY_H)
    if password:
        field.setEchoMode(QLineEdit.EchoMode.Password)
    field.returnPressed.connect(on_enter)
    return field


def _error_label():
    err = QLabel("")
    err.setWordWrap(True)
    err.setStyleSheet(f"color:{C['red']}; font-size:12px;")
    err.setVisible(False)
    return err


def _keypad(field):
    """On-screen digit keypad (the line has no keyboard) typing into `field`."""
    def press(key):
        if key == "CLR":
            field.clear()
        elif key == "DEL":
            field.backspace()
        elif len(field.text()) < field.maxLength():
            field.insert(key)

    box = QFrame()
    pad = QGridLayout(box)
    pad.setContentsMargins(0, 0, 0, 0)
    pad.setSpacing(6)
    keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "CLR", "0", "DEL"]
    for i, key in enumerate(keys):
        label = tr("CLR") if key == "CLR" else tr("DEL") if key == "DEL" else key
        b = QPushButton(label)
        b.setObjectName("ghostBtn")
        b.setFixedHeight(_KEY_H)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet("font-size:18px;")
        b.clicked.connect(lambda _=False, k=key: press(k))
        pad.addWidget(b, i // 3, i % 3)
    return box


def _button(text, name, slot):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.clicked.connect(slot)
    return b


def _lock_message():
    left = maintenance_pin.seconds_locked()
    return tr("Too many wrong entries. Try again in {n} s.", n=left) if left else ""


class AuthorizeDialog(QDialog):
    """Approval prompt: administrator, maintenance PIN or support code.

    After exec(): `approved_by` is {"method": "admin"|"pin"|"support",
    "name", "id"} when approved, else None; `end_reason` says why not."""

    def __init__(self, action, parent=None):
        super().__init__(parent)
        self._action = action
        self.approved_by = None
        self.end_reason = "cancelled"
        self._auth = _camera_auth()
        self._cloud_down = False        # set after a network error in this dialog
        self._busy = False
        self._worker = None
        self._method = ""
        self._admin_mode = "pin"        # admin on shift: own PIN; else "email"
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(int(getattr(config, "MAINTENANCE_DIALOG_IDLE_S", 90)) * 1000)
        self._idle.timeout.connect(self._on_idle)
        self._build(action)
        self._go(self._first_method())
        self._idle.start()

    # Which ways to approve are open right now
    def _shift_admin(self):
        """The operator on shift, when the cloud signed them in as an admin."""
        op = (self._auth.operator if self._auth else None) or {}
        return op if _is_admin(op) and op.get("id") else None

    def _admin_available(self):
        a = self._auth
        return bool(a and a.signed_in and a.cloud_login_available and not self._cloud_down)

    def _pin_available(self):
        if not maintenance_pin.is_set():
            return False
        offline_only = getattr(config, "MAINTENANCE_PIN_OFFLINE_ONLY", True)
        return not offline_only or not self._admin_available()

    def _methods(self):
        out = []
        if self._admin_available():
            out.append("admin")
        if self._pin_available():
            out.append("pin")
        if maintenance_pin.support_ready():
            out.append("support")
        return out

    def _first_method(self):
        """Administrator, else PIN; support opens its own dialog, so a device
        with only support unlock shows the support tab on its own."""
        methods = self._methods()
        for m in ("admin", "pin"):
            if m in methods:
                return m
        return "support" if methods else "none"

    # Layout
    def _build(self, action):
        body = _card(self, tr("Approval required"))
        body.addWidget(_label("confirmMsg", action))
        self._detail = _label("confirmDetail")
        body.addWidget(self._detail)

        tabs = QHBoxLayout()
        tabs.setSpacing(6)
        self._tabs = {}
        for key, text in (("admin", tr("Administrator")), ("pin", tr("Maintenance PIN")),
                          ("support", tr("Support code"))):
            b = _button(text, "ghostBtn", lambda _=False, k=key: self._pick(k))
            b.setFixedHeight(_KEY_H - 4)
            self._tabs[key] = b
            tabs.addWidget(b)
        body.addLayout(tabs)
        body.addSpacing(4)

        self._who = _label("confirmDetail")
        body.addWidget(self._who)
        self._email_box = QFrame()
        el = QVBoxLayout(self._email_box)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(8)
        self._email = _text_field(tr("Admin email"), self._submit)
        self._password = _text_field(tr("Password"), self._submit, password=True)
        for f in (self._email, self._password):
            f.textChanged.connect(self._touch)
            el.addWidget(f)
        body.addWidget(self._email_box)

        # Digits: the admin's own PIN or the maintenance PIN
        self._digits = _digit_field(maintenance_pin.MAX_LENGTH, self._submit)
        self._digits.textChanged.connect(self._touch)
        body.addWidget(self._digits)
        self._error = _error_label()
        body.addWidget(self._error)
        self._pad = _keypad(self._digits)
        body.addWidget(self._pad)

        # Other way to sign in / Forgot PIN?, on its own row so long
        # translations are never cut off.
        self._alt = _button("", "tourSkip", self._alternate)
        body.addWidget(self._alt, alignment=Qt.AlignmentFlag.AlignLeft)

        body.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        self._cancel = _button(tr("CANCEL"), "confirmCancel", self.reject)
        actions.addWidget(self._cancel)
        self._ok = _button(tr("APPROVE"), "confirmOk", self._submit)
        actions.addWidget(self._ok)
        body.addLayout(actions)

    def _show_error(self, text):
        self._error.setText(text)
        self._error.setVisible(bool(text))

    @Slot()
    def _touch(self, *_):
        if not self._busy:
            self._idle.start()

    @Slot()
    def _on_idle(self):
        if self._busy:
            self._idle.start()
            return
        self.end_reason = "timeout"
        self.reject()

    def _pick(self, method):
        if self._busy:
            return
        if method == "support":
            self._support()
        else:
            self._go(method)

    def _go(self, method, error=""):
        self._method = method
        methods = self._methods()
        for key, b in self._tabs.items():
            # Support is a button that opens its own dialog: always shown.
            b.setVisible(key in methods and (len(methods) > 1 or key == "support"))
            b.setObjectName("primaryBtn" if key == method else "ghostBtn")
            b.style().unpolish(b)
            b.style().polish(b)
        self._digits.clear()
        self._password.clear()
        for w in (self._email_box, self._who, self._digits, self._pad, self._alt):
            w.setVisible(False)
        self._ok.setVisible(method in ("admin", "pin"))
        self._ok.setText(tr("APPROVE"))
        self._cancel.setText(tr("CANCEL"))
        self._show_error(error)

        if method == "admin":
            shift_admin = self._shift_admin()
            if not shift_admin:
                self._admin_mode = "email"
            self._detail.setText(tr("An administrator must approve this. "
                                    "Every approval is recorded with their name."))
            self._who.setVisible(True)
            if self._admin_mode == "pin":
                self._who.setText(tr("Confirm as {name}: enter your own PIN.",
                                     name=shift_admin.get("name", "")))
                self._digits.setVisible(True)
                self._pad.setVisible(True)
                self._alt.setText(tr("Use email and password"))
                self._alt.setVisible(True)
                self._digits.setFocus()
            else:
                self._who.setText(tr("Sign in with your Checkology admin account."))
                self._email_box.setVisible(True)
                if shift_admin:
                    self._alt.setText(tr("Use my PIN"))
                    self._alt.setVisible(True)
                self._email.setFocus()
        elif method == "pin":
            self._detail.setText(
                tr("The cloud cannot check an administrator right now. "
                   "Enter the maintenance PIN.")
                if not self._admin_available() else tr("Enter the maintenance PIN."))
            self._digits.setVisible(True)
            self._pad.setVisible(True)
            self._alt.setText(tr("Forgot PIN?"))
            self._alt.setVisible(True)
            self._digits.setFocus()
            if not error:
                self._show_error(_lock_message())
        elif method == "support":
            self._detail.setText(tr("The cloud cannot check an administrator right now. "
                                    "Call support for a one-time code."))
        else:
            self._detail.setText(tr(
                "This cannot be approved on this device right now: the cloud cannot "
                "check an administrator and no maintenance PIN or support unlock is "
                "set up. Ask maintenance to run tools/set_maintenance_pin.py."))
            self._cancel.setText(tr("CLOSE"))
            self.end_reason = "no_method"
        self.adjustSize()

    def _set_busy(self, busy, label=""):
        self._busy = busy
        for w in (self._ok, self._cancel, self._alt, *self._tabs.values()):
            w.setEnabled(not busy)
        if busy:
            self._ok.setText(label)
            self._idle.stop()
        else:
            self._ok.setText(tr("APPROVE"))
            self._idle.start()

    def reject(self):
        if self._busy:              # a cloud call is running; its thread must finish
            return
        self._idle.stop()
        self._password.clear()
        self._digits.clear()
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)

    # Actions
    @Slot()
    def _alternate(self):
        if self._busy:
            return
        if self._method == "admin":
            self._admin_mode = "email" if self._admin_mode == "pin" else "pin"
            self._go("admin")
        elif self._method == "pin":
            self._forgot()

    @Slot()
    def _submit(self):
        if self._busy:
            return
        if self._method == "admin":
            self._admin_submit()
        elif self._method == "pin":
            self._pin_submit()

    def _admin_submit(self):
        if self._admin_mode == "pin":
            op = self._shift_admin()
            pin = self._digits.text()
            if not op:
                self._go("admin")
                return
            if not pin:
                self._show_error(tr("Enter your PIN."))
                return
            kwargs = {"operator_id": op.get("id"), "pin": pin}
        else:
            email, password = self._email.text().strip(), self._password.text()
            if not email or not password:
                self._show_error(tr("Enter the admin email and password."))
                return
            kwargs = {"email": email, "password": password}
        from ui_pyside.workers import AuthWorker
        self._show_error("")
        self._set_busy(True, tr("CHECKING…"))
        self._worker = AuthWorker(self._auth.operator_login, parent=self, **kwargs)
        self._worker.done.connect(self._on_admin_result)
        self._worker.start()

    @Slot(object)
    def _on_admin_result(self, res):
        self._worker.wait()
        self._set_busy(False)
        self._password.clear()
        self._digits.clear()
        if not res.get("ok"):
            code = res.get("error") or ""
            maintenance_pin.audit("admin_check_failed", action=self._action, error=code)
            if code == "NETWORK":
                # Fall back to the offline ways (PIN appears now if offline-only).
                self._cloud_down = True
                nxt = self._first_method()
                self._go(nxt, tr("The cloud cannot be reached.") if nxt != "none" else "")
                return
            self._show_error(res.get("message") or tr("Sign-in failed"))
            return
        operator = res.get("operator") or {}
        _signed_in_hook(operator)
        who = {"name": operator.get("name", ""), "id": str(operator.get("id", ""))}
        if not _is_admin(operator):
            maintenance_pin.audit("admin_check_refused", action=self._action,
                                  reason="not admin", **who)
            name = who["name"] or email_mask(self._email.text())
            self._admin_mode = "email"
            self._go("admin", tr("{name} is not an administrator and cannot approve "
                                 "this. {name} is now the operator on shift.", name=name))
            return
        self.approved_by = {"method": "admin", **who}
        self.end_reason = ""
        self.accept()

    def _pin_submit(self):
        locked = _lock_message()
        if locked:
            self._show_error(locked)
            return
        ok, reason = maintenance_pin.check(self._digits.text())
        self._digits.clear()
        if ok:
            self.approved_by = {"method": "pin", "name": "", "id": ""}
            self.end_reason = ""
            self.accept()
        elif reason == "broken":
            self._show_error(tr("The PIN file is damaged. Ask maintenance to set the PIN again."))
        elif reason == "not_set":
            self._go(self._first_method())
        else:
            self._show_error(_lock_message() or tr("Wrong PIN."))

    def _forgot(self):
        self._idle.stop()
        dlg = ForgotPinDialog(self._action, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.verified_by:
            # Identity proven and a new PIN set: that is the approval.
            v = dlg.verified_by
            self.approved_by = {"method": v["method"], "name": v.get("name", ""),
                                "id": v.get("id", "")}
            self.end_reason = ""
            self.accept()
            return
        self._idle.start()
        self._go("pin")

    def _support(self):
        self._idle.stop()
        dlg = ForgotPinDialog(self._action, self, start="support", reset=False)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.verified_by:
            self.approved_by = {"method": "support", "name": "", "id": ""}
            self.end_reason = ""
            self.accept()
            return
        self._idle.start()
        nxt = self._method if self._method in ("admin", "pin") else "none"
        self._go(nxt if nxt in self._methods() else self._first_method())


# Older name, kept for any import of it.
MaintenancePinDialog = AuthorizeDialog


class ForgotPinDialog(QDialog):
    """Prove who is asking (admin sign-in or support unlock code), then set a
    new PIN. Accepted once the new PIN is saved.

    With reset=False (and start="support") it only checks a support unlock
    code and is accepted once the code is right: a one-time approval, the PIN
    is not changed. `verified_by` says who was verified, after exec()."""

    def __init__(self, action, parent=None, start="choose", reset=True):
        super().__init__(parent)
        self._action = action
        self._reset = reset
        self._step = ""
        self._busy = False
        self._worker = None
        self._verified_by = None        # {"method": ..., "by": ...} once proven
        self._new_pin = ""
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._build()
        if start == "support" and not maintenance_pin.support_ready():
            start = "choose"
        self._go(start)

    @property
    def verified_by(self):
        return self._verified_by

    # Layout: one card, the parts of each step are shown or hidden.
    def _build(self):
        body = _card(self, tr("Forgot PIN") if self._reset else tr("Support code"))
        self._msg = _label("confirmMsg")
        body.addWidget(self._msg)
        self._detail = _label("confirmDetail")
        body.addWidget(self._detail)

        # Step "choose"
        self._choose_box = QFrame()
        cl = QVBoxLayout(self._choose_box)
        cl.setContentsMargins(0, 4, 0, 0)
        cl.setSpacing(8)
        self._admin_btn = _button(tr("Sign in as admin"), "primaryBtn",
                                  lambda: self._go("admin"))
        self._admin_btn.setFixedHeight(_KEY_H + 4)
        cl.addWidget(self._admin_btn)
        cl.addWidget(_label("confirmDetail",
                            tr("A Checkology admin signs in with email and password.")))
        cl.addSpacing(4)
        self._support_btn = _button(tr("Unlock code from support"), "ghostBtn",
                                    lambda: self._go("support"))
        self._support_btn.setFixedHeight(_KEY_H + 4)
        cl.addWidget(self._support_btn)
        self._support_hint = _label("confirmDetail")
        cl.addWidget(self._support_hint)
        body.addWidget(self._choose_box)

        # Step "admin"
        self._admin_box = QFrame()
        al = QVBoxLayout(self._admin_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(8)
        self._email = _text_field(tr("Admin email"), self._next)
        self._password = _text_field(tr("Password"), self._next, password=True)
        al.addWidget(self._email)
        al.addWidget(self._password)
        body.addWidget(self._admin_box)

        # Step "support": device ID + request number to read out
        self._support_box = QFrame()
        self._support_box.setObjectName("statusCard")
        sl = QGridLayout(self._support_box)
        sl.setContentsMargins(14, 10, 14, 10)
        sl.setHorizontalSpacing(12)
        sl.setVerticalSpacing(2)
        sl.addWidget(_label("statLabel", tr("Device ID")), 0, 0)
        sl.addWidget(_label("statLabel", tr("Request number")), 0, 1)
        self._mac_lbl = QLabel("")
        self._mac_lbl.setStyleSheet(f"font-size:14px; font-weight:600; color:{C['text1']};")
        self._mac_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._chal_lbl = QLabel("")
        self._chal_lbl.setStyleSheet(f"font-size:20px; font-weight:700; color:{C['text1']};")
        sl.addWidget(self._mac_lbl, 1, 0)
        sl.addWidget(self._chal_lbl, 1, 1)
        self._expiry_lbl = _label("confirmDetail")
        sl.addWidget(self._expiry_lbl, 2, 0, 1, 2)
        body.addWidget(self._support_box)

        # Digit entry (support code, new PIN, repeat)
        self._field = _digit_field(max(pin_unlock.UNLOCK_DIGITS, maintenance_pin.MAX_LENGTH),
                                   self._next)
        body.addWidget(self._field)
        self._error = _error_label()
        body.addWidget(self._error)
        self._pad = _keypad(self._field)
        body.addWidget(self._pad)

        body.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        self._back = _button(tr("CANCEL"), "confirmCancel", self._back_or_cancel)
        actions.addWidget(self._back)
        self._ok = _button(tr("NEXT"), "confirmOk", self._next)
        actions.addWidget(self._ok)
        body.addLayout(actions)

    def _show_error(self, text):
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def _go(self, step, error=""):
        if step == "support" and self._step != "support":
            if not maintenance_pin.support_ready():
                self._show_error(tr("Support unlock is not set up on this device."))
                return
            maintenance_pin.open_challenge()
        if self._step == "support" and step != "support":
            maintenance_pin.close_challenge()
        self._step = step
        self._field.clear()
        self._password.clear()
        self._show_error(error)

        self._choose_box.setVisible(step == "choose")
        self._admin_box.setVisible(step == "admin")
        self._support_box.setVisible(step == "support")
        digits = step in ("support", "new", "repeat")
        self._field.setVisible(digits)
        self._pad.setVisible(digits)
        self._ok.setVisible(step != "choose")
        self._back.setText(tr("CANCEL") if step in ("choose", "new", "repeat") or not self._reset
                           else tr("BACK"))
        self._timer.stop()

        if step == "choose":
            self._msg.setText(tr("Reset the maintenance PIN"))
            self._detail.setText(tr("A new PIN can only be set after confirming who you are."))
            ready = maintenance_pin.support_ready()
            self._support_btn.setEnabled(ready)
            self._support_hint.setText(
                tr("Call support. They check who you are and give you a one-time code.")
                if ready else tr("Not set up on this device."))
        elif step == "admin":
            self._msg.setText(tr("Admin sign-in"))
            self._detail.setText(tr(
                "Sign in with a Checkology admin account. The admin is then the "
                "operator on shift."))
            self._ok.setText(tr("SIGN IN"))
            self._email.setFocus()
        elif step == "support":
            self._msg.setText(tr("Unlock code from support"))
            self._detail.setText(tr(
                "Call support and read them the device ID and request number. "
                "Enter the {n}-digit code they give you.", n=pin_unlock.UNLOCK_DIGITS))
            self._mac_lbl.setText(maintenance_pin.support_mac())
            self._field.setEchoMode(QLineEdit.EchoMode.Normal)
            self._ok.setText(tr("CHECK"))
            self._timer.start()
            self._tick()
        elif step == "new":
            self._msg.setText(tr("Choose a new PIN"))
            self._detail.setText(tr("Enter a new maintenance PIN of {lo} to {hi} digits.",
                                    lo=maintenance_pin.MIN_LENGTH,
                                    hi=maintenance_pin.MAX_LENGTH))
            self._field.setEchoMode(QLineEdit.EchoMode.Password)
            self._ok.setText(tr("NEXT"))
        elif step == "repeat":
            self._msg.setText(tr("Repeat the new PIN"))
            self._detail.setText(tr("Enter the same PIN again to confirm it."))
            self._field.setEchoMode(QLineEdit.EchoMode.Password)
            self._ok.setText(tr("SAVE"))
        self.adjustSize()

    @Slot()
    def _tick(self):
        left = maintenance_pin.challenge_seconds_left()
        if self._step != "support":
            return
        if left <= 0:
            self._chal_lbl.setText("—")
            self._expiry_lbl.setText(tr("This request has expired."))
            self._ok.setText(tr("NEW NUMBER"))
            self._timer.stop()
            return
        self._chal_lbl.setText(pin_unlock.group(maintenance_pin.current_challenge()))
        self._expiry_lbl.setText(tr("Valid for {m}:{s}", m=left // 60, s=f"{left % 60:02d}"))

    def _set_busy(self, busy, label=""):
        self._busy = busy
        self._back.setEnabled(not busy)
        self._ok.setEnabled(not busy)
        if busy:
            self._ok.setText(label)

    @Slot()
    def _back_or_cancel(self):
        if self._busy:
            return
        if self._step in ("admin", "support") and self._reset:
            self._go("choose")
        else:
            self.reject()

    def reject(self):
        if self._busy:              # a cloud call is running; its thread must finish
            return
        maintenance_pin.close_challenge()
        self._timer.stop()
        self._new_pin = ""
        super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)

    # Step actions
    @Slot()
    def _next(self):
        if self._busy:
            return
        handler = {"admin": self._admin_sign_in, "support": self._check_support,
                   "new": self._take_new, "repeat": self._save}.get(self._step)
        if handler:
            handler()

    def _admin_sign_in(self):
        email, password = self._email.text().strip(), self._password.text()
        if not email or not password:
            self._show_error(tr("Enter the admin email and password."))
            return
        try:
            from modules.camera_auth import get_camera_auth
            from ui_pyside.workers import AuthWorker
            auth = get_camera_auth()
        except Exception:
            log.exception("Admin sign-in unavailable")
            self._show_error(tr("Admin sign-in is not available. Use a code from support."))
            return
        if not auth.cloud_login_available:
            self._show_error(tr("Admin sign-in is not available on this cloud server. "
                                "Use a code from support."))
            return
        self._show_error("")
        self._set_busy(True, tr("SIGNING IN…"))
        self._worker = AuthWorker(auth.operator_login, parent=self,
                                  email=email, password=password)
        self._worker.done.connect(self._on_admin_result)
        self._worker.start()

    @Slot(object)
    def _on_admin_result(self, res):
        self._worker.wait()
        self._set_busy(False)
        self._ok.setText(tr("SIGN IN"))
        self._password.clear()
        if not res.get("ok"):
            maintenance_pin.audit("admin_reset_failed", error=res.get("error") or "")
            self._show_error(res.get("message") or tr("Sign-in failed"))
            return
        operator = res.get("operator") or {}
        # The cloud has switched the shift to whoever signed in: show that.
        if _on_admin_signed_in:
            try:
                _on_admin_signed_in(operator)
            except Exception:
                log.exception("Admin sign-in hook failed")
        who = {"name": operator.get("name", ""), "id": str(operator.get("id", ""))}
        if not _is_admin(operator):
            maintenance_pin.audit("admin_reset_refused", reason="not admin", **who)
            self._show_error(tr("{name} is not an admin. Only an admin can reset the PIN.",
                                name=who["name"] or email_mask(self._email.text())))
            return
        self._verified_by = {"method": "admin", **who}
        maintenance_pin.audit("admin_verified", **who)
        self._go("new")

    def _check_support(self):
        if maintenance_pin.challenge_seconds_left() <= 0:
            maintenance_pin.close_challenge()
            self._step = ""                 # force a fresh request number
            self._go("support")
            return
        ok, reason = maintenance_pin.check_unlock(self._field.text())
        self._field.clear()
        if ok:
            self._timer.stop()
            self._verified_by = {"method": "support"}
            if not self._reset:
                # One-time approval: the PIN stays as it is.
                super().accept()
                return
            self._go("new")
        elif reason == "locked":
            self._show_error(_lock_message())
        elif reason == "expired":
            self._show_error(tr("This request has expired. Tap NEW NUMBER."))
            self._tick()
        elif reason == "not_ready":
            self._show_error(tr("Support unlock is not set up on this device."))
        else:
            self._show_error(_lock_message() or tr("Wrong unlock code."))

    def _take_new(self):
        text = self._field.text()
        if not maintenance_pin.valid_format(text):
            self._go("new", tr("The PIN must be {lo} to {hi} digits.",
                               lo=maintenance_pin.MIN_LENGTH, hi=maintenance_pin.MAX_LENGTH))
            return
        self._new_pin = text
        self._go("repeat")

    def _save(self):
        if not self._verified_by:           # cannot happen through the steps
            self._go("choose")
            return
        if self._field.text() != self._new_pin:
            self._new_pin = ""
            self._go("new", tr("The two PINs are different. Enter the new PIN again."))
            return
        try:
            maintenance_pin.set_pin(self._new_pin)
        except (OSError, ValueError) as e:
            log.error("Maintenance PIN: could not save the new PIN (%s)", type(e).__name__)
            self._go("new", tr("The new PIN could not be saved. Call maintenance."))
            return
        finally:
            self._new_pin = ""
        maintenance_pin.audit("pin_reset", action=self._action, **self._verified_by)
        if self._verified_by["method"] == "admin":
            text = tr("Maintenance PIN reset by admin {name}",
                      name=self._verified_by.get("name") or "?")
        else:
            text = tr("Maintenance PIN reset with a support unlock code")
        _activity("warning", text)
        self.accept()


def email_mask(email: str) -> str:
    """"john.doe@brewery.com" -> "j***@brewery.com" for on-screen messages."""
    email = (email or "").strip()
    if "@" not in email:
        return tr("This account")
    user, domain = email.split("@", 1)
    return f"{user[:1]}***@{domain}"
