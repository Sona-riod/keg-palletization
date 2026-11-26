"""
Settings -> Camera Exposure: an ON / OFF switch for the camera's auto
exposure and AE Min / AE Max sliders for its range.

    POST /camera/auto_exposure_op      auto_exposure_op = 1 (on) / 0 (off)
    POST /camera/auto_exposure_range   min_exposure, max_exposure (100 µs units)

The calls themselves are in modules/exposure.py.

The range runs from CAMERA_AE_LIMIT_MIN to CAMERA_AE_LIMIT_MAX (1..10000, i.e.
0.1 ms .. 1 s), so the sliders are logarithmic: equal steps of the slider are
equal ratios of exposure, which keeps the useful low end (a few ms) as easy to
set as the top.

AE Min is kept below AE Max: dragging one past the other pushes the other
along. Both values are always sent together, once the sliders have rested for
_DEBOUNCE_MS, on one worker thread.

Like the light card: the sliders stay disabled until the camera has started
(attach_backend), the range last set here is sent again at that point, and on
a device where nothing was set yet the camera's current range is read instead.
A range or switch position the camera confirmed is saved to device_state.json
("camera_exposure"). The switch is a standard header button (Auto ON / Auto
OFF). While auto exposure is off the range section is faded and its sliders
disabled, since the range only applies in auto.

Presets (one tap sets both sliders, sends the range and switches auto
exposure on); the one matching the sliders is highlighted:
    Surrounding light, to follow the hall lighting:
      BRIGHT          CAMERA_AE_PRESET_BRIGHT, 1 - 10 ms: strong daylight.
      NORMAL (SAFE)   CAMERA_AE_PRESET_STANDARD, 5 - 40 ms: the default for a
                      new operator. The line ran 21-22 Oct 2026 at 5 - 46.6 ms
                      and 5 - 39.8 ms with normal reads.
      DIM             CAMERA_AE_PRESET_DIM, 10 - 70 ms: a darker hall.
      DARK            CAMERA_AE_PRESET_DARK, 20 - 100 ms: very dark; turn the
                      camera light up first, long exposures blur moving kegs.
    Only NORMAL is measured on the line; the others are starting points.
All presets stay inside the warning limits below.
    Warning           AE Max above CAMERA_AE_WARN_MAX (100 ms): codes on moving
                      kegs blur. AE Min below CAMERA_AE_WARN_MIN (1 ms): the
                      picture gets too dark and noisy.

Reset (header button) sends the Standard range and switches auto exposure on,
after a confirmation, and saves it. (It used to send the vendor defaults
5 ms - 1 s, which is inside the warning zone.)

On exit (Exit app or Power off device) auto exposure is switched off
(CAMERA_AE_OFF_ON_EXIT). That is not saved, so the next start restores the
operator's setting.
"""

import logging
import math
import threading

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

import config
from .i18n import tr
from .theme import C
from .widgets import (Card, SectionHeader, HeaderMeta, field_label, caution_note,
                      header_button, svg_icon, ConfirmDialog, CONTROL_H, CARD_MARGINS)

log = logging.getLogger("ui_pyside.exposure_card")

# How long the sliders have to rest before the range is sent (ms).
_DEBOUNCE_MS = 300
# Slider positions; mapped onto the exposure range on a log scale.
_STEPS = 1000
# Opacity of the range section while auto exposure is off.
_FADED = 0.4
# Recommended ranges and warning limits (100 us units). Read with defaults so a
# camera whose config.py predates them still gets them.
_STANDARD = tuple(getattr(config, "CAMERA_AE_PRESET_STANDARD", (50, 400)))   # 5 - 40 ms
_BRIGHT = tuple(getattr(config, "CAMERA_AE_PRESET_BRIGHT", (10, 100)))       # 1 - 10 ms
_DIM = tuple(getattr(config, "CAMERA_AE_PRESET_DIM", (100, 700)))            # 10 - 70 ms
_DARK = tuple(getattr(config, "CAMERA_AE_PRESET_DARK", (200, 1000)))         # 20 - 100 ms
_WARN_MAX = int(getattr(config, "CAMERA_AE_WARN_MAX", 1000))                 # 100 ms
_WARN_MIN = int(getattr(config, "CAMERA_AE_WARN_MIN", 10))                   # 1 ms
# Compact header buttons (Auto ON/OFF, Reset): height and icon size in px.
_BTN_H = 26
_ICON_PX = 13

def compact_button(btn, icon=None, icon_color=None):
    """Make a header_button compact: 26 px high, 13 px icon, 11 px text.

    Used for the small actions on the Camera Light / Camera Exposure headers.
    Keeps the #headerBtn look from the theme; only size, padding and font change.
    """
    btn.setFixedHeight(_BTN_H)
    btn.setStyleSheet("font-size: 11px; padding: 0 8px;")
    if icon:
        btn.setIcon(svg_icon(icon, icon_color or C["ck_navy"], _ICON_PX))
    btn.setIconSize(QSize(_ICON_PX, _ICON_PX))
    return btn


def choice_button(text, color, tooltip=""):
    """A tap-to-select button: outlined, filled in `color` while selected.

    Used for the exposure presets and the light mode / intensity steps. The
    card sets the selection (setChecked); a tap only reports the click."""
    q = QColor(color)
    tint = f"rgba({q.red()},{q.green()},{q.blue()},"
    b = QPushButton(text)
    b.setCheckable(True)
    b.setAutoExclusive(False)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setMinimumHeight(34)
    b.setStyleSheet(
        f"QPushButton {{ color:{color}; background:{tint}0.08); border:1px solid {tint}0.45);"
        f" border-radius:6px; padding:3px 8px; font-size:11px; font-weight:700; }}"
        f"QPushButton:checked {{ color:white; background:{color}; border:1px solid {color}; }}"
        f"QPushButton:hover:!checked {{ background:{tint}0.18); }}"
        f"QPushButton:disabled {{ color:{tint}0.35); border-color:{tint}0.20); }}")
    if tooltip:
        b.setToolTip(tooltip)
    # A click must not toggle on its own: the card decides what is selected.
    b.clicked.connect(lambda _=False, b=b: b.setChecked(not b.isChecked()))
    return b


_READ, _SET, _AUTO = "read", "set", "auto"


class _ExposureWorker(QThread):
    """Runs queued jobs (read / set) one at a time, until the queue is empty."""
    done = Signal(int, int, bool, str)   # ae_min, ae_max, ok, message
    auto_done = Signal(bool, bool, str)  # on, ok, message
    range_read = Signal(int, int)

    def __init__(self, controller, next_job, parent=None):
        super().__init__(parent)
        self._exp = controller
        self._next_job = next_job

    def run(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            kind, values = job
            if kind == _AUTO:
                on = bool(values[0])
                try:
                    ok, message = self._exp.set_auto(on)
                except Exception as e:     # set_auto() shouldn't raise; report it if it does
                    log.exception("Auto exposure switch failed")
                    ok, message = False, f"Auto exposure switch failed: {e}"
                self.auto_done.emit(on, bool(ok), message)
                continue
            ae_min, ae_max = values
            if kind == _READ:
                rng = self._exp.read_range()
                if rng is not None:
                    self.range_read.emit(*rng)
                continue
            try:
                ok, message = self._exp.set_range(ae_min, ae_max)
            except Exception as e:     # set_range() shouldn't raise; report it if it does
                log.exception("Exposure change failed")
                ok, message = False, f"Exposure change failed: {e}"
            self.done.emit(int(ae_min), int(ae_max), bool(ok), message)


class _WhiteBalanceWorker(QThread):
    """Fixes the white balance (or sets it back to auto) off the GUI thread:
    fixing waits a few seconds for auto white balance to settle."""
    done = Signal(str, bool, str)        # mode, ok, message

    def __init__(self, controller, mode, parent=None):
        super().__init__(parent)
        self._wb = controller
        self._mode = mode

    def run(self):
        try:
            if self._mode == "fixed":
                ok, message = self._wb.fix()
            elif self._mode == "hold":
                ok, message = self._wb.hold()
            else:
                ok, message = self._wb.set_auto(True)
        except Exception as e:           # the controller shouldn't raise; report it if it does
            log.exception("White balance change failed")
            ok, message = False, f"White balance change failed: {e}"
        self.done.emit(self._mode, bool(ok), message)


class CameraExposureCard(Card):
    """The Camera Exposure card. Talks to the page through three signals."""

    activity = Signal(str, str)   # (kind, text) for the Activity Log
    toast = Signal(str, str)      # (message, kind)
    log_line = Signal(str)        # technical line for logs/app.log

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        try:
            from modules.exposure import ExposureController, exposure_limits, format_exposure
            self._exp = ExposureController()
            self._lo, self._hi = exposure_limits()
            self._fmt = format_exposure
            self._import_error = ""
        except Exception as e:     # don't let this take the Settings page down
            self._exp = None
            self._lo, self._hi = 1, 10000
            self._fmt = str
            self._import_error = str(e)

        self._lock = threading.Lock()
        self._jobs = {}            # kind -> (min, max), oldest first
        self._running = False
        self._workers = []
        self._started = False
        self._closing = False
        self._known = False        # True once the range is known (saved or read)
        saved_auto = config.camera_auto_exposure()
        self._auto = True if saved_auto is None else saved_auto   # start-up turns it on

        cl = QVBoxLayout(self)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        header = SectionHeader(tr("Camera Exposure"))
        self._meta = HeaderMeta()
        header.add_right(self._meta)
        # Auto exposure on/off: one standard header button, like Refresh / Test
        # print on the printer card. Text and icon show the current state.
        self._auto_btn = compact_button(header_button(tr("Auto ON"), icon="power"))
        self._auto_btn.clicked.connect(lambda: self._set_auto(not self._auto))
        header.add_right(self._auto_btn)
        self._reset_btn = compact_button(
            header_button(tr("Reset"), tr("Set the exposure back to its initial values"),
                          icon="rotate-ccw"), icon="rotate-ccw")
        self._reset_btn.clicked.connect(self._reset_clicked)
        header.add_right(self._reset_btn)
        cl.addWidget(header)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(*CARD_MARGINS)
        bl.setSpacing(10)

        # The range rows sit in their own box so they can be faded as one.
        self._range_box = QWidget()
        g = QGridLayout(self._range_box)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(6)
        g.setColumnStretch(1, 1)
        bl.addWidget(self._range_box)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._send_range)

        self._min_slider, self._min_val = self._add_row(g, 0, tr("AE Min (shortest exposure)"))
        self._max_slider, self._max_val = self._add_row(g, 2, tr("AE Max (longest exposure)"))
        self._min_slider.valueChanged.connect(self._on_min_moved)
        self._max_slider.valueChanged.connect(self._on_max_moved)

        bl.addWidget(caution_note(tr("A lower AE Max gives sharper codes on a moving pallet "
                                     "but a darker image. Changes go to the camera straight "
                                     "away."), "info"))

        # Presets: one tap sets both sliders. The one matching them is highlighted.
        self._presets = []          # (range, button)
        groups = (
            (tr("Surrounding light"), (
                (tr("BRIGHT"), _BRIGHT, C['amber_dark'],
                 tr("Strong daylight or a very bright hall.")),
                (tr("NORMAL (SAFE)"), _STANDARD, C['green_dark'],
                 tr("Usual hall light. The safe default for normal kegs.")),
                (tr("DIM"), _DIM, C['blue'],
                 tr("A darker hall, evening or night shift.")),
                (tr("DARK"), _DARK, C['ck_navy'],
                 tr("Very dark. Turn the camera light up first: long exposures blur "
                    "moving kegs.")))),
        )
        for caption, items in groups:
            bl.addWidget(field_label(caption))
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            for name, rng, color, tip in items:
                btn = choice_button(f"{name}\n{self._fmt(rng[0])} – {self._fmt(rng[1])}",
                                    color, tip)
                btn.setMinimumHeight(44)
                btn.clicked.connect(lambda _=False, r=rng, n=name: self._apply_preset(r, n))
                row.addWidget(btn, 1)
                self._presets.append((rng, btn))
            if len(items) < 4:
                row.addStretch(4 - len(items))
            bl.addLayout(row)
        bl.addWidget(caution_note(tr("New operators: keep NORMAL. Pick BRIGHT, DIM or DARK "
                                     "when the hall light changes. Only NORMAL is tested on "
                                     "the line so far."), "info"))
        # Shown only while the range is somewhere it should not be.
        self._warn_max = caution_note(tr("Do not use: AE Max above {limit}. Codes on moving "
                                         "kegs blur and are not read.",
                                         limit=self._fmt(_WARN_MAX)), "warning")
        self._warn_min = caution_note(tr("Do not use: AE Min below {limit}. The picture gets "
                                         "too dark and noisy to read codes.",
                                         limit=self._fmt(_WARN_MIN)), "warning")
        for w in (self._warn_max, self._warn_min):
            w.setVisible(False)
            bl.addWidget(w)

        # White balance: fixed, so a coloured pallet does not tint the picture.
        try:
            from modules.white_balance import WhiteBalanceController
            self._wb = WhiteBalanceController()
        except Exception:
            self._wb = None
        self._wb_mode = config.camera_white_balance()
        self._wb_worker = None
        bl.addWidget(field_label(tr("White balance")))
        wrow = QHBoxLayout()
        wrow.setContentsMargins(0, 0, 0, 0)
        wrow.setSpacing(8)
        self._wb_fixed = choice_button(
            tr("FIXED"), C['green_dark'],
            tr("Colours stay the same whatever is under the camera (recommended)."))
        self._wb_fixed.clicked.connect(lambda: self._set_white_balance("fixed"))
        self._wb_auto = choice_button(
            tr("AUTO"), C['slate'],
            tr("The camera adjusts the colours to each picture. A coloured pallet then "
               "tints the whole view."))
        self._wb_auto.clicked.connect(lambda: self._set_white_balance("auto"))
        self._wb_now = choice_button(
            tr("SET NOW"), C['blue'],
            tr("Fix the white balance again on what the camera sees now."))
        self._wb_now.clicked.connect(lambda: self._set_white_balance("fixed"))
        for b in (self._wb_fixed, self._wb_auto, self._wb_now):
            b.setMinimumHeight(40)
            wrow.addWidget(b, 1)
        bl.addLayout(wrow)
        self._wb_status = QLabel("")
        self._wb_status.setWordWrap(True)
        self._wb_status.setStyleSheet(f"color:{C['text2']}; font-size:12px;"
                                      f" background:transparent;")
        bl.addWidget(self._wb_status)
        bl.addWidget(caution_note(tr("To fix the white balance, put a neutral surface under "
                                     "the camera first (the empty floor, or a grey or white "
                                     "sheet), not a coloured pallet, then press SET NOW. It "
                                     "takes a few seconds."), "info"))
        self._restyle_wb()
        bl.addStretch()

        # The saved range goes on the sliders only now: showing it updates the
        # warnings above, so they must exist first.
        saved = config.camera_exposure()
        if saved:
            self._show_range(*saved)
        cl.addWidget(body, 1)

        self._restyle_auto()
        self._set_enabled(False)
        if self._exp is None:
            self._unavailable(tr("Exposure control unavailable ({error})",
                                 error=self._import_error))
        elif not self._exp.available:
            self._unavailable(tr("No exposure control on this camera: {target}.",
                                 target=self._exp.describe()))
        else:
            self._meta.set_status(tr("Waiting for camera…"), C['slate'])

    def _add_row(self, g, row, caption):
        g.addWidget(field_label(caption), row, 0, 1, 3)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, _STEPS)
        slider.setSingleStep(1)
        slider.setPageStep(_STEPS // 20)
        slider.setMinimumHeight(CONTROL_H)
        slider.setCursor(Qt.CursorShape.PointingHandCursor)

        val = QLabel("–")           # "–" until the range is known
        val.setMinimumWidth(110)
        val.setStyleSheet(f"color: {C['ck_navy']}; font-size: 14px; font-weight: 700;")

        rng = QLabel(f"{self._fmt(self._lo)} – {self._fmt(self._hi)}")
        rng.setMinimumWidth(100)
        rng.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        rng.setStyleSheet(f"color: {C['slate']}; font-size: 12px;")

        g.addWidget(val, row + 1, 0)
        g.addWidget(slider, row + 1, 1)
        g.addWidget(rng, row + 1, 2)
        return slider, val

    # Log-scale mapping between slider position and exposure value
    def _to_value(self, pos):
        return int(round(self._lo * (self._hi / self._lo) ** (pos / _STEPS)))

    def _to_pos(self, value):
        value = max(self._lo, min(self._hi, int(value)))
        return int(round(_STEPS * math.log(value / self._lo) / math.log(self._hi / self._lo)))

    def _values(self):
        return (self._to_value(self._min_slider.value()),
                self._to_value(self._max_slider.value()))

    # Slider handling: keep AE Min below AE Max
    def _on_min_moved(self, pos):
        if pos >= self._max_slider.value():
            if self._max_slider.value() >= _STEPS:
                self._min_slider.setValue(_STEPS - 1)
                return
            self._max_slider.setValue(pos + 1)
        self._after_move()

    def _on_max_moved(self, pos):
        if pos <= self._min_slider.value():
            if self._min_slider.value() <= 0:
                self._max_slider.setValue(1)
                return
            self._min_slider.setValue(pos - 1)
        self._after_move()

    def _after_move(self):
        self._known = True
        self._refresh_labels()
        self._timer.start()

    def _refresh_labels(self):
        if not self._known:
            return
        # Highlight the preset the sliders are on (compared as slider positions:
        # the log scale does not give every value back exactly, so a preset's
        # own values are shown, which are also what was sent).
        pos = (self._min_slider.value(), self._max_slider.value())
        ae_min, ae_max = self._values()
        for rng, btn in self._presets:
            on = pos == (self._to_pos(rng[0]), self._to_pos(rng[1]))
            btn.setChecked(on)
            if on:
                ae_min, ae_max = rng
        self._min_val.setText(f"{self._fmt(ae_min)}  ({ae_min})")
        self._max_val.setText(f"{self._fmt(ae_max)}  ({ae_max})")
        self._warn_max.setVisible(ae_max > _WARN_MAX)
        self._warn_min.setVisible(ae_min < _WARN_MIN)

    def _show_range(self, ae_min, ae_max):
        """Put a range on the sliders without sending it."""
        for s in (self._min_slider, self._max_slider):
            s.blockSignals(True)
        self._min_slider.setValue(self._to_pos(ae_min))
        self._max_slider.setValue(max(self._to_pos(ae_max), self._min_slider.value() + 1))
        for s in (self._min_slider, self._max_slider):
            s.blockSignals(False)
        self._known = True
        self._refresh_labels()

    # Page hooks
    def attach_backend(self):
        """Called once the camera has started: enable the sliders and bring the
        camera in line with them."""
        if self._exp is None or not self._exp.available or self._started:
            return
        self._started = True
        self._set_enabled(True)
        self._meta.set_status(tr("Ready"), C['green_dark'],
                              tr("Controls: {target}", target=self._exp.describe()))
        saved = config.camera_exposure()
        if saved:
            self.log_line.emit(f"[exposure] Restoring the last exposure range: {saved}")
            self._queue(_SET, saved)
        else:
            self._queue(_READ, (0, 0))
        if config.camera_auto_exposure() is not None:
            self._queue(_AUTO, (int(self._auto),))
        # White balance as last chosen (fixed by default). Fixed is only held
        # here, not learned again: at start-up a coloured pallet may fill the
        # view. SET NOW learns it, on a neutral surface.
        self.log_line.emit(f"[white balance] Start-up: {self._wb_mode}")
        self._run_white_balance("hold" if self._wb_mode == "fixed" else "auto")

    def shutdown(self):
        """App exit: stop sending changes, then switch auto exposure off.

        The worker is joined first (Qt aborts if a running QThread is
        destroyed), so a change in flight cannot land after the switch-off.
        The off state is not saved.
        """
        self._closing = True
        self._timer.stop()
        with self._lock:
            self._jobs.clear()
        for w in list(self._workers):
            try:
                if w.isRunning():
                    w.wait(5000)
            except Exception:
                pass
        if (self._exp is None or not self._exp.available
                or not config.CAMERA_AE_OFF_ON_EXIT):
            return
        try:
            ok, message = self._exp.set_auto(False)
        except Exception as e:     # set_auto() shouldn't raise; never block the exit
            ok, message = False, f"Auto exposure off failed: {e}"
        if ok:
            log.info("[exposure] On exit: %s", message)
        else:
            log.error("[exposure] On exit, auto exposure was not switched off: %s", message)

    # White balance
    def _restyle_wb(self):
        self._wb_fixed.setChecked(self._wb_mode == "fixed")
        self._wb_auto.setChecked(self._wb_mode == "auto")
        self._wb_now.setChecked(False)
        busy = self._wb_worker is not None
        usable = self._wb is not None and self._wb.available and self._started
        for b in (self._wb_fixed, self._wb_auto, self._wb_now):
            b.setEnabled(usable and not busy)

    def _set_white_balance(self, mode):
        if self._wb_worker is not None:
            return
        self._wb_mode = mode
        config.save_camera_white_balance(mode)
        self.activity.emit("info", tr("White balance: fixing on the picture now…")
                           if mode == "fixed" else tr("White balance set to auto"))
        self._run_white_balance(mode)

    def _run_white_balance(self, mode):
        if self._wb is None or not self._wb.available or self._closing:
            self._wb_status.setText(tr("No white balance control on this camera."))
            return
        self._wb_status.setText(tr("Fixing the white balance… keep the scene still.")
                                if mode == "fixed" else
                                tr("Holding the white balance…") if mode == "hold" else
                                tr("Switching to auto white balance…"))
        w = _WhiteBalanceWorker(self._wb, mode, self)
        w.done.connect(self._on_white_balance_done)
        w.finished.connect(lambda w=w: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        self._wb_worker = w
        self._restyle_wb()
        w.start()

    @Slot(str, bool, str)
    def _on_white_balance_done(self, mode, ok, message):
        self._wb_worker = None
        self._restyle_wb()
        self.log_line.emit(f"[white balance] {message}")
        if ok and mode == "hold":
            self._wb_status.setText(tr("White balance held. If the picture looks yellow or "
                                       "blue, put a neutral surface under the camera and "
                                       "press SET NOW."))
        elif ok:
            self._wb_status.setText(tr("White balance fixed: the colours no longer change "
                                       "with the pallet.") if mode == "fixed"
                                    else tr("White balance is automatic."))
            self.activity.emit("success", tr("White balance fixed") if mode == "fixed"
                               else tr("White balance set to auto"))
        else:
            # The technical reason (HTTP code, config names) goes to the log
            # only; the operator gets a short line.
            self._wb_status.setText(tr("White balance not changed — see the log"))
            self.activity.emit("error", tr("White balance not changed — see the log"))
            self.toast.emit(tr("White balance not changed — see Camera Exposure"), "error")

    # Worker queue
    @Slot()
    def _send_range(self):
        self._queue(_SET, self._values())

    def _queue(self, kind, values):
        if self._closing:
            return
        with self._lock:
            self._jobs.pop(kind, None)       # keep only the latest, at the back
            self._jobs[kind] = tuple(int(v) for v in values)
            if self._running:
                return
            self._running = True
        w = _ExposureWorker(self._exp, self._take_job, self)
        w.done.connect(self._on_done)
        w.auto_done.connect(self._on_auto_done)
        w.range_read.connect(self._on_range_read)
        w.finished.connect(lambda w=w: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(w)
        w.start()

    def _take_job(self):
        """Next job for the worker (worker thread). None ends the worker."""
        with self._lock:
            if not self._jobs:
                self._running = False
                return None
            kind = next(iter(self._jobs))
            return kind, self._jobs.pop(kind)

    # GUI thread
    @Slot(int, int, bool, str)
    def _on_done(self, ae_min, ae_max, ok, message):
        self.log_line.emit(f"[exposure] {message}")
        if ok:
            config.save_camera_exposure(ae_min, ae_max)
            self._meta.set_status(f"{self._fmt(ae_min)} – {self._fmt(ae_max)}",
                                  C['green_dark'], message)
            self.activity.emit("info", tr("Camera exposure: {change}", change=message))
        else:
            self._meta.set_status(tr("Not applied"), C['red'], message)
            self.activity.emit("error", tr("Camera exposure not changed — {error}",
                                           error=message))
            self.toast.emit(tr("Camera exposure could not be changed — try again"), "error")

    @Slot(bool, bool, str)
    def _on_auto_done(self, on, ok, message):
        self.log_line.emit(f"[exposure] {message}")
        if ok:
            config.save_camera_exposure(auto=on)
            self._meta.set_status(tr("Auto exposure on") if on else tr("Auto exposure off"),
                                  C['green_dark'], message)
            self.activity.emit("info", tr("Camera exposure: {change}", change=message))
        else:
            # Put the switch back where the camera still is.
            self._auto = not on
            self._restyle_auto()
            self._set_enabled(self._started)
            self._meta.set_status(tr("Not applied"), C['red'], message)
            self.activity.emit("error", tr("Camera exposure not changed — {error}",
                                           error=message))
            self.toast.emit(tr("Camera exposure could not be changed — try again"), "error")

    @Slot(int, int)
    def _on_range_read(self, ae_min, ae_max):
        self._show_range(ae_min, ae_max)
        self.log_line.emit(f"[exposure] Camera exposure range is {ae_min}–{ae_max}.")

    # Auto exposure switch
    def _set_auto(self, on):
        if not self._started or bool(on) == self._auto:
            return
        self._auto = bool(on)
        self._restyle_auto()
        self._set_enabled(True)
        self._queue(_AUTO, (int(self._auto),))

    def _restyle_auto(self):
        """Header button shows the current state: green power icon when on."""
        on = self._auto
        self._auto_btn.setText("  " + (tr("Auto ON") if on else tr("Auto OFF")))
        compact_button(self._auto_btn, "power", C['green_dark'] if on else C['slate'])
        self._auto_btn.setToolTip(
            tr("Auto exposure is ON — tap to switch it off") if on else
            tr("Auto exposure is OFF — tap to switch it on"))

    # Recommended values
    def _apply_preset(self, rng, name):
        """Put a recommended range on the sliders, send it, and make sure auto
        exposure is on (the range only applies then)."""
        if not self._started:
            return
        self.log_line.emit(f"[exposure] Operator chose the {name} range {rng[0]}–{rng[1]}")
        self._timer.stop()                     # drop a drag still waiting to send
        self._show_range(*rng)
        self._queue(_SET, rng)
        if not self._auto:
            self._set_auto(True)

    # Reset
    @Slot()
    def _reset_clicked(self):
        ae_min, ae_max = _STANDARD
        if not ConfirmDialog.ask(
                self, tr("Reset Camera Exposure"), tr("Reset the camera exposure?"),
                tr("Auto exposure ON, range {low} – {high}.",
                   low=self._fmt(ae_min), high=self._fmt(ae_max)),
                confirm_text=tr("RESET")):
            return
        self.reset()

    def reset(self):
        """Send the Standard range and switch auto on."""
        if not self._started:
            return
        self.log_line.emit("[exposure] Operator reset the camera exposure")
        ae_min, ae_max = _STANDARD
        self._timer.stop()                     # drop a drag still waiting to send
        self._show_range(ae_min, ae_max)
        self._queue(_SET, (ae_min, ae_max))
        self._auto = True
        self._restyle_auto()
        self._set_enabled(True)
        self._queue(_AUTO, (1,))               # always: the initial state is auto on

    # Helpers
    def _set_enabled(self, on):
        """Switch follows `on`; the range section only while auto exposure is on,
        and is faded while it is off."""
        self._auto_btn.setEnabled(on)
        self._reset_btn.setEnabled(on)
        for _, btn in self._presets:
            btn.setEnabled(on)
        # The opacity effect is attached only while faded: left on at full
        # opacity it caches a stale picture and the rows paint over each other.
        if self._auto:
            self._range_box.setGraphicsEffect(None)
        elif not isinstance(self._range_box.graphicsEffect(), QGraphicsOpacityEffect):
            fade = QGraphicsOpacityEffect(self._range_box)
            fade.setOpacity(_FADED)
            self._range_box.setGraphicsEffect(fade)
        for s in (self._min_slider, self._max_slider):
            s.setEnabled(on and self._auto)
        self._range_box.setToolTip("" if self._auto else
                                   tr("The range applies only while auto exposure is ON."))
        if hasattr(self, "_wb_fixed"):
            self._restyle_wb()

    def _unavailable(self, why):
        self._meta.set_status(tr("Unavailable"), C['red'], why)
        for w in [self._min_slider, self._max_slider, self._auto_btn, self._reset_btn] + \
                [btn for _, btn in self._presets]:
            w.setToolTip(why)
