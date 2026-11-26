"""
Camera Setup: the one-time screen that sets this device's camera MAC ID.

Replaces editing CAMERA_MAC_ID in config.py for every camera.

    First start on a new camera (no MAC saved) -> main.py shows this screen
    before the main window. Settings -> Device Identity -> CHANGE opens it again.

The MAC is offered from this device's wired Ethernet port as a suggestion only.
Nothing is saved until VERIFY passes: the cloud must accept the MAC and the
camera password (POST /api/camera/auth/login, see modules.camera_auth.
verify_camera). SAVE & RESTART is enabled only for the exact values that were
verified, so a typo or an unregistered MAC can never be saved.

After saving, the app restarts: other modules copy config.CAMERA_MAC_ID when
they are imported, so a clean start is the only way they all see the new value.
"""

import logging

from PySide6.QtCore import Qt, Slot, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QGraphicsDropShadowEffect,
)

import config
from modules.camera_auth import verify_camera, NETWORK
from ui_pyside.theme import C
from ui_pyside.i18n import tr
from ui_pyside.widgets import (field_label, add_password_toggle, IconLabel,
                               caution_note, svg_icon)
from ui_pyside.workers import AuthWorker

log = logging.getLogger("ui_pyside.setup")

BTN_H = 40
CARD_W = 500

_OK_BG = "rgba(34,197,94,0.10)"
_ERR_BG = "rgba(239,68,68,0.08)"
_WARN_BG = "rgba(245,158,11,0.12)"


def _button(text, name, height=BTN_H):
    b = QPushButton(text)
    b.setObjectName(name)
    b.setFixedHeight(height)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class CameraSetupDialog(QDialog):
    """Enter / confirm the camera MAC and password, verify with the cloud, save.

    first_run=True : shown before the app starts (full screen in kiosk mode);
                     the only way out without saving is EXIT.
    first_run=False: opened from Settings over the running app; CANCEL keeps
                     the current MAC.
    exec() returns Accepted only after the MAC (and password) were saved.
    """

    def __init__(self, first_run=True, parent=None):
        super().__init__(parent)
        self._first_run = first_run
        self._verified = None          # (mac, password) the cloud accepted
        self._worker = None
        self._detected_mac, self._detected_iface = config.detect_device_mac()
        self.setModal(True)
        self.setWindowTitle(tr("Camera Setup"))
        if first_run:
            # Own window before the main window exists: themed page background.
            self.setObjectName("splash")
            self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.resize(1280, 800)
        else:
            # Over the running app, like ConfirmDialog.
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._build()
        self._prefill()

    # Layout
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(CARD_W)
        shadow = QGraphicsDropShadowEffect(root)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 120 if self._first_run else 170))
        root.setGraphicsEffect(shadow)
        rl = QVBoxLayout(root)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)

        header = QFrame()
        header.setObjectName("confirmHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        title = QLabel(tr("Camera Setup") if self._first_run else tr("Change Camera MAC"))
        title.setObjectName("confirmTitle")
        hl.addWidget(title)
        hl.addStretch()
        rl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(24, 18, 24, 20)
        body.setSpacing(8)

        intro = QLabel(
            tr("This camera has no MAC ID yet. Enter the MAC ID and camera password "
               "registered for it in Keg Management. They are checked with the cloud "
               "before anything is saved.")
            if self._first_run else
            tr("Enter the MAC ID and camera password registered in Keg Management. "
               "The current MAC stays in use until the new one is verified and saved."))
        intro.setObjectName("confirmDetail")
        intro.setWordWrap(True)
        body.addWidget(intro)
        body.addSpacing(4)

        # MAC
        mac_row = QHBoxLayout()
        mac_row.addWidget(field_label(tr("CAMERA MAC ID")))
        mac_row.addStretch()
        self._use_detected_btn = QPushButton(tr("Use this device's MAC"))
        self._use_detected_btn.setObjectName("headerBtn")
        self._use_detected_btn.setFixedHeight(30)
        self._use_detected_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._use_detected_btn.clicked.connect(self._use_detected)
        self._use_detected_btn.setVisible(bool(self._detected_mac))
        mac_row.addWidget(self._use_detected_btn)
        body.addLayout(mac_row)

        self._mac_edit = QLineEdit()
        self._mac_edit.setPlaceholderText("3C:6D:66:2C:D8:16")
        self._mac_edit.setMaxLength(23)
        self._mac_edit.setFixedHeight(BTN_H)
        self._mac_edit.textChanged.connect(self._on_edited)
        self._mac_edit.editingFinished.connect(self._tidy_mac)
        body.addWidget(self._mac_edit)

        if self._detected_mac:
            hint = tr("This device's Ethernet MAC ({iface}): {mac}",
                      iface=self._detected_iface, mac=self._detected_mac)
        else:
            hint = tr("This device's MAC could not be read. Type the MAC registered "
                      "in Keg Management.")
        body.addWidget(caution_note(hint, "info"))
        body.addSpacing(4)

        # Password
        body.addWidget(field_label(tr("CAMERA PASSWORD")))
        self._pw_edit = QLineEdit()
        self._pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        add_password_toggle(self._pw_edit)
        self._pw_edit.setPlaceholderText(tr("24-character camera password"))
        self._pw_edit.setFixedHeight(BTN_H)
        self._pw_edit.textChanged.connect(self._on_edited)
        self._pw_edit.returnPressed.connect(self._verify)
        body.addWidget(self._pw_edit)
        where = QLabel(tr("Keg Management → Scanner → Cameras → this camera → "
                          "Camera login → Generate password. It is shown once; it is "
                          "stored on this device only."))
        where.setObjectName("confirmDetail")
        where.setStyleSheet("font-size:11px;")
        where.setWordWrap(True)
        body.addWidget(where)
        body.addSpacing(6)

        # Result of the cloud check
        self._status = IconLabel(size=12, icon_size=16, radius=6, pad=(10, 8), wrap=True)
        self._status.setVisible(False)
        body.addWidget(self._status)

        self._verify_btn = _button(tr("VERIFY WITH CLOUD"), "ghostBtn")
        self._verify_btn.clicked.connect(self._verify)
        body.addWidget(self._verify_btn)

        # Actions
        body.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        if self._first_run:
            self._cancel_btn = QPushButton(tr("  Exit"))
            self._cancel_btn.setIcon(svg_icon("power", C['ck_pink_dark'], 16))
            self._cancel_btn.setIconSize(QSize(16, 16))
        else:
            self._cancel_btn = QPushButton(tr("CANCEL"))
        self._cancel_btn.setObjectName("confirmCancel")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn = QPushButton(tr("SAVE && START") if self._first_run
                                     else tr("SAVE && RESTART"))
        self._save_btn.setObjectName("confirmOk")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # The theme has no disabled look for confirmOk; make "locked" obvious.
        self._save_btn.setStyleSheet(
            f"QPushButton#confirmOk:disabled {{ background: {C['blue_border']};"
            f" color: white; }}")
        self._save_btn.setEnabled(False)
        self._save_btn.setToolTip(tr("Verify with the cloud first"))
        self._save_btn.clicked.connect(self._save)
        actions.addWidget(self._cancel_btn)
        actions.addStretch()
        actions.addWidget(self._save_btn)
        body.addLayout(actions)
        rl.addLayout(body)

        outer.addStretch(1)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)

    def _prefill(self):
        if self._first_run:
            self._mac_edit.setText(self._detected_mac)
            (self._pw_edit if self._detected_mac else self._mac_edit).setFocus()
        else:
            self._mac_edit.setText(config.CAMERA_MAC_ID)
            self._mac_edit.setFocus()
            self._mac_edit.selectAll()

    # Helpers
    def _set_status(self, kind, text):
        icon, fg, bg = {"ok":   ("circle-check", C['green_dark'], _OK_BG),
                        "warn": ("circle-alert", C['amber_dark'], _WARN_BG),
                        "err":  ("circle-x",     C['red_dark'],   _ERR_BG),
                        "busy": ("info",         C['slate'],      C['blue_pale'])}[kind]
        self._status.set(icon, text, fg, bg)
        self._status.setVisible(True)

    def _values(self):
        return config.parse_mac(self._mac_edit.text()), self._pw_edit.text().strip()

    @Slot()
    def _use_detected(self):
        self._mac_edit.setText(self._detected_mac)
        self._pw_edit.setFocus()

    @Slot()
    def _tidy_mac(self):
        """Show a typed MAC in the cloud's form (upper case, colons)."""
        mac = config.parse_mac(self._mac_edit.text())
        if mac and mac != self._mac_edit.text():
            self._mac_edit.setText(mac)

    @Slot()
    def _on_edited(self, *_):
        """Any edit after a successful check needs a new check before saving."""
        if self._verified is not None and self._values() != self._verified:
            self._verified = None
            self._save_btn.setEnabled(False)
            self._status.setVisible(False)

    # Cloud check
    @Slot()
    def _verify(self):
        if self._worker is not None:
            return
        mac, password = self._values()
        if not mac:
            self._set_status("err", tr("That is not a valid MAC address "
                                       "(e.g. 3C:6D:66:2C:D8:16)."))
            self._mac_edit.setFocus()
            return
        if not password:
            self._set_status("err", tr("Enter the camera password."))
            self._pw_edit.setFocus()
            return
        self._tidy_mac()
        self._verified = None
        self._save_btn.setEnabled(False)
        self._verify_btn.setEnabled(False)
        self._verify_btn.setText(tr("CHECKING…"))
        self._set_status("busy", tr("Checking {mac} with the cloud…", mac=mac))
        self._worker = AuthWorker(verify_camera, mac, password, parent=self)
        self._worker.done.connect(lambda res, m=mac, p=password: self._on_verified(res, m, p))
        self._worker.finished.connect(self._worker_finished)
        self._worker.start()

    @Slot()
    def _worker_finished(self):
        if self._worker is not None:
            self._worker.deleteLater()
        self._worker = None
        self._verify_btn.setEnabled(True)
        self._verify_btn.setText(tr("VERIFY WITH CLOUD"))

    def _on_verified(self, res, mac, password):
        if self._values() != (mac, password):
            return                      # edited while the check ran: ignore it
        if not res.get("ok"):
            code = res.get("error")
            msg = res.get("message") or code or tr("Unknown error")
            if code == NETWORK:
                msg = tr("Cannot reach the cloud. Connect this device to the network "
                         "and try again — the MAC cannot be saved without the check.")
            self._set_status("err", msg)
            log.warning("Camera setup: %s rejected (%s)", mac, code)
            return

        cam = res.get("camera") or {}
        parts = [cam.get("cameraName"),
                 tr("Line {line}", line=cam["lineNumber"]) if cam.get("lineNumber") else None,
                 tr("Brewery {id}", id=cam["breweryId"]) if cam.get("breweryId") else None]
        who = " · ".join(str(p) for p in parts if p)
        if res.get("password_checked", True):
            text = tr("Verified: {mac} is registered and the password is correct.", mac=mac)
            kind = "ok"
        else:
            text = tr("Verified: {mac} is registered. This cloud server has no camera "
                      "login yet, so the password could not be checked.", mac=mac)
            kind = "warn"
        if who:
            text += "\n" + who
        self._set_status(kind, text)
        self._verified = (mac, password)
        self._save_btn.setEnabled(True)
        self._save_btn.setToolTip("")
        self._save_btn.setFocus()

    # Save
    @Slot()
    def _save(self):
        if self._verified is None or self._values() != self._verified:
            self._save_btn.setEnabled(False)
            return
        mac, password = self._verified
        try:
            config.save_camera_mac(mac)
            config.save_camera_password(password, mac_id=mac)
        except Exception as e:
            log.exception("Camera setup: could not save")
            self._set_status("err", tr("Save failed: {error}", error=e))
            return
        log.info("Camera setup: MAC %s saved", mac)
        self.accept()

    def reject(self):
        if self._worker is not None and self._worker.isRunning():
            # Let the check finish (it is bounded by API_TIMEOUT) so the
            # QThread is never destroyed while running.
            self._worker.wait()
        super().reject()

    def keyPressEvent(self, e):  # noqa: N802 (Qt naming)
        # Enter in a field must not close the dialog; Escape only via the button
        # on the first run (there is nothing to go back to).
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return
        if e.key() == Qt.Key.Key_Escape and self._first_run:
            return
        super().keyPressEvent(e)
