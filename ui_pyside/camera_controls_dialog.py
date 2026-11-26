"""
Camera focus, light and exposure on the operator page: a side panel opened from
the settings icon on the Live Camera Feed header.

The panel is a child of the operator page laid exactly over its right-hand
control column (scanned codes / batch configuration). It is not a separate
window on purpose: the desktop's window manager centres new dialog windows on
their parent, which put the controls on top of the camera preview. As a child
widget it stays where it is placed, so the whole live preview remains visible
and the operator sees each change as it lands.

It holds the same Camera Focus, Camera Light and Camera Exposure cards as
Settings: they are
borrowed from the Settings page while the panel is open and handed back when
it closes. One instance of each card means one worker, one saved state and one
exit switch-off, so the two places can never disagree. The panel also closes
(and hands the cards back) whenever the operator page is hidden, e.g. when the
operator opens Settings.
"""

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .i18n import tr
from .theme import C
from .widgets import svg_icon, _nav_icon

_HEADER_H, _FOOTER_H = 48, 62


def camera_settings_icon(color=None, size=16):
    """The sidebar's Settings icon (sliders), for the header button."""
    icon = QIcon(_nav_icon("settings", color or C["ck_navy"], 40))
    return icon, QSize(size, size)


class CameraControlsPanel(QFrame):
    """Side panel over `anchor` (the operator page's control column)."""

    closed = Signal()

    def __init__(self, settings_page, anchor, parent):
        super().__init__(parent)
        self.setObjectName("confirmRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)
        self._settings = settings_page
        self._anchor = anchor
        self._closed = False

        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # Header: the same navy bar as the app's dialogs, with a close button.
        header = QFrame()
        header.setObjectName("confirmHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 12, 0)
        title = QLabel(tr("Camera Settings"))
        title.setObjectName("confirmTitle")
        hl.addWidget(title)
        hl.addStretch()
        close = QPushButton()
        close.setIcon(svg_icon("x", "#FFFFFF", 16))
        close.setIconSize(QSize(16, 16))
        close.setFixedSize(30, 30)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip(tr("Close"))
        close.setStyleSheet("QPushButton { background: transparent; border: none; border-radius: 6px; }"
                            "QPushButton:hover { background: rgba(255,255,255,0.15); }")
        close.clicked.connect(self.close_panel)
        hl.addWidget(close)
        vl.addWidget(header)

        # Body: the borrowed cards, scrollable if the column is short.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._body = QWidget()
        bl = QVBoxLayout(self._body)
        bl.setContentsMargins(14, 14, 14, 6)
        bl.setSpacing(12)
        for card in settings_page.take_camera_cards():
            bl.addWidget(card)
        bl.addStretch()
        scroll.setWidget(self._body)
        vl.addWidget(scroll, 1)

        # Footer: one action, the standard primary button.
        footer = QHBoxLayout()
        footer.setContentsMargins(14, 8, 14, 14)
        footer.addStretch()
        done = QPushButton(tr("DONE"))
        done.setObjectName("confirmOk")
        done.setMinimumWidth(120)
        done.setCursor(Qt.CursorShape.PointingHandCursor)
        done.clicked.connect(self.close_panel)
        footer.addWidget(done)
        vl.addLayout(footer)

        # Follow the column when the window resizes; close when the page hides.
        anchor.installEventFilter(self)
        parent.installEventFilter(self)
        self._place()
        self.raise_()
        self.show()

    def _place(self):
        """Cover the control column: same left edge and width, top-aligned,
        as tall as the cards need (at most the column's height)."""
        a = self._anchor
        top_left = a.mapTo(self.parentWidget(), QPoint(0, 0))
        need = _HEADER_H + self._body.sizeHint().height() + _FOOTER_H
        self.setGeometry(top_left.x(), top_left.y(), a.width(), min(need, a.height()))

    def eventFilter(self, obj, event):
        if obj is self._anchor and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._place()
        elif obj is self.parentWidget() and event.type() == QEvent.Type.Hide:
            self.close_panel()
        return False

    def close_panel(self):
        """Give the cards back to Settings and remove the panel."""
        if self._closed:
            return
        self._closed = True
        self._anchor.removeEventFilter(self)
        self.parentWidget().removeEventFilter(self)
        self._settings.return_camera_cards()
        self.hide()
        self.closed.emit()
        self.deleteLater()
