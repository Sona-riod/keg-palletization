"""
Settings -> Camera Light: two rows of 8 buttons for the camera's Flash LED.

    Light mode   0..7    POST /camera/led_status   buttons 0 1 2 3 4 5 6 7
    Intensity    0..25   POST /camera/led_gain     buttons 0 4 7 11 14 18 21 25

The calls themselves are in modules/lighting.py. Intensity has 26 values, so
its 8 buttons are 8 even steps across the range (_steps()).

A tap applies at once, with no SAVE button: the operator watches the live view.
The selected button is highlighted. A value from before the buttons (an old
slider position such as intensity 10) is shown as a number with no button
highlighted. The calls run in order on one worker thread, never on the GUI
thread.

The buttons stay disabled until the camera has started (attach_backend), so
nothing is sent while the start-up sequence is closing and reopening the
camera. At that point the values the operator last set are sent again, so the
light matches the sliders after a restart. On a device where nothing was set
yet, the mode is read from the camera instead.

A value the camera confirmed is saved to device_state.json ("camera_light").
A refused one is reported in the card header, a toast and the Activity Log,
and is not saved.

Reset (header button) sends CAMERA_LED_RESET_VALUES (mode 0, intensity 0)
after a confirmation, and saves them like any other change.

On exit (shutdown, from MainWindow._cleanup) the light is turned off with
CAMERA_LED_OFF_VALUES. That is not saved, so the next start restores the
operator's values.
"""

import logging
import threading

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import config
from .i18n import tr
from .theme import C
from .widgets import (Card, SectionHeader, HeaderMeta, field_label, caution_note,
                      header_button, ConfirmDialog, CONTROL_H, CARD_MARGINS)

from .exposure_card import compact_button, choice_button

log = logging.getLogger("ui_pyside.lighting_card")

_READ = "read"
_BUTTONS = 8                # buttons per row


def _steps(lo, hi):
    """_BUTTONS values from lo to hi, evenly spread (every value if they fit)."""
    if hi - lo + 1 <= _BUTTONS:
        return list(range(lo, hi + 1))
    return sorted({round(lo + i * (hi - lo) / (_BUTTONS - 1)) for i in range(_BUTTONS)})


class _LightWorker(QThread):
    """Sends queued light changes one at a time, until the queue is empty."""
    done = Signal(str, int, bool, str)   # kind, value, ok, message
    mode_read = Signal(int)

    def __init__(self, controller, next_job, parent=None):
        super().__init__(parent)
        self._light = controller
        self._next_job = next_job

    def run(self):
        while True:
            job = self._next_job()
            if job is None:
                return
            kind, value = job
            if kind == _READ:
                mode = self._light.read_mode()
                if mode is not None:
                    self.mode_read.emit(mode)
                continue
            try:
                ok, message = self._light.apply(kind, value)
            except Exception as e:     # apply() shouldn't raise; report it if it does
                log.exception("Light change failed")
                ok, message = False, f"Light change failed: {e}"
            self.done.emit(kind, int(value), bool(ok), message)


class CameraLightCard(Card):
    """The Camera Light card. Talks to the page through three signals."""

    activity = Signal(str, str)   # (kind, text) for the Activity Log
    toast = Signal(str, str)      # (message, kind)
    log_line = Signal(str)        # technical line for logs/app.log

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        try:
            from modules.lighting import LightController, MODE, INTENSITY, light_range
            self._light = LightController()
            self._import_error = ""
        except Exception as e:     # don't let this take the Settings page down
            self._light = None
            self._import_error = str(e)
            MODE, INTENSITY = "mode", "intensity"
            light_range = None
        self._MODE, self._INTENSITY = MODE, INTENSITY

        self._lock = threading.Lock()
        self._jobs = {}            # kind -> value, oldest first; only the latest per kind
        self._running = False
        self._workers = []
        self._started = False      # True once the camera is up (attach_backend)
        self._closing = False      # True once the app is exiting (shutdown)

        cl = QVBoxLayout(self)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        header = SectionHeader(tr("Camera Light"))
        self._meta = HeaderMeta()
        header.add_right(self._meta)
        self._reset_btn = compact_button(
            header_button(tr("Reset"), tr("Set the light back to its initial values"),
                          icon="rotate-ccw"), icon="rotate-ccw")
        self._reset_btn.clicked.connect(self._reset_clicked)
        header.add_right(self._reset_btn)
        cl.addWidget(header)

        # Same body layout as the Camera Exposure card: rows at the top, the
        # note under them, spare height below.
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(*CARD_MARGINS)
        bl.setSpacing(10)
        rows_box = QWidget()
        g = QGridLayout(rows_box)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(12)
        g.setVerticalSpacing(6)
        g.setColumnStretch(1, 1)
        bl.addWidget(rows_box)

        saved = config.camera_light()
        self._buttons, self._values, self._current = {}, {}, {}
        rows = ((MODE, tr("Light mode")), (INTENSITY, tr("Intensity")))
        for i, (kind, caption) in enumerate(rows):
            lo, hi = light_range(kind) if light_range else (0, 1)
            self._add_row(g, i * 2, kind, caption, lo, hi, saved.get(kind))

        bl.addWidget(caution_note(tr("Tap a value: it goes to the camera straight away. Watch "
                                     "the live view while you adjust."), "info"))
        bl.addStretch()
        cl.addWidget(body, 1)

        self._set_enabled(False)
        if self._light is None:
            self._unavailable(tr("Light control unavailable ({error})",
                                 error=self._import_error))
        elif not self._light.available:
            self._unavailable(tr("No light control on this camera: {target}.",
                                 target=self._light.describe()))
        else:
            self._meta.set_status(tr("Waiting for camera…"), C['slate'])

    def _add_row(self, g, row, kind, caption, lo, hi, value):
        g.addWidget(field_label(f"{caption}  ({lo}–{hi})"), row, 0, 1, 2)

        # "–" until the value is known (set here before, or read from the camera).
        val = QLabel("–")
        # Same value column as the Camera Exposure card, so the two cards line up.
        val.setMinimumWidth(110)
        val.setStyleSheet(f"color: {C['ck_navy']}; font-size: 14px; font-weight: 700;")

        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        buttons = {}
        for v in _steps(lo, hi):
            b = choice_button(str(v), C['blue'], tr("{caption} {value}", caption=caption,
                                                     value=v))
            b.setMinimumHeight(CONTROL_H)
            b.clicked.connect(lambda _=False, k=kind, v=v: self._choose(k, v))
            h.addWidget(b, 1)
            buttons[v] = b

        g.addWidget(val, row + 1, 0)
        g.addWidget(box, row + 1, 1)
        self._buttons[kind], self._values[kind] = buttons, val
        if value is not None:
            self._select(kind, max(lo, min(hi, int(value))))

    def _select(self, kind, value):
        """Show `value` as the current one: its number, and its button lit."""
        self._current[kind] = int(value)
        self._values[kind].setText(str(int(value)))
        for v, b in self._buttons[kind].items():
            b.setChecked(v == int(value))

    def _choose(self, kind, value):
        """Operator tapped a button: show it and send it."""
        if not self._started:
            return
        self._select(kind, value)
        self._queue(kind, value)

    # Page hooks
    def attach_backend(self):
        """Called once the camera has started: enable the sliders and bring the
        light in line with them."""
        if self._light is None or not self._light.available or self._started:
            return
        self._started = True
        self._set_enabled(True)
        self._meta.set_status(tr("Ready"), C['green_dark'],
                              tr("Controls: {target}", target=self._light.describe()))

        saved = config.camera_light()
        if saved:
            self.log_line.emit(f"[light] Restoring the last light settings: {saved}")
        for kind in (self._MODE, self._INTENSITY):
            if kind in saved:
                self._queue(kind, saved[kind])
        if self._MODE not in saved:
            self._queue(_READ, 0)

    def shutdown(self):
        """App exit: stop sending changes, then turn the light off.

        The worker is joined first (Qt aborts if a running QThread is
        destroyed), so a change still in flight cannot switch the light back on
        after it was turned off. The off values are not saved.
        """
        self._closing = True
        with self._lock:
            self._jobs.clear()
        for w in list(self._workers):
            try:
                if w.isRunning():
                    w.wait(5000)
            except Exception:
                pass
        if (self._light is None or not self._light.available
                or not config.CAMERA_LED_OFF_ON_EXIT):
            return
        try:
            ok, message = self._light.turn_off()
        except Exception as e:     # turn_off() shouldn't raise; never block the exit
            ok, message = False, f"Light off failed: {e}"
        if ok:
            log.info("[light] On exit: %s", message)
        else:
            log.error("[light] On exit, the light was not turned off: %s", message)

    # Worker queue
    def _queue(self, kind, value):
        if self._closing:          # a debounce timer firing during exit
            return
        with self._lock:
            self._jobs.pop(kind, None)       # keep only the latest, at the back
            self._jobs[kind] = int(value)
            if self._running:
                return
            self._running = True
        w = _LightWorker(self._light, self._take_job, self)
        w.done.connect(self._on_done)
        w.mode_read.connect(self._on_mode_read)
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
    @Slot(str, int, bool, str)
    def _on_done(self, kind, value, ok, message):
        self.log_line.emit(f"[light] {message}")
        if ok:
            config.save_camera_light(**{kind: value})
            self._meta.set_status(self._summary(), C['green_dark'], message)
            self.activity.emit("info", tr("Camera light: {change}", change=message))
        else:
            self._meta.set_status(tr("Not applied"), C['red'], message)
            self.activity.emit("error", tr("Camera light not changed — {error}",
                                           error=message))
            self.toast.emit(tr("Camera light could not be changed — try again"), "error")

    @Slot(int)
    def _on_mode_read(self, mode):
        """Show the camera's current mode without sending it back."""
        self._select(self._MODE, mode)
        self.log_line.emit(f"[light] Camera light is in mode {mode}.")

    # Reset
    @Slot()
    def _reset_clicked(self):
        values = config.CAMERA_LED_RESET_VALUES
        if not ConfirmDialog.ask(
                self, tr("Reset Camera Light"), tr("Reset the camera light?"),
                tr("Light mode {mode} and intensity {intensity}.",
                   mode=values.get(self._MODE, "–"),
                   intensity=values.get(self._INTENSITY, "–")),
                confirm_text=tr("RESET")):
            return
        self.reset()

    def reset(self):
        """Send the initial values (CAMERA_LED_RESET_VALUES) and show them."""
        if not self._started:
            return
        self.log_line.emit("[light] Operator reset the camera light")
        values = config.CAMERA_LED_RESET_VALUES
        for kind in (self._MODE, self._INTENSITY):
            if kind not in values:
                continue
            self._select(kind, int(values[kind]))
            self._queue(kind, int(values[kind]))

    # Helpers
    def _summary(self):
        return tr("Mode {mode} · Intensity {intensity}",
                  mode=self._values[self._MODE].text(),
                  intensity=self._values[self._INTENSITY].text())

    def _all_buttons(self):
        return [b for row in self._buttons.values() for b in row.values()]

    def _set_enabled(self, on):
        self._reset_btn.setEnabled(on)
        for b in self._all_buttons():
            b.setEnabled(on)

    def _unavailable(self, why):
        self._meta.set_status(tr("Unavailable"), C['red'], why)
        for w in self._all_buttons() + [self._reset_btn]:
            w.setToolTip(why)
