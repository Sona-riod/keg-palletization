"""
Secondary pages: Pallet Records (table) and Settings (recent activity + camera info).

These pages are kept light; the operator page carries the real workflow.
Records shows the pallets the cloud has confirmed and the reports still waiting
to upload. Settings shows the operator's recent activity in plain words (the
technical log goes to logs/app.log for support), device identity, the operator
on the line, printer status and the camera controls. Camera Focus shows the
lens position and the focus curve for the automatic QR focus and manual focus
(double-tap) on the live feed, and has RESET AUTO-FOCUS (the camera's own reset +
auto-focus, the last two steps of the start-up sequence). See modules/focus.py.
"""

import json
import logging
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Slot, QTimer, Signal, QSize, QRectF
from PySide6.QtGui import QColor, QImage, QPixmap, QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTableWidgetItem, QHeaderView, QSizePolicy,
    QFrame, QLineEdit, QSpinBox, QMessageBox, QScrollArea, QStackedWidget,
    QDialog, QGraphicsDropShadowEffect,
)

import config
from .i18n import tr, N_, notifier
from .theme import C, IS_DARK
from .widgets import (Card, SectionHeader, SearchableCombo, ArrowCombo, HeaderMeta,
                      field_label, header_button, make_table, IconLabel, ActivityList,
                      ConfirmDialog, svg_icon, svg_pixmap, caution_note, FocusCurve,
                      FocusRangeChart, StackMap, CONTROL_H, CARD_MARGINS)
from .workers import (PrintWorker, FocusWorker, FocusSearchWorker, FocusReadWorker,
                      FocusMoveWorker, FocusStepWorker, LayerFocusWorker, BeerTypeFetcher,
                      OperatorFetcher, WebcamFocusWorker)
from .maintenance import authorize as authorize_maintenance
from .lighting_card import CameraLightCard
from .exposure_card import CameraExposureCard
# Technical lines for support go to logs/app.log via stderr, not on screen.
_tech_log = logging.getLogger("activity")

# Status words in the tables. The light theme keeps Qt's dark tones; on the
# dark theme those are unreadable, so brighter ones are used there.
if IS_DARK:
    _OK, _WAIT, _BAD, _NONE = (QColor(C["green"]), QColor(C["amber"]),
                               QColor("#F87171"), QColor(C["slate"]))
else:
    _OK, _WAIT, _BAD, _NONE = (Qt.GlobalColor.darkGreen, Qt.GlobalColor.darkYellow,
                               Qt.GlobalColor.darkRed, Qt.GlobalColor.darkGray)


# The auto-focus button is its own progress indicator: these two captions are
# all the feedback while the lens hunts.
FOCUS_BTN_IDLE = N_("RESET AUTO-FOCUS")
FOCUS_BTN_BUSY = N_("FOCUSING…")


FOCUS_BTN_TIP = N_("Reset the lens and run the camera's own auto-focus — the same "
                   "focus the camera performs when the application starts. Keep a "
                   "pallet in view while it runs.")


def _page_title(text):
    """Page heading. Fixed height so the first card starts at the same place on every page."""
    lbl = QLabel(text)
    lbl.setObjectName("pageTitle")
    lbl.setFixedHeight(34)
    return lbl


def _page_header(text, page):
    """Back button and page title.

    Back emits page.back_requested; the main window returns to the previous page."""
    row = QHBoxLayout()
    row.setSpacing(12)
    back = QPushButton(tr("  Back"))
    back.setObjectName("backBtn")
    back.setIcon(svg_icon("arrow-left", C["ck_navy"], 16))
    back.setIconSize(QSize(16, 16))
    back.setFixedHeight(34)
    back.setCursor(Qt.CursorShape.PointingHandCursor)
    back.setToolTip(tr("Back to the previous page"))
    back.clicked.connect(page.back_requested.emit)
    row.addWidget(back)
    row.addWidget(_page_title(text))
    row.addStretch()
    return row


def _card(title):
    """A Card with its SectionHeader. Returns (card, header, layout)."""
    card = Card()
    cl = QVBoxLayout(card)
    cl.setContentsMargins(0, 0, 0, 0)
    cl.setSpacing(0)
    header = SectionHeader(title)
    cl.addWidget(header)
    return card, header, cl


def _period_bounds(key):
    """(since, until) for a history period filter as stored-format strings;
    either may be None (no limit). Stored times sort as text, so strings compare."""
    if key in (None, "all"):
        return None, None
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S"
    if key == "yesterday":
        return (today - timedelta(days=1)).strftime(fmt), today.strftime(fmt)
    days = {"today": 0, "7d": 6, "30d": 29}.get(key, 0)
    return (today - timedelta(days=days)).strftime(fmt), None


def _note_row(text, kind="warning"):
    """A caution note padded like a card body, for under a table."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(16, 8, 16, 10)
    lay.addWidget(caution_note(text, kind))
    return box


def _card_body(layout_cls=QVBoxLayout):
    """The padded body of a card, the same on every card."""
    body = QWidget()
    lay = layout_cls(body)
    lay.setContentsMargins(*CARD_MARGINS)
    return body, lay


class BatchHistoryDialog(QDialog):
    """Pallets the cloud confirmed on this camera, with the batch it recorded.

    The same details Keg Management shows on its Pallets page and pallet
    details, kept in batch_history.db (modules/batch_history.py), so the list
    survives restarts. Read once when opened; costs nothing while closed.
    The filters (batch or pallet ID text, period, operator) narrow the list, and
    the line under them totals the pallets and kegs shown.
    """
    COLS = [N_("Received"), N_("Batch"), N_("Pallet ID"), N_("Stack"), N_("Layer"),
            N_("Kegs"), N_("Operator")]
    _BATCH_COL = 1
    # Period filter: key -> caption. Days counts today, so "7d" is today and
    # the six days before it.
    _PERIODS = [("all", N_("All dates")), ("today", N_("Today")),
                ("yesterday", N_("Yesterday")), ("7d", N_("Last 7 days")),
                ("30d", N_("Last 30 days"))]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        from modules import batch_history, stack_store
        self._rows = batch_history.recent()
        stacked = stack_store.layers_by_session()
        for r in self._rows:
            layer = stacked.get(r["session_id"], {})
            r["stack_id"] = layer.get("stack_id", "")
            r["keg_ids"] = layer.get("keg_ids", [])
        self._build()
        self._apply_filter()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(1110)
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
        ic.setPixmap(svg_pixmap("clock", "#FFFFFF", 18))
        ic.setStyleSheet("background:transparent;")
        hl.addWidget(ic)
        t = QLabel(tr("Batch History"))
        t.setObjectName("confirmTitle")
        hl.addWidget(t)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(10)
        sub = QLabel(tr("Pallets Keg Management confirmed from this camera, with the "
                        "batch it recorded on their kegs. Kept for {n} days, also after "
                        "a restart. Point at a row for its times.",
                        n=config.BATCH_HISTORY_KEEP_DAYS))
        sub.setObjectName("confirmDetail")
        sub.setWordWrap(True)
        body.addWidget(sub)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(tr("Search batch, pallet or stack ID"))
        self._filter.setFixedHeight(CONTROL_H)
        self._filter.setFixedWidth(260)
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        row.addWidget(self._filter)

        self._period = ArrowCombo()
        self._period.setFixedHeight(CONTROL_H)
        self._period.setFixedWidth(190)
        for key, caption in self._PERIODS:
            self._period.addItem(tr(caption), key)
        self._period.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._period)

        self._operator = ArrowCombo()
        self._operator.setFixedHeight(CONTROL_H)
        self._operator.setFixedWidth(200)
        self._operator.addItem(tr("All operators"), None)
        for name in sorted({r["operator"] for r in self._rows if r["operator"]},
                           key=str.lower):
            self._operator.addItem(name, name)
        self._operator.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._operator)

        reset = QPushButton(tr("Reset filters"))
        reset.setObjectName("ghostBtn")
        reset.setFixedHeight(CONTROL_H)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setToolTip(tr("Show every pallet again"))
        reset.clicked.connect(self._reset_filters)
        row.addWidget(reset)
        row.addStretch()
        body.addLayout(row)

        self._summary = QLabel()
        self._summary.setObjectName("confirmDetail")
        body.addWidget(self._summary)

        self._table = make_table([tr(c) for c in self.COLS])
        self._table.setFixedHeight(420)
        self._table.setWordWrap(False)
        # Fixed-format columns get their width; Batch and Operator share the rest.
        hh = self._table.horizontalHeader()
        for col, width in ((0, 170), (2, 230), (3, 210), (4, 70), (5, 90)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._table.setColumnWidth(col, width)
        body.addWidget(self._table)

        actions = QHBoxLayout()
        actions.addStretch()
        close = QPushButton(tr("CLOSE"))
        close.setObjectName("confirmCancel")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        body.addLayout(actions)
        vl.addLayout(body)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    @Slot()
    def _reset_filters(self):
        for w in (self._filter, self._period, self._operator):
            w.blockSignals(True)
        self._filter.clear()
        self._period.setCurrentIndex(0)
        self._operator.setCurrentIndex(0)
        for w in (self._filter, self._period, self._operator):
            w.blockSignals(False)
        self._apply_filter()

    @Slot()
    def _apply_filter(self):
        wanted = self._filter.text().strip().lower()
        since, until = _period_bounds(self._period.currentData())
        operator = self._operator.currentData()
        rows = [r for r in self._rows
                if (not wanted or wanted in str(r["batch"]).lower()
                    or wanted in str(r["pallet_id"]).lower()
                    or wanted in str(r["stack_id"]).lower())
                and (since is None or r["received_at"] >= since)
                and (until is None or r["received_at"] < until)
                and (operator is None or r["operator"] == operator)]
        self._table.setRowCount(0)
        kegs = 0
        for r in rows:
            kegs += r["palletised"] if r["palletised"] is not None else r["kegs_sent"]
            self._append(r)
        pallets = (tr("{n} pallet", n=len(rows)) if len(rows) == 1
                   else tr("{n} pallets", n=len(rows)))
        kegs = tr("{n} keg", n=kegs) if kegs == 1 else tr("{n} kegs", n=kegs)
        self._summary.setText(pallets + "  ·  " + kegs)

    def _append(self, r):
        sent, done = r["kegs_sent"], r["palletised"]
        kegs = (tr("{done} of {sent}", done=done, sent=sent)
                if done is not None and done != sent else str(sent if done is None else done))
        row = self._table.rowCount()
        self._table.insertRow(row)
        values = [r["received"], r["batch"] or "—", r["pallet_id"] or "—",
                  r["stack_id"] or "—", r["layer"], kegs, r["operator"] or "—"]

        tip = [tr("Scanned: {time}", time=r["scanned"] or "—"),
               tr("Sent: {time}", time=r["sent"] or "—"),
               tr("Received: {time}", time=r["received"] or "—")]
        where = " · ".join(x for x in (r["camera_name"],
                                       tr("Line {n}", n=r["line_number"])
                                       if r["line_number"] else "") if x)
        if where:
            tip.append(where)
        if r["keg_ids"]:
            tip.append(tr("Kegs on this layer: {codes}", codes=", ".join(r["keg_ids"])))
        if r["batch_differs"]:
            tip.append(tr("Batch sent was {batch}; Keg Management recorded the one shown.",
                          batch=r["batch_sent"]))
        if r["unmatched"]:
            tip.append(tr("Not matched to a keg: {codes}",
                          codes=", ".join(str(c) for c in r["unmatched"][:10])))
        if r["changed"] == 0:
            tip.append(tr("Repeat report: nothing changed in Keg Management."))
        tip = "\n".join(tip)

        for c, v in enumerate(values):
            item = QTableWidgetItem(str(v))
            item.setToolTip(tip)
            if c == self._BATCH_COL and r["batch_differs"]:
                item.setForeground(_BAD)
            self._table.setItem(row, c, item)

    @staticmethod
    def show_for(parent):
        dlg = BatchHistoryDialog(parent)
        dlg.exec()
        dlg.deleteLater()


class StackHistoryDialog(QDialog):
    """Stacks built on this camera: how many each day, and what is in each one.

    Kept in stack_history.db (modules/stack_store.py), so the list survives
    restarts, and it includes layers still waiting to upload. Read once when
    opened. Tap a day to list only its stacks, and a stack to see its layers;
    the details are on screen rather than in tooltips because the camera's
    display is a touch screen.
    """
    DAY_COLS = [N_("Date"), N_("Stacks built"), N_("Complete"), N_("Short"),
                N_("In progress"), N_("Pallets"), N_("Kegs")]
    STACK_COLS = [N_("Started"), N_("Stack"), N_("Status"), N_("Layers"),
                  N_("Pallets"), N_("Kegs")]
    _STATUS = {"open": N_("In progress"), "complete": N_("Complete"), "short": N_("Short")}
    _STATUS_COLOR = {"open": _WAIT, "complete": _OK, "short": _BAD}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        from modules import stack_store
        self._store = stack_store
        self._rows = stack_store.recent_stacks()
        self._days = []          # rows of the day table, in order
        self._shown = []         # rows of the stack table, in order
        self._build()
        self._apply_filter()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(940)
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
        ic.setPixmap(svg_pixmap("clock", "#FFFFFF", 18))
        ic.setStyleSheet("background:transparent;")
        hl.addWidget(ic)
        t = QLabel(tr("Stack History"))
        t.setObjectName("confirmTitle")
        hl.addWidget(t)
        hl.addStretch()
        vl.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 16)
        body.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(12)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText(tr("Search stack, pallet or keg ID"))
        self._filter.setFixedHeight(CONTROL_H)
        self._filter.setFixedWidth(280)
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        row.addWidget(self._filter)

        self._period = ArrowCombo()
        self._period.setFixedHeight(CONTROL_H)
        self._period.setFixedWidth(190)
        for key, caption in BatchHistoryDialog._PERIODS:
            self._period.addItem(tr(caption), key)
        self._period.currentIndexChanged.connect(self._apply_filter)
        row.addWidget(self._period)

        reset = QPushButton(tr("Reset filters"))
        reset.setObjectName("ghostBtn")
        reset.setFixedHeight(CONTROL_H)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.setToolTip(tr("Show every stack again"))
        reset.clicked.connect(self._reset_filters)
        row.addWidget(reset)
        row.addStretch()
        body.addLayout(row)

        self._summary = QLabel()
        self._summary.setObjectName("confirmDetail")
        body.addWidget(self._summary)

        self._day_table = make_table([tr(c) for c in self.DAY_COLS])
        self._day_table.setFixedHeight(120)
        self._day_table.setWordWrap(False)
        self._day_table.itemSelectionChanged.connect(self._show_stacks)
        body.addWidget(self._day_table)

        self._stack_table = make_table([tr(c) for c in self.STACK_COLS])
        self._stack_table.setFixedHeight(170)
        self._stack_table.setWordWrap(False)
        hh = self._stack_table.horizontalHeader()
        for col, width in ((0, 170), (1, 240), (2, 120)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._stack_table.setColumnWidth(col, width)
        self._stack_table.itemSelectionChanged.connect(self._show_details)
        body.addWidget(self._stack_table)

        self._details = QLabel()
        self._details.setObjectName("confirmDetail")
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        # The picked stack drawn layer by layer, every keg in its cell, beside
        # the same in words.
        detail_row = QHBoxLayout()
        detail_row.setSpacing(12)
        self._map = StackMap(full_codes=True)
        map_scroll = QScrollArea()
        map_scroll.setWidgetResizable(True)
        map_scroll.setFrameShape(QFrame.Shape.NoFrame)
        map_scroll.setFixedHeight(260)
        map_scroll.setWidget(self._map)
        detail_row.addWidget(map_scroll, 3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFixedHeight(260)
        scroll.setWidget(self._details)
        detail_row.addWidget(scroll, 2)
        body.addLayout(detail_row)

        actions = QHBoxLayout()
        actions.addStretch()
        close = QPushButton(tr("CLOSE"))
        close.setObjectName("confirmCancel")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        body.addLayout(actions)
        vl.addLayout(body)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def _date(day):
        try:
            return datetime.strptime(day, "%Y-%m-%d").strftime("%d-%m-%Y")
        except ValueError:
            return day

    @staticmethod
    def _time(stamp):
        try:
            return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").strftime("%d-%m-%Y %H:%M:%S")
        except ValueError:
            return stamp or "—"

    @staticmethod
    def _matches(stack, wanted):
        if not wanted or wanted in stack["stack_id"].lower():
            return True
        if wanted in (stack.get("cloud_stack_id") or "").lower():
            return True
        return any(wanted in layer["pallet_id"].lower()
                   or any(wanted in str(code).lower() for code in layer["keg_ids"])
                   for layer in stack["layers"])

    @staticmethod
    def _fill(table, rows):
        table.setRowCount(0)
        for values in rows:
            row = table.rowCount()
            table.insertRow(row)
            for c, v in enumerate(values):
                table.setItem(row, c, QTableWidgetItem(str(v)))

    @Slot()
    def _reset_filters(self):
        for w in (self._filter, self._period):
            w.blockSignals(True)
        self._filter.clear()
        self._period.setCurrentIndex(0)
        for w in (self._filter, self._period):
            w.blockSignals(False)
        self._apply_filter()

    def _filtered(self):
        wanted = self._filter.text().strip().lower()
        since, until = _period_bounds(self._period.currentData())
        return [s for s in self._rows
                if self._matches(s, wanted)
                and (since is None or s["started_at"] >= since)
                and (until is None or s["started_at"] < until)]

    @Slot()
    def _apply_filter(self):
        """Redraw the day table; a redraw also drops any day picked before."""
        rows = self._filtered()
        self._days = self._store.daily_summary(rows)
        self._day_table.blockSignals(True)
        self._fill(self._day_table, [
            (self._date(d["day"]), d["built"], d["complete"], d["short"], d["open"],
             d["pallets"], d["kegs"]) for d in self._days])
        self._day_table.clearSelection()
        self._day_table.blockSignals(False)
        built = sum(d["built"] for d in self._days)
        kegs = sum(d["kegs"] for d in self._days)
        self._summary.setText("  ·  ".join((
            tr("{n} stacks built", n=built),
            tr("{n} pallets", n=sum(d["pallets"] for d in self._days)),
            tr("{n} kegs", n=kegs),
            tr("Tap a day to list its stacks, and a stack to see its layers."))))
        self._show_stacks()

    @Slot()
    def _show_stacks(self):
        picked = self._day_table.currentRow() if self._day_table.selectedItems() else -1
        day = self._days[picked]["day"] if 0 <= picked < len(self._days) else None
        self._shown = [s for s in self._filtered()
                       if day is None or s["started_at"].startswith(day)]
        rows = []
        for s in self._shown:
            pallets, kegs = self._store.stack_totals(s)
            rows.append((self._time(s["started_at"]), s.get("cloud_stack_id") or s["stack_id"],
                         tr(self._STATUS.get(s["status"], s["status"])),
                         tr("{n} of {total}", n=pallets, total=s["layers_target"]),
                         pallets, kegs))
        self._stack_table.blockSignals(True)
        self._fill(self._stack_table, rows)
        for row, s in enumerate(self._shown):
            color = self._STATUS_COLOR.get(s["status"])
            if color is not None:
                self._stack_table.item(row, 2).setForeground(color)
        self._stack_table.clearSelection()
        self._stack_table.blockSignals(False)
        self._show_details()

    @Slot()
    def _show_details(self):
        picked = self._stack_table.currentRow() if self._stack_table.selectedItems() else -1
        if not 0 <= picked < len(self._shown):
            self._details.setText(tr("Tap a stack to see its layers, pallets and kegs."))
            self._map.set_stack(None)
            return
        from modules.keg_layout import stack_map_data
        stack = self._shown[picked]
        self._map.set_stack(stack_map_data(stack))
        lines = [tr("Stack {id}", id=stack.get("cloud_stack_id") or stack["stack_id"])]
        report = stack.get("report_status")
        if report == self._store.REPORT_PENDING:
            lines.append(tr("Sent as one stack: waiting for Keg Management.") +
                         (" " + stack["report_error"] if stack.get("report_error") else ""))
        elif report == self._store.REPORT_REJECTED:
            lines.append(tr("Refused by Keg Management, nothing of it was written: {error}",
                            error=stack.get("report_error") or "?"))
        elif not stack.get("cloud_stack_id"):
            lines.append(tr("No stack id from Keg Management yet."))
        for layer in stack["layers"]:
            if layer["status"] == self._store.LAYER_REJECTED:
                pallet = tr("refused by Keg Management")
            elif layer["status"] == self._store.LAYER_HELD:
                pallet = tr("kept on the camera, sent with the stack")
            else:
                pallet = layer["pallet_id"] or tr("waiting to upload")
            lines.append(tr("Layer {n}: pallet {pallet} — {count} kegs: {codes}",
                            n=layer["layer"], pallet=pallet, count=len(layer["keg_ids"]),
                            codes=", ".join(self._keg_text(c, layer) for c in layer["keg_ids"])))
        self._details.setText("\n".join(lines))

    @staticmethod
    def _keg_text(code, layer):
        """A keg with its cell, "2015-H-1000042 (row 1, col 2)"."""
        pos = (layer.get("positions") or {}).get(code)
        return (tr("{code} (row {r}, col {c})", code=code, r=pos[0], c=pos[1])
                if pos else str(code))

    @staticmethod
    def show_for(parent):
        dlg = StackHistoryDialog(parent)
        dlg.exec()
        dlg.deleteLater()


class RecordsPage(QWidget):
    """Cloud-confirmed pallets and reports still waiting to upload.

    The top table shows confirmed pallets only: a row appears once the cloud
    has acknowledged the report and issued a paletteId. A pallet shown earlier
    might not exist upstream, and the operator would wrongly believe the kegs
    are recorded.

    The middle table holds unsent reports. Hiding them would leave the operator
    unable to tell a queued pallet from one never built, and they would re-scan
    kegs that are already taken. A queued report is shown as captured, held
    locally and waiting for the network. It moves up into the confirmed table,
    with its real pallet ID, as soon as the cloud accepts it.
    """
    COLS = [N_("Pallet ID"), N_("Kegs"), N_("Batch"), N_("Status"), N_("Label"),
            N_("Created"), ""]
    _STATUS_COL = 3
    _LABEL_COL = 4
    PENDING_COLS = [N_("Local Ref"), N_("Kegs"), N_("Batch"), N_("State"),
                    N_("Captured"), ""]
    _PENDING_STATE_COL = 3
    LABEL_COLS = [N_("Pallet ID"), N_("Kegs"), N_("Batch"), N_("Label"), N_("Since"), ""]
    _LABEL_STATE_COL = 3
    # The row's action buttons live in the last column of each table, at a fixed
    # width: one button, or two (REPRINT + REMOVE / LABEL OK).
    _ACTION_W = 124
    _ACTION_W2 = 244

    # Print-queue job state -> what the Label column says, and its colour.
    _LABEL_TEXT = {
        "pending":  (N_("WAITING"),   _WAIT),
        "printing": (N_("PRINTING"),  _WAIT),
        "printed":  (N_("PRINTED"),   _OK),
        "failed":   (N_("FAILED"),    _BAD),
        "check":    (N_("CHECK"),     _BAD),
        "resolved": (N_("OK"),        _OK),
        "replaced": (N_("REPRINTED"), _OK),
        "removed":  (N_("REMOVED"),   _NONE),
    }

    # Row action key -> button caption.
    _ACTION_TEXT = {"REPRINT": N_("Reprint"), "REMOVE": N_("Remove"),
                    "DISCARD": N_("Discard"), "LABEL OK": N_("Label OK")}

    # A removed pallet is gone from this device only. Emitted so the operator
    # page can rebuild its already-palletized keg map (the removed pallet's kegs
    # become available again locally).
    pallet_removed = Signal(str)     # session_id
    back_requested = Signal()        # page Back button
    toast = Signal(str, str)         # (message, kind) feedback pop-up
    # REPRINT pressed: (pallet_id, session_id, batch, keg_count) -> operator page,
    # which hands it to the one print thread.
    reprint_requested = Signal(str, str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._database = None
        self._print_queue = None
        self._label_status = {}          # session_id -> newest label job state

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addLayout(_page_header(tr("Pallet Records"), self))

        # Each card carries its own count in its header, the same way.
        card, header, cl = _card(tr("Pallets Confirmed By Cloud"))
        self._count_lbl = HeaderMeta(tr("{n} pallets", n=0))
        header.add_right(self._count_lbl)
        # The table below starts empty after a restart; the history does not.
        history = header_button(tr("batch history"),
                                tr("Every pallet confirmed on this camera with its batch, "
                                   "also from before the last restart."), icon="clock")
        history.clicked.connect(lambda: BatchHistoryDialog.show_for(self))
        header.add_right(history)
        stacks = header_button(tr("stack history"),
                               tr("Stacks built on this camera, how many each day, and the "
                                  "pallets and kegs on each layer."), icon="clock")
        stacks.clicked.connect(lambda: StackHistoryDialog.show_for(self))
        header.add_right(stacks)
        self._table = self._make_table(self.COLS, self._ACTION_W2)
        cl.addWidget(self._table)
        cl.addWidget(_note_row(tr("Remove only clears the record on this camera — the "
                                  "pallet stays in Keg Management."), "info"))
        root.addWidget(card, 3)

        card, header, cl = _card(tr("Waiting To Upload"))
        self._pending_count_lbl = HeaderMeta(tr("{n} waiting", n=0))
        header.add_right(self._pending_count_lbl)
        self._pending_table = self._make_table(self.PENDING_COLS)
        cl.addWidget(self._pending_table)
        cl.addWidget(_note_row(tr("Saved pallets are sent automatically. Discard only if "
                                  "your supervisor asks — its kegs are then on no "
                                  "pallet.")))
        root.addWidget(card, 2)

        # Kept in print_queue.db, so this survives a restart even though the
        # pallet tables above start empty after the local-data wipe.
        card, header, cl = _card(tr("Labels Not Printed Yet"))
        self._label_count_lbl = HeaderMeta(tr("{n} labels", n=0))
        header.add_right(self._label_count_lbl)
        clear = header_button(tr("clear queue"),
                              tr("Take every label not printed yet off the print queue."),
                              icon="trash")
        clear.clicked.connect(self._confirm_clear_labels)
        header.add_right(clear)
        self._label_table = self._make_table(self.LABEL_COLS, self._ACTION_W2)
        cl.addWidget(self._label_table)
        cl.addWidget(_note_row(tr("Look at the label on the pallet before pressing Label "
                                  "OK.")))
        root.addWidget(card, 2)

        # Table cells hold translated status words; re-read them in the new language.
        notifier().changed.connect(lambda _code: self.refresh_all())

    def _make_table(self, cols, action_w=None):
        """A records table with a fixed-width last column for the row's action
        button(s)."""
        table = make_table([tr(c) for c in cols])
        last = len(cols) - 1
        table.horizontalHeader().setSectionResizeMode(
            last, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(last, action_w or self._ACTION_W)
        return table

    def attach_backend(self, database, print_queue=None):
        """Called once the backend is ready; loads everything from the DB.

        Both pallet tables are DB-backed, not session memory: a pallet confirmed
        yesterday is still a pallet, and a report queued when the app was last
        closed is still waiting to go. It also gives the remove button the
        persisted records to act on.
        """
        self._database = database
        self._print_queue = print_queue
        self.refresh_all()

    @Slot()
    def refresh_all(self):
        """Re-read the pallet tables and the label table."""
        self._load_pallets()
        self.refresh_pending()
        self.refresh_labels()

    def _load_pallets(self):
        if self._database is None:
            return
        try:
            pallets = self._database.get_pallets()
        except Exception:
            return
        try:
            self._label_status = (self._print_queue.label_status_by_session()
                                  if self._print_queue is not None else {})
        except Exception:
            self._label_status = {}
        self._table.setRowCount(0)
        # get_pallets() is newest-first; insert reversed so the newest sits at
        # the bottom, where a live confirmation appends it.
        for p in reversed(pallets):
            self._append_pallet(p)
        self._refresh_pallet_count()
        self._table.scrollToBottom()

    @Slot()
    def refresh_pending(self):
        """Re-read the pending table; attempt counts and next-retry times change."""
        if self._database is None:
            return
        try:
            pending = self._database.get_pending_pallets()
        except Exception:
            return
        self._pending_table.setRowCount(0)
        for p in pending:          # already oldest-first: the delivery order
            self._append_pending(p)
        self._refresh_pending_count()

    @Slot()
    def refresh_labels(self):
        """Re-read the labels still to print / needing a decision."""
        if self._print_queue is None:
            return
        try:
            jobs = self._print_queue.jobs_needing_attention()
        except Exception:
            return
        self._label_table.setRowCount(0)
        for job in jobs:           # oldest first: the print order
            self._append_label_job(job)
        n = self._label_table.rowCount()
        self._label_count_lbl.setText(tr("{n} label", n=n) if n == 1
                                      else tr("{n} labels", n=n))

    # Row helpers
    def _row_for_session(self, table, session_id):
        """The row currently holding `session_id`, or None.

        Looked up by id rather than by a remembered index because rows shift
        whenever one is removed or promoted out of the pending table.
        """
        if not session_id:
            return None
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == session_id:
                return row
        return None

    def _fill_row(self, table, row, values, session_id, colour_col=None, colour=None):
        for c, v in enumerate(values):
            item = QTableWidgetItem(str(v))
            if c == 0:
                # The session id is the handle for remove/promote; it is not
                # shown as a column of its own.
                item.setData(Qt.ItemDataRole.UserRole, session_id)
            if colour is not None and c == colour_col:
                item.setForeground(colour)
            table.setItem(row, c, item)

    def _add_action_button(self, table, row, label, tooltip, handler, session_id):
        self._add_actions(table, row, [(label, tooltip, lambda s=session_id: handler(s),
                                        True)])

    def _add_actions(self, table, row, actions):
        """Put buttons in the row's last column.

        actions: [(label, tooltip, callback, danger)]. Each callback is bound to
        its record, not the row index, which goes stale as soon as another row
        is removed.
        """
        cell = QWidget()
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(6)
        icons = {"REPRINT": "printer", "REMOVE": "trash", "DISCARD": "trash",
                 "LABEL OK": "check"}
        for label, tooltip, callback, danger in actions:
            text = tr(self._ACTION_TEXT.get(label, label.capitalize()))
            btn = QPushButton("  " + text)
            btn.setObjectName("dangerBtn" if danger else "ghostBtn")
            icon = icons.get(label)
            if icon:
                btn.setIcon(svg_icon(icon, C["red_dark"] if danger else C["ck_navy"], 15))
                btn.setIconSize(QSize(15, 15))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(tooltip)
            btn.setFixedHeight(32)
            # The global 8px/16px button padding left no room for the text in a row.
            btn.setStyleSheet("padding:0 10px; font-size:12px;")
            btn.clicked.connect(lambda _=False, cb=callback: cb())
            lay.addWidget(btn)
        # Wrapped so the buttons are centred in the row instead of pinned to its top.
        table.setCellWidget(row, table.columnCount() - 1, cell)

    def _append_pallet(self, p, label_state=None):
        session_id = p.get("session_id", "")
        pallet_id = p.get("pallet_id", "")
        state = label_state or self._label_status.get(session_id)
        label_text, label_colour = self._LABEL_TEXT.get(
            state, ("—", _NONE))
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._fill_row(
            self._table, row,
            [pallet_id, p.get("keg_count", ""), p.get("batch", ""),
             tr(p.get("status", N_("CREATED"))), tr(label_text),
             p.get("created_at", ""), ""],
            session_id,
            colour_col=self._STATUS_COL, colour=_OK)
        item = self._table.item(row, self._LABEL_COL)
        if item:
            item.setForeground(label_colour)

        actions = []
        if pallet_id and self._print_queue is not None:
            actions.append((
                "REPRINT", tr("Print this pallet's label again (it joins the print "
                              "queue and prints in turn)."),
                lambda p=p: self._request_reprint(
                    p.get("pallet_id", ""), p.get("session_id", ""),
                    p.get("batch", ""), p.get("keg_count", "")),
                False))
        actions.append((
            "REMOVE", tr("Remove this pallet's record from THIS DEVICE. The cloud keeps "
                         "its copy — there is no delete on the pallet API."),
            lambda s=session_id: self._confirm_remove_pallet(s), True))
        self._add_actions(self._table, row, actions)

    def _append_label_job(self, job):
        state = job.get("status", "")
        label_text, colour = self._LABEL_TEXT.get(state, (state.upper(),
                                                          _NONE))
        label_text = tr(label_text)
        if state == "pending" and job.get("attempts"):
            label_text = tr("RETRYING ({n})", n=job['attempts'])
        row = self._label_table.rowCount()
        self._label_table.insertRow(row)
        self._fill_row(
            self._label_table, row,
            [job.get("pallet_id", ""), job.get("keg_count", ""), job.get("batch", ""),
             label_text, job.get("created_at", ""), ""],
            job.get("session_id", ""),
            colour_col=self._LABEL_STATE_COL, colour=colour)

        tip = {
            "pending":  N_("Waiting its turn, or waiting for the printer — it prints "
                           "by itself."),
            "printing": N_("Printing now."),
            "failed":   N_("The printer failed this label several times. Fix the "
                           "printer, then REPRINT."),
            "check":    N_("The app stopped while this label was printing — it may be "
                           "missing or cut off. Look at the pallet: REPRINT if it is "
                           "bad, LABEL OK if it is fine."),
        }.get(state, "")
        tip = tr(tip)
        if job.get("error") and state != "printing":
            tip = (tip + "\n" + tr("Last error: {error}", error=job['error'])).strip()
        for c in range(self._label_table.columnCount() - 1):
            item = self._label_table.item(row, c)
            if item:
                item.setToolTip(tip)

        def reprint(j=job):
            self._request_reprint(j.get("pallet_id", ""), j.get("session_id", ""),
                                  j.get("batch", ""), j.get("keg_count", ""))
        if state in ("failed", "check"):
            self._add_actions(self._label_table, row, [
                ("REPRINT", tr("Print this label again."), reprint, False),
                ("LABEL OK", tr("The label on the pallet is fine — clear this entry."),
                 lambda j=job: self._label_ok(j), False),
            ])
        elif state == "pending":
            self._add_actions(self._label_table, row, [
                ("REPRINT", tr("Queue this label again and check the printer now. The "
                               "waiting copy is replaced, so it prints only once."),
                 reprint, False),
                ("REMOVE", tr("Take this label off the print queue. It will not be "
                              "printed."),
                 lambda j=job: self._confirm_remove_label(j), True),
            ])

    def _request_reprint(self, pallet_id, session_id, batch, keg_count):
        if not pallet_id:
            return
        self.reprint_requested.emit(str(pallet_id), str(session_id or ""),
                                    str(batch or ""), str(keg_count or ""))
        # The job is handed over asynchronously; re-read once it is saved.
        QTimer.singleShot(1000, self.refresh_all)

    def _label_ok(self, job):
        if self._print_queue is None:
            return
        if not ConfirmDialog.ask(
                self, tr("Label OK"),
                tr("Is the label on pallet {pallet} printed correctly?",
                   pallet=job.get('pallet_id', '')),
                tr("Only confirm after checking the label on the pallet. If it is "
                   "missing or damaged, press REPRINT instead."),
                confirm_text=tr("LABEL OK")):
            return
        self._print_queue.mark_label_ok(job.get("id"))
        self.toast.emit(tr("Label for pallet {pallet} marked OK",
                           pallet=job.get('pallet_id', '')), "success")
        QTimer.singleShot(1000, self.refresh_labels)

    def _confirm_remove_label(self, job):
        """Take one waiting label off the print queue, on confirmation."""
        if self._print_queue is None:
            return
        pallet = job.get("pallet_id", "")
        if not ConfirmDialog.ask(
                self, tr("Remove Label"),
                tr("Remove the label for pallet {pallet} from the print queue?",
                   pallet=pallet),
                tr("It will not be printed. The pallet itself is not changed — you "
                   "can still print its label later with REPRINT."),
                confirm_text=tr("REMOVE"), danger=True):
            return
        self._print_queue.remove_job(job.get("id"))
        self.toast.emit(tr("Label for pallet {pallet} removed from the print queue",
                           pallet=pallet), "success")
        QTimer.singleShot(1000, self.refresh_all)

    def _confirm_clear_labels(self):
        """Take every label not printed yet off the print queue, on confirmation."""
        if self._print_queue is None:
            return
        n = self._label_table.rowCount()
        if not n:
            self.toast.emit(tr("The print queue is already empty"), "info")
            return
        if not authorize_maintenance(self, tr("Clear the print queue")):
            return
        if not ConfirmDialog.ask_twice(
                self, tr("Clear Print Queue"),
                tr("Remove all {n} label(s) from the print queue?", n=n),
                tr("None of them will be printed. The pallets themselves are not "
                   "changed — a label can still be printed later with REPRINT. A "
                   "label printing right now is left to finish."),
                confirm_text=tr("CLEAR"),
                final_message=tr("Clear the print queue now?"),
                final_detail=tr("This cannot be undone.")):
            return
        self._print_queue.clear_queue()
        self.toast.emit(tr("Print queue cleared"), "success")
        QTimer.singleShot(1000, self.refresh_all)

    def _append_pending(self, p):
        session_id = p.get("session_id", "")
        queued = p.get("queued", True)
        attempts = p.get("attempts", 0) or 0
        error = p.get("error", "")

        if not queued:
            state, colour = tr("NEEDS ATTENTION"), _BAD
        elif attempts <= 0:
            state, colour = tr("WAITING"), _WAIT
        else:
            state, colour = tr("RETRYING ({n})", n=attempts), _WAIT

        row = self._pending_table.rowCount()
        self._pending_table.insertRow(row)
        self._fill_row(
            self._pending_table, row,
            [session_id, p.get("keg_count", ""), p.get("batch", ""),
             state, p.get("created_at", ""), ""],
            session_id, colour_col=self._PENDING_STATE_COL, colour=colour)

        # The tooltip shows the error and when the next attempt is due. The
        # queue keeps retrying, so "waiting" does not mean stuck.
        tip = error or (tr("Waiting for the network.") if queued else "")
        nxt = p.get("next_retry")
        if queued and nxt:
            tip = (tip + "\n" + tr("Next attempt due: {time}", time=nxt)).strip()
        if not queued:
            tip = (tip + "\n" + tr("The cloud rejected this report permanently — it is "
                                   "NOT being retried and needs someone to look at "
                                   "it.")).strip()
        if tip:
            for c in range(self._pending_table.columnCount() - 1):
                item = self._pending_table.item(row, c)
                if item:
                    item.setToolTip(tip)

        self._add_action_button(
            self._pending_table, row, "DISCARD",
            tr("Give up on this report and delete it. Nothing was ever sent to the "
               "cloud, so nothing upstream changes."),
            self._confirm_discard_pending, session_id)

    def _refresh_pallet_count(self):
        n = self._table.rowCount()
        self._count_lbl.setText(tr("{n} pallet", n=n) if n == 1
                                else tr("{n} pallets", n=n))

    def _refresh_pending_count(self):
        n = self._pending_table.rowCount()
        self._pending_count_lbl.setText(tr("{n} waiting", n=n))

    # Removal
    def _confirm_remove_pallet(self, session_id):
        """Remove a confirmed pallet's record from this device, on confirmation.

        The warning says the cloud copy survives, because the operator can't
        see or undo that from here.
        """
        row = self._row_for_session(self._table, session_id)
        label = ""
        if row is not None:
            item = self._table.item(row, 0)
            label = item.text() if item else ""

        if not ConfirmDialog.ask_twice(
                self, tr("Remove Pallet Record"),
                tr("Remove pallet {pallet} from this screen?",
                   pallet=label or session_id),
                tr("This only removes it from this camera. The pallet still exists in "
                   "Keg Management. Its kegs can then be used on a new pallet here "
                   "without a warning."),
                confirm_text=tr("REMOVE"),
                final_message=tr("Remove pallet {pallet} now?",
                                 pallet=label or session_id),
                final_detail=tr("This cannot be undone.")):
            return
        self._delete_record(self._table, session_id, self._refresh_pallet_count)

    def _confirm_discard_pending(self, session_id):
        """Discard a report that never reached the cloud, on confirmation."""
        if not ConfirmDialog.ask_twice(
                self, tr("Discard Saved Pallet"),
                tr("Discard this saved pallet?"),
                tr("It was never sent, so its kegs will NOT be on any pallet and must "
                   "be scanned again. If you are only waiting for the internet, leave "
                   "it — it is sent automatically."),
                confirm_text=tr("DISCARD"),
                final_message=tr("Discard it now? Its kegs must be scanned again."),
                final_detail=tr("This cannot be undone.")):
            return
        self._delete_record(self._pending_table, session_id,
                            self._refresh_pending_count)

    def _delete_record(self, table, session_id, recount):
        """Delete from the DB first, and drop the row only if that worked.

        Otherwise the record would vanish from screen but still be in the
        database, and come back on the next refresh.
        """
        if self._database is None:
            return
        try:
            ok = self._database.delete_pallet(session_id)
        except Exception as e:
            QMessageBox.critical(self, tr("Remove failed"),
                                 tr("Could not remove {session}:", session=session_id)
                                 + f"\n{e}")
            return
        if not ok:
            QMessageBox.warning(self, tr("Nothing removed"),
                                tr("No record found for {session}.", session=session_id)
                                + " " + tr("It may already have been removed."))
        row = self._row_for_session(table, session_id)
        if row is not None:
            table.removeRow(row)
        recount()
        self.pallet_removed.emit(session_id)

    @Slot(dict)
    def add_pallet(self, p):
        """A pallet the cloud has just confirmed.

        Also clears the report from the pending table. A confirmation can arrive
        for a report queued minutes or hours ago, and leaving it listed as
        waiting would double-count the same kegs on screen.
        """
        session_id = p.get("session_id", "")
        if self._row_for_session(self._table, session_id) is not None:
            return                              # already listed; don't duplicate
        self._drop_pending_row(session_id)
        # Its label was handed to the print queue a moment ago; the next
        # refresh shows its real state.
        self._append_pallet(p, label_state="pending" if p.get("pallet_id") else None)
        self._refresh_pallet_count()
        self._table.scrollToBottom()

    @Slot(dict)
    def add_pending(self, p):
        """A report that could not be delivered and is now queued."""
        if self._row_for_session(self._pending_table, p.get("session_id", "")) is not None:
            return
        self._append_pending(p)
        self._refresh_pending_count()
        self._pending_table.scrollToBottom()

    def _drop_pending_row(self, session_id):
        row = self._row_for_session(self._pending_table, session_id)
        if row is not None:
            self._pending_table.removeRow(row)
            self._refresh_pending_count()


class _SectionButton(QPushButton):
    """One entry in the Settings side panel: icon, title, a one-line hint and
    an optional status dot (e.g. green / red for the label printer)."""
    def __init__(self, icon, title, hint, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsNavBtn")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(62)
        self.setToolTip(hint)
        self._icon_name = icon
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 12, 8)
        lay.setSpacing(12)
        self._icon = QLabel()
        self._icon.setFixedSize(20, 20)
        # Title and hint kept together, centred in the button's height.
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self._title = QLabel(title)
        self._hint = QLabel(hint)
        self._hint.setObjectName("settingsNavHint")
        self._hint.setWordWrap(True)
        texts.addStretch(1)
        texts.addWidget(self._title)
        texts.addWidget(self._hint)
        texts.addStretch(1)
        self._dot = QLabel()
        self._dot.setFixedSize(10, 10)
        self._dot.setVisible(False)
        lay.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(texts, 1)
        lay.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        # The labels are decoration: taps go to the button.
        for w in (self._icon, self._title, self._hint, self._dot):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            w.setStyleSheet(w.styleSheet() + "background:transparent;")
        self.toggled.connect(self._apply)
        self._apply(False)

    def _apply(self, active):
        color = C["ck_navy"] if active else C["text2"]
        self._icon.setPixmap(svg_pixmap(self._icon_name, color, 20))
        self._title.setStyleSheet(
            f"background:transparent; font-size:14px; font-weight:"
            f"{700 if active else 600}; color:{C['ck_navy'] if active else C['text1']};")

    def set_status(self, color):
        """A small dot after the title; None hides it."""
        if color:
            self._dot.setPixmap(svg_pixmap("dot", color, 10))
        self._dot.setVisible(bool(color))


class SettingsPage(QWidget):
    """Activity log, system/camera info and the operator's lens-focus control.

    Also holds the shift-level settings that are set once and left alone:
    device identity, who is on the line (Operator), and the beer-type feature.
    """

    # Settings -> Operator. These are the only signals flowing this way; every
    # other cross-page signal originates on the operator page.
    beer_type_changed = Signal(bool)     # feature enabled / disabled
    beer_types_loaded = Signal(object)   # {name: _id}
    operator_changed  = Signal(str, str)  # (id, name); "" / "" when cleared
    # A new camera MAC was verified and saved: the app must restart so every
    # module picks it up (they copy config.CAMERA_MAC_ID at import).
    camera_mac_changed = Signal(str)
    back_requested = Signal()        # page Back button
    toast = Signal(str, str)         # (message, kind) feedback pop-up
    # Manual-focus marker on the operator page's live feed: full-frame x, y,
    # "busy" / "ok" / "fail", and the box actually focused on (x1, y1, x2, y2,
    # full-frame pixels) once known, else None.
    focus_marker = Signal(float, float, str, object)
    # Automatic layer refocus finished: layer, ok, lens position (-1 unknown),
    # message. ok means the codes read at that position.
    layer_focus_done = Signal(int, bool, int, str)
    # The lens position a layer was read at, found just before a step to the
    # next layer: layer, lens position.
    layer_lens_known = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Focus control is built now without a camera, because the card exists
        # before InitWorker starts one. The REST (iCam-540) path needs no camera;
        # the webcam path becomes available in attach_backend().
        try:
            from modules.focus import FocusController
            self._focus = FocusController()
        except Exception as e:   # don't let this take the Settings page down
            self._focus = None
            self._focus_import_error = str(e)
        self._focus_worker = None
        self._focus_read_worker = None
        self._focus_busy = False
        self._focus_start = None      # lens position when the last search began
        self._focus_point = None      # the tap being focused on (full-frame x, y)
        self._focus_layer = None      # the layer an automatic refocus is for

        # Beer-type state. The cached catalogue is read from disk now so the
        # selector is populated at start-up, before or without a cloud fetch.
        self._api = None
        self._beer_busy = False
        self._beer_workers = []
        self._beer_fetched_at = None
        self._beer_types = self._load_cached_beer_types()

        # Operator state. Like the beer catalogue, the cached list is read from
        # disk now so the picker is populated before or without a cloud fetch.
        self._operator_busy = False
        self._operator_workers = []
        self._operators = self._load_cached_operators()

        # Title on top; below it the side panel of sections on the left and the
        # chosen section on the right. One section at a time, so each fits on
        # the screen instead of one long page of unrelated cards.
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addLayout(_page_header(tr("Settings & Diagnostics"), self))
        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        self._sections = {}           # key -> (nav button, index in the stack)
        self._section_nav = QFrame()
        self._section_nav.setObjectName("settingsNav")
        self._section_nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._section_nav.setFixedWidth(250)
        nav = QVBoxLayout(self._section_nav)
        nav.setContentsMargins(0, 8, 0, 8)
        nav.setSpacing(2)
        self._nav_layout = nav
        body.addWidget(self._section_nav, 0, Qt.AlignmentFlag.AlignTop)
        self._stack = QStackedWidget()
        body.addWidget(self._stack, 1)

        # Line setup: set once and left alone.
        # Device identity: the same software runs on every pallet camera, so the
        # operator sets this device's location and line here. Values persist to
        # device_state.json and are sent with every batch.
        # Operator: who is on the line. The cloud team says the operator "picks
        # once a shift", so it lives here rather than on the operator page.
        # Beer type: some lines report a beer type with the pallet and some
        # don't, so this is a feature the operator turns on. Disabled is the
        # default: no selector on the operator page and no beer keys in the
        # cloud payload at all.
        line = QGridLayout()
        line.setHorizontalSpacing(12)
        line.setVerticalSpacing(12)
        line.setColumnStretch(0, 1)
        line.setColumnStretch(1, 1)
        line.addWidget(self._build_identity_card(), 0, 0, 1, 2)
        line.addWidget(self._build_operator_card(), 1, 0)
        line.addWidget(self._build_beer_type_card(), 1, 1)
        line.addWidget(self._build_test_mode_card(), 2, 0)
        self._add_section("line", "sliders", tr("Line setup"),
                          tr("Location, line, operator, beer type and test mode"),
                          tr("Where this camera is and who works the line. Set once, "
                             "then left alone."), line)

        # Label printer. A pallet's QR label prints as soon as the cloud returns
        # a pallet id, so the operator needs to know up front whether a printer
        # is connected. The status card probes the print backend live; the
        # second card lists the printers that work.
        printer = QVBoxLayout()
        printer.setSpacing(12)
        printer.addWidget(self._build_printer_card())
        printer.addWidget(self._build_supported_printers_card())
        self._add_section("printer", "printer", tr("Label printer"),
                          tr("Status, test print and models"),
                          tr("The printer for the pallet QR labels. Plug in any "
                             "supported USB printer; nothing to set."), printer)

        # Camera. Focus is the one camera setting that drifts in service (a
        # knock during wash-down, a changed pallet height, a boot auto-focus
        # that locked onto an empty conveyor). Light: Flash LED mode and
        # intensity (ui_pyside/lighting_card.py). Exposure: auto-exposure range
        # (ui_pyside/exposure_card.py). The three cards are also lent to the
        # operator page's camera panel (take_camera_cards).
        self._focus_card = self._build_focus_card()
        self._light_card = CameraLightCard()
        self._light_card.activity.connect(self.add_activity)
        self._light_card.toast.connect(self.toast)
        self._light_card.log_line.connect(self._log_line)
        self._exposure_card = CameraExposureCard()
        self._exposure_card.activity.connect(self.add_activity)
        self._exposure_card.toast.connect(self.toast)
        self._exposure_card.log_line.connect(self._log_line)
        self._camera_grid = QGridLayout()
        self._camera_grid.setHorizontalSpacing(12)
        self._camera_grid.setVerticalSpacing(12)
        self._camera_grid.setColumnStretch(0, 1)
        self._camera_grid.setColumnStretch(1, 1)
        self._camera_one_column = False   # narrow screen: cards stacked (resizeEvent)
        self._camera_lent = False         # on the operator page's camera panel
        self._place_camera_cards()
        self._add_section("camera", "camera", tr("iCam settings"),
                          tr("iCam focus, light and exposure"),
                          tr("Change these only when the keg labels look blurry, dark "
                             "or too bright on the live picture. Changes go to the "
                             "iCam straight away."), self._camera_grid)

        # Cameras: which cameras are connected, which one reads each layer, and
        # the layer-1 USB webcam on / off (modules/camera_select.py). Off: the
        # iCam reads every layer, as it did before the webcam was added.
        self._camera_obj = None
        cameras = QVBoxLayout()
        cameras.setSpacing(12)
        cameras.addWidget(self._build_layer1_webcam_card())
        cameras.addWidget(self._build_connected_cameras_card())
        cameras.addWidget(self._build_layer_cameras_card())
        self._add_section("cameras", "copy", tr("Cameras & layers"),
                          tr("Which camera reads each layer"),
                          tr("The iCam reads the pallet from above. A USB webcam "
                             "plugged into the iCam can read layer 1, the layer "
                             "furthest away. Turn it off here to let the iCam read "
                             "every layer."), cameras)
        self._cameras_timer = QTimer(self)
        self._cameras_timer.setInterval(3000)
        self._cameras_timer.timeout.connect(self._refresh_cameras_if_shown)
        self._cameras_timer.start()
        self._refresh_cameras()

        # USB webcam focus: automatic focus for the layer-1 webcam. Put keg QR
        # codes where layer 1 stands and press START AUTO FOCUS: the focus is
        # moved by itself until the codes decode, then set and saved
        # (modules/webcam_focus.py).
        self._qr_detector = None
        self._wf_worker = None
        self._wf_range = None          # (min, max, step)
        self._wf_value = None          # focus the lens is at now
        self._wf_points = []           # (value, measure) tried by the auto focus
        self._wf_boxes = []            # last measured boxes, full-frame pixels
        self._wf_frame_size = None
        self._wf_last_frame = None
        self._wf_focusing = False      # an auto-focus run is going on
        wf = QVBoxLayout()
        wf.setSpacing(12)
        wf.addWidget(self._build_webcam_picture_card())
        wf.addWidget(self._build_webcam_focus_card())
        self._add_section("webcam_focus", "eye", tr("USB webcam focus"),
                          tr("Automatic focus for the layer-1 webcam"),
                          tr("Use this when layer 1 does not read. Put kegs with their "
                             "QR codes where layer 1 stands and press START AUTO "
                             "FOCUS. The webcam finds the focus where the codes "
                             "decode, sets it and saves it."), wf)
        self._wf_timer = QTimer(self)
        self._wf_timer.setInterval(100)
        self._wf_timer.timeout.connect(self._wf_draw_live)
        self._wf_update_controls()

        # Recent activity: plain-language events for the operator (pallet sent,
        # saved offline, label printed...). The technical log is not shown here;
        # it goes to logs/app.log.
        act_card, _, acl = _card(tr("Audit Log"))
        act_body, ab = _card_body()
        self._activity = ActivityList(max_items=30)
        self._activity.setMinimumHeight(220)
        ab.addWidget(self._activity)
        acl.addWidget(act_body, 1)
        activity = QVBoxLayout()
        activity.addWidget(act_card, 1)
        self._add_section("activity", "clock", tr("Audit Log"),
                          tr("What happened this shift"),
                          tr("Pallets sent, labels printed and anything that needs "
                             "attention, newest first."), activity, fill=True)

        nav.addStretch()
        self.show_section("line")

    # Side panel and sections
    def _add_section(self, key, icon, title, hint, lead, content, fill=False):
        """One entry in the side panel and its page.

        The page scrolls instead of squeezing; at kiosk resolutions squeezed
        cards make their fields and text overlap. Cards keep their own height
        unless `fill`, so no card is stretched into empty space."""
        btn = _SectionButton(icon, title, hint)
        btn.clicked.connect(lambda _=False, k=key: self.show_section(k))
        self._nav_layout.addWidget(btn)

        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("sectionTitleBig")
        lay.addWidget(heading)
        lead_lbl = QLabel(lead)
        lead_lbl.setObjectName("sectionLead")
        lead_lbl.setWordWrap(True)
        lay.addWidget(lead_lbl)
        lay.addLayout(content, 1 if fill else 0)
        if not fill:
            lay.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)
        self._sections[key] = (btn, self._stack.addWidget(scroll))

    def show_section(self, key, widgets=None):
        """Show one section. Returns `widgets`, so a tour step can open the
        section of the parts it points at: lambda: show_section("x", [...])."""
        if key in self._sections:
            for k, (btn, index) in self._sections.items():
                btn.setChecked(k == key)
                if k == key:
                    self._stack.setCurrentIndex(index)
            # Lay the page out now, so a tour can measure its parts straight away.
            current = self._stack.currentWidget()
            if current is not None and current.widget() is not None:
                current.widget().layout().activate()
            if key == "cameras":
                self._refresh_cameras()
            if key != "webcam_focus" and getattr(self, "_wf_worker", None) is not None:
                self._wf_stop()
        return widgets

    def hideEvent(self, event):  # noqa: N802 (Qt naming)
        # Leaving Settings closes the webcam focus. A run not finished is
        # stopped and the focus goes back to where it was.
        if getattr(self, "_wf_worker", None) is not None:
            self._wf_stop()
        super().hideEvent(event)

    # Cameras section (modules/camera_select.py)
    _ICAM_LABEL = N_("Advantech iCam-540")

    def _build_layer1_webcam_card(self):
        """USB webcam for layer 1 on / off. Saved (config.layer1_webcam_use)."""
        card, header, cl = _card(tr("USB Webcam For Layer 1"))
        self._webcam_meta = HeaderMeta()
        header.add_right(self._webcam_meta)
        body, b = _card_body()
        b.setSpacing(6)
        b.addWidget(field_label(tr("Read layer 1 with the USB webcam")))
        row = QHBoxLayout()
        row.setSpacing(0)
        self._webcam_on_btn = QPushButton(tr("ON (USB WEBCAM)"))
        self._webcam_off_btn = QPushButton(tr("OFF (ICAM ONLY)"))
        for btn, on in ((self._webcam_on_btn, True), (self._webcam_off_btn, False)):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, v=on: self._set_layer1_webcam(v))
            row.addWidget(btn, 1)
        b.addLayout(row)
        self._webcam_note = QLabel()
        self._webcam_note.setWordWrap(True)
        self._webcam_note.setObjectName("sectionLead")
        b.addWidget(self._webcam_note)
        b.addWidget(caution_note(tr("ON: a USB webcam, when one is plugged in, reads layer 1 "
                                    "and the iCam reads layers 2 and up. OFF: the USB "
                                    "webcam is not used and the iCam reads every layer."),
                                 kind="info"))
        cl.addWidget(body, 1)
        return card

    def _build_connected_cameras_card(self):
        card, header, cl = _card(tr("Connected Cameras"))
        refresh = header_button(tr("Refresh"), tr("Look for cameras again"), "rotate-ccw")
        refresh.clicked.connect(self._refresh_cameras)
        header.add_right(refresh)
        self._cams_table = make_table([tr("Camera"), tr("Status")])
        cl.addWidget(self._cams_table)
        return card

    def _build_layer_cameras_card(self):
        card, header, cl = _card(tr("Camera For Each Layer"))
        self._layers_meta = HeaderMeta()
        header.add_right(self._layers_meta)
        self._layers_table = make_table([tr("Layer"), tr("Camera")])
        cl.addWidget(self._layers_table)
        return card

    @staticmethod
    def _fill_table(table, rows):
        """rows: [(cells, color or None)]. Fixed height: no scroll inside the page."""
        table.setRowCount(len(rows))
        for r, (cells, color) in enumerate(rows):
            for c, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if color is not None and c == len(cells) - 1:
                    item.setForeground(color)
                table.setItem(r, c, item)
        rh = table.verticalHeader().defaultSectionSize()
        table.setFixedHeight(table.horizontalHeader().height() + rh * max(1, len(rows)) + 4)

    def _layer_camera(self):
        """The LayerCamera wrapper, or None (feature off / laptop webcam / not started)."""
        cam = self._camera_obj
        return cam if hasattr(cam, "set_enabled") else None

    def _refresh_cameras_if_shown(self):
        sec = self._sections.get("cameras")
        if sec and self.isVisible() and self._stack.currentIndex() == sec[1]:
            self._refresh_cameras()

    def _refresh_cameras(self):
        try:
            self._do_refresh_cameras()
        except Exception:
            _tech_log.exception("Cameras section refresh failed")

    def _do_refresh_cameras(self):
        lc = self._layer_camera()
        cam = self._camera_obj
        icam = tr(self._ICAM_LABEL)
        st = lc.status() if lc is not None else None
        enabled = st["enabled"] if st else config.layer1_webcam_use()
        master_on = (bool(getattr(config, "LAYER1_WEBCAM_ENABLED", False))
                     and str(getattr(config, "CAMERA_SOURCE", "")).lower() == "icam")

        # Toggle
        self._webcam_on_btn.setObjectName("segLeftOn" if enabled else "segLeftOff")
        self._webcam_off_btn.setObjectName("segRightOff" if enabled else "segRightOn")
        for btn in (self._webcam_on_btn, self._webcam_off_btn):
            btn.setEnabled(master_on)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        # Connected cameras
        rows = []
        if cam is None:
            rows.append(((icam, tr("Starting…")), _NONE))
        elif st is not None:
            rows.append(((icam,
                          (tr("Reading now") if st["active"] == "icam" else tr("Standing by"))
                          if st["icam_running"] else tr("Not running")),
                         _OK if st["icam_running"] else _BAD))
        else:
            label = icam if str(getattr(config, "CAMERA_SOURCE", "")).lower() == "icam" \
                else tr("Local webcam (testing)")
            running = bool(getattr(cam, "is_running", False))
            rows.append(((label, tr("Reading now") if running else tr("Not running")),
                         _OK if running else _BAD))

        if st is not None:
            usb_name = st["name"] or st["detected_name"] or tr("USB webcam")
            usb_dev = st["device"] or st["detected_device"]
            if st["connected"]:
                status, color = ((tr("Reading now"), _OK) if st["active"] == "webcam"
                                 else (tr("Connected — standing by"), _OK))
            elif usb_dev is not None:
                status, color = ((tr("Detected — turned off"), _NONE) if not enabled
                                 else (tr("Detected — starting…"), _WAIT))
            else:
                status, color = tr("Not detected"), _WAIT
            rows.append(((tr("USB webcam: {name}", name=usb_name) if usb_dev is not None
                          else tr("USB webcam"), status), color))
        elif master_on:
            rows.append(((tr("USB webcam"), tr("Not available")), _NONE))
        self._fill_table(self._cams_table, rows)

        # Header status and note on the toggle card
        if not master_on:
            self._webcam_meta.set_status(tr("Not available"))
            self._webcam_note.setText(tr("The USB webcam is turned off in the software "
                                         "settings (config.py) or the camera source is "
                                         "not the iCam. The iCam reads every layer."))
        elif not enabled:
            self._webcam_meta.set_status(tr("Off — iCam only"))
            self._webcam_note.setText(
                tr("A USB webcam is plugged in but not used.")
                if st and st["detected_device"] is not None
                else tr("The iCam reads every layer."))
        elif st and st["connected"]:
            self._webcam_meta.set_status(tr("On — connected"), C["green"])
            self._webcam_note.setText(tr("The USB webcam reads layer 1."))
        else:
            self._webcam_meta.set_status(tr("On — no webcam"), C["amber"])
            self._webcam_note.setText(tr("No USB webcam is connected, so the iCam reads "
                                         "every layer. A webcam plugged in later is used "
                                         "from the next layer 1."))

        # Camera for each layer
        usb_reads_1 = bool(st and enabled and st["connected"])
        usb_label = tr("USB webcam ({name})", name=st["name"]) if usb_reads_1 else ""
        icam_fallback = (tr("{icam} (USB webcam not connected)", icam=icam)
                         if (st and enabled and not st["connected"]) else icam)
        layers = max(1, int(config.layers_per_stack()))
        rows = []
        if layers == 1:
            single = bool(st and st.get("single_pallet", True))
            rows.append(((tr("Single pallet (layer 1)"),
                          usb_label if (usb_reads_1 and single) else
                          (icam_fallback if single else icam)), None))
        else:
            rows.append(((tr("Layer 1"), usb_label if usb_reads_1 else icam_fallback), None))
            for n in range(2, layers + 1):
                rows.append(((tr("Layer {n}", n=n), icam), None))
        self._fill_table(self._layers_table, rows)
        self._layers_meta.set_status(tr("{n} layers per stack", n=layers) if layers > 1
                                     else tr("Stacking off"))

    def _set_layer1_webcam(self, on):
        if bool(on) == config.layer1_webcam_use() and (
                self._layer_camera() is None or self._layer_camera().enabled == bool(on)):
            return
        if not ConfirmDialog.ask(
                self, tr("USB Webcam For Layer 1"),
                tr("Turn ON the USB webcam for layer 1?") if on
                else tr("Turn OFF the USB webcam?"),
                tr("When a USB webcam is plugged in, it reads layer 1 from the next "
                   "layer 1. The iCam reads layers 2 and up.") if on else
                tr("The iCam reads every layer, as before the USB webcam was added. "
                   "This takes effect straight away."),
                confirm_text=tr("TURN ON") if on else tr("TURN OFF")):
            return
        if not config.save_layer1_webcam_use(on):
            self._webcam_meta.set_status(tr("Save failed"), C['red'])
            return
        lc = self._layer_camera()
        if lc is not None:
            lc.set_enabled(on)
        self._log_line(f"Layer-1 USB webcam turned {'ON' if on else 'OFF'} in Settings")
        self.add_activity("info", tr("USB webcam for layer 1 turned on") if on
                          else tr("USB webcam turned off — the iCam reads every layer"))
        self.toast.emit(tr("USB webcam on") if on else tr("USB webcam off"), "info")
        self._refresh_cameras()
        # A webcam being started on its own thread shows up a moment later.
        QTimer.singleShot(2500, self._refresh_cameras)

    # USB webcam focus section (modules/webcam_focus.py)
    def _build_webcam_picture_card(self):
        card, header, cl = _card(tr("Live Picture — USB Webcam"))
        self._wf_meta = HeaderMeta(max_w=480)
        header.add_right(self._wf_meta)
        body, b = _card_body()
        self._wf_picture = QLabel()
        self._wf_picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Fixed height: inside the scrolling page a stretchy label collapses.
        self._wf_picture.setFixedHeight(420)
        self._wf_picture.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Fixed)
        self._wf_picture.setStyleSheet(f"background:#111; color:{C['slate']}; "
                                       "border-radius:6px;")
        b.addWidget(self._wf_picture)
        b.addWidget(caution_note(tr("Green box: the code reads. Amber box: a code is "
                                    "seen but does not read yet — move the focus."),
                                 kind="info"))
        cl.addWidget(body, 1)
        return card

    def _build_webcam_focus_card(self):
        card, header, cl = _card(tr("Auto Focus"))
        self._wf_saved_meta = HeaderMeta()
        header.add_right(self._wf_saved_meta)
        body, b = _card_body()
        b.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._wf_start_btn = QPushButton(tr("START AUTO FOCUS"))
        self._wf_start_btn.setObjectName("primaryBtn")
        self._wf_start_btn.setMinimumHeight(CONTROL_H)
        self._wf_start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wf_start_btn.clicked.connect(self._wf_start_clicked)
        row.addWidget(self._wf_start_btn, 2)
        self._wf_close_btn = QPushButton(tr("CLOSE CAMERA"))
        self._wf_close_btn.setMinimumHeight(CONTROL_H)
        self._wf_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wf_close_btn.setToolTip(tr("Stop showing the USB webcam picture"))
        self._wf_close_btn.clicked.connect(self._wf_stop)
        row.addWidget(self._wf_close_btn, 1)
        b.addLayout(row)

        # What is happening now, in one large line, and the focus value.
        status = QHBoxLayout()
        self._wf_state_lbl = QLabel(tr("Ready"))
        self._wf_state_lbl.setWordWrap(True)
        status.addWidget(self._wf_state_lbl, 1)
        self._wf_value_lbl = QLabel("")
        self._wf_value_lbl.setStyleSheet("font-size:16px; font-weight:700;")
        status.addWidget(self._wf_value_lbl)
        b.addLayout(status)
        self._wf_state(tr("Ready"))

        b.addWidget(field_label(tr("Sharpness of the codes at each focus tried")))
        self._wf_curve = FocusCurve(0, 255)
        self._wf_curve.setMinimumHeight(130)
        self._wf_curve.set_note(tr("Press START AUTO FOCUS. Each focus value tried "
                                   "shows here."))
        b.addWidget(self._wf_curve)

        self._wf_note = QLabel()
        self._wf_note.setObjectName("sectionLead")
        self._wf_note.setWordWrap(True)
        b.addWidget(self._wf_note)
        b.addWidget(caution_note(tr("Do this between pallets, with the kegs standing "
                                    "still where layer 1 stands. It takes about 20 "
                                    "seconds. If the USB webcam is reading layer 1 now, "
                                    "its focus changes straight away.")))
        cl.addWidget(body, 1)
        return card

    # State
    def _wf_saved(self):
        lc = self._layer_camera()
        saved = config.layer1_webcam_focus()
        return saved if saved is not None else (lc.configured_focus if lc else None)

    def _wf_update_controls(self):
        if not hasattr(self, "_wf_start_btn"):
            return
        lc = self._layer_camera()
        running = self._wf_worker is not None
        self._wf_start_btn.setEnabled(lc is not None and
                                      (not running or self._wf_range is not None))
        if self._wf_focusing:
            self._wf_start_btn.setText(tr("STOP"))
        elif running:
            self._wf_start_btn.setText(tr("FOCUS AGAIN"))
        else:
            self._wf_start_btn.setText(tr("START AUTO FOCUS"))
        self._wf_close_btn.setEnabled(running and not self._wf_focusing)
        saved = self._wf_saved()
        self._wf_saved_meta.set_status(tr("Saved focus {s}", s=saved) if saved is not None
                                       else tr("No focus saved"),
                                       C["green"] if saved is not None else None)
        self._wf_value_lbl.setText(tr("Focus {v}", v=self._wf_value)
                                   if running and self._wf_value is not None else "")
        if lc is None:
            self._wf_picture.setText(tr("The USB webcam is not available on this camera "
                                        "(Settings → Cameras & layers)."))
        elif not running:
            self._wf_picture.setPixmap(QPixmap())
            self._wf_picture.setText(tr("Put kegs where layer 1 stands and press START "
                                        "AUTO FOCUS."))

    def _wf_state(self, text, color=None):
        self._wf_state_lbl.setText(text)
        self._wf_state_lbl.setStyleSheet("font-size:16px; font-weight:700;"
                                         + (f" color:{color};" if color else ""))

    def _wf_reset_curve(self, note):
        self._wf_points = []
        self._wf_curve.set_note(note)

    # Start / stop
    def _wf_start_clicked(self):
        if self._wf_focusing and self._wf_worker is not None:
            self._wf_worker.cancel()          # STOP: the lens goes back
            self._wf_state(tr("Stopping…"))
            return
        if self._wf_worker is not None:       # FOCUS AGAIN
            self._wf_focusing = True
            self._wf_reset_curve(tr("Focusing…"))
            self._wf_state(tr("Focusing… moving the focus to find the codes"), C["amber"])
            self._wf_worker.focus_again()
            self._wf_update_controls()
            return
        lc = self._layer_camera()
        if lc is None:
            return
        self._wf_range, self._wf_value, self._wf_boxes = None, None, []
        self._wf_last_frame = None
        self._wf_reset_curve(tr("Focusing…"))
        self._wf_picture.setPixmap(QPixmap())
        self._wf_picture.setText(tr("Opening the USB webcam…"))
        self._wf_meta.set_status(tr("Opening…"))
        self._wf_state(tr("Opening the USB webcam…"))
        self._wf_note.setText("")
        w = WebcamFocusWorker(lc, self._qr_detector, self)
        w.opened.connect(self._wf_on_opened)
        w.measured.connect(self._wf_on_measured)
        w.focus_set.connect(self._wf_on_focus_set)
        w.sweep_point.connect(self._wf_on_sweep_point)
        w.focus_done.connect(self._wf_on_focus_done)
        w.closed.connect(lambda w=w: self._wf_on_closed(w))
        self._wf_worker = w
        self._wf_focusing = True
        w.start()
        self._wf_update_controls()
        self._log_line("Webcam auto focus started")

    def _wf_stop(self):
        """Close the webcam picture. A run not finished is stopped, and the
        focus goes back to where it was."""
        w = self._wf_worker
        if w is None:
            return
        w.stop()
        self._wf_worker = None
        self._wf_focusing = False
        self._wf_timer.stop()
        self._wf_state(tr("Ready"))
        self._wf_update_controls()

    def _wf_on_closed(self, w):
        if self._wf_worker is w:
            self._wf_worker = None
            self._wf_focusing = False
            self._wf_timer.stop()
            self._wf_update_controls()
        self._log_line("Webcam auto focus closed")

    # From the worker
    @Slot(bool, str, object)
    def _wf_on_opened(self, ok, message, rng):
        if not ok:
            self._wf_focusing = False
            self._wf_meta.set_status(tr("No USB webcam"), C["red"], message)
            self._wf_state(tr("No USB webcam: {why}", why=message), C["red"])
            self.toast.emit(tr("USB webcam not available: {why}", why=message), "warning")
            self._wf_picture.setText(tr("No USB webcam: {why}", why=message))
            return
        lo, hi, step, current = rng
        self._wf_range = (lo, hi, step)
        if current is not None:
            self._wf_value = int(current)
        self._wf_curve = self._wf_replace_curve(lo, hi)
        self._wf_note.setText(tr("{name}: focus range {lo}–{hi}.", name=message, lo=lo,
                                 hi=hi))
        self._wf_state(tr("Focusing… moving the focus to find the codes"), C["amber"])
        self._wf_timer.start()
        self._wf_update_controls()

    def _wf_replace_curve(self, lo, hi):
        """FocusCurve's range is fixed at creation: swap in one for this webcam."""
        old = self._wf_curve
        new = FocusCurve(lo, hi)
        new.setMinimumHeight(old.minimumHeight())
        new.set_note(tr("Focusing…"))
        old.parentWidget().layout().replaceWidget(old, new)
        old.deleteLater()
        return new

    @Slot(object, object)
    def _wf_on_measured(self, m, frame):
        self._wf_boxes = m.get("boxes") or []
        self._wf_frame_size = m.get("frame_size")
        if frame is not None:
            self._wf_last_frame = frame
        if not m.get("frame_size"):
            self._wf_meta.set_status(tr("No picture from the USB webcam"), C["red"])
            return
        found, read = m["found"], m["read"]
        color = (C["green"] if found and read == found else
                 C["amber"] if found else None)
        self._wf_meta.set_status(
            tr("{f} codes seen · {r} decoded", f=found, r=read) if found else
            tr("No code in view"), color, ", ".join(m.get("codes") or []))

    @Slot(bool, int)
    def _wf_on_focus_set(self, ok, value):
        if ok:
            self._wf_value = int(value)
            self._wf_value_lbl.setText(tr("Focus {v}", v=value))

    @Slot(int, object)
    def _wf_on_sweep_point(self, value, m):
        self._wf_points.append((value, m))
        top = max((p[1]["sharpness"] for p in self._wf_points), default=0) or 1.0
        pts = sorted((v, min(1.0, mm["sharpness"] / top)) for v, mm in self._wf_points)
        self._wf_curve.set_data(pts, current=value, start=None, best=None)
        self._wf_state(tr("Focusing… focus {v}: {r} of {f} codes decoded",
                          v=value, r=m["read"], f=m["found"]), C["amber"])

    @Slot(bool, int, str, int, int)
    def _wf_on_focus_done(self, ok, value, message, found, read):
        self._wf_focusing = False
        if self._wf_points:
            top = max(p[1]["sharpness"] for p in self._wf_points) or 1.0
            pts = sorted((v, min(1.0, m["sharpness"] / top)) for v, m in self._wf_points)
            self._wf_curve.set_data(pts, current=None, start=None,
                                    best=value if ok else None)
        if ok:
            # Decoded: keep this focus from now on.
            saved = config.save_layer1_webcam_focus(value)
            lc = self._layer_camera()
            if lc is not None:
                lc.remember_focus(value)
            self._log_line(f"Layer-1 webcam auto focus: {value} "
                           f"({read}/{found} decoded, saved={saved})")
            full = bool(found) and read == found
            self._wf_state(tr("Focus set and saved: {v}", v=value) if saved
                           else tr("Focus set to {v}, but it could not be saved", v=value),
                           C["green"] if (saved and full) else C["amber"])
            self.add_activity("success" if full else "warning", message)
            self.toast.emit(tr("USB webcam focused: {v}", v=value),
                            "success" if full else "warning")
        else:
            self._wf_state(tr("Focus not changed"), C["red"])
            self.add_activity("warning", message)
        self._wf_note.setText(message)
        self._wf_update_controls()

    # Live picture
    def _wf_draw_live(self):
        w = self._wf_worker
        if w is None or not self._wf_picture.isVisible():
            return
        cam = getattr(w, "_cam", None)
        img, src = (None, None)
        if cam is not None and hasattr(cam, "get_preview"):
            try:
                img, src = cam.get_preview()
            except Exception:
                img = None
        if img is None:
            img = self._wf_last_frame
            src = self._wf_frame_size
        if img is None:
            return
        try:
            import cv2
            lw, lh = max(1, self._wf_picture.width()), max(1, self._wf_picture.height())
            h, w_ = img.shape[:2]
            k = min(lw / w_, lh / h)
            small = cv2.resize(img, (max(1, int(w_ * k)), max(1, int(h * k))),
                               interpolation=cv2.INTER_AREA)
            if small.ndim == 2:
                small = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                          QImage.Format.Format_RGB888).copy()
        except Exception:
            _tech_log.debug("Webcam focus picture failed", exc_info=True)
            return
        pix = QPixmap.fromImage(qimg)
        full = src or self._wf_frame_size
        if self._wf_boxes and full:
            f = pix.width() / float(full[0])
            p = QPainter(pix)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            for x1, y1, x2, y2, read in self._wf_boxes:
                p.setPen(QPen(QColor(C["green"] if read else C["amber"]), 3))
                p.drawRect(QRectF(x1 * f, y1 * f, (x2 - x1) * f, (y2 - y1) * f))
            p.end()
        self._wf_picture.setPixmap(pix)

    def _place_camera_cards(self):
        """Focus and Light on the left, Exposure (the tallest) on the right; one
        column under the other when the screen is too narrow for two."""
        if self._camera_one_column:
            self._camera_grid.addWidget(self._focus_card, 0, 0, 1, 2)
            self._camera_grid.addWidget(self._light_card, 1, 0, 1, 2)
            self._camera_grid.addWidget(self._exposure_card, 2, 0, 1, 2)
        else:
            self._camera_grid.addWidget(self._focus_card, 0, 0)
            self._camera_grid.addWidget(self._light_card, 1, 0)
            self._camera_grid.addWidget(self._exposure_card, 0, 1, 2, 1)

    # Below this width the two camera columns cut off the light buttons.
    _CAMERA_TWO_COLUMNS_MIN = 1000

    def resizeEvent(self, event):
        super().resizeEvent(event)
        one = self._stack.width() < self._CAMERA_TWO_COLUMNS_MIN
        if one != self._camera_one_column and not self._camera_lent:
            self._camera_one_column = one
            for card in (self._focus_card, self._light_card, self._exposure_card):
                self._camera_grid.removeWidget(card)
            self._place_camera_cards()

    # Device identity card
    def _build_identity_card(self):
        card, header, cl = _card(tr("Device Identity"))
        self._id_meta = HeaderMeta()
        header.add_right(self._id_meta)

        # Captions on row 0, controls on row 1. SAVE sits on the controls row so
        # every field and the button share one baseline.
        body, g = _card_body(QGridLayout)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(6)

        # Location name (free text)
        self._loc_edit = QLineEdit(str(config.LOCATION_NAME))
        self._loc_edit.setPlaceholderText(tr("e.g. Brewery-Main"))
        self._loc_edit.setMaxLength(64)
        g.addWidget(field_label(tr("Location name")), 0, 0)
        g.addWidget(self._loc_edit, 1, 0)

        # Line number (integer); the camera number is derived from it.
        self._line_spin = QSpinBox()
        # A plain number field like its neighbours, since the stock arrow
        # buttons don't render in the themed frame. Type it or use arrow keys.
        self._line_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._line_spin.setRange(1, 999)
        self._line_spin.setValue(int(config.LINE_NUMBER))
        self._line_spin.valueChanged.connect(self._refresh_cam_preview)
        g.addWidget(field_label(tr("Line number")), 0, 1)
        g.addWidget(self._line_spin, 1, 1)

        # Camera number: always Cam3.<line>, read-only.
        self._cam_edit = QLineEdit(self._auto_cam_preview())
        self._cam_edit.setReadOnly(True)
        self._cam_edit.setMaxLength(32)
        g.addWidget(field_label(tr("Camera number")), 0, 2)
        g.addWidget(self._cam_edit, 1, 2)

        self._save_id_btn = QPushButton(tr("SAVE"))
        self._save_id_btn.setObjectName("captureBtn")
        self._save_id_btn.setMinimumWidth(120)
        self._save_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_id_btn.clicked.connect(self._save_identity)
        g.addWidget(self._save_id_btn, 1, 3)

        for w in (self._loc_edit, self._line_spin, self._cam_edit):
            w.setFixedHeight(CONTROL_H)
        g.setColumnStretch(0, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)

        g.addWidget(caution_note(tr("Changes the camera number sent with every pallet. "
                                    "Only change it when the camera moves to another "
                                    "line.")), 2, 0, 1, 4)

        # Camera MAC: how the cloud recognises this camera. Set on the Camera
        # Setup screen; changed only through it (cloud check, then restart).
        self._mac_edit = QLineEdit(config.CAMERA_MAC_ID)
        self._mac_edit.setReadOnly(True)
        self._mac_edit.setFixedHeight(CONTROL_H)
        self._mac_edit.setToolTip(self._mac_source_text())
        g.addWidget(field_label(tr("Camera MAC ID")), 3, 0)
        g.addWidget(self._mac_edit, 4, 0, 1, 3)
        self._change_mac_btn = QPushButton(tr("CHANGE"))
        self._change_mac_btn.setObjectName("ghostBtn")
        self._change_mac_btn.setMinimumWidth(120)
        self._change_mac_btn.setFixedHeight(CONTROL_H)
        self._change_mac_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._change_mac_btn.clicked.connect(self._change_camera_mac)
        if config.CAMERA_MAC_SOURCE == "env":
            self._change_mac_btn.setEnabled(False)
            self._change_mac_btn.setToolTip(tr("Set by KEG_CAMERA_MAC on this device"))
        g.addWidget(self._change_mac_btn, 4, 3)
        g.addWidget(caution_note(tr("The cloud identifies this camera by its MAC ID. "
                                    "Change it only when told to by Checkology support "
                                    "or after replacing the device.")), 5, 0, 1, 4)
        cl.addWidget(body)
        return card

    def _mac_source_text(self):
        return {"env":      tr("Set by KEG_CAMERA_MAC on this device"),
                "saved":    tr("Set on the Camera Setup screen"),
                "migrated": tr("Taken from the stored camera login"),
                }.get(config.CAMERA_MAC_SOURCE, "")

    @Slot()
    def _change_camera_mac(self):
        """Open Camera Setup for a new MAC. Saved only after the cloud check,
        then the app restarts."""
        if not authorize_maintenance(self, tr("Change the camera MAC")):
            return
        if not ConfirmDialog.ask_twice(
                self, tr("Change Camera MAC"),
                tr("Change the MAC ID this camera uses with the cloud?"),
                tr("Pallets, operators and the blacklist all follow the MAC. "
                   "The app restarts after the new MAC is saved."),
                confirm_text=tr("CHANGE"),
                final_detail=tr("The new MAC and its camera password are checked "
                                "with the cloud before they are saved.")):
            return
        from .setup_page import CameraSetupDialog
        old = config.CAMERA_MAC_ID
        dlg = CameraSetupDialog(first_run=False, parent=self.window())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new = config.CAMERA_MAC_ID
        self._mac_edit.setText(new)
        self._mac_edit.setToolTip(self._mac_source_text())
        self.append_log(f"[settings] Camera MAC changed {old} → {new} "
                        f"(verified with the cloud; restarting)")
        self.add_activity("info", tr("Camera MAC changed to {mac}", mac=new))
        self.camera_mac_changed.emit(new)

    # Printer status card
    def _build_printer_card(self):
        card, header, cl = _card(tr("Label Printer"))
        self._printer_refresh_btn = header_button(tr("Refresh"),
                                                  tr("Check the printer again"))
        self._printer_refresh_btn.clicked.connect(self._printer_refresh_clicked)
        header.add_right(self._printer_refresh_btn)
        self._printer_test_btn = header_button(
            tr("Test print"), tr("Print a test label to confirm the printer works"))
        self._printer_test_btn.clicked.connect(self._test_print)
        header.add_right(self._printer_test_btn)

        body, b = _card_body()
        # One colour-coded status line (Ready / Not detected). Backend, printer
        # and the probe detail are in its tooltip and the Activity Log.
        self._printer_status = IconLabel("printer", tr("Checking printer…"), C['slate'],
                                         size=15, icon_size=20, pad=(0, 0))
        self._printer_status.setFixedHeight(CONTROL_H)
        b.addWidget(self._printer_status)
        # What to do when the printer cannot be used.
        self._printer_hint = caution_note("", "warning")
        self._printer_hint.setVisible(False)
        b.addWidget(self._printer_hint)
        b.addWidget(caution_note(tr("Test print uses one real label."), "info"))

        cl.addWidget(body)
        self._print_test_workers = []
        # Probe once shortly after construction (keeps __init__ snappy).
        QTimer.singleShot(300, self.refresh_printer_status)
        return card

    def _build_supported_printers_card(self):
        """The printers a label can go to (modules/usb_printer.py), so the
        operator knows what to plug in before anything goes wrong."""
        from modules.usb_printer import SUPPORTED_GROUPS, NOT_SUPPORTED_PRINTERS
        card, _, cl = _card(tr("Supported Printers"))
        body, g = _card_body(QGridLayout)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(8)
        for row, (group, models) in enumerate(SUPPORTED_GROUPS):
            icon = QLabel()
            icon.setPixmap(svg_pixmap("circle-check", C['green_dark'], 16))
            icon.setFixedWidth(18)
            name = QLabel(tr(group))
            name.setStyleSheet(f"color:{C['text1']}; font-weight:600;")
            detail = QLabel(tr(models))
            detail.setStyleSheet(f"color:{C['text2']};")
            detail.setWordWrap(True)
            g.addWidget(icon, row, 0, Qt.AlignmentFlag.AlignTop)
            g.addWidget(name, row, 1, Qt.AlignmentFlag.AlignTop)
            g.addWidget(detail, row, 2)
        g.setColumnStretch(2, 1)
        g.addWidget(caution_note(tr("Not supported: {printers}",
                                    printers=tr(NOT_SUPPORTED_PRINTERS))),
                    len(SUPPORTED_GROUPS), 0, 1, 3)
        cl.addWidget(body)
        return card

    # Camera focus card
    def _build_focus_card(self):
        """Manual-focus hint, RESET AUTO-FOCUS, the lens position and the curve.

        The camera's own auto-focus can lock on an object at the side of the
        view instead of the keg labels, so the app focuses itself: on the QR
        codes automatically the first time they are in view after start-up,
        and on whatever the operator double-taps on the live feed
        (run_qr_focus / run_point_focus).
        The curve fills in while it searches. RESET AUTO-FOCUS repeats what
        start-up does without a saved position:
        reset_focus_position -> autofocus_start.

        The button is its own indicator: it disables while any focusing runs
        and reads FOCUSING… during auto-focus. The outcome goes to the card
        header and the Activity Log, which is what gets copied into a fault
        report.
        """
        card, header, cl = _card(tr("Camera Focus"))
        self._focus_meta = HeaderMeta()
        header.add_right(self._focus_meta)

        body, b = _card_body()
        b.addWidget(caution_note(tr("Double-tap a QR code or an area on the live camera "
                                    "feed to focus there. The QR codes are focused automatically "
                                    "when they first come into view after start-up."),
                                 "info"))
        row = QHBoxLayout()
        self._focus_btn = QPushButton(tr(FOCUS_BTN_IDLE))
        self._focus_btn.setObjectName("ghostBtn")
        self._focus_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._focus_btn.setFixedHeight(CONTROL_H)
        self._focus_btn.clicked.connect(self._focus_clicked)
        row.addWidget(self._focus_btn)
        row.addStretch()
        b.addLayout(row)

        # Where the lens is and what start-up will do with it.
        self._focus_pos_label = QLabel()
        self._focus_pos_label.setStyleSheet(f"color:{C['text2']}; font-size:12px;")
        self._focus_pos_label.setWordWrap(True)
        b.addWidget(self._focus_pos_label)
        self._focus_curve = FocusCurve(config.CAMERA_FOCUS_POSITION_MIN,
                                       config.CAMERA_FOCUS_POSITION_MAX)
        self._focus_curve.set_note(tr("How sharp the target is at each lens position "
                                      "shows here while the camera focuses."))
        b.addWidget(self._focus_curve)

        # Focus range by layer: each layer height has its own sharp band of lens
        # positions. Measured one layer at a time, then compared, to see whether
        # one focus reads every layer of a stack.
        b.addWidget(field_label(tr("Focus range by layer")))
        self._range_chart = FocusRangeChart()
        self._range_chart.set_note(tr("Not checked yet. Press CHECK FOCUS FOR LAYERS and "
                                      "follow the steps."))
        b.addWidget(self._range_chart)
        self._range_verdict = QLabel()
        self._range_verdict.setStyleSheet(f"color:{C['text2']}; font-size:12px;")
        self._range_verdict.setWordWrap(True)
        b.addWidget(self._range_verdict)
        rrow = QHBoxLayout()
        self._range_btn = QPushButton(tr("CHECK FOCUS FOR LAYERS"))
        self._range_btn.setObjectName("ghostBtn")
        self._range_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._range_btn.setFixedHeight(CONTROL_H)
        self._range_btn.clicked.connect(self._focus_check_clicked)
        rrow.addWidget(self._range_btn)
        # The lens positions the camera learned for each layer of a stack
        # (automatic layer focus). They belong to one rig: after a change of
        # pallet, keg or camera height they send the lens to the wrong place.
        self._forget_btn = QPushButton(tr("FORGET LAYER FOCUS"))
        self._forget_btn.setObjectName("ghostBtn")
        self._forget_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._forget_btn.setFixedHeight(CONTROL_H)
        self._forget_btn.setToolTip(tr("Forget the lens positions learned for each layer. "
                                       "Use it after changing the pallet, the kegs or the "
                                       "camera height."))
        self._forget_btn.clicked.connect(self._forget_layer_focus_clicked)
        rrow.addWidget(self._forget_btn)
        rrow.addStretch()
        b.addLayout(rrow)
        self._show_focus_position(None)

        b.addWidget(caution_note(tr("Detection pauses while the camera focuses. Keep the "
                                    "pallet still until it is done.")))
        b.addStretch()

        cl.addWidget(body, 1)
        self._refresh_focus_availability()
        return card

    # Beer type card
    @staticmethod
    def _load_cached_beer_types():
        """Read the on-disk catalogue so the selector works before any fetch,
        and on a device that boots with no cloud. Never raises."""
        try:
            path = config.BEER_TYPES_CACHE_FILE
            if not path.exists():
                return {}
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            mapping = data.get("beerTypes") if isinstance(data, dict) else None
            return dict(mapping) if isinstance(mapping, dict) else {}
        except Exception:
            return {}

    def _build_beer_type_card(self):
        """Enable/disable the beer-type feature and refresh the cloud catalogue.

        ENABLED shows a searchable beer-type selector on the operator page and
        sends the choice with the pallet report. DISABLED removes the selector
        and sends no beer fields at all.
        """
        card, header, cl = _card(tr("Beer Type"))
        self._beer_meta = HeaderMeta()
        header.add_right(self._beer_meta)
        self._beer_refresh_btn = header_button(
            tr("Refresh list"),
            tr("Re-fetch the beer-type list from the cloud. The last good list is "
               "cached on disk, so the selector keeps working while the cloud is down."))
        self._beer_refresh_btn.clicked.connect(self.refresh_beer_types)
        header.add_right(self._beer_refresh_btn)

        body, b = _card_body()
        b.setSpacing(6)
        b.addWidget(field_label(tr("Send beer type with pallets")))
        # Joined ENABLED/DISABLED pair, the same segmented control as the
        # operator page's AUTO/MANUAL (objectName swap + repolish).
        row = QHBoxLayout()
        row.setSpacing(0)
        self._beer_on_btn = QPushButton(tr("ENABLED"))
        self._beer_off_btn = QPushButton(tr("DISABLED"))
        for btn, on in ((self._beer_on_btn, True), (self._beer_off_btn, False)):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, v=on: self._set_beer_enabled(v))
            row.addWidget(btn, 1)
        b.addLayout(row)
        b.addWidget(caution_note(tr("When ENABLED, a beer type must be chosen before each "
                                    "pallet is sent.")))
        b.addStretch()

        cl.addWidget(body, 1)
        self._restyle_beer_toggle()
        self._describe_beer_catalogue()
        return card

    def _build_test_mode_card(self):
        """Test mode on / off (modules/test_mode.py). While on, kegs already on
        a pallet are not checked, so the same kegs can be used again and again
        while testing; the operator page shows a banner the whole time."""
        card, header, cl = _card(tr("Test Mode"))
        self._test_meta = HeaderMeta()
        header.add_right(self._test_meta)
        body, b = _card_body()
        b.setSpacing(6)
        b.addWidget(field_label(tr("Check kegs already on a pallet")))
        row = QHBoxLayout()
        row.setSpacing(0)
        # Same joined pair as the beer type: ENABLED = test mode on.
        self._test_on_btn = QPushButton(tr("TEST MODE ON"))
        self._test_off_btn = QPushButton(tr("OFF (PRODUCTION)"))
        for btn, on in ((self._test_on_btn, True), (self._test_off_btn, False)):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, v=on: self._set_test_mode(v))
            row.addWidget(btn, 1)
        b.addLayout(row)
        b.addWidget(caution_note(tr("For testing only. While ON, the same kegs can be used "
                                    "again: no \"already on a pallet\" question. Pallets are "
                                    "still sent to the cloud.")))
        b.addStretch()
        cl.addWidget(body, 1)
        self._restyle_test_toggle()
        return card

    def _restyle_test_toggle(self):
        from modules import test_mode
        on = test_mode.enabled()
        self._test_on_btn.setObjectName("segLeftOn" if on else "segLeftOff")
        self._test_off_btn.setObjectName("segRightOff" if on else "segRightOn")
        for btn in (self._test_on_btn, self._test_off_btn):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._test_meta.set_status(tr("ON — testing") if on else tr("Off"),
                                   C['unreg'] if on else None)

    def _set_test_mode(self, on):
        from modules import test_mode
        if bool(on) == test_mode.enabled():
            return
        if not authorize_maintenance(
                self, tr("Turn on test mode") if on else tr("Turn off test mode")):
            return
        if not ConfirmDialog.ask(
                self, tr("Test Mode"),
                tr("Turn ON test mode?") if on else tr("Turn OFF test mode?"),
                tr("Kegs already on a pallet are then not checked, so the same kegs can "
                   "be used again. Use only for testing.") if on else
                tr("Kegs already on a pallet are checked again (production)."),
                confirm_text=tr("TURN ON") if on else tr("TURN OFF")):
            return
        try:
            test_mode.set_enabled(on)
        except Exception as e:
            self._test_meta.set_status(tr("Save failed"), C['red'],
                                       tr("Could not save the setting: {error}", error=e))
            return
        self._restyle_test_toggle()
        self._log_line(f"[test] Test mode {'ON' if on else 'OFF'}")
        self.toast.emit(tr("Test mode on") if on else tr("Test mode off"), "success")

    def _restyle_beer_toggle(self):
        """Selected segment reads as primary, the other as muted."""
        on = bool(config.BEER_TYPE_ENABLED)
        self._beer_on_btn.setObjectName("segLeftOn" if on else "segLeftOff")
        self._beer_off_btn.setObjectName("segRightOff" if on else "segRightOn")
        for btn in (self._beer_on_btn, self._beer_off_btn):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._beer_refresh_btn.setEnabled(on and not self._beer_busy)

    def _set_beer_enabled(self, enabled):
        if bool(enabled) == bool(config.BEER_TYPE_ENABLED):
            return
        if not ConfirmDialog.ask(
                self, tr("Beer Type"),
                tr("Turn ON the beer type?") if enabled else tr("Turn OFF the beer type?"),
                tr("Operators must then choose a beer type before each pallet is sent.")
                if enabled else
                tr("Pallets are then sent with no beer type."),
                confirm_text=tr("TURN ON") if enabled else tr("TURN OFF")):
            return
        try:
            config.save_beer_type_pref(enabled=enabled)
        except Exception as e:
            self._beer_meta.set_status(tr("Save failed"), C['red'],
                                       tr("Could not save the setting: {error}", error=e))
            return
        self._restyle_beer_toggle()
        self.beer_type_changed.emit(bool(enabled))
        self._log_line(f"[beer] Beer type {'ENABLED' if enabled else 'DISABLED'}")
        self.toast.emit(tr("Beer type turned on") if enabled else
                        tr("Beer type turned off"), "success")
        self._describe_beer_catalogue()
        # Only worth a fetch once the feature is actually on.
        if enabled and self._api is not None and not self._beer_types:
            self.refresh_beer_types()

    def _describe_beer_catalogue(self):
        if not config.BEER_TYPE_ENABLED:
            self._beer_meta.set_status(
                tr("Disabled"), None,
                tr("No beer type is selected on the operator page and no beer fields "
                   "are sent to the cloud."))
            return
        n = len(self._beer_types)
        if n:
            when = self._beer_fetched_at
            self._beer_meta.set_status(
                tr("{n} types · {time}", n=n, time=when) if when
                else tr("{n} types", n=n), None,
                tr("{n} beer type(s) · last fetched {time}", n=n,
                   time=when or tr("unknown (loaded from cache)")))
        else:
            self._beer_meta.set_status(tr("List not fetched yet"))

    @Slot()
    def refresh_beer_types(self):
        """Re-pull the catalogue on a worker thread."""
        if self._api is None or self._beer_busy:
            return
        self._beer_busy = True
        self._beer_refresh_btn.setEnabled(False)
        self._beer_refresh_btn.setText(tr("FETCHING…"))
        self._beer_meta.set_status(tr("Fetching…"))
        worker = BeerTypeFetcher(self._api, self)
        worker.result_ready.connect(self._on_beer_types)
        worker.failed.connect(self._on_beer_types_failed)
        worker.finished.connect(self._reap_beer_worker)
        self._beer_workers.append(worker)
        worker.start()

    def refresh_beer_types_quietly(self):
        """Periodic refresh (every few seconds).

        Same fetch, but the card doesn't flash FETCHING…; it only changes if
        the result does."""
        if self._api is None or self._beer_busy:
            return
        self._beer_busy = True
        worker = BeerTypeFetcher(self._api, self)
        worker.result_ready.connect(self._on_beer_types)
        worker.failed.connect(self._on_beer_types_failed)
        worker.finished.connect(self._reap_beer_worker)
        self._beer_workers.append(worker)
        worker.start()

    @Slot()
    def _reap_beer_worker(self):
        self._beer_workers = [w for w in self._beer_workers if w.isRunning()]

    def _end_beer_fetch(self):
        self._beer_busy = False
        self._beer_refresh_btn.setText(tr("REFRESH LIST"))
        self._restyle_beer_toggle()

    @Slot(object, object)
    def _on_beer_types(self, mapping, warning, force_emit=False):
        # Refreshed every few seconds, so the operator's selector is rebuilt and
        # the activity log written only when something changed.
        changed = force_emit or dict(mapping or {}) != self._beer_types
        self._beer_types = dict(mapping or {})
        if self._beer_types:
            self._beer_fetched_at = datetime.now().strftime("%H:%M:%S")
        note = (f"[beer] {warning}" if warning else
                f"[beer] Fetched {len(self._beer_types)} beer type(s)")
        if changed or note != getattr(self, "_beer_last_note", None):
            self._log_line(note)
        self._beer_last_note = note
        if warning:
            # A warning still carries a usable (cached) list; say both.
            self._beer_meta.set_status(tr("Using cached list"), C['red'], str(warning))
        else:
            self._beer_meta.set_status(tr("{n} types · up to date",
                                          n=len(self._beer_types)),
                                       C['green_dark'])
        if changed:
            self.beer_types_loaded.emit(dict(self._beer_types))
        self._end_beer_fetch()

    @Slot(str)
    def _on_beer_types_failed(self, err):
        self._beer_meta.set_status(tr("Fetch failed"), C['red'],
                                   tr("Could not fetch beer types: {error}", error=err))
        note = f"[beer] Fetch failed: {err}"
        if note != getattr(self, "_beer_last_note", None):
            self._log_line(note)
        self._beer_last_note = note
        self._end_beer_fetch()

    # Operator card
    @staticmethod
    def _load_cached_operators():
        """Read the on-disk operator list so the picker works before any fetch,
        and on a device that boots with no cloud. Never raises."""
        try:
            path = config.OPERATORS_CACHE_FILE
            if not path.exists():
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("operators") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return []
            out = []
            for x in items:
                if not isinstance(x, dict):
                    continue
                name = str(x.get("name", "")).strip()
                if name:
                    out.append({"id": str(x.get("id", "")).strip(),
                                "name": name,
                                "role": str(x.get("role", "")).strip()})
            return out
        except Exception:
            return []

    def _build_operator_card(self):
        """Who is working the line.

        Not a toggle like the beer type: with nobody selected, no operator key
        is sent, which the cloud accepts. A line that ignores the feature loses
        nothing. The list is fetched from the cloud at start-up.
        """
        card, header, cl = _card(tr("Operator"))
        self._operator_meta = HeaderMeta()
        header.add_right(self._operator_meta)

        # Caption on row 0, picker on row 1.
        body, g = _card_body(QGridLayout)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(6)
        self._operator_combo = SearchableCombo(placeholder=tr("Search operator…"))
        self._operator_combo.setFixedHeight(CONTROL_H)
        self._operator_combo.setToolTip(
            tr("Type to filter. The selected operator is recorded against every "
               "pallet this camera reports. Leave it empty and no operator is sent."))
        self._operator_combo.currentTextChanged.connect(self._on_operator_changed)
        g.addWidget(field_label(tr("On the line")), 0, 0)
        g.addWidget(self._operator_combo, 1, 0)
        g.setColumnStretch(0, 1)
        g.setRowStretch(2, 1)

        cl.addWidget(body, 1)
        self._populate_operators()
        return card

    def _populate_operators(self):
        """Fill the picker and restore the saved choice.

        The picker shows the saved name, but matches on the saved id when there
        is one: two operators can share a name, and a cloud rename must not
        silently point the selection at someone else. A saved operator missing
        from the list stays selected and is flagged. Dropping them would send
        the next pallets unattributed with nothing on screen to say so.
        """
        names = [o["name"] for o in self._operators]
        # set_items() does its own blockSignals(True/False) and leaves the combo
        # unblocked, so a block around this whole method would be lifted halfway
        # and the restore below would fire _on_operator_changed. The guard goes
        # on setCurrentText, the only call here that can emit.
        self._operator_combo.set_items(names)
        want_id, want_name = config.current_operator()
        if want_id or want_name:
            match = self._find_operator(want_id, want_name)
            self._operator_combo.blockSignals(True)
            self._operator_combo.setCurrentText(match["name"] if match else want_name)
            self._operator_combo.blockSignals(False)
            # Matched, but the name or id has changed: the cloud renamed them, or
            # a name-only pick now has an id. Save it, or later reports keep the
            # stale name and the "not in the list" warning would be wrong.
            if match and (match["id"], match["name"]) != (want_id, want_name):
                try:
                    config.save_operator_pref(selected_id=match["id"],
                                              selected_name=match["name"])
                    self._log_line(f"[operator] {want_name or want_id} is now "
                                   f"{match['name']} in the cloud — selection updated")
                except Exception as e:
                    self._log_line(f"[operator] Could not update the saved operator: {e}")
        self._describe_operator_selection()

    def _find_operator(self, ident, name):
        """Find the catalogue entry for an (id, name) pair: by id first, then by name."""
        ident = (ident or "").strip()
        name = (name or "").strip().lower()
        if ident:
            for o in self._operators:
                if o["id"] and o["id"] == ident:
                    return o
        if name:
            for o in self._operators:
                if o["name"].strip().lower() == name:
                    return o
        return None

    def _describe_operator_selection(self):
        """Say plainly what the next pallet will be attributed to."""
        ident, name = config.current_operator()
        if not (ident or name):
            self._set_operator_status(
                tr("No operator"), C['slate'],
                tr("No operator selected — pallets are reported without one, exactly "
                   "as before."))
            return
        known = self._find_operator(ident, name) is not None
        if known and ident:
            self._set_operator_status(tr("Reporting as {name}", name=name),
                                      C['green_dark'])
        elif known:
            self._set_operator_status(
                tr("Reporting as {name}", name=name), C['green_dark'],
                tr("Pallets are recorded against {name} — sent by name, as the cloud "
                   "gave no id for them.", name=name))
        else:
            self._set_operator_status(
                tr("Not in current list"), C['red'],
                tr("{name} is not in the current list — still sent, but the cloud may "
                   "not recognise them.", name=name or ident)
                + " " + tr("Press REFRESH LIST, then re-pick."))

    def _set_operator_status(self, text, color, detail=""):
        self._operator_meta.set_status(text, color, detail)

    def _on_operator_changed(self, text):
        """Persist the pick so it survives a restart."""
        text = (text or "").strip()
        if not text:
            return                      # cleared via CLEAR, handled there
        match = self._find_operator("", text)
        if match is None:
            return                      # mid-typing, not a real choice yet
        if (match["id"], match["name"]) == config.current_operator():
            return
        try:
            config.save_operator_pref(selected_id=match["id"],
                                      selected_name=match["name"])
        except Exception as e:
            self._set_operator_status(tr("Save failed"), C['red'],
                                      tr("Could not save the operator: {error}", error=e))
            return
        self._describe_operator_selection()
        self.operator_changed.emit(match["id"], match["name"])
        self._log_line(
            f"[operator] Now reporting as {match['name']}"
            + (f" (id {match['id']})" if match["id"] else " (by name — no id)"))

    def set_signed_in_operator(self, operator):
        """Reflect the signed-in operator in the picker.

        While someone is signed in, operator login owns the operator and the
        picker is read-only. None means nobody is signed in: the picker is
        editable again, and its pick travels in the report's operator fields,
        which the cloud uses only when nobody is signed in."""
        signed = bool(operator)
        self._operator_combo.setEnabled(not signed)
        self._operator_combo.blockSignals(True)
        if signed:
            self._operator_combo.setCurrentText(operator.get("name", ""))
        else:
            self._operator_combo.setCurrentIndex(-1)
            self._operator_combo.lineEdit().setText(config.current_operator()[1])
        self._operator_combo.blockSignals(False)
        if signed:
            self._set_operator_status(
                tr("Signed in: {name}", name=operator.get('name', '')), C['green_dark'],
                tr("Every report is recorded against the signed-in operator. "
                   "Sign out from the top bar at the end of the shift."))
        else:
            self._describe_operator_selection()

    @Slot()
    def refresh_operators(self):
        """Re-pull the operator list on a worker thread."""
        if self._api is None or self._operator_busy:
            return
        self._operator_busy = True
        self._set_operator_status(tr("Fetching…"), C['slate'])
        worker = OperatorFetcher(self._api, self)
        worker.result_ready.connect(self._on_operators)
        worker.failed.connect(self._on_operators_failed)
        worker.finished.connect(self._reap_operator_worker)
        self._operator_workers.append(worker)
        worker.start()

    @Slot()
    def _reap_operator_worker(self):
        self._operator_workers = [w for w in self._operator_workers if w.isRunning()]

    def _end_operator_fetch(self):
        self._operator_busy = False

    @Slot(object, object)
    def _on_operators(self, operators, warning):
        self._operators = list(operators or [])
        self._populate_operators()
        if warning:
            # A warning still carries a usable (cached) list. Say both, and let
            # _describe_operator_selection keep the selection message.
            self._set_operator_status(tr("Using cached list"), C['red'], str(warning))
            self._log_line(f"[operator] {warning}")
        else:
            self._log_line(f"[operator] Fetched {len(self._operators)} operator(s)")
        self._end_operator_fetch()

    @Slot(str)
    def _on_operators_failed(self, err):
        self._set_operator_status(tr("Fetch failed"), C['red'],
                                  tr("Could not fetch the operator list: {error}",
                                     error=err))
        self._log_line(f"[operator] Fetch failed: {err}")
        self._end_operator_fetch()

    def _log_line(self, msg):
        """Write a technical line to the support log."""
        try:
            self.append_log(msg)
        except Exception:
            pass

    def _refresh_focus_availability(self):
        """Enable/disable the buttons for what this camera can actually do."""
        why = (self._focus.qr_focus_unavailable_reason() if self._focus is not None
               else tr("not loaded"))
        self._range_btn.setEnabled(not why and not self._focus_busy)
        self._range_btn.setToolTip(
            tr("Step by step: measure each layer as you build the stack, and find a focus "
               "that reads them all.") if not why else
            tr("Not available: {reason}.", reason=why))
        if self._focus is None:
            self._focus_btn.setEnabled(False)
            why = tr("Focus control unavailable ({error})",
                     error=getattr(self, '_focus_import_error', tr("not loaded")))
            self._focus_btn.setToolTip(why)
            self._focus_meta.set_status(tr("Unavailable"), C['red'], why)
            return
        available = self._focus.available
        self._focus_btn.setEnabled(available and not self._focus_busy)
        # Show what the button drives and where, so a failure can be traced to
        # the right thing (daemon URL vs. driver property).
        if available:
            self._focus_btn.setToolTip(
                tr(FOCUS_BTN_TIP) + "\n\n"
                + tr("Controls: {target}", target=self._focus.describe()))
        else:
            why = tr("No focus control on this camera: {target}.",
                     target=self._focus.describe())
            self._focus_btn.setToolTip(why)
            self._focus_meta.set_status(tr("Unavailable"), C['red'], why)

    @Slot()
    def _focus_clicked(self):
        detail = tr("Detection pauses until focusing is done (up to a minute). Keep a "
                    "pallet under the camera.")
        if config.camera_focus_position() is not None:
            detail += " " + tr("The saved QR focus is cleared: start-up uses the "
                               "camera's auto-focus again.")
        if not ConfirmDialog.ask(
                self, tr("Reset Auto-Focus"), tr("Reset the camera focus now?"),
                detail, confirm_text=tr("RESET FOCUS")):
            return
        self.run_autofocus()

    def run_autofocus(self):
        """Reset the lens and auto-focus it, on a worker thread."""
        if self._focus is None or self._focus_busy or not self._focus.available:
            return

        self._focus_busy = True
        self._focus_btn.setEnabled(False)
        self._range_btn.setEnabled(False)
        self._focus_btn.setText(tr(FOCUS_BTN_BUSY))
        self._set_focus_status(tr("Resetting the lens and running auto-focus…"),
                               C['slate'])
        self.toast.emit(tr("Focusing the camera — keep a pallet in view…"), "info")
        self.append_log("[focus] Operator started auto-focus — "
                        f"{self._focus.describe()}")

        self._focus_worker = FocusWorker(self._focus, self)
        self._focus_worker.done.connect(self._on_focus_done)
        self._focus_worker.start()

    @Slot(bool, str)
    def _on_focus_done(self, ok, message):
        self._focus_busy = False
        self._focus_btn.setText(tr(FOCUS_BTN_IDLE))
        self._refresh_focus_availability()
        self._set_focus_status(message, C['green_dark'] if ok else C['red'])
        self.append_log(f"[focus] {message}")
        self.add_activity("success" if ok else "error",
                          tr("Camera focus reset") if ok else
                          tr("Camera focus could not be reset — try again"))
        self.toast.emit(tr("Camera focus reset") if ok else
                        tr("Camera focus could not be reset — try again"),
                        "success" if ok else "error")
        self._focus_worker = None
        self._show_focus_position(self._focus.last_position if ok else None)
        if ok:
            self._focus_curve.set_note(tr("Auto-focus locked at lens position {pos}. "
                                          "Double-tap a QR code on the live feed to focus on it.",
                                          pos=self._focus.last_position))

    def run_qr_focus(self):
        """Focus on all the QR codes in view (the operator page asks for this
        the first time codes are seen after start-up and do not read). A quick
        check around the lens first (the boot put it at the saved focus), the
        full search only when that does not read."""
        self._start_focus_search(None, tr("Focusing on the QR codes…"),
                                 "[focus] Automatic focus on the QR codes in view",
                                 quick=True)

    def run_point_focus(self, x, y):
        """Focus on the operator's double-tap on the live feed (full-frame pixels)."""
        if self._focus is None or self._focus_busy:
            self.toast.emit(tr("The camera is already focusing"), "info")
            return
        why = self._focus.qr_focus_unavailable_reason()
        if why:
            self.toast.emit(tr("Manual focus is not available: {reason}.", reason=why),
                            "warning")
            return
        self._focus_point = (x, y)
        self.focus_marker.emit(x, y, "busy", None)
        self._start_focus_search((x, y), tr("Focusing on the chosen point…"),
                                 f"[focus] Operator double-tapped the feed at ({x:.0f}, {y:.0f})")

    def run_region_focus(self, region, region_px):
        """FOCUS button on the live feed: the QR codes in `region` (fractions
        of the frame), else the region itself. `region_px` is the same in
        full-frame pixels, for the marker."""
        if self._focus is None or self._focus_busy:
            self.toast.emit(tr("The camera is already focusing"), "info")
            return
        why = self._focus.qr_focus_unavailable_reason()
        if why:
            self.toast.emit(tr("Focus is not available: {reason}.", reason=why), "warning")
            return
        x1, y1, x2, y2 = region_px
        self._focus_point = ((x1 + x2) / 2, (y1 + y2) / 2)
        self.focus_marker.emit(*self._focus_point, "busy", tuple(region_px))
        self._start_focus_search(None, tr("Focusing on the QR codes in view…"),
                                 f"[focus] Operator pressed FOCUS (region {region})",
                                 region=tuple(region))

    def _start_focus_search(self, point, status, log_line, region=None, save=True,
                            quick=False):
        if self._focus is None or self._focus_busy or not self._focus.qr_focus_available:
            if self._focus_layer is not None:
                layer, self._focus_layer = self._focus_layer, None
                self.layer_focus_done.emit(layer, False, -1, "Layer focus not started")
            return
        self._focus_busy = True
        self._focus_btn.setEnabled(False)
        self._range_btn.setEnabled(False)
        self._focus_start = None
        self._focus_curve.set_note(status)
        self._set_focus_status(status, C['slate'])
        self.append_log(log_line)
        if quick:
            self._focus_worker = LayerFocusWorker(self._focus, None, self, region=region,
                                                  save=save)
        else:
            self._focus_worker = FocusSearchWorker(self._focus, point, self, region=region,
                                                   save=save)
        self._focus_worker.progress.connect(self._on_focus_search_progress)
        self._focus_worker.done.connect(self._on_focus_search_done)
        self._focus_worker.start()

    @Slot(str, object, int)
    def _on_focus_search_progress(self, text, points, position):
        if self._focus_start is None:
            self._focus_start = position     # the first call is made before any move
        if points:
            self._focus_curve.set_data(points, current=position, start=self._focus_start)
            self._set_focus_status(tr("{step} lens {pos}", step=tr(text), pos=position),
                                   C['slate'])
        else:
            self._set_focus_status(tr(text), C['slate'])
        self._show_focus_position(position)

    @Slot(bool, str, object)
    def _on_focus_search_done(self, ok, message, result):
        self._focus_busy = False
        self._refresh_focus_availability()
        # Focused as sharp as it gets, but the code still does not read.
        unread = ok and result.get("read") is False
        self._set_focus_status(message, C['amber_dark'] if unread else
                               C['green_dark'] if ok else C['red'])
        self.append_log(f"[focus] {message} ({result.get('seconds', '?')} s)")
        if unread:
            self._focus_curve.set_data(result["points"], start=result["start"],
                                       best=result["position"])
            self.add_activity("warning", tr("Camera focused (lens {pos}) but the QR code "
                                            "still does not read", pos=result["position"]))
            self.toast.emit(tr("Focused, but the code still does not read — it may be too "
                               "small, glared or at an angle"), "warning")
        elif ok:
            self._focus_curve.set_data(result["points"], start=result["start"],
                                       best=result["position"])
            what = (tr("{n} QR code(s)", n=result["boxes"]) if result["target"] == "qr"
                    else tr("the chosen area"))
            self.add_activity("success", tr("Camera focused on {what} · lens {pos}",
                                            what=what, pos=result["position"]))
            self.toast.emit(tr("Camera focused · lens {pos}", pos=result["position"]),
                            "success")
        else:
            self._focus_curve.set_note(message)
            self.add_activity("error", tr("Camera focus did not finish — {reason}",
                                          reason=message))
            self.toast.emit(tr("Camera focus did not finish — see Camera Focus"), "error")
        if self._focus_point is not None:
            self.focus_marker.emit(*self._focus_point, "ok" if ok and not unread else "fail",
                                   result.get("box"))
            self._focus_point = None
        self._focus_worker = None
        self._show_focus_position(self._focus.last_position)
        if self._focus_layer is not None:
            layer, self._focus_layer = self._focus_layer, None
            self.layer_focus_done.emit(layer, bool(ok and not unread),
                                       int(result.get("position", -1)) if ok else -1, message)

    def report_layer_lens(self, layer):
        """The operator page sent a layer: answer where the lens is, so that
        layer's position is remembered (layer_lens_known)."""
        if self._focus is None:
            return
        pos = self._focus.last_position
        if pos is not None:
            self.layer_lens_known.emit(int(layer), int(pos))
            return
        worker = FocusReadWorker(self._focus, self)

        def got(ok, position, w=worker):
            if ok and position >= 0:
                self.layer_lens_known.emit(int(layer), int(position))
            w.deleteLater()
        worker.done.connect(got)
        self._layer_read_worker = worker
        worker.start()

    def run_layer_focus(self, layer, position=None, guess=None, region=None, quick_only=False):
        """Automatic focus for one layer of a stack (operator page). Neither
        way touches the start-up focus. Answers with layer_focus_done.

          position: the lens just goes there (learned or predicted);
          position {"step": n}: the lens moves n steps from where it is (the
            next layer of the first stack: no search);
          otherwise: the QR focus search, started from `guess` (a prediction)
          or from where the lens is, on the codes inside `region` (the stack's
          area, full-frame pixels) when there are any.
        """
        why = ("busy" if self._focus is None or self._focus_busy else
               self._focus.qr_focus_unavailable_reason())
        if why:
            self.layer_focus_done.emit(int(layer), False, -1,
                                       f"Layer focus not started: {why}")
            return
        if position is None:
            self._focus_layer = int(layer)
            self._focus_busy = True
            self._focus_btn.setEnabled(False)
            self._range_btn.setEnabled(False)
            self._focus_start = None
            status = tr("Focusing on layer {n}…", n=layer)
            self._focus_curve.set_note(status)
            self._set_focus_status(status, C['slate'])
            self.append_log(f"[focus] Layer {layer}: codes not reading — focusing from "
                            f"lens {guess if guess is not None else 'now'}")
            self._focus_worker = LayerFocusWorker(self._focus, guess, self, region=region,
                                                  full=not quick_only)
            self._focus_worker.progress.connect(self._on_focus_search_progress)
            self._focus_worker.done.connect(self._on_focus_search_done)
            self._focus_worker.start()
            return
        if isinstance(position, dict) and "step" in position:
            self._focus_busy = True
            self._focus_btn.setEnabled(False)
            self._range_btn.setEnabled(False)
            step = int(position["step"])
            self.append_log(f"[focus] Layer {layer}: lens {step:+d} steps for the next layer")
            sworker = FocusStepWorker(self._focus, step, self)

            def stepped(ok, message, start, target, w=sworker):
                self._focus_busy = False
                self._refresh_focus_availability()
                self._show_focus_position(self._focus.last_position)
                self.append_log(f"[focus] Layer {layer}: {message}")
                if start >= 0 and layer > 1:
                    # The layer below was just read and sent there.
                    self.layer_lens_known.emit(int(layer) - 1, int(start))
                self.layer_focus_done.emit(int(layer), bool(ok), int(target) if ok else -1,
                                           message)
                w.deleteLater()
            sworker.done.connect(stepped)
            self._layer_move_worker = sworker
            sworker.start()
            return
        self._focus_busy = True
        self._focus_btn.setEnabled(False)
        self._range_btn.setEnabled(False)
        self.append_log(f"[focus] Layer {layer}: lens to its learned position {position}")
        worker = FocusMoveWorker(self._focus, int(position), self, save=False)

        def moved(ok, message, w=worker):
            self._focus_busy = False
            self._refresh_focus_availability()
            self._show_focus_position(self._focus.last_position)
            self.append_log(f"[focus] Layer {layer}: {message}")
            self.layer_focus_done.emit(int(layer), bool(ok),
                                       int(position) if ok else -1, message)
            w.deleteLater()
        worker.done.connect(moved)
        self._layer_move_worker = worker      # kept alive while it runs
        worker.start()

    def _show_focus_position(self, position):
        """The lens position line: where the lens is, and what start-up does."""
        saved = config.camera_focus_position()
        now = (tr("Lens position {pos}", pos=position) if position is not None
               else tr("Lens position —"))
        boot = (tr("start-up: saved QR focus {pos}", pos=saved) if saved is not None
                else tr("start-up: camera auto-focus"))
        self._focus_pos_label.setText(f"{now}  ·  {boot}")
        self._show_focus_range(position if position is not None else saved)

    # Focus check for stacked layers
    @Slot()
    def _forget_layer_focus_clicked(self):
        learned = config.layer_focus_positions()
        if not learned:
            self.toast.emit(tr("No layer focus is learned yet."), "info")
            return
        text = ", ".join(tr("layer {n}: {pos}", n=n, pos=p) for n, p in sorted(learned.items()))
        if not ConfirmDialog.ask(self, tr("Forget Layer Focus"),
                                 tr("Forget the lens positions learned for each layer?"),
                                 tr("Learned now: {positions}.", positions=text) + " " +
                                 tr("The camera learns them again on the next stack."),
                                 confirm_text=tr("FORGET")):
            return
        config.save_layer_focus(None, None)
        config.save_stack_area(None)     # the stack may stand elsewhere now too
        self.append_log(f"[focus] Layer focus forgotten by the operator (was {text})")
        self.add_activity("info", tr("Layer focus forgotten — learned again on the next stack"))
        self.toast.emit(tr("Layer focus forgotten"), "success")

    def _focus_check_clicked(self):
        """Open the step-by-step check (ui_pyside/focus_check_dialog.py). The
        card counts as busy meanwhile, so no other focusing starts under it."""
        if self._focus is None or self._focus_busy or not self._focus.qr_focus_available:
            return
        from .focus_check_dialog import FocusCheckDialog
        self._focus_busy = True
        self._focus_btn.setEnabled(False)
        self._range_btn.setEnabled(False)
        self.append_log("[focus] Operator started the focus check for stacked layers")
        dlg = FocusCheckDialog(self._focus, config.layers_per_stack(), self)
        try:
            dlg.exec()
        finally:
            self._focus_busy = False
            for line in dlg.log_lines:
                self.append_log(line)
            dlg.deleteLater()
            self._refresh_focus_availability()
            self._show_focus_position(self._focus.last_position)

    def _show_focus_range(self, position):
        """Redraw the chart from the last check and say what it means."""
        try:
            from .focus_check_dialog import range_summary
            rows, common, text = range_summary(config.focus_range_layers(), position)
        except Exception:
            return
        if not rows:
            self._range_chart.set_data([])
            self._range_verdict.setText("")
            return
        self._range_chart.set_data(rows, position, common,
                                   sharp=float(config.CAMERA_FOCUS_STOP_FRACTION))
        self._range_verdict.setText(text)

    def _read_focus_position(self):
        """Read the lens position once, off the GUI thread."""
        if (self._focus is None or self._focus.backend != "rest"
                or self._focus_read_worker is not None):
            return
        self._focus_read_worker = FocusReadWorker(self._focus, self)
        self._focus_read_worker.done.connect(self._on_focus_read)
        self._focus_read_worker.start()

    @Slot(bool, int)
    def _on_focus_read(self, ok, position):
        self._focus_read_worker = None
        if ok and not self._focus_busy:
            self._show_focus_position(position)

    def _set_focus_status(self, text, color):
        self._focus_meta.set_status(text, color)

    @Slot()
    def _printer_refresh_clicked(self):
        self.refresh_printer_status()
        st = getattr(self, "_printer_last", None) or {}
        if st.get("ready"):
            self.toast.emit(tr("Printer ready") + f" · {st.get('printer_name', '')}",
                            "success")
        elif st.get("summary"):
            self.toast.emit(tr(st["summary"]) + " — " + tr(st.get("hint") or ""),
                            "warning")
        else:
            self.toast.emit(tr("Printer not ready — check power, labels and the cable"),
                            "warning")

    def refresh_printer_status(self):
        """Live-probe the print backend and repaint the card."""
        self._printer_last = None
        try:
            from modules.printer import probe_printer
            st = probe_printer()
        except Exception as e:
            self._printer_status.set("printer", tr("Printer status unavailable"), C['red'])
            self._printer_status.setToolTip(str(e))
            self._sections["printer"][0].set_status(C['red'])
            return
        self._printer_last = st

        color = C['green_dark'] if st["ready"] else C['red']
        text = tr(st['summary'])
        if st["printer_name"] and (st["ready"] or st.get("problem") != "not_connected"):
            text += f"  ·  {st['printer_name']}"
        self._printer_status.set("printer", text, color)
        hint = st.get("hint") or ""
        if hint and st.get("device_msg") and not st["ready"]:
            hint = f"{tr(hint)} ({st['device_msg']})"
        self._printer_hint.set(text=hint)
        self._printer_hint.setVisible(bool(hint))
        # Green or red dot on "Label printer" in the side panel.
        self._sections["printer"][0].set_status(color)
        # Whichever half is failing; when ready, the reachability note.
        detail = st["render_msg"] if not st["render_ok"] else st["device_msg"]
        self._printer_status.setToolTip(
            tr("Printer: {name}", name=st['printer_name']) + "\n"
            + tr("Backend: {backend}", backend=st['backend']) + "\n"
            + tr("Detail: {detail}", detail=detail))
        self._printer_test_btn.setEnabled(st["ready"])
        self.append_log(f"[printer] {st['summary']} — {st['backend']} · "
                        f"{st['printer_name']} ({st['device_msg']})"
                        + (f" — {st['hint']}" if st.get("hint") else ""))

    @Slot()
    def _test_print(self):
        """Send a one-off test label so the operator can confirm the hardware."""
        try:
            from modules.printer import get_printer
            printer = get_printer()
        except Exception as e:
            self.append_log(f"[printer] Test print failed to start: {e}")
            return
        self.append_log("[printer] Sending test label…")
        self.toast.emit(tr("Printing a test label…"), "info")
        self._printer_test_btn.setEnabled(False)
        w = PrintWorker(printer, "TEST-PRINT", self)
        w.done.connect(self._on_test_print_done)
        w.finished.connect(
            lambda ww=w: self._print_test_workers.remove(ww)
            if ww in self._print_test_workers else None)
        self._print_test_workers.append(w)
        w.start()

    @Slot(bool, str)
    def _on_test_print_done(self, ok, err):
        self._printer_test_btn.setEnabled(True)
        if ok:
            self.append_log("[printer] Test label printed OK")
            self.add_activity("success", tr("Test label printed"))
            self.toast.emit(tr("Test label printed"), "success")
        else:
            self.append_log(f"[printer] Test print FAILED: {err}")
            self.add_activity("error", tr("Test label did not print — check the printer "
                                          "has power, labels and the cable") +
                              (f" ({err})" if err else ""))
            self.toast.emit(tr("Test label did not print") + (f": {err}" if err else ""),
                            "error")
            self.refresh_printer_status()

    def _auto_cam_preview(self):
        """The Cam<pos>.<line> value the camera number takes (position is fixed)."""
        return f"Cam{int(config.CAMERA_POSITION)}.{self._line_spin.value()}"

    @Slot()
    def _refresh_cam_preview(self, *_):
        """Keep the camera number in sync with the line number."""
        self._cam_edit.setText(self._auto_cam_preview())

    @Slot()
    def _save_identity(self):
        if not authorize_maintenance(self, tr("Change the device identity")):
            return
        cam = self._auto_cam_preview()
        if not ConfirmDialog.ask(
                self, tr("Save Device Settings"),
                tr("Save location '{location}' and line {line}?",
                   location=self._loc_edit.text().strip(), line=self._line_spin.value()),
                tr("Every pallet from now on is sent as camera {camera}.", camera=cam),
                confirm_text=tr("SAVE")):
            return
        try:
            identity = config.save_device_identity(
                location_name=self._loc_edit.text(),
                line_number=self._line_spin.value(),
                camera_auto=True,
                camera_number=None,
            )
        except ValueError as e:
            self._id_meta.set_status(tr("Not saved"), C['red'], str(e))
            return
        except Exception as e:
            self._id_meta.set_status(tr("Save failed"), C['red'],
                                     tr("Save failed: {error}", error=e))
            return

        # Reflect the re-derived camera number and confirm.
        self._loc_edit.setText(identity["location_name"])
        self._cam_edit.setText(identity["camera_number"])
        self._id_meta.set_status(
            tr("Saved"), C['green_dark'],
            tr("Saved · {location} · Line {line} · {camera}",
               location=identity['location_name'], line=identity['line_number'],
               camera=identity['camera_number']))
        self.append_log(
            f"[settings] Device identity updated → location={identity['location_name']}, "
            f"line={identity['line_number']}, camera={identity['camera_number']} "
            f"(saved to device_state.json; used on next batch send)")
        self.add_activity("info", tr("Device settings saved · {location} · Line {line}",
                                     location=identity['location_name'],
                                     line=identity['line_number']))
        self.toast.emit(tr("Device settings saved"), "success")

    def take_camera_cards(self):
        """Lend the Camera Focus, Camera Light and Camera Exposure cards to the
        operator page's camera settings panel (ui_pyside/camera_controls_dialog.py).
        One instance each, so both places always show the same state."""
        cards = (self._focus_card, self._light_card, self._exposure_card)
        for card in cards:
            self._camera_grid.removeWidget(card)
        self._camera_lent = True
        return cards

    def return_camera_cards(self):
        """Put the cards back in the Camera section."""
        self._camera_lent = False
        self._camera_one_column = self._stack.width() < self._CAMERA_TWO_COLUMNS_MIN
        self._place_camera_cards()
        for card in (self._focus_card, self._light_card, self._exposure_card):
            card.show()

    def attach_backend(self, components):
        """Called once InitWorker finishes.

        The focus control needs the backend: on the webcam / generic OpenCV path
        the lens is driven through the live capture, which only exists once the
        camera has started. The iCam-540 REST path is unaffected; the daemon
        owns that lens either way.
        """
        self._camera_obj = components.get("camera")
        self._qr_detector = components.get("qr_detector")
        self._refresh_cameras()
        self._wf_update_controls()
        if self._focus is not None:
            # The iCam's lens: never the layer-1 webcam (modules/camera_select.py).
            from modules.camera_select import primary_camera
            self._focus.attach_camera(primary_camera(components.get("camera")),
                                      components.get("qr_detector"))
        self._refresh_focus_availability()
        self._read_focus_position()
        self._light_card.attach_backend()
        self._exposure_card.attach_backend()

        # Beer types: publish the cached list so the operator page is populated
        # at once, then refresh from the cloud only if the feature is on.
        self._api = components.get("api_sender")
        self._describe_beer_catalogue()
        if self._beer_types:
            self.beer_types_loaded.emit(dict(self._beer_types))
        if config.BEER_TYPE_ENABLED:
            self.beer_type_changed.emit(True)
            # Fetched on the loading screen; fetched here only if that didn't
            # finish in time.
            cloud = components.get("cloud_data") or {}
            if "beer_types" in cloud:
                mapping, warning = cloud["beer_types"]
                self._on_beer_types(mapping, warning, force_emit=True)
            else:
                QTimer.singleShot(1500, self.refresh_beer_types)
        self._restyle_beer_toggle()

        # Operators: always refreshed, since there is no toggle and a stale list
        # leads to picking the wrong person. Staggered behind the beer-type fetch
        # so start-up doesn't fire both at once. The cached list is already on
        # screen, so a failure only changes the status line.
        ident, name = config.current_operator()
        if ident or name:
            self.operator_changed.emit(ident, name)
        QTimer.singleShot(2500, self.refresh_operators)

    def shutdown(self):
        """Join this page's worker threads.

        Covers the beer-type and operator fetchers plus the focus and test-print
        workers. Qt aborts the process if it destroys a running QThread at exit.
        """
        workers = (list(self._beer_workers)
                   + list(self._operator_workers)
                   + list(getattr(self, "_print_test_workers", [])))
        # A focus search stops at its next step and moves the lens back.
        if self._focus is not None:
            self._focus.cancel()
        for fw in (self._focus_worker, self._focus_read_worker):
            if fw is not None:
                workers.append(fw)
        if self._wf_worker is not None:
            self._wf_worker.stop()
            workers.append(self._wf_worker)
        for w in workers:
            try:
                if w is not None and w.isRunning():
                    w.wait(5000)
            except Exception:
                pass
        self._beer_workers.clear()
        self._operator_workers.clear()
        self._light_card.shutdown()
        self._exposure_card.shutdown()

    @Slot(str)
    def append_log(self, line):
        """Technical line: goes to logs/app.log for support, not onto the screen."""
        _tech_log.info(line)

    @Slot(str, str)
    def add_activity(self, kind, text):
        """Add one plain-language event for the operator.

        kind: success / info / warning / error."""
        self._activity.add(kind, text)

    @Slot(bool)
    def set_network(self, online):
        """No-op. Online/offline is shown in the top bar.

        Kept as a slot because main.py connects operator.net_changed to it."""
