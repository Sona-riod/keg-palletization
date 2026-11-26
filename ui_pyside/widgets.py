"""
Reusable PySide6 widgets in the VisionCore visual language.

Adapted from visioncore_dashboard.py so the look matches the approved demo,
but kept generic so the pages can combine them freely.
"""

import re
from functools import lru_cache

from PySide6.QtCore import Qt, Signal, QSize, QRectF, QPointF, QTimer
from PySide6.QtGui import (
    QColor, QFont, QImage, QPixmap, QPainter, QPen, QIcon,
    QIntValidator, QStandardItemModel, QStandardItem,
)
from PySide6.QtWidgets import (
    QWidget, QLabel, QFrame, QPushButton, QHBoxLayout, QVBoxLayout, QComboBox,
    QGraphicsDropShadowEffect, QDialog, QLineEdit, QScrollArea, QCompleter,
    QTableWidget, QHeaderView, QAbstractItemView, QSizePolicy,
)

from .theme import C, now_time, asset_path
from .i18n import tr, N_

LOGO_FILE = "checkology_logo.png"
# Same logo, wordmark recoloured (pink mark unchanged): navy for light
# surfaces, white for the navy brand badge.
LOGO_FILES = {"black": LOGO_FILE,
              "navy":  "checkology_logo_navy.png",
              "white": "checkology_logo_white.png"}


# Simple primitives
def vline(color=None):
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    col = color or C["border"]
    f.setStyleSheet(f"color:{col}; background:{col}; border:none;")
    f.setFixedWidth(1)
    return f


class Card(QFrame):
    """White rounded card with a soft drop shadow."""
    def __init__(self, dark=False, parent=None):
        super().__init__(parent)
        self.setObjectName("darkCard" if dark else "card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(10)
        shadow.setOffset(0, 1)
        shadow.setColor(QColor(15, 23, 42, 18))
        self.setGraphicsEffect(shadow)


class BrandLogo(QFrame):
    """Checkology logo on a plaque so the dark wordmark stays readable.

    Falls back to a styled text wordmark if the image file is missing
    (expected at ui_pyside/assets/checkology_logo.png).

    plaque: "white" - bright white (top bar)
            "tint"  - soft off-white blue-to-pink gradient (splash)
            "badge" - Checkology navy tile, as on the Keg Management site
                      (pair with variant="white")
            "none"  - transparent, for light surfaces such as cards
    variant: wordmark colour, "black" (original), "navy" or "white".
    """
    _PLAQUE_OBJ = {
        "white": "brandPlaque",
        "tint":  "brandPlaqueTint",
        "badge": "brandBadge",
        "none":  "brandPlaquePlain",
    }

    def __init__(self, height=28, plaque="white", variant="black", parent=None):
        super().__init__(parent)
        self.setObjectName(self._PLAQUE_OBJ.get(plaque, "brandPlaque"))
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        pad = 4 if plaque == "none" else max(10, height // 2)
        lay.setContentsMargins(pad, max(2, pad // 2), pad, max(2, pad // 2))
        lay.setSpacing(0)

        pm = QPixmap(asset_path(LOGO_FILES.get(variant, LOGO_FILE)))
        if pm.isNull():
            pm = QPixmap(asset_path(LOGO_FILE))
        if not pm.isNull():
            lbl = QLabel()
            lbl.setPixmap(pm.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation))
            lbl.setStyleSheet("background:transparent;")
        else:
            lbl = QLabel("CHECKOLOGY")
            lbl.setStyleSheet(
                f"color:{'#FFFFFF' if variant == 'white' else C['navy']};"
                f" font-size:{height // 2 + 2}px; font-weight:800;"
                f" letter-spacing:1px; background:transparent;")
        lay.addWidget(lbl)


class SectionHeader(QWidget):
    """Card sub-header: uppercase title on the left, optional widgets on the right.

    The right side holds the card's status (HeaderMeta) and small actions
    (header_button), in the same place on every card.
    """
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("sectionHeader")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(42)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(16, 0, 12, 0)
        self._lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("sectionTitle")
        self._lay.addWidget(t)
        self._lay.addStretch()

    def add_right(self, w):
        self._lay.addWidget(w)


# Shared sizes, so controls placed side by side line up on every page
CONTROL_H = 40        # one standard height: inputs, combos, buttons, toggles
HEADER_BTN_H = 32     # small actions in a card's SectionHeader
CARD_MARGINS = (16, 14, 16, 16)   # body padding inside every card


def field_label(text):
    """Small uppercase caption above a form field."""
    lbl = QLabel(text.upper())
    lbl.setObjectName("fieldLabel")
    return lbl


def header_button(text, tooltip="", icon=None):
    """Compact action for the right side of a SectionHeader.

    Sentence case with an optional SVG icon, matching the Keg Management web app."""
    btn = QPushButton(("  " if icon else "") + text[:1].upper() + text[1:])
    if icon:
        btn.setIcon(svg_icon(icon, C["ck_navy"], 15))
        btn.setIconSize(QSize(15, 15))
    btn.setObjectName("headerBtn")
    btn.setFixedHeight(HEADER_BTN_H)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class HeaderMeta(QLabel):
    """One-line status on the right of a SectionHeader.

    Long text is elided to fit; the full text (or `detail`) goes in the tooltip.
    """
    def __init__(self, text="", max_w=360, parent=None):
        super().__init__(parent)
        self.setObjectName("headerMeta")
        self.setMaximumWidth(max_w)
        self.set_status(text)

    def i18n_refresh(self, swap):
        """Language changed: show the same status in the new language."""
        text, color, detail = self._status
        self.set_status(swap(text) or text, color, swap(detail) or detail)

    def set_status(self, text, color=None, detail=""):
        text = str(text or "")
        self._status = (text, color, detail)
        self.setToolTip(detail or text)
        self.setStyleSheet(f"color:{color};" if color else "")
        self.ensurePolished()
        super().setText(self.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, self.maximumWidth()))

    def setText(self, text):  # noqa: N802 (Qt naming) — plain counts use this
        self.set_status(text)


def make_table(cols):
    """Read-only table that selects whole rows.

    Header text lines up with the cells (both left-aligned, same padding in
    the stylesheet)."""
    table = QTableWidget(0, len(cols))
    table.setHorizontalHeaderLabels(cols)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(44)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setShowGrid(False)
    hh = table.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    hh.setHighlightSections(False)
    # The card draws the frame. Keep this scoped to the table: a bare
    # "border:none" also strips the outline from row buttons inside it.
    table.setStyleSheet("QTableWidget { border: none; }")
    return table


class StatCell(QWidget):
    """Navy footer stat tile: small caps label over a big white number."""
    def __init__(self, label, value="0", parent=None):
        super().__init__(parent)
        self.setObjectName("statTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(2)
        lbl = QLabel(label.upper())
        lbl.setObjectName("statLabel")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._val = QLabel(value)
        self._val.setObjectName("statValue")
        self._val.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(lbl)
        lay.addWidget(self._val)

    def set_value(self, v):
        self._val.setText(str(v))


_KEG_ID_RE = re.compile(r"^(.*?)(\d+)$")


def split_keg_id(code):
    """(prefix, number) of a keg ID for display: CHECKOLOGY-2026-0010 ->
    ("CHECKOLOGY-2026-", "0010"). An ID that does not end in digits is
    returned whole as the number, with no prefix."""
    m = _KEG_ID_RE.match(code or "")
    if not m or not m.group(1):
        return "", code or ""
    return m.group(1), m.group(2)


class QRItem(QWidget):
    """One row in the scanned-QR list.

    Visual modes:
      - normal      - green dot, counted keg, remove (✕) button.
      - blacklisted - red, "FLAGGED" tag (matched the cloud blacklist).
      - reminder    - amber dot and "IN <pallet>" tag, but still a counted keg
                      with its index and remove button. The keg is already in
                      another pallet; the operator is reminded, but the keg is
                      counted and sent, not dropped.
      - moving      - reminder where the operator chose to move the keg. The
                      tag turns blue and reads "MOVE FROM <pallet>". The colour
                      is distinct on purpose: this is the only state that edits
                      an existing pallet's record.
      - unregistered - the cloud does not know this keg yet but it can be
                      registered: fuchsia code (same colour as its preview box),
                      a tick box for registering several at once, and a small
                      "REGISTER" button that opens the form for this one keg.
                      The keg is still counted; nothing else changes.
      - matched     - extra grey "MATCHED" tag: the label was too damaged to
                      read, and the keg was identified from the brewery's serial
                      list. Counted like any keg; the tag is there so the
                      operator can check it by eye.
    """
    remove_requested = Signal(str)
    copy_requested = Signal(str)     # copy this keg ID to the clipboard
    accept_requested = Signal(str)   # operator: include this already-used keg
    reject_requested = Signal(str)   # operator: keep this keg excluded
    register_requested = Signal(str) # operator: register this unknown keg
    select_toggled = Signal(str, bool)  # operator: tick/untick for bulk registration

    def __init__(self, code, index, blacklisted=False, used_pallet=None,
                 reminder_pallet=None, moving=False, unregistered=False,
                 selected=False, matched=False, fresh=False, parent=None):
        super().__init__(parent)
        self.code = code
        self.setObjectName("qrItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(38)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 8, 0)
        lay.setSpacing(8)

        used = used_pallet is not None
        reminder = reminder_pallet is not None and not used
        moving = moving and reminder
        unregistered = unregistered and not (blacklisted or used or reminder)
        dot_color = (C['blue'] if moving
                     else (C['amber'] if (used or reminder)
                           else (C['red'] if blacklisted
                                 else (C['unreg'] if unregistered else C['green']))))
        if unregistered:
            # The tick box takes the dot's place, in the same colour.
            self._selected = bool(selected)
            dot = _icon_button("square-check" if self._selected else "square",
                               C['unreg'], size=24, icon_px=16)
            dot.setToolTip(tr("Select this keg to register it with others"))
            dot.clicked.connect(self._toggle_selected)
            self._tick = dot
        else:
            dot = QLabel()
            dot.setPixmap(svg_pixmap("dot", dot_color, 12))
            dot.setStyleSheet("background:transparent;")
            dot.setFixedWidth(14)
        # The full keg ID: the list is where every keg is checked and acted on.
        # (The stack picture shows only the number, with the common part once.)
        code_lbl = QLabel(code)
        code_lbl.setObjectName("qrCode")
        if unregistered:
            code_lbl.setStyleSheet(f"color:{C['unreg']}; font-size:14px; font-weight:600;")
            code_lbl.setToolTip(tr("This keg is not registered yet"))
        elif moving:
            code_lbl.setStyleSheet(f"color:{C['blue']}; font-size:14px; font-weight:600;")
            code_lbl.setToolTip(tr("Moving onto this pallet — will be removed from "
                                   "pallet {pallet}", pallet=reminder_pallet))
        elif used or reminder:
            pal = used_pallet or reminder_pallet
            code_lbl.setStyleSheet(f"color:{C['amber_dark']}; font-size:14px; font-weight:600;")
            code_lbl.setToolTip(tr("Already in pallet {pallet} — counted & sent (reminder)", pallet=pal))
        elif blacklisted:
            code_lbl.setStyleSheet(f"color:{C['red']}; font-size:14px; font-weight:600;")
            code_lbl.setToolTip(tr("Flagged by cloud blacklist"))
        # Never cut: the tags and the gap give way first.
        code_lbl.setMinimumWidth(code_lbl.sizeHint().width())
        lay.addWidget(dot)
        lay.addWidget(code_lbl)
        if fresh:
            # The keg just read: a soft green tint for a moment, nothing more
            # (the operator page sets `fresh`).
            self.setStyleSheet(
                f"#qrItem {{ background-color: rgba(34,197,94,0.12);"
                f" border-bottom: 1px solid {C['border']}; }}")
        # Next to the code, well away from the remove (x) button.
        cp = _icon_button("copy", C['slate'], size=28, icon_px=14)
        cp.setToolTip(tr("Copy this keg ID"))
        cp.clicked.connect(lambda: self.copy_requested.emit(self.code))
        lay.addWidget(cp)
        lay.addStretch()

        if used:
            tag = QLabel(tr("IN {pallet}", pallet=used_pallet))
            tag.setStyleSheet(
                f"color:{C['amber_dark']}; background:rgba(245,158,11,0.14); border-radius:4px;"
                f" padding:2px 6px; font-size:9px; font-weight:700; letter-spacing:0.3px;")
            tag.setToolTip(tr("This keg is already in pallet {pallet}", pallet=used_pallet))
            lay.addWidget(tag)
            acc = _icon_button("check", C['green_dark'])
            acc.setToolTip(tr("Accept — include this keg in the current pallet"))
            acc.clicked.connect(lambda: self.accept_requested.emit(self.code))
            rej = _icon_button("x", C['red'])
            rej.setToolTip(tr("Reject — keep this keg out of the pallet"))
            rej.clicked.connect(lambda: self.reject_requested.emit(self.code))
            lay.addWidget(acc)
            lay.addWidget(rej)
            return

        # Reminder mode: show the "IN <pallet>" tag, then fall through to the
        # normal index and remove controls so the keg reads as counted.
        if reminder:
            if moving:
                tag = QLabel(tr("MOVE FROM {pallet}", pallet=reminder_pallet))
                tag.setStyleSheet(
                    f"color:{C['blue']}; background:rgba(37,99,235,0.12); border-radius:4px;"
                    f" padding:2px 6px; font-size:9px; font-weight:700; letter-spacing:0.3px;")
                tag.setToolTip(tr("Will be added to this pallet and removed from "
                                  "pallet {pallet}", pallet=reminder_pallet))
            else:
                tag = QLabel(tr("IN {pallet}", pallet=reminder_pallet))
                tag.setStyleSheet(
                    f"color:{C['amber_dark']}; background:rgba(245,158,11,0.14); border-radius:4px;"
                    f" padding:2px 6px; font-size:9px; font-weight:700; letter-spacing:0.3px;")
                tag.setToolTip(tr("Already in pallet {pallet} — counted & sent anyway", pallet=reminder_pallet))
            lay.addWidget(tag)

        if matched:
            mt = QLabel(tr("MATCHED"))
            mt.setStyleSheet(
                f"color:{C['slate']}; background:rgba(100,116,139,0.14); border-radius:4px;"
                f" padding:2px 6px; font-size:9px; font-weight:700; letter-spacing:0.3px;")
            mt.setToolTip(tr("Label too damaged to read. Identified from the serial list; "
                             "check it by eye."))
            lay.addWidget(mt)
        if blacklisted:
            flag = QLabel(tr("FLAGGED"))
            flag.setStyleSheet(
                f"color:{C['red']}; background:rgba(239,68,68,0.12); border-radius:4px;"
                f" padding:2px 6px; font-size:9px; font-weight:700; letter-spacing:0.5px;")
            lay.addWidget(flag)
        if unregistered:
            reg = chip_button(tr("REGISTER"), "circle-plus", C['blue'])
            reg.setToolTip(tr("This keg is not registered yet. Register it now."))
            reg.clicked.connect(lambda: self.register_requested.emit(self.code))
            lay.addWidget(reg)
        idx = QLabel(f"#{index}")
        idx.setObjectName("qrIndex")
        idx.setFixedWidth(34)
        idx.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rm = _icon_button("x", C['slate'])
        rm.setToolTip(tr("Remove this keg from the list"))
        rm.clicked.connect(lambda: self.remove_requested.emit(self.code))
        lay.addWidget(idx)
        lay.addWidget(rm)

    def _toggle_selected(self):
        self._selected = not self._selected
        self._tick.setIcon(svg_icon("square-check" if self._selected else "square",
                                    C['unreg'], 16))
        self.select_toggled.emit(self.code, self._selected)


# Sidebar nav icons (monochrome line glyphs drawn in code, no assets)
def _nav_icon(name, color, px=40):
    """Return a crisp line-icon QPixmap for the sidebar, tinted to `color`."""
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if name == "operator":            # camera
        p.drawRoundedRect(QRectF(9, 15, 22, 15), 3, 3)
        p.drawRoundedRect(QRectF(13, 11, 8, 5), 1.5, 1.5)
        p.drawEllipse(QPointF(20, 22.5), 4.2, 4.2)
    elif name == "records":           # list rows with bullets
        p.setBrush(QColor(color))
        for y in (13.0, 20.0, 27.0):
            p.drawEllipse(QPointF(11.0, y), 1.6, 1.6)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(16.0, y), QPointF(31.0, y))
            p.setBrush(QColor(color))
    elif name == "settings":          # sliders
        for y, hx in ((13.0, 16.0), (20.0, 25.0), (27.0, 14.0)):
            p.drawLine(QPointF(10.0, y), QPointF(30.0, y))
            p.setBrush(QColor(color))
            p.drawEllipse(QPointF(hx, y), 2.6, 2.6)
            p.setBrush(Qt.BrushStyle.NoBrush)
    p.end()
    return pm


# SVG line icons (Lucide style, 24x24 grid, stroke only)
_SVG_ICONS = {
    "user": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="10" r="3"/>'
            '<path d="M7 20.662V19a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v1.662"/>',
    "log-out": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
               '<polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
    "log-in": '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>'
              '<polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/>'
            '<path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/>'
            '<path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "eye": '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0'
           ' 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696'
               ' 10.747 10.747 0 0 1-1.444 2.49"/><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/>'
               '<path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696'
               ' 10.75 10.75 0 0 1 4.446-5.143"/><path d="m2 2 20 20"/>',
    "dot": '<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>',
    "wifi": '<path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/>'
            '<path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/>',
    "wifi-off": '<path d="M12 20h.01"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/>'
                '<path d="M5 12.859a10 10 0 0 1 5.17-2.69"/>'
                '<path d="M19 12.859a10 10 0 0 0-2.007-1.523"/>'
                '<path d="M2 8.82a15 15 0 0 1 4.177-2.643"/>'
                '<path d="M22 8.82a15 15 0 0 0-11.288-3.764"/><path d="m2 2 20 20"/>',
    "circle-check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "circle-x": '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    "circle-alert": '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/>'
                    '<line x1="12" x2="12.01" y1="16" y2="16"/>',
    "triangle-alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16'
                      'a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "delete": '<path d="M10 5a2 2 0 0 0-1.344.519l-6.328 5.74a1 1 0 0 0 0 1.481l6.328'
              ' 5.741A2 2 0 0 0 10 19h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z"/>'
              '<path d="m12 9 6 6"/><path d="m18 9-6 6"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "power": '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "circle-help": '<circle cx="12" cy="12" r="10"/>'
                   '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
             '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "rotate-ccw": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>'
                  '<path d="M3 3v5h5"/>',
    "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    "printer": '<path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5'
               'a2 2 0 0 1-2 2h-2"/><path d="M6 9V3a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v6"/>'
               '<rect x="6" y="14" width="12" height="8" rx="1"/>',
    "package": '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73'
               'l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/>'
               '<path d="M12 22V12"/><polyline points="3.29 7 12 12 20.71 7"/>'
               '<path d="m7.5 4.27 9 5.15"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/>'
           '<path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/>'
           '<path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/>'
           '<path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>'
            '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "globe": '<circle cx="12" cy="12" r="10"/>'
             '<path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "circle-plus": '<circle cx="12" cy="12" r="10"/><path d="M8 12h8"/><path d="M12 8v8"/>',
    "square": '<rect width="18" height="18" x="3" y="3" rx="2"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9'
              'a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    "sliders": '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/>'
               '<line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/>'
               '<line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/>'
               '<line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/>'
               '<line x1="16" x2="16" y1="18" y2="22"/>',
    "square-check": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="m9 12 2 2 4-4"/>',
}


@lru_cache(maxsize=256)
def svg_pixmap(name, color, size):
    """Render one of the SVG line icons as a `size`-px pixmap in `color`.

    Cached because list rows re-create the same few icons all the time."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="2" stroke-linecap="round" '
           f'stroke-linejoin="round">{_SVG_ICONS[name]}</svg>').replace("currentColor", color)
    scale = 2                                  # render at 2× for HiDPI panels
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    pm.setDevicePixelRatio(scale)
    return pm


def svg_icon(name, color, size=18):
    return QIcon(svg_pixmap(name, color, size))


class ActivityList(QWidget):
    """"Recent activity" list for operators.

    Newest first, one plain sentence per event with a time and an icon. Only
    the last `max_items` are kept so it stays bounded; the full technical log
    is in logs/app.log."""

    KINDS = {
        "success": ("circle-check",   "green_dark"),
        "info":    ("info",           "ck_navy"),
        "warning": ("triangle-alert", "amber_dark"),
        "error":   ("circle-x",       "red"),
    }

    def __init__(self, max_items=30, parent=None):
        super().__init__(parent)
        self._max = max_items
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._empty = QLabel(tr("Nothing yet — events of this shift will appear here."))
        self._empty.setObjectName("emptyLabel")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setMinimumHeight(60)
        outer.addWidget(self._empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._rows = QVBoxLayout(inner)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(0)
        self._rows.addStretch()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

    def add(self, kind, text):
        from .theme import now_time
        icon, color_key = self.KINDS.get(kind, self.KINDS["info"])
        row = QWidget()
        row.setObjectName("activityRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hl = QHBoxLayout(row)
        hl.setContentsMargins(4, 8, 4, 8)
        hl.setSpacing(10)
        t = QLabel(now_time()[:5])                 # HH:MM
        t.setObjectName("activityTime")
        t.setFixedWidth(42)
        ic = QLabel()
        ic.setPixmap(svg_pixmap(icon, C[color_key], 16))
        ic.setFixedSize(16, 16)
        msg = QLabel(text)
        msg.setObjectName("activityText")
        msg.setWordWrap(True)
        hl.addWidget(t, 0, Qt.AlignmentFlag.AlignTop)
        hl.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
        hl.addWidget(msg, 1)
        self._rows.insertWidget(0, row)             # newest on top
        while self._rows.count() - 1 > self._max:   # last item is the stretch
            old = self._rows.takeAt(self._rows.count() - 2).widget()
            if old is not None:
                old.deleteLater()
        self._empty.setVisible(False)


def _icon_button(icon, color, size=32, icon_px=16):
    """Small flat button showing only an SVG icon (row actions)."""
    b = QPushButton()
    b.setIcon(svg_icon(icon, color, icon_px))
    b.setIconSize(QSize(icon_px, icon_px))
    b.setFixedSize(size, size)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet("QPushButton { background:transparent; border:none; padding:0; border-radius:4px; }"
                    f"QPushButton:hover {{ background:{C['blue_pale']}; }}")
    return b


def chip_button(text, icon, color):
    """Small tinted pill with an SVG icon and short text (row and list-bar actions)."""
    q = QColor(color)
    tint = f"rgba({q.red()},{q.green()},{q.blue()},"
    b = QPushButton(" " + text)
    b.setIcon(svg_icon(icon, color, 12))
    b.setIconSize(QSize(12, 12))
    b.setFixedHeight(24)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setStyleSheet(
        f"QPushButton {{ color:{color}; background:{tint}0.10);"
        f" border:none; border-radius:4px; padding:2px 8px; font-size:9px;"
        f" font-weight:700; letter-spacing:0.3px; }}"
        f"QPushButton:hover {{ background:{tint}0.20); }}"
        f"QPushButton:disabled {{ color:{C['text3']}; background:transparent; }}")
    return b


class FocusCurve(QWidget):
    """Target sharpness against lens position, filled in while the focus search runs.

    One dot per measured position (score 0-1, see modules/focus.py), an amber
    line where the lens is now, a slate line where it started, and a green
    line at the position chosen."""
    def __init__(self, lo, hi, parent=None):
        super().__init__(parent)
        self._lo, self._hi = lo, max(hi, lo + 1)
        self._points, self._current, self._start, self._best = [], None, None, None
        self._note = ""
        self.setMinimumHeight(110)

    def set_data(self, points, current=None, start=None, best=None):
        self._points = list(points)
        self._current, self._start, self._best = current, start, best
        self._note = ""
        self.update()

    def set_note(self, text):
        """No curve: show `text` instead."""
        self._points, self._current, self._start, self._best = [], None, None, None
        self._note = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["bg"]))
        p.drawRoundedRect(frame, 6, 6)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        if not self._points:
            p.setPen(QColor(C["text3"]))
            p.drawText(frame.adjusted(10, 0, -10, 0),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._note)
            return
        plot = frame.adjusted(12, 18, -12, -18)

        def x(pos):
            return plot.left() + (pos - self._lo) / (self._hi - self._lo) * plot.width()

        def marker(pos, color, label, other=None, dashed=False):
            """Vertical line at `pos`; its label on the side away from `other`
            (the other labelled line), so the two never overlap."""
            if pos is None:
                return
            pen = QPen(QColor(color), 1.5)
            if dashed:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(x(pos), plot.top()), QPointF(x(pos), plot.bottom()))
            if not label:
                return
            left = (other is not None and other > pos) or (other is None and pos > self._hi / 2)
            width = p.fontMetrics().horizontalAdvance(label) + 2
            # Flip to the other side rather than run off the chart.
            if left and x(pos) - 4 - width < frame.left():
                left = False
            elif not left and x(pos) + 4 + width > frame.right():
                left = True
            box = (QRectF(x(pos) - 4 - width, frame.top() + 2, width, 14) if left else
                   QRectF(x(pos) + 4, frame.top() + 2, width, 14))
            p.drawText(box, (Qt.AlignmentFlag.AlignRight if left else Qt.AlignmentFlag.AlignLeft)
                       | Qt.AlignmentFlag.AlignVCenter, label)

        other = self._best if self._best is not None else self._current
        marker(self._start, C["slate"], tr("start {pos}", pos=self._start),
               other=other, dashed=True)
        if self._best is None:
            marker(self._current, C["amber"], "")
        marker(self._best, C["green_dark"], tr("focus {pos}", pos=self._best),
               other=self._start)

        pts = [QPointF(x(pos), plot.bottom() - s * plot.height()) for pos, s in self._points]
        p.setPen(QPen(QColor(C["blue"]), 2))
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        p.setBrush(QColor(C["blue"]))
        p.setPen(Qt.PenStyle.NoPen)
        for pt in pts:
            p.drawEllipse(pt, 2.5, 2.5)

        p.setPen(QColor(C["text3"]))
        p.drawText(QRectF(plot.left() - 10, plot.bottom() + 2, 60, 14),
                   Qt.AlignmentFlag.AlignLeft, str(self._lo))
        p.drawText(QRectF(plot.right() - 50, plot.bottom() + 2, 60, 14),
                   Qt.AlignmentFlag.AlignRight, str(self._hi))
        p.drawText(QRectF(plot.left() + 40, plot.bottom() + 2, plot.width() - 80, 14),
                   Qt.AlignmentFlag.AlignCenter, tr("lens position"))


class FocusRangeChart(QWidget):
    """Side view of the focus range per layer height.

    The camera at the top and the layers below it, highest layer (closest to
    the camera) first. Each row is that layer's sharp band of lens positions in
    green, with a tick at its sharpest; the amber line is where the lens is;
    a light green column marks the positions sharp for every layer, when there
    are any. On the right, how sharp each layer is at the lens position now.
    """
    _ROW_H, _LEFT, _RIGHT = 30, 86, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows, self._current, self._common, self._sharp = [], None, None, 0.5
        self._note = ""
        self.setMinimumHeight(110)

    def set_data(self, rows, current=None, common=None, sharp=0.5):
        """rows: [(layer, band, score at the lens now or None)], highest first.
        band is focus_band()'s dict; common is (lo, hi) or None."""
        self._rows, self._current, self._common = list(rows), current, common
        self._sharp = sharp
        self.setMinimumHeight(66 + max(1, len(self._rows)) * self._ROW_H)
        self.update()

    def set_note(self, text):
        self._note = text
        self.update()

    def _view(self):
        """The lens positions shown: the bands and the lens, plus a margin. Sharp
        bands are only tens of steps wide, so the full 0-1600 would hide them."""
        ends = [v for _, b, _ in self._rows for v in (b["lo"], b["hi"])]
        if self._current is not None:
            ends.append(self._current)
        lo, hi = min(ends), max(ends)
        pad = max(20, (hi - lo) * 0.15)
        return lo - pad, hi + pad

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["bg"]))
        p.drawRoundedRect(frame, 6, 6)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        if not self._rows:
            p.setPen(QColor(C["text3"]))
            p.drawText(frame.adjusted(10, 0, -10, 0),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._note)
            return

        top = frame.top() + 26
        track_l, track_r = frame.left() + self._LEFT, frame.right() - self._RIGHT
        bottom = top + len(self._rows) * self._ROW_H
        lo, hi = self._view()

        def x(pos):
            return track_l + (pos - lo) / (hi - lo) * (track_r - track_l)

        # The camera, looking down on the stack.
        cam = QRectF(frame.left() + 12, frame.top() + 6, 22, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["slate"]))
        p.drawRoundedRect(cam, 2, 2)
        p.drawRect(QRectF(cam.center().x() - 4, cam.bottom(), 8, 4))
        p.setPen(QColor(C["text2"]))
        p.drawText(QRectF(cam.right() + 6, frame.top() + 4, 120, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tr("camera"))
        pen = QPen(QColor(C["border"]), 1, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(cam.center().x(), cam.bottom() + 4), QPointF(cam.center().x(), bottom))

        # Lens positions sharp for every layer.
        if self._common:
            p.setPen(Qt.PenStyle.NoPen)
            col = QColor(C["green"])
            col.setAlpha(45)
            p.setBrush(col)
            p.drawRect(QRectF(x(self._common[0]), top, max(2.0, x(self._common[1]) - x(self._common[0])),
                              bottom - top))

        green = QColor(C["green_dark"])
        for i, (layer, band, score) in enumerate(self._rows):
            mid = top + i * self._ROW_H + self._ROW_H / 2
            p.setPen(QColor(C["text1"]))
            p.drawText(QRectF(cam.center().x() + 8, mid - 8, self._LEFT - 30, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       tr("Layer {n}", n=layer))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(C["border"]))
            p.drawRoundedRect(QRectF(track_l, mid - 2, track_r - track_l, 4), 2, 2)
            band_rect = QRectF(x(band["lo"]), mid - 6, max(3.0, x(band["hi"]) - x(band["lo"])), 12)
            fill = QColor(green)
            fill.setAlpha(170)
            p.setBrush(fill)
            p.drawRoundedRect(band_rect, 3, 3)
            # An open edge: the band reaches at least that far.
            p.setPen(QPen(green, 1.5))
            for open_, edge, d in ((band.get("lo_open"), band_rect.left(), -6),
                                   (band.get("hi_open"), band_rect.right(), 6)):
                if open_:
                    p.drawLine(QPointF(edge, mid), QPointF(edge + d, mid))
            p.setPen(QPen(QColor(C["text1"]), 2))
            p.drawLine(QPointF(x(band["peak"]), mid - 7), QPointF(x(band["peak"]), mid + 7))

            if self._current is None:
                word, color = "—", C["text3"]          # lens position not known yet
            elif score is None:
                word, color = tr("not measured here"), C["text3"]
            elif score >= self._sharp:
                word, color = tr("{pct}% sharp", pct=round(score * 100)), C["green_dark"]
            elif score >= self._sharp / 2:
                word, color = tr("{pct}% soft", pct=round(score * 100)), C["amber_dark"]
            else:
                word, color = tr("{pct}% blurred", pct=round(score * 100)), C["red"]
            p.setPen(QColor(color))
            p.drawText(QRectF(track_r + 8, mid - 8, self._RIGHT - 12, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, word)

        if self._current is not None:
            p.setPen(QPen(QColor(C["amber"]), 2))
            p.drawLine(QPointF(x(self._current), top - 4), QPointF(x(self._current), bottom + 2))
            label = tr("lens now {pos}", pos=self._current)
            width = p.fontMetrics().horizontalAdvance(label) + 4
            left = min(max(track_l, x(self._current) - width / 2), track_r - width)
            p.drawText(QRectF(left, frame.top() + 6, width, 14),
                       Qt.AlignmentFlag.AlignCenter, label)

        p.setPen(QColor(C["text3"]))
        p.drawText(QRectF(track_l, bottom + 4, 60, 14), Qt.AlignmentFlag.AlignLeft, str(round(lo)))
        p.drawText(QRectF(track_r - 60, bottom + 4, 60, 14), Qt.AlignmentFlag.AlignRight,
                   str(round(hi)))
        p.drawText(QRectF(track_l + 50, bottom + 4, track_r - track_l - 100, 14),
                   Qt.AlignmentFlag.AlignCenter,
                   tr("lens position · green = sharp · shaded = sharp for all layers")
                   if self._common else tr("lens position · green = sharp"))


class FocusZonePanel(QWidget):
    """Side view of the stack laid over the live preview: the camera at its
    height, the band of heights in focus, and each layer green (sharp), red
    (blurred) or grey (too close to fit the view). Fed by focus_zone()."""
    _BG = QColor(15, 23, 42, 225)
    _STATUS = {"sharp": QColor(34, 197, 94), "blurred": QColor(239, 68, 68),
               "too close": QColor(148, 163, 184)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zone = None
        self.setFixedSize(250, 236)

    def set_zone(self, zone):
        self._zone = zone
        self.update()

    def paintEvent(self, _):
        if not self._zone:
            return
        z = self._zone
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._BG)
        p.drawRoundedRect(frame, 8, 8)
        font = p.font()
        font.setPixelSize(10)
        p.setFont(font)
        white, dim = QColor(255, 255, 255), QColor(148, 163, 184)

        p.setPen(white)
        bold = QFont(font)
        bold.setBold(True)
        p.setFont(bold)
        p.drawText(QRectF(10, 6, frame.width() - 20, 14), Qt.AlignmentFlag.AlignLeft,
                   tr("Focus zone"))
        p.setFont(font)

        # Side view: floor at the bottom, camera at the top, to scale.
        top_y, floor_y = 40.0, frame.height() - 46
        cam_h = z["camera_h"]

        def y(h):
            return floor_y - h / cam_h * (floor_y - top_y)

        axis_x, box_l, box_r = 58.0, 76.0, 160.0
        low, high = z["zone"]
        band = QColor(34, 197, 94, 55)
        p.setBrush(band)
        p.drawRect(QRectF(box_l - 8, y(min(high, cam_h)), box_r - box_l + 16,
                          y(low) - y(min(high, cam_h))))
        p.setPen(QPen(QColor(34, 197, 94), 1, Qt.PenStyle.DashLine))
        for h in (low, min(high, cam_h)):
            p.drawLine(QPointF(box_l - 8, y(h)), QPointF(box_r + 8, y(h)))

        # Camera and its height.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(white)
        cam = QRectF(axis_x - 10, top_y - 16, 20, 10)
        p.drawRoundedRect(cam, 2, 2)
        p.drawRect(QRectF(axis_x - 3, cam.bottom(), 6, 3))
        p.setPen(dim)
        p.drawText(QRectF(4, top_y - 18, axis_x - 18, 14), Qt.AlignmentFlag.AlignRight,
                   f"{cam_h:.2f} m")
        p.setPen(QPen(dim, 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(axis_x, top_y - 3), QPointF(axis_x, floor_y))

        # Layers, bottom up.
        for n, top, status in z["layers"]:
            color = self._STATUS[status]
            r = QRectF(box_l, y(top), box_r - box_l, y(top - z["layer_h"]) - y(top))
            fill = QColor(color)
            fill.setAlpha(90)
            p.setBrush(fill)
            p.setPen(QPen(color, 1.5))
            p.drawRect(r.adjusted(0, 1, 0, -1))
            p.setPen(white)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, tr("Layer {n}", n=n))
            p.setPen(dim)
            p.drawText(QRectF(4, y(top) - 7, axis_x - 10, 14), Qt.AlignmentFlag.AlignRight,
                       f"{top:.2f} m")
            p.setPen(color)
            word = {"sharp": tr("sharp"), "blurred": tr("blurred"),
                    "too close": tr("too close")}[status]
            p.drawText(QRectF(box_r + 12, r.center().y() - 7, frame.width() - box_r - 16, 14),
                       Qt.AlignmentFlag.AlignLeft, word)
        p.setPen(QPen(dim, 1))
        p.drawLine(QPointF(box_l - 14, floor_y), QPointF(box_r + 14, floor_y))
        p.setPen(dim)
        p.drawText(QRectF(4, floor_y - 7, axis_x - 10, 14), Qt.AlignmentFlag.AlignRight,
                   tr("floor"))

        # In words.
        p.setPen(QColor(34, 197, 94) if z["in_focus"] else QColor(239, 68, 68))
        p.drawText(QRectF(10, floor_y + 6, frame.width() - 20, 14), Qt.AlignmentFlag.AlignLeft,
                   tr("Sharp from {low:.2f} m to {high:.2f} m · up to {n} layer(s)",
                      low=low, high=min(high, cam_h), n=z["in_focus"]))
        p.setPen(dim)
        note = tr("Focus set on layer {n} ({h:.2f} m)", n=z["focus_layer"], h=z["focus_h"])
        if z["estimated"]:
            note += " · " + tr("lens values estimated")
        p.drawText(QRectF(10, floor_y + 22, frame.width() - 20, 14), Qt.AlignmentFlag.AlignLeft,
                   note)


class StackMap(QWidget):
    """The stack as a tower, beside one of its layers seen from above.

    Left, the tower: one block per layer, layer 1 at the bottom on its pallet
    like the real stack, the layers still to come dashed above. Every block
    says its state with an icon and a word as well as a colour, and its kegs
    read of expected:

      green  tick    sent, in Keg Management      blue   NOW   being scanned
      slate  box     kept on the camera           amber  clock sending
      red    cross   refused                      grey   dashed, still to come

    Right, the layer picked in the tower (the one being scanned, else the top
    one) drawn large from above: every place on its pallet numbered; a keg
    read there shows its keg number, an empty place is a dashed ring with its
    number. Tap a tower block to look at that layer (it goes back by itself
    after a few seconds); tap a keg to remove it (set_editable).

    Takes plain dicts (keg_layout.stack_map_data), so the live stack and one
    from Stack History are drawn the same way. Grows taller for a tall stack
    (put it in a scroll area) instead of squeezing its layers.
    """
    _GREEN, _BLUE = QColor("#16A34A"), QColor("#2563EB")
    _AMBER, _RED, _GREY = QColor("#D97706"), QColor("#DC2626"), QColor("#94A3B8")
    _SLATE = QColor("#475569")
    _WOOD, _WOOD_DARK = QColor("#C99A6B"), QColor("#8B5E3C")
    _STATE_COLOR = {"done": _GREEN, "building": _BLUE, "queued": _AMBER,
                    "refused": _RED, "planned": _GREY, "held": _SLATE}
    _STATE_WORD = {"done": N_("SENT"), "building": N_("NOW"),
                   "queued": N_("SENDING"), "refused": N_("REFUSED"),
                   "held": N_("KEPT"), "planned": N_("TO COME")}
    _STATE_LONG = {"done": N_("Sent to Keg Management"), "building": N_("Scanning now"),
                   "queued": N_("Sending…"), "refused": N_("Refused by Keg Management"),
                   "held": N_("Kept on the camera"), "planned": N_("Still to come")}
    _STATE_ICON = {"done": "circle-check", "refused": "circle-x", "queued": "clock",
                   "held": "package"}
    _TITLE_H = 24         # "STACK  1 of 3 layers" above the tower
    _BLOCK_MIN, _BLOCK_MAX = 36, 72
    _GAP = 6
    _BASE_H = 10          # the pallet the tower stands on
    _HEAD_H = 48          # layer name, count and the common keg ID part
    _FOOT_H = 16          # a note under it, only when needed
    _PICK_MS = 10000      # a picked layer is shown this long, then the current again
    keg_clicked = Signal(int, str)     # layer, code: a keg was tapped

    def __init__(self, parent=None, show_figures=True, full_codes=False):
        super().__init__(parent)
        self._data = None
        self._full = full_codes
        self._cells = []               # (rect, layer, code) of the kegs drawn large
        self._blocks = []              # (rect, layer) of the tower blocks
        self._editable = False
        self._picked = None            # layer tapped in the tower, or None
        self._pick_timer = QTimer(self)
        self._pick_timer.setSingleShot(True)
        self._pick_timer.timeout.connect(self._unpick)
        self.setMinimumHeight(220)

    def set_editable(self, on):
        """Tapping a keg emits keg_clicked (the operator page removes it)."""
        self._editable = bool(on)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _unpick(self):
        self._picked = None
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            for rect, layer in self._blocks:
                if rect.contains(pos):
                    # Tap again (or on the current layer) to go back at once.
                    self._picked = None if layer == self._picked else layer
                    if self._picked is not None:
                        self._pick_timer.start(self._PICK_MS)
                    self.update()
                    return
            if self._editable:
                for rect, layer, code in self._cells:
                    if rect.contains(pos):
                        self.keg_clicked.emit(int(layer), str(code))
                        return
        super().mousePressEvent(event)

    # Data and size
    def _rows(self):
        """The rows to draw, top layer first: planned ones above the real ones."""
        d = self._data or {}
        layers = d.get("layers") or []
        target = max(int(d.get("target") or 1), len(layers))
        planned = [{"layer": n, "state": "planned", "grid": None}
                   for n in range(target, len(layers), -1)]
        return planned + list(reversed(layers)), target, layers

    def _height_for(self):
        rows, _, _ = self._rows()
        h = self._TITLE_H + len(rows) * (self._BLOCK_MIN + self._GAP) + self._BASE_H + 8
        return int(max(220, h))

    def set_stack(self, data):
        """data: {"target", "layers": [{"layer", "state", "grid", "loose",
        "count", "expected", "pallet"}], bottom first} or None."""
        self._data = data
        if self._picked is not None and not any(
                l["layer"] == self._picked for l in (data or {}).get("layers") or []):
            self._picked = None
        self.setMinimumHeight(self._height_for())
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        return QSize(460, self._height_for())

    def _shown_layer(self, layers):
        """The layer drawn large: the one tapped, else the one being scanned,
        else the top one."""
        if self._picked is not None:
            for l in layers:
                if l["layer"] == self._picked:
                    return l
        building = next((l for l in layers if l["state"] == "building"), None)
        return building or (layers[-1] if layers else None)

    # Drawing helpers
    @staticmethod
    def _elide(p, text, width, mode=Qt.TextElideMode.ElideLeft):
        return p.fontMetrics().elidedText(text, mode, int(max(0, width)))

    @staticmethod
    def _font(base, px, bold=False):
        f = QFont(base)
        f.setPixelSize(int(px))
        f.setBold(bold)
        return f

    def _tower(self, p, area, rows, layers, target, shown_no):
        base = p.font()
        done = sum(1 for l in layers if l["state"] in ("done", "queued", "held"))
        p.setFont(self._font(base, 11, True))
        p.setPen(QColor(C["text2"]))
        p.drawText(QRectF(area.left(), area.top(), area.width(), 18),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, tr("LAYERS"))
        p.setPen(self._GREEN if done >= target else QColor(C["text1"]))
        p.drawText(QRectF(area.left(), area.top(), area.width(), 18),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   tr("{d} of {t} done", d=done, t=target))

        avail = area.height() - self._TITLE_H - self._BASE_H - 4
        n = max(1, len(rows))
        bh = max(self._BLOCK_MIN, min(self._BLOCK_MAX, (avail - self._GAP * (n - 1)) / n))
        # From the top, level with the layer picture beside it.
        y = area.top() + self._TITLE_H
        for l in rows:
            rect = QRectF(area.left(), y, area.width(), bh)
            y += bh + self._GAP
            self._blocks.append((rect, l["layer"]))
            self._block(p, rect, l, l["layer"] == shown_no)
        # The pallet under the bottom layer.
        deck = QRectF(area.left() - 3, y - self._GAP + 3, area.width() + 6, self._BASE_H)
        p.setPen(QPen(self._WOOD_DARK, 1))
        p.setBrush(self._WOOD)
        p.drawRoundedRect(deck, 3, 3)
        p.setFont(base)

    def _block(self, p, rect, l, shown):
        base = p.font()
        state = l["state"]
        color = self._STATE_COLOR[state]
        planned = state == "planned"
        fill = QColor(color)
        fill.setAlpha(0 if planned else 46 if state == "building" else 26)
        pen = QPen(color, 2.5 if (state == "building" or shown) else 1.2)
        if planned:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(rect, 7, 7)

        # Left: the state icon, or the layer number in a disc.
        d = min(rect.height() - 12, 26.0)
        disc = QRectF(rect.left() + 8, rect.center().y() - d / 2, d, d)
        icon = self._STATE_ICON.get(state)
        if icon:
            p.drawPixmap(disc.toRect(), svg_pixmap(icon, color.name(), int(d)))
        else:
            p.setPen(QPen(color, 1.4, Qt.PenStyle.DashLine if planned else Qt.PenStyle.SolidLine))
            p.setBrush(color if state == "building" else Qt.BrushStyle.NoBrush)
            p.drawEllipse(disc)
            p.setFont(self._font(base, max(10, d * 0.5), True))
            p.setPen(QColor("white") if state == "building" else color)
            p.drawText(disc, Qt.AlignmentFlag.AlignCenter, str(l["layer"]))

        x = disc.right() + 8
        right = rect.right() - 10
        name = tr("Layer {n}", n=l["layer"])
        if planned:
            count, count_color = "", QColor(C["text3"])   # its word goes under the name
        else:
            n, e = l["count"], l["expected"] or l["count"]
            short = state != "building" and n < e
            count = f"{n}/{e}"
            count_color = (self._RED if short or n > e else
                           self._GREEN if n == e else color)
        p.setFont(self._font(base, 13, True))
        cw = p.fontMetrics().horizontalAdvance(count)
        p.setPen(count_color)
        p.drawText(QRectF(right - cw, rect.top(), cw, rect.height()),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count)
        two_lines = rect.height() >= 46
        p.setPen(QColor(C["text3"]) if planned else QColor(C["text1"]))
        p.setFont(self._font(base, 13, True))
        name_box = QRectF(x, rect.top(), max(0.0, right - cw - 6 - x),
                          rect.height() / 2 + 2 if two_lines else rect.height())
        p.drawText(name_box, (Qt.AlignmentFlag.AlignBottom if two_lines
                              else Qt.AlignmentFlag.AlignVCenter) | Qt.AlignmentFlag.AlignLeft,
                   self._elide(p, name, name_box.width(), Qt.TextElideMode.ElideRight))
        if two_lines:
            p.setFont(self._font(base, 10, True))
            p.setPen(QColor(C["text3"]) if planned else color)
            p.drawText(QRectF(x, rect.center().y() + 2, name_box.width(), rect.height() / 2 - 2),
                       Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                       tr(self._STATE_WORD[state]))
        if shown and not planned:
            # A pointer from the block to the big picture beside it.
            cy = rect.center().y()
            tip = [QPointF(rect.right() + 2, cy - 6), QPointF(rect.right() + 9, cy),
                   QPointF(rect.right() + 2, cy + 6)]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawPolygon(tip)
        p.setFont(base)

    def _panel(self, p, area, l, current):
        base = p.font()
        state = l["state"]
        color = self._STATE_COLOR[state]
        # Header: layer name and its state on the left, kegs read on the right.
        p.setFont(self._font(base, 18, True))
        p.setPen(QColor(C["text1"]))
        name = tr("Layer {n}", n=l["layer"])
        nw = p.fontMetrics().horizontalAdvance(name)
        p.drawText(QRectF(area.left(), area.top(), nw + 2, 26),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        n, e = l["count"], l["expected"] or l["count"]
        big = str(n)
        small = " / " + tr("{e} kegs", e=e)
        p.setFont(self._font(base, 26, True))
        bw = p.fontMetrics().horizontalAdvance(big)
        p.setFont(self._font(base, 13, True))
        sw = p.fontMetrics().horizontalAdvance(small)
        cx = area.right() - sw - bw
        p.setFont(self._font(base, 26, True))
        p.setPen(self._RED if n > e else self._GREEN if n == e else color)
        p.drawText(QRectF(cx, area.top() - 4, bw, 32),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, big)
        p.setFont(self._font(base, 13, True))
        p.setPen(QColor(C["text2"]))
        p.drawText(QRectF(cx + bw, area.top(), sw, 28),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, small)

        # The part every keg ID on the layer shares (CHECKOLOGY-DEMO-), said
        # once here; the kegs below show only their own number.
        codes = [c for row in (l.get("grid") or []) for c in row if c] + list(l.get("loose") or [])
        prefixes = {split_keg_id(c)[0] for c in codes}
        if prefixes:
            if len(prefixes) == 1 and next(iter(prefixes)):
                label, value = tr("Keg ID:"), next(iter(prefixes)) + "·" * 4
            else:
                label, value = tr("Keg IDs:"), tr("different prefixes — full IDs in the list")
            p.setFont(self._font(base, 12, True))
            p.setPen(QColor(C["text2"]))
            lw = p.fontMetrics().horizontalAdvance(label + " ")
            p.drawText(QRectF(area.left(), area.top() + 28, lw, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
            p.setFont(self._font(base, 13, True))
            p.setPen(QColor(C["text1"]))
            p.drawText(QRectF(area.left() + lw, area.top() + 28, area.width() - lw, 18),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._elide(p, value, area.width() - lw, Qt.TextElideMode.ElideMiddle))
        grid_area = QRectF(area.left(), area.top() + self._HEAD_H + 8, area.width(),
                           area.height() - self._HEAD_H - self._FOOT_H - 12)
        grid = l.get("grid")
        if grid:
            self._pallet(p, grid_area, grid, color, l["layer"], set(l.get("flagged") or ()))

        # Footer: only when something needs saying.
        loose = l.get("loose") or []
        if loose:
            hint, hint_color = tr("+{n} keg(s) read but not placed in the picture",
                                  n=len(loose)), self._AMBER
        else:
            hint, hint_color = "", None
        if hint:
            p.setFont(self._font(base, 11, True))
            p.setPen(hint_color)
            p.drawText(QRectF(area.left(), area.bottom() - self._FOOT_H, area.width(),
                              self._FOOT_H), Qt.AlignmentFlag.AlignCenter,
                       self._elide(p, hint, area.width(), Qt.TextElideMode.ElideRight))
        p.setFont(base)

    def _pallet(self, p, area, grid, color, layer_no, flagged=()):
        """A wooden pallet with the kegs standing on it, seen from above, every
        place numbered in reading order."""
        rows, cols = len(grid), len(grid[0])
        pad = 6
        cell = min((area.width() - 2 * pad) / cols, (area.height() - 2 * pad) / rows, 130.0)
        if cell < 12:
            return
        dw, dh = cols * cell + 2 * pad, rows * cell + 2 * pad
        deck = QRectF(area.left() + (area.width() - dw) / 2,
                      area.top() + (area.height() - dh) / 2, dw, dh)
        p.setPen(QPen(self._WOOD_DARK, 1))
        p.setBrush(self._WOOD)
        p.drawRoundedRect(deck, 6, 6)
        boards = max(3, rows * 2 + 1)
        p.setPen(QPen(QColor(139, 94, 60, 110), 1))
        for i in range(1, boards):
            y = deck.top() + deck.height() * i / boards
            p.drawLine(QPointF(deck.left() + 3, y), QPointF(deck.right() - 3, y))
        d = max(10.0, cell - 6)
        base = p.font()
        num_px = max(10, min(24, int(d * (0.22 if self._full else 0.27))))
        for r in range(rows):
            for c in range(cols):
                place = r * cols + c + 1
                code = grid[r][c]
                cx = deck.left() + pad + cell * (c + 0.5)
                cy = deck.top() + pad + cell * (r + 0.5)
                keg = QRectF(cx - d / 2, cy - d / 2, d, d)
                if not code:
                    p.setBrush(QColor(255, 255, 255, 150))
                    p.setPen(QPen(QColor(C["slate"]), 1.6, Qt.PenStyle.DashLine))
                    p.drawEllipse(keg)
                    p.setFont(self._font(base, max(11, d * 0.30), True))
                    p.setPen(QColor(C["slate"]))
                    p.drawText(keg, Qt.AlignmentFlag.AlignCenter, str(place))
                    continue
                self._cells.append((keg, layer_no, code))
                # A keg already on another pallet: amber, like its row in the
                # scanned list, so the operator sees which keg it is.
                kc = self._AMBER if code in flagged else color
                p.setPen(QPen(QColor(0, 0, 0, 60), 1))
                p.setBrush(kc)
                p.drawEllipse(keg)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(kc).lighter(130))
                inner = keg.adjusted(d * 0.10, d * 0.10, -d * 0.10, -d * 0.10)
                p.drawEllipse(inner)
                # The keg number (CHECKOLOGY-2026-0010 -> 0010), big.
                number = split_keg_id(code)[1]
                # Smaller type before cutting it: the whole number must show.
                px = num_px
                p.setFont(self._font(base, px, True))
                while px > 9 and p.fontMetrics().horizontalAdvance(number) > inner.width() - 4:
                    px -= 1
                    p.setFont(self._font(base, px, True))
                p.setPen(QColor("white"))
                p.drawText(inner.adjusted(2, 0, -2, 0), Qt.AlignmentFlag.AlignCenter,
                           self._elide(p, number, inner.width() - 4))
        p.setFont(base)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        base = p.font()
        base.setPixelSize(12)
        p.setFont(base)
        self._cells, self._blocks = [], []
        rows, target, layers = self._rows()
        if not layers:
            p.setPen(QColor(C["text3"]))
            p.drawText(frame, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       tr("No layers yet"))
            return
        shown = self._shown_layer(layers)
        tw = max(130.0, min(200.0, frame.width() * 0.34))
        self._tower(p, QRectF(frame.left(), frame.top(), tw, frame.height()),
                    rows, layers, target, shown["layer"] if shown else None)
        if shown:
            panel = QRectF(frame.left() + tw + 18, frame.top(),
                           frame.width() - tw - 18, frame.height())
            self._panel(p, panel, shown, current=self._picked is None)


class NextStepCard(QFrame):
    """What the operator does now: an icon, a title and one short line.

    kind sets the colour and icon (the colour is never the only sign):
      action  blue   arrow        busy   amber  clock
      done    green  tick         warn   amber  warning sign
      error   red    cross
    """
    _KINDS = {
        "action": ("blue", "arrow-right"),
        "busy":   ("amber_dark", "clock"),
        "done":   ("green_dark", "circle-check"),
        "warn":   ("amber_dark", "triangle-alert"),
        "error":  ("red_dark", "circle-x"),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("nextStep")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Never squeezed below its text: the stack picture gives up the room.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(12)
        self._icon = QLabel()
        self._icon.setFixedSize(34, 34)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(1)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._detail = QLabel()
        self._detail.setWordWrap(True)
        col.addWidget(self._title)
        col.addWidget(self._detail)
        lay.addLayout(col, 1)
        self._shown = None
        self.set_step("action", "")

    def text(self):
        return self._title.text()

    def set_step(self, kind, title, detail=""):
        shown = (kind, title, detail)
        if shown == self._shown:        # called every detection round
            return
        self._shown = shown
        key, icon = self._KINDS.get(kind, self._KINDS["action"])
        color = C[key]
        q = QColor(color)
        rgb = f"{q.red()},{q.green()},{q.blue()}"
        self.setStyleSheet(
            f"#nextStep {{ background: rgba({rgb},0.08); border: 1px solid rgba({rgb},0.30);"
            f" border-left: 5px solid {color}; border-radius: 8px; }}")
        self._icon.setStyleSheet(f"background:{color}; border-radius:17px;")
        self._icon.setPixmap(svg_pixmap(icon, "#FFFFFF", 20))
        self._title.setText(title)
        self._title.setStyleSheet(f"color:{C['text1']}; font-size:17px; font-weight:700;"
                                  f" background:transparent;")
        self._detail.setText(detail)
        self._detail.setStyleSheet(f"color:{C['text2']}; font-size:13px; background:transparent;")
        self._detail.setVisible(bool(detail))


def caution_note(text, kind="warning"):
    """Small note under a control that needs care.

    Amber warning triangle, or a slate info icon for a plain hint. 11 px, wraps."""
    icon, color = (("triangle-alert", C["amber_dark"]) if kind == "warning"
                   else ("info", C["slate"]))
    return IconLabel(icon, text, color, size=11, weight=500, icon_size=13,
                     pad=(0, 2), wrap=True)


class IconLabel(QWidget):
    """SVG icon and text on one line, used for status chips and pills."""
    def __init__(self, icon=None, text="", color=None, size=12, weight=700,
                 icon_size=14, bg=None, radius=4, pad=(8, 3), wrap=False, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(pad[0], pad[1], pad[0], pad[1])
        lay.setSpacing(6)
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(icon_size, icon_size)
        self._icon_lbl.setStyleSheet("background:transparent;")
        self._text_lbl = QLabel()
        self._text_lbl.setProperty("i18n_skip", True)   # see i18n_refresh
        self._text_lbl.setWordWrap(wrap)
        lay.addWidget(self._icon_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._text_lbl, 1 if wrap else 0)
        self._size, self._weight, self._icon_size, self._radius = size, weight, icon_size, radius
        self._icon, self._text, self._color, self._bg = icon, text, color or C["text1"], bg
        self._apply()

    def set(self, icon=None, text=None, color=None, bg=None):
        if icon is not None:
            self._icon = icon
        if text is not None:
            self._text = text
        if color is not None:
            self._color = color
        if bg is not None:
            self._bg = bg
        self._apply()

    def setText(self, text):  # noqa: N802 (Qt naming)
        self.set(text=text)

    def i18n_refresh(self, swap):
        """Language changed: keep the text this widget re-applies in step."""
        new = swap(self._text)
        if new is not None:
            self.set(text=new)
        tip = swap(self.toolTip())
        if tip is not None:
            self.setToolTip(tip)

    def _apply(self):
        self._icon_lbl.setVisible(bool(self._icon))
        if self._icon:
            self._icon_lbl.setPixmap(svg_pixmap(self._icon, self._color, self._icon_size))
        self._text_lbl.setText(self._text)
        self._text_lbl.setStyleSheet(
            f"color:{self._color}; font-size:{self._size}px; font-weight:{self._weight};"
            f" background:transparent;")
        self.setStyleSheet(
            f"IconLabel {{ background-color:{self._bg or 'transparent'};"
            f" border-radius:{self._radius}px; }}")


# Show/hide toggle for password fields
def add_password_toggle(edit):
    """Add an eye icon at the right of a password QLineEdit.

    Tap to show the text (the icon becomes a crossed-out eye), tap again to hide it."""
    action = edit.addAction(svg_icon("eye", C["slate"]),
                            QLineEdit.ActionPosition.TrailingPosition)
    action.setToolTip(tr("Show password"))

    def toggle():
        shown = edit.echoMode() == QLineEdit.EchoMode.Password
        set_password_visible(edit, shown)

    action.triggered.connect(toggle)
    edit._pw_toggle = action
    return action


def set_password_visible(edit, visible):
    edit.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
    action = getattr(edit, "_pw_toggle", None)
    if action is not None:
        action.setIcon(svg_icon("eye-off" if visible else "eye", C["slate"]))
        action.setToolTip(tr("Hide password") if visible else tr("Show password"))


# Email field with remembered-email suggestions, like a browser's
class EmailField(QLineEdit):
    """Email input with a mail icon and a dropdown of remembered emails.

    Tapping the field shows every remembered email; typing filters them
    (contains, case-insensitive). Picking one fills the field. Only emails are
    stored, never passwords.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.addAction(svg_icon("mail", C["slate"]), QLineEdit.ActionPosition.LeadingPosition)
        self._model = QStandardItemModel(self)
        comp = QCompleter(self._model, self)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setMaxVisibleItems(6)
        popup = comp.popup()
        popup.setIconSize(QSize(18, 18))
        popup.setStyleSheet(
            f"QListView {{ background:{C['white']}; color:{C['text1']};"
            f" border:1px solid {C['border']}; border-radius:6px; outline:0; padding:4px; }}"
            f"QListView::item {{ padding:8px 6px; border-radius:4px; }}"
            f"QListView::item:selected, QListView::item:hover {{"
            f" background:{C['blue_pale']}; color:{C['ck_navy']}; }}")
        self.setCompleter(comp)
        self.textEdited.connect(lambda _t: self._show_suggestions())

    def set_suggestions(self, emails):
        self._model.clear()
        icon = svg_icon("user", C["slate"])
        for e in emails:
            self._model.appendRow(QStandardItem(icon, e))

    def _show_suggestions(self):
        comp = self.completer()
        if comp is None or not self._model.rowCount():
            return
        comp.setCompletionPrefix(self.text())
        n = comp.completionCount()
        if not n:
            comp.popup().hide()
            return
        comp.complete()
        # Qt sizes the popup without the rows' stylesheet padding, so fit it to
        # its rows (up to maxVisibleItems) to avoid a scrollbar on short lists.
        popup = comp.popup()
        rows = min(n, comp.maxVisibleItems())
        popup.setFixedHeight(rows * max(popup.sizeHintForRow(0), 34) + 12)

    def mousePressEvent(self, e):  # noqa: N802 (Qt naming)
        super().mousePressEvent(e)
        self._show_suggestions()       # tap → show all remembered emails


# Sidebar nav button (icon and full label, like the cloud app)
class NavButton(QPushButton):
    """Wide rail button: simple line icon on the left, full text label."""
    def __init__(self, icon_name, label, tooltip=None, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self.setText("   " + label)
        self.setToolTip(tooltip or label)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)
        self.setIconSize(QSize(20, 20))
        self._apply(False)

    def _apply(self, active):
        # Plain dark text. The active page is brand navy on a light tint, with
        # a navy bar on the sidebar's right edge.
        fg = C["ck_navy"] if active else C["text1"]
        icon = C["ck_navy"] if active else C["text2"]
        bg = C["chrome2"] if active else "transparent"
        edge = C["ck_navy"] if active else "transparent"
        weight = 600 if active else 400
        self.setIcon(QIcon(_nav_icon(self._icon_name, icon)))
        self.setStyleSheet(
            f"QPushButton {{ background:{bg}; color:{fg}; border:none;"
            f" border-right:3px solid {edge}; border-radius:0; text-align:left;"
            f" padding-left:18px; font-size:14px; font-weight:{weight}; }}"
            f"QPushButton:hover {{ background:{C['chrome2']}; }}")

    def set_active(self, active):
        self._apply(active)
        self.setChecked(active)


# Camera feed widget
class CameraFeedWidget(QWidget):
    """Shows an RGB frame plus a small HUD (timestamp and red alert border).

    QR boxes are already drawn into the frame by the operator page's display tick.
    A double-tap (double-click) on the picture emits `double_tapped` with the
    point as a fraction of the frame (0-1), for manual focus; a single tap does
    nothing, so a stray touch cannot refocus the camera. set_focus_marker()
    draws the square being focused on.
    """
    double_tapped = Signal(float, float)

    _MARKER_COLORS = {"busy": QColor(245, 158, 11), "ok": QColor(34, 197, 94),
                      "fail": QColor(239, 68, 68)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cameraFeed")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._pixmap = None
        self._alert = False
        self._dwell = 0
        self._image_rect = None      # where the picture was last drawn
        self._marker = None          # ((x1, y1, x2, y2) fractions of the frame, state)
        self._corner = None          # (toggle button, panel) in the bottom-right corner
        self.setMinimumHeight(200)

    def set_corner_widgets(self, button, panel):
        """Keep a toggle button in the bottom-right corner and its panel just
        above it. The top-left holds the REC time, so nothing overlaps."""
        self._corner = (button, panel)
        self._place_corner()

    def _place_corner(self):
        if not self._corner:
            return
        button, panel = self._corner
        margin = 10
        button.adjustSize()
        bx = self.width() - button.width() - margin
        by = self.height() - button.height() - margin
        button.move(bx, by)
        panel.move(self.width() - panel.width() - margin,
                   max(margin, by - panel.height() - 6))
        button.raise_()
        panel.raise_()

    def resizeEvent(self, e):  # noqa: N802 (Qt naming)
        super().resizeEvent(e)
        self._place_corner()

    def set_focus_marker(self, rect, state):
        """Box `rect` = (x1, y1, x2, y2) as fractions of the frame; state
        "busy" / "ok" / "fail". None clears."""
        self._marker = None if rect is None else (rect, state)
        self.update()

    def mouseDoubleClickEvent(self, e):  # noqa: N802 (Qt naming)
        # A double-tap on the touchscreen arrives here too (Qt synthesises
        # mouse events from touch).
        super().mouseDoubleClickEvent(e)
        r = self._image_rect
        pos = e.position()
        if r is not None and r.contains(pos):
            self.double_tapped.emit((pos.x() - r.x()) / r.width(),
                                    (pos.y() - r.y()) / r.height())

    def set_frame_rgb(self, frame_rgb):
        """frame_rgb: contiguous HxWx3 uint8 RGB ndarray."""
        h, w, ch = frame_rgb.shape
        img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(img.copy())
        self.update()

    def set_alert(self, active, secs=0):
        self._alert = active
        self._dwell = secs
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()

        if self._pixmap is not None:
            scaled = self._pixmap.scaled(
                r.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            x = (r.width() - scaled.width()) // 2
            y = (r.height() - scaled.height()) // 2
            painter.fillRect(r, QColor(15, 23, 42))
            painter.drawPixmap(x, y, scaled)
            self._image_rect = QRectF(x, y, scaled.width(), scaled.height())
        else:
            painter.fillRect(r, QColor(15, 23, 42))
            self._image_rect = None

        # Manual focus: the square being focused on.
        if self._marker is not None and self._image_rect is not None:
            (x1, y1, x2, y2), state = self._marker
            ir = self._image_rect
            painter.setPen(QPen(self._MARKER_COLORS.get(state, QColor("white")), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(ir.x() + x1 * ir.width(), ir.y() + y1 * ir.height(),
                                    (x2 - x1) * ir.width(), (y2 - y1) * ir.height()))

        # The only boxes on the feed are the per-QR green boxes, painted into
        # the frame by the operator page's _draw_overlays_scaled.

        # HUD text
        painter.setPen(QPen(QColor(148, 163, 184, 200)))
        painter.setFont(QFont("Consolas", 9))
        painter.drawText(12, 20, f"REC · {now_time()}")

        # Alert state is just a red border, so nothing covers the feed or QR boxes.
        if self._alert:
            painter.setPen(QPen(QColor(239, 68, 68, 170), 3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(1, 1, r.width() - 2, r.height() - 2)

        painter.end()


# Combo box that always shows a chevron (QSS styling hides the native one)
class ArrowCombo(QComboBox):
    """QComboBox that paints a blue chevron in the drop-down zone.

    A fully QSS-styled combo loses its native arrow; this keeps it readable as
    a dropdown."""
    def paintEvent(self, e):
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(C["blue"]), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        cx, cy = self.width() - 12, self.height() // 2
        p.drawLine(cx - 4, cy - 2, cx, cy + 2)
        p.drawLine(cx, cy + 2, cx + 4, cy - 2)
        p.end()


# Searchable dropdown (type-to-filter, sorted by name)
class SearchableCombo(ArrowCombo):
    """Editable dropdown with type-to-search filtering.

    Click and start typing to filter a long list; matching is "contains" and
    case-insensitive. The prompt is a grey placeholder, not a list item, so the
    field starts empty and the operator can type straight away. Items are
    sorted by name. currentText() returns "" until a real item is chosen.
    """
    def __init__(self, placeholder=None, parent=None):
        super().__init__(parent)
        if placeholder is None:
            placeholder = tr("Select…")
        self._placeholder = placeholder
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)   # don't add typed text as an item
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        le = self.lineEdit()
        le.setPlaceholderText(placeholder)
        # Show the filtered popup live as the operator types.
        le.textEdited.connect(self._on_text_edited)

        completer = self.completer()
        if completer is not None:
            completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
            completer.setFilterMode(Qt.MatchFlag.MatchContains)  # match anywhere, not just prefix
            # Match the app's list styling (the completer popup is its own view).
            completer.popup().setStyleSheet(
                f"background:{C['white']}; color:{C['text1']};"
                f" border:1px solid {C['border']}; outline:0; padding:2px;"
                f" selection-background-color:{C['blue_pale']};"
                f" selection-color:{C['blue']};")

        # Start with nothing selected, so the empty field shows the placeholder.
        self.setCurrentIndex(-1)

    def _on_text_edited(self, text):
        """Drive the completer while typing so the filtered list pops up at once.

        Uses contains-matching, so it works even when the text is not a prefix."""
        comp = self.completer()
        if comp is None:
            return
        comp.setCompletionPrefix(text)
        if text:
            comp.complete()          # show/refresh the filtered popup
        else:
            comp.popup().hide()      # empty field, no popup

    def focusInEvent(self, e):
        # Select existing text so typing replaces it (fresh search).
        super().focusInEvent(e)
        self.lineEdit().selectAll()

    def set_items(self, items, sort=True):
        """Replace the list, sorted by name by default.

        Leaves the field empty with the placeholder shown and nothing selected."""
        names = [str(x) for x in items]
        if sort:
            names = sorted(names, key=lambda s: s.lower())
        self.blockSignals(True)
        self.clear()
        self.addItems(names)
        self.setCurrentIndex(-1)
        self.lineEdit().clear()
        self.blockSignals(False)
        return names


# Searchable select (button opening a popup with a search box and list)


class CountCombo(ArrowCombo):
    """Editable combo: presets in the dropdown, free numeric entry, range-clamped.

    Spin-box style API: value() / setValue(int) / valueChanged(int).
    """
    valueChanged = Signal(int)

    def __init__(self, value, minimum, maximum, parent=None):
        super().__init__(parent)
        self.setObjectName("countCombo")
        self._min, self._max = int(minimum), int(maximum)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lineEdit().setValidator(QIntValidator(self._min, self._max, self))
        self.lineEdit().setAlignment(Qt.AlignmentFlag.AlignCenter)
        for v in range(self._min, self._max + 1):
            self.addItem(str(v))

        self._value = max(self._min, min(self._max, int(value)))
        self.setCurrentText(str(self._value))

        # Commit on a dropdown pick or when typing finishes, not per keystroke.
        self.textActivated.connect(self._commit_text)
        self.lineEdit().editingFinished.connect(self._on_edit_finished)

    def _commit(self, v):
        v = max(self._min, min(self._max, int(v)))
        changed = (v != self._value)
        self._value = v
        if self.currentText() != str(v):
            self.setCurrentText(str(v))
        if changed:
            self.valueChanged.emit(v)

    def _commit_text(self, text):
        text = text.strip()
        if text.isdigit():
            self._commit(int(text))

    def _on_edit_finished(self):
        text = self.lineEdit().text().strip()
        if text.isdigit():
            self._commit(int(text))
        else:
            self.setCurrentText(str(self._value))   # revert invalid entry

    def value(self):
        return self._value

    def setValue(self, v):
        self._commit(v)


# Confirmation dialog (styled card instead of a bare QMessageBox)
class ConfirmDialog(QDialog):
    """Themed Yes/No dialog with a navy header, message body and action row."""

    def __init__(self, title, message, detail="", confirm_text=None,
                 danger=False, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        if confirm_text is None:
            confirm_text = tr("CONFIRM")
        self._build(title, message, detail, confirm_text, danger)

    def _build(self, title, message, detail, confirm_text, danger):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(390)
        shadow = QGraphicsDropShadowEffect(root)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 170))
        root.setGraphicsEffect(shadow)

        vl = QVBoxLayout(root)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        header = QFrame()
        header.setObjectName("confirmDangerHeader" if danger else "confirmHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        t = QLabel(title)
        t.setObjectName("confirmTitle")
        hl.addWidget(t)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(22, 20, 22, 18)
        body.setSpacing(8)
        msg = QLabel(message)
        msg.setObjectName("confirmMsg")
        msg.setWordWrap(True)
        body.addWidget(msg)
        if detail:
            det = QLabel(detail)
            det.setObjectName("confirmDetail")
            det.setWordWrap(True)
            body.addWidget(det)

        body.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        cancel = QPushButton(tr("CANCEL"))
        cancel.setObjectName("confirmCancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton(confirm_text)
        ok.setObjectName("confirmDanger" if danger else "confirmOk")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(ok)
        body.addLayout(actions)
        vl.addLayout(body)

        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def ask(parent, title, message, detail="", confirm_text=None, danger=False):
        """Show the dialog modally; return True if the user confirmed."""
        dlg = ConfirmDialog(title, message, detail, confirm_text, danger, parent)
        return dlg.exec() == QDialog.DialogCode.Accepted

    @staticmethod
    def ask_twice(parent, title, message, detail="", confirm_text=None,
                  final_message=None, final_detail=None):
        """Double confirmation for risky actions.

        Asks the normal (red) question, then a second "are you sure?" before
        anything happens."""
        if confirm_text is None:
            confirm_text = tr("CONFIRM")
        if final_message is None:
            final_message = tr("Are you sure?")
        if final_detail is None:
            final_detail = tr("This cannot be undone.")
        if not ConfirmDialog.ask(parent, title, message, detail, confirm_text,
                                 danger=True):
            return False
        return ConfirmDialog.ask(parent, tr("Please Confirm Again"), final_message,
                                 final_detail, tr("YES, {action}", action=confirm_text),
                                 danger=True)


class DuplicateKegDialog(QDialog):
    """What to do with kegs that are already on another pallet.

    There are three valid answers, which a Yes/No dialog can't offer:

      MOVE   - the keg joins this pallet and comes off the one it was on (a
               re-stacked keg is on one pallet, not two).
      KEEP   - the keg joins this pallet and the old pallet is left untouched.
      CANCEL - build no pallet; the operator pulls the kegs instead.

    Every choice except CANCEL sends the keg, so nothing is silently dropped
    from a layer.

    The same question is asked at two moments. `repeat=True` is shown when an
    already-palletized keg is detected (the same load still under the camera);
    `repeat=False` is shown at send time. Only the wording differs. The
    outcomes are the same, so an answer given at detection time is reused and
    the send-time dialog does not ask again.
    """

    MOVE   = "move"
    KEEP   = "keep"
    CANCEL = "cancel"

    # (title, lead-in, question, MOVE caption, KEEP label, KEEP caption,
    #  CANCEL label, CANCEL caption) per moment.
    _COPY = {
        False: (
            N_("Keg Already In Another Pallet"),
            N_("{n} keg(s) in this batch are already on another pallet."),
            "",
            N_("MOVE TO THIS PALLET"),
            N_("Adds the keg here and removes it from the pallet it is on now."),
            N_("KEEP ON BOTH PALLETS"),
            N_("Adds the keg here and leaves the previous pallet unchanged."),
            N_("CANCEL"),
            N_("Create no pallet — remove the kegs and scan again."),
        ),
        True: (
            N_("These Kegs Already Made a Pallet"),
            N_("{n} keg(s) under the camera are already on a pallet. Automatic "
               "sending is paused for these kegs until you choose."),
            N_("Do you need to create another pallet with them?"),
            N_("MOVE TO A NEW PALLET"),
            N_("Builds a new pallet and removes these kegs from the pallet they are on now."),
            N_("CREATE ANOTHER PALLET"),
            N_("Builds a new pallet with these kegs and leaves the existing pallet unchanged."),
            N_("NO — NOT NEEDED"),
            N_("No new pallet. Take the kegs off the camera; you won't be asked again."),
        ),
    }

    def __init__(self, flagged, repeat=False, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._choice = self.CANCEL
        self._build(flagged or [], bool(repeat))

    def _build(self, flagged, repeat):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(460)
        shadow = QGraphicsDropShadowEffect(root)
        shadow.setBlurRadius(60)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 170))
        root.setGraphicsEffect(shadow)

        vl = QVBoxLayout(root)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        (title, lead, question, move_lbl, move_cap,
         keep_lbl, keep_cap, cancel_lbl, cancel_cap) = (tr(s) for s in self._COPY[repeat])

        header = QFrame()
        header.setObjectName("confirmDangerHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        hl.setSpacing(10)
        warn = QLabel()
        warn.setPixmap(svg_pixmap("triangle-alert", "#FFFFFF", 20))
        warn.setStyleSheet("background:transparent;")
        hl.addWidget(warn)
        t = QLabel(title)
        t.setObjectName("confirmTitle")
        hl.addWidget(t)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(22, 18, 22, 18)
        body.setSpacing(8)

        msg = QLabel(lead.format(n=len(flagged)))
        msg.setObjectName("confirmMsg")
        msg.setWordWrap(True)
        body.addWidget(msg)

        body.addWidget(self._keg_list(flagged))

        pallets = ", ".join(sorted({p for _, p in flagged}))
        det = QLabel(tr("Existing pallet(s): {pallets}", pallets=pallets))
        det.setObjectName("confirmDetail")
        det.setWordWrap(True)
        body.addWidget(det)

        if question:
            q = QLabel(question)
            q.setObjectName("confirmMsg")
            q.setWordWrap(True)
            body.addSpacing(2)
            body.addWidget(q)

        body.addSpacing(6)

        # Stacked full-width choices, each with a caption saying what happens to
        # the old pallet. That is the only difference between them, and the
        # operator should not have to guess it.
        body.addLayout(self._choice_row(move_lbl, "confirmOk", self.MOVE, move_cap))
        body.addLayout(self._choice_row(keep_lbl, "dupBtn", self.KEEP, keep_cap))

        cancel = QPushButton(cancel_lbl)
        cancel.setObjectName("confirmCancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setMinimumHeight(38)
        cancel.setToolTip(cancel_cap)
        cancel.clicked.connect(self.reject)
        body.addSpacing(2)
        body.addWidget(cancel)
        cap = QLabel(cancel_cap)
        cap.setStyleSheet(f"color:{C['text2']}; font-size:11px; background:transparent;")
        cap.setWordWrap(True)
        body.addWidget(cap)

        vl.addLayout(body)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    def _keg_list(self, flagged):
        """Scrollable 'code on pallet' list naming every keg."""
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(10, 8, 10, 8)
        il.setSpacing(3)
        for code, pallet in flagged:
            row = QLabel(tr("{code}    on pallet {pallet}", code=code, pallet=pallet))
            row.setStyleSheet(
                f"color:{C['amber_dark']}; font-family:'Consolas'; font-size:12px;"
                f" background:transparent;")
            il.addWidget(row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        scroll.setMaximumHeight(150)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: rgba(245,158,11,0.10);"
            f" border:1px solid {C['amber']}; border-radius:6px; }}"
            f"QWidget {{ background: transparent; }}")
        return scroll

    def _choice_row(self, text, obj_name, choice, caption):
        col = QVBoxLayout()
        col.setSpacing(2)
        btn = QPushButton(text)
        btn.setObjectName(obj_name)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setMinimumHeight(44)
        btn.setToolTip(caption)
        btn.clicked.connect(lambda: self._choose(choice))
        cap = QLabel(caption)
        cap.setStyleSheet(f"color:{C['text2']}; font-size:11px; background:transparent;")
        cap.setWordWrap(True)
        col.addWidget(btn)
        col.addWidget(cap)
        return col

    def _choose(self, choice):
        self._choice = choice
        self.accept()

    @staticmethod
    def ask(parent, flagged, repeat=False):
        """Show modally. Returns 'move', 'keep' or 'cancel'.

        `flagged` is [(code, pallet_label), ...] as returned by
        SessionStore.flagged_list(). `repeat=True` uses the detection-time
        wording ("these kegs already made a pallet, need another one?").
        """
        dlg = DuplicateKegDialog(flagged, repeat, parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._choice
        return DuplicateKegDialog.CANCEL


class KegRegisterDialog(QDialog):
    """Form for registering an unknown keg, drawn from the cloud's field list.

    One input per entry in form["fields"], in order, nothing added or renamed:
      number -> numeric text box, min/max checked
      text   -> text box, maxLength and pattern (whole value) checked
      select -> drop-down of options (label shown, value sent)
      other  -> plain text box; the cloud checks it
    Inputs start from form["defaults"]. Checks here are only a convenience;
    the cloud checks every answer again.

    `code` is one keg ID or a list of them. With several, the same answers are
    used for every keg (the owner sends them one by one).

    The dialog does not send anything itself. REGISTER emits `submitted(values)`;
    the owner sends it and calls finish() on success or show_errors() to keep
    the form open with the cloud's messages.
    """
    submitted = Signal(object)   # {key: value}

    def __init__(self, code, form, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.codes = [code] if isinstance(code, str) else list(code)
        self.code = self.codes[0]
        self.form = form or {}
        self.form_version = self.form.get("formVersion")
        self._inputs = {}    # key -> (field dict, widget, error label)
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(440)
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
        self._title = QLabel()
        self._title.setObjectName("confirmTitle")
        hl.addWidget(self._title)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(22, 18, 22, 18)
        body.setSpacing(8)
        self._msg = QLabel()
        self._msg.setObjectName("confirmMsg")
        self._msg.setWordWrap(True)
        body.addWidget(self._msg)
        self._code_list = QLabel()
        self._code_list.setWordWrap(True)
        self._code_list.setStyleSheet(f"color:{C['unreg']}; font-size:11px; font-weight:600;"
                                      f" background:transparent;")
        body.addWidget(self._code_list)
        self._show_codes()

        inner = QWidget()
        fl = QVBoxLayout(inner)
        fl.setContentsMargins(0, 0, 4, 0)
        fl.setSpacing(4)
        defaults = self.form.get("defaults") or {}
        for field in self.form.get("fields") or []:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key = str(field["key"])
            label = str(field.get("label") or key)
            fl.addWidget(field_label(label + (" *" if field.get("required") else "")))
            widget = self._make_input(field, defaults.get(key))
            fl.addWidget(widget)
            err = QLabel("")
            err.setWordWrap(True)
            err.setStyleSheet(f"color:{C['red']}; font-size:11px; background:transparent;")
            err.setVisible(False)
            fl.addWidget(err)
            fl.addSpacing(4)
            self._inputs[key] = (field, widget, err)
        fl.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        scroll.setMaximumHeight(380)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body.addWidget(scroll)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet(f"color:{C['red']}; font-size:12px; font-weight:600;"
                                  f" background:transparent;")
        self._error.setVisible(False)
        body.addWidget(self._error)

        body.addSpacing(6)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        cancel = QPushButton(tr("CANCEL"))
        cancel.setObjectName("confirmCancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        self._ok = QPushButton(tr("REGISTER"))
        self._ok.setObjectName("confirmOk")
        self._ok.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok.clicked.connect(self._submit)
        actions.addWidget(cancel)
        actions.addWidget(self._ok)
        body.addLayout(actions)
        vl.addLayout(body)

        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    def _make_input(self, field, default):
        ftype = field.get("type")
        if ftype == "select":
            combo = ArrowCombo()
            combo.setFixedHeight(CONTROL_H)
            options = [o for o in (field.get("options") or []) if isinstance(o, dict)]
            if not options:
                # The brewery has none configured: leave the field empty.
                combo.addItem(tr("None configured"), None)
                combo.setEnabled(False)
                return combo
            combo.addItem(tr("Select..."), None)
            for opt in options:
                combo.addItem(str(opt.get("label", opt.get("value", ""))), opt.get("value"))
            if default is not None:
                for i in range(1, combo.count()):
                    if str(combo.itemData(i)) == str(default):
                        combo.setCurrentIndex(i)
                        break
            return combo
        edit = QLineEdit()
        edit.setFixedHeight(CONTROL_H)
        if ftype == "text" and field.get("maxLength"):
            try:
                edit.setMaxLength(int(field["maxLength"]))
            except (TypeError, ValueError):
                pass
        if ftype == "number":
            edit.setInputMethodHints(Qt.InputMethodHint.ImhFormattedNumbersOnly)
        if default is not None:
            edit.setText(str(default))
        return edit

    @staticmethod
    def _value_of(field, widget):
        """The answer as it is sent, or None when left empty."""
        if isinstance(widget, QComboBox):
            return widget.currentData() if widget.isEnabled() else None
        text = widget.text().strip()
        return text or None

    def _check(self, field, value):
        """Operator-convenience check of one answer. Returns an error or ""."""
        label = str(field.get("label") or field.get("key"))
        if value is None:
            return tr("{label} is required", label=label) if field.get("required") else ""
        ftype = field.get("type")
        if ftype == "number":
            try:
                num = float(str(value).replace(",", "."))
            except ValueError:
                return tr("{label} must be a number", label=label)
            if field.get("min") is not None and num < float(field["min"]):
                return tr("{label} must be at least {n}", label=label, n=field["min"])
            if field.get("max") is not None and num > float(field["max"]):
                return tr("{label} must be at most {n}", label=label, n=field["max"])
        elif ftype == "text" and field.get("pattern"):
            try:
                if not re.fullmatch(str(field["pattern"]), str(value)):
                    return field.get("patternMessage") or tr("{label} is not valid", label=label)
            except re.error:
                pass   # a pattern Python can't read: leave it to the cloud
        return ""

    def _submit(self):
        values, ok = {}, True
        for key, (field, widget, err) in self._inputs.items():
            value = self._value_of(field, widget)
            problem = self._check(field, value)
            err.setText(problem)
            err.setVisible(bool(problem))
            if problem:
                ok = False
            if problem or value is None:
                continue
            if field.get("type") == "number":
                num = float(str(value).replace(",", "."))
                value = int(num) if num.is_integer() else num
            values[key] = value
        self._error.setVisible(False)
        if not ok:
            return
        self.set_busy(True)
        self.submitted.emit(values)

    def _show_codes(self):
        n = len(self.codes)
        if n == 1:
            self._title.setText(tr("Register Keg"))
            self._msg.setText(tr("Keg {code} is not registered yet. Fill in its details.",
                                 code=self.code))
            self._code_list.setVisible(False)
            return
        self._title.setText(tr("Register {n} Kegs", n=n))
        self._msg.setText(tr("{n} kegs are not registered yet. The details you fill in "
                             "are used for all of them.", n=n))
        shown = ", ".join(self.codes[:6])
        if n > 6:
            shown += " " + tr("and {n} more", n=n - 6)
        self._code_list.setText(shown)
        self._code_list.setVisible(True)

    def set_codes(self, codes):
        """Narrow the form to the kegs still to register (after a partial send)."""
        self.codes = list(codes)
        self.code = self.codes[0]
        self._show_codes()

    def set_busy(self, busy):
        self._ok.setEnabled(not busy)
        self._ok.setText(tr("SENDING...") if busy else tr("REGISTER"))

    def set_progress(self, done, total):
        """Bulk send: show how far it has got on the REGISTER button."""
        self._ok.setText(tr("SENDING {done}/{total}...", done=done, total=total))

    def show_errors(self, message, field_errors=None):
        """The cloud refused the answers: show why and keep the form open."""
        self.set_busy(False)
        for key, (_, _, err) in self._inputs.items():
            text = (field_errors or {}).get(key)
            err.setText(str(text or ""))
            err.setVisible(bool(text))
        self._error.setText(message or "")
        self._error.setVisible(bool(message))

    def finish(self):
        self.accept()


class ExitChoiceDialog(QDialog):
    """Exit / Shut Down chooser for the kiosk.

    Offers two outcomes, quit the app or power off the device, plus Cancel.
    Both run the same graceful cleanup (camera released, uploads and prints
    drained). POWEROFF then halts the machine so mains power to the iCam-540
    can be cut safely. Returns one of CANCEL / EXIT / POWEROFF.
    """

    CANCEL   = "cancel"
    EXIT     = "exit"
    POWEROFF = "poweroff"

    def __init__(self, detail="", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._choice = self.CANCEL
        self._build(detail)

    def _build(self, detail):
        """Compact message-box layout.

        Slim title bar with a close ×, a "?" icon beside the question, and the
        three choices on one row."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)   # room for the shadow

        root = QFrame()
        root.setObjectName("exitRoot")
        root.setFixedWidth(400)
        shadow = QGraphicsDropShadowEffect(root)
        shadow.setBlurRadius(30)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 120))
        root.setGraphicsEffect(shadow)

        vl = QVBoxLayout(root)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # Title bar: centred title, close × on the right
        header = QFrame()
        header.setObjectName("exitTitleBar")
        header.setFixedHeight(36)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(36, 0, 6, 0)          # left pad balances the ×
        t = QLabel(tr("Exit or Shut Down"))
        t.setObjectName("exitTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hl.addWidget(t, 1)
        close = QPushButton()
        close.setObjectName("exitClose")
        close.setIcon(svg_icon("x", C["slate"], 16))
        close.setIconSize(QSize(16, 16))
        close.setFixedSize(32, 32)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip(tr("Cancel"))
        close.clicked.connect(self.reject)
        hl.addWidget(close)
        vl.addWidget(header)

        # Icon and message
        body = QHBoxLayout()
        body.setContentsMargins(18, 16, 18, 12)
        body.setSpacing(14)
        icon = QLabel("?")
        icon.setObjectName("exitIcon")
        icon.setFixedSize(40, 40)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(6)
        msg = QLabel(tr("What would you like to do?"))
        msg.setObjectName("exitMsg")
        msg.setWordWrap(True)
        text.addWidget(msg)
        det = QLabel(detail or tr("The camera will be released and services closed first."))
        det.setObjectName("exitDetail")
        det.setWordWrap(True)
        text.addWidget(det)
        body.addLayout(text, 1)
        vl.addLayout(body)

        # One row of choices: Cancel, Exit App, Shut Down
        row = QHBoxLayout()
        row.setContentsMargins(14, 4, 14, 14)
        row.setSpacing(8)
        # Cancel is the safe, neutral choice; Shut Down is red.
        for label, slot, obj, icon, color in (
                (tr("Cancel"),    self.reject,                        "exitCancelBtn",
                 "x",        C["ck_navy_dark"]),
                (tr("Exit App"),  lambda: self._choose(self.EXIT),     "exitChoiceBtn",
                 "log-out",  "#FFFFFF"),
                (tr("Shut Down"), lambda: self._choose(self.POWEROFF), "exitDangerBtn",
                 "power",    "#FFFFFF")):
            b = QPushButton("  " + label)
            b.setObjectName(obj)
            b.setIcon(svg_icon(icon, color, 16))
            b.setIconSize(QSize(16, 16))
            b.setFixedHeight(40)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            row.addWidget(b, 1)
        vl.addLayout(row)

        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    def _choose(self, choice):
        self._choice = choice
        self.accept()

    @staticmethod
    def ask(parent, detail=""):
        """Show modally. Returns 'exit', 'poweroff', or 'cancel'."""
        dlg = ExitChoiceDialog(detail, parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._choice
        return ExitChoiceDialog.CANCEL


def language_button(height=34):
    """Small button showing the screen language by its own name (globe icon).

    Opens LanguageDialog; the caller connects clicked. Call
    update_language_button() after a change."""
    btn = QPushButton()
    btn.setObjectName("headerBtn")
    btn.setProperty("i18n_skip", True)     # its text is a language name
    btn.setIcon(svg_icon("globe", C["ck_navy"], 16))
    btn.setIconSize(QSize(16, 16))
    btn.setFixedHeight(height)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    update_language_button(btn)
    return btn


def update_language_button(btn):
    from .i18n import native_name, tr
    btn.setText("  " + native_name())
    btn.setToolTip(tr("Change the screen language"))


class _LanguageTile(QPushButton):
    """One language in the picker: its own name, and its English name below."""
    def __init__(self, native, english, selected, available):
        super().__init__()
        self.setObjectName("langTileOn" if selected else "langTile")
        self.setFixedHeight(52)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setEnabled(available)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 4, 10, 4)
        lay.setSpacing(6)
        names = QVBoxLayout()
        names.setSpacing(0)
        native_obj = "langNative" if available else "langNativeOff"
        for text, obj in ((native, native_obj), (english, "langEnglish")):
            lbl = QLabel(text)
            lbl.setObjectName(obj)
            lbl.setProperty("i18n_skip", True)
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            names.addWidget(lbl)
        lay.addLayout(names, 1)
        if selected:
            tick = QLabel()
            tick.setPixmap(svg_pixmap("check", C["blue"], 16))
            tick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lay.addWidget(tick, 0, Qt.AlignmentFlag.AlignVCenter)


class LanguageDialog(QDialog):
    """Pick the screen language. Every language is listed by its own name, so
    an operator finds theirs whatever language the screen is in now.

    A language whose script has no font on this device is shown but disabled."""

    COLUMNS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._choice = None
        self._build()

    def _build(self):
        from PySide6.QtWidgets import QGridLayout
        from .i18n import LANGUAGES, current, has_font, tr
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(620)
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
        hl.setSpacing(10)
        ic = QLabel()
        ic.setPixmap(svg_pixmap("globe", "#FFFFFF", 18))
        ic.setStyleSheet("background:transparent;")
        hl.addWidget(ic)
        t = QLabel(tr("Language"))
        t.setObjectName("confirmTitle")
        hl.addWidget(t)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(10)
        sub = QLabel(tr("Choose the language for this screen. It changes at once; "
                        "the camera and detection keep running."))
        sub.setObjectName("confirmDetail")
        sub.setWordWrap(True)
        body.addWidget(sub)

        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 4, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        cur = current()
        missing_font = tr("This device has no font for this language yet.")
        for i, (code, native, english, _ws, _rtl) in enumerate(LANGUAGES):
            available = code == cur or has_font(code)
            tile = _LanguageTile(native, english, code == cur, available)
            if not available:
                tile.setToolTip(missing_font)
            tile.clicked.connect(lambda _=False, c=code: self._choose(c))
            grid.addWidget(tile, i // self.COLUMNS, i % self.COLUMNS)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(grid_w)
        rows = (len(LANGUAGES) + self.COLUMNS - 1) // self.COLUMNS
        scroll.setFixedHeight(min(rows * 60, 540))     # 9 rows fit on a 768 px panel
        body.addWidget(scroll)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton(tr("CANCEL"))
        cancel.setObjectName("confirmCancel")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        body.addLayout(actions)
        vl.addLayout(body)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    def _choose(self, code):
        self._choice = code
        self.accept()

    @staticmethod
    def ask(parent):
        """Show modally. Returns the chosen language code, or None."""
        dlg = LanguageDialog(parent)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return dlg._choice
        return None
