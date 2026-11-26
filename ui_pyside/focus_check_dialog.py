"""
Focus check for stacked layers: a guided, step-by-step window.

Can one lens position read every layer of a stack? Each layer height has its
own narrow band of sharp lens positions, so the operator builds the stack under
the camera one layer at a time and the window measures each one as it goes
(FocusController.measure_range). At the end it says in words whether the bands
overlap and, when they do, offers to set the lens in the middle of the overlap
and save it for start-up.

Every check starts from nothing, so there is no layer number to pick and no
earlier result to overwrite by mistake. The results are kept (config
focus_range_layers) for the chart on the Camera Focus card.
"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout)

import config
from .i18n import tr
from .theme import C
from .widgets import CountCombo, FocusRangeChart, CONTROL_H
from .workers import FocusSearchWorker, FocusMoveWorker


def range_summary(layers, position):
    """(chart rows, common (lo, hi) or None, sentence) for measured `layers`
    ({layer: {"band", "points"}}) with the lens at `position` (or None)."""
    from modules.focus import score_at, common_band
    rows = [(n, d["band"], score_at([tuple(p) for p in d.get("points") or []], position))
            for n, d in sorted(layers.items(), reverse=True)]
    if not rows:
        return rows, None, ""
    common = common_band([d["band"] for d in layers.values()]) if len(rows) > 1 else None
    if len(rows) == 1:
        n, band, _ = rows[0]
        return rows, None, tr("Layer {n} is sharp from lens {lo} to {hi}.",
                              n=n, lo=band["lo"], hi=band["hi"])
    if common:
        text = tr("One focus reads every layer: any lens position from {lo} to {hi}.",
                  lo=common[0], hi=common[1])
        if common[1] - common[0] < config.FOCUS_CHECK_MIN_OVERLAP:
            text += " " + tr("The overlap is narrow, so the focus can drift out of it. "
                             "Check both layers read after setting it.")
        if position is not None and not common[0] <= position <= common[1]:
            text += " " + tr("The lens is at {pos} now, outside that range.", pos=position)
        return rows, common, text
    needs = "; ".join(tr("layer {n} {lo}–{hi}", n=n, lo=b["lo"], hi=b["hi"])
                      for n, b, _ in reversed(rows))
    return rows, None, (tr("No single focus reads every layer ({needs}).", needs=needs) + " " +
                        tr("The lens has to refocus for each layer, or the camera has to "
                           "be mounted higher."))


class FocusCheckDialog(QDialog):
    """Intro (how many layers) -> one step per layer -> result."""

    def __init__(self, controller, layers=2, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self._focus = controller
        self._total = max(2, int(layers))
        self._layer = 0               # layer being measured now (0 = intro)
        self._results = {}            # layer -> {"band", "points"}
        self._worker = None
        self._common = None
        self.log_lines = []           # for the Settings support log
        self._build()
        self._show_intro()

    # Layout
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        root = QFrame()
        root.setObjectName("confirmRoot")
        root.setFixedWidth(560)
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
        body.setSpacing(10)
        self._message = QLabel()
        self._message.setObjectName("confirmMsg")
        self._message.setWordWrap(True)
        body.addWidget(self._message)
        self._detail = QLabel()
        self._detail.setObjectName("confirmDetail")
        self._detail.setWordWrap(True)
        body.addWidget(self._detail)

        self._count_row = QHBoxLayout()
        lbl = QLabel(tr("Layers in the stack"))
        lbl.setObjectName("confirmDetail")
        self._count_row.addWidget(lbl)
        self._count = CountCombo(self._total, 2, config.MAX_LAYERS_PER_STACK)
        self._count.setFixedSize(90, CONTROL_H)
        self._count_row.addWidget(self._count)
        self._count_row.addStretch()
        body.addLayout(self._count_row)

        self._done = QLabel()         # one line per layer measured so far
        self._done.setObjectName("confirmDetail")
        self._done.setWordWrap(True)
        body.addWidget(self._done)
        self._chart = FocusRangeChart()
        self._chart.setVisible(False)
        body.addWidget(self._chart)
        self._status = QLabel()
        self._status.setWordWrap(True)
        body.addWidget(self._status)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        self._close = QPushButton(tr("CANCEL"))
        self._close.setObjectName("confirmCancel")
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.clicked.connect(self.reject)
        actions.addWidget(self._close)
        self._go = QPushButton()
        self._go.setObjectName("confirmOk")
        self._go.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go.clicked.connect(self._next)
        actions.addWidget(self._go)
        body.addLayout(actions)
        vl.addLayout(body)
        outer.addWidget(root, alignment=Qt.AlignmentFlag.AlignCenter)

    def _set_status(self, text, color=None):
        self._status.setText(text)
        self._status.setStyleSheet(f"color:{color or C['text2']}; font-size:12px;"
                                   " background:transparent;")

    def _set_count_visible(self, shown):
        for i in range(self._count_row.count()):
            w = self._count_row.itemAt(i).widget()
            if w is not None:
                w.setVisible(shown)

    # Steps
    def _show_intro(self):
        self._title.setText(tr("Check Focus for Stacked Layers"))
        self._message.setText(tr("Can one focus read every layer of a stack?"))
        self._detail.setText(
            tr("You build the stack under the camera one layer at a time, bottom first, "
               "and the camera measures each layer.") + " " +
            tr("Each layer takes about 20 seconds. Detection pauses meanwhile, and the "
               "focus goes back to where it was until you choose to change it."))
        self._set_count_visible(True)
        self._go.setText(tr("START"))

    def _show_step(self):
        n, total = self._layer, self._total
        self._title.setText(tr("Step {n} of {total}", n=n, total=total))
        if n == 1:
            self._message.setText(tr("Put only the bottom layer under the camera."))
            self._detail.setText(tr("Nothing on top of it yet. Keep it still, then press "
                                    "MEASURE."))
        else:
            self._message.setText(tr("Put layer {n} on top of the stack.", n=n))
            self._detail.setText(tr("Keep it still, then press MEASURE."))
        self._set_count_visible(False)
        self._go.setText(tr("MEASURE LAYER {n}", n=n))
        self._go.setEnabled(True)
        self._set_status("")

    def _show_result(self):
        position = self._focus.last_position
        rows, self._common, text = range_summary(self._results, position)
        self._title.setText(tr("Result"))
        self._message.setText(text)
        self._detail.setText(tr("Green = sharp. Shaded = sharp for every layer. "
                                "Amber = where the lens is now."))
        self._chart.set_data(rows, position, self._common,
                             sharp=float(config.CAMERA_FOCUS_STOP_FRACTION))
        self._chart.setVisible(True)
        self._done.setVisible(False)
        self._set_status("")
        self._close.setText(tr("CLOSE"))
        if self._common:
            self._go.setText(tr("USE FOCUS {pos}", pos=self._middle()))
            self._go.setEnabled(True)
        else:
            self._go.setVisible(False)

    def _middle(self):
        return round((self._common[0] + self._common[1]) / 2)

    @Slot()
    def _next(self):
        if self._worker is not None:
            return
        if self._layer == 0:
            self._total = self._count.value()
            self._results = {}
            config.save_focus_range_layer(None, None, None)   # every check starts fresh
            self._layer = 1
            self._show_step()
        elif self._layer <= self._total:
            self._measure()
        elif self._common:
            self._use_focus()

    def _busy(self, text):
        self._go.setEnabled(False)
        self._close.setEnabled(False)
        self._set_status(text, C['blue'])

    def _measure(self):
        n = self._layer
        self._busy(tr("Measuring layer {n}… keep it still.", n=n))
        self.log_lines.append(f"[focus] Focus check: measuring layer {n} of {self._total}")
        self._worker = FocusSearchWorker(self._focus, None, self, measure_range=True)
        self._worker.progress.connect(
            lambda text, _pts, pos: self._set_status(
                tr("Measuring layer {n}… keep it still. Lens {pos}", n=n, pos=pos), C['blue']))
        self._worker.done.connect(self._measured)
        self._worker.start()

    @Slot(bool, str, object)
    def _measured(self, ok, message, result):
        self._worker = None
        self._close.setEnabled(True)
        n = self._layer
        self.log_lines.append(f"[focus] Focus check layer {n}: {message}")
        if not ok:
            self._go.setText(tr("TRY AGAIN"))
            self._go.setEnabled(True)
            self._set_status(message, C['red'])
            return
        band = result["band"]
        self._results[n] = {"band": band, "points": result["points"]}
        config.save_focus_range_layer(n, band, result["points"])
        self._done.setText("\n".join(
            "✓ " + tr("Layer {n}: sharp from lens {lo} to {hi}", n=k,
                      lo=r["band"]["lo"], hi=r["band"]["hi"])
            for k, r in sorted(self._results.items())))
        self._layer += 1
        if self._layer <= self._total:
            self._show_step()
        else:
            self._show_result()

    def _use_focus(self):
        position = self._middle()
        self._busy(tr("Setting the lens to {pos}…", pos=position))
        self._worker = FocusMoveWorker(self._focus, position, self)
        self._worker.done.connect(self._focus_set)
        self._worker.start()

    @Slot(bool, str)
    def _focus_set(self, ok, message):
        self._worker = None
        self._close.setEnabled(True)
        self.log_lines.append(f"[focus] Focus check: {message}")
        if ok:
            self._go.setVisible(False)
            self._set_status(message + " " + tr("Now check on the live preview that every "
                                                "layer reads."), C['green_dark'])
            rows, common, _ = range_summary(self._results, self._focus.last_position)
            self._chart.set_data(rows, self._focus.last_position, common,
                                 sharp=float(config.CAMERA_FOCUS_STOP_FRACTION))
        else:
            self._go.setEnabled(True)
            self._set_status(message, C['red'])

    def reject(self):
        if self._worker is None:     # never close under a running measurement
            super().reject()
