"""
Application tour: a short guided walk through the operator screen for new
operators, with Skip for anyone who already knows the app.

A transparent overlay covers the main window and dims everything except the
part being explained (a rounded "hole" around it). A small card beside it has
Back / Next / Skip tour. Esc skips; the arrow keys and Enter step through.

The overlay exists only while the tour is open and repaints only when the step
changes or the window resizes. It does not touch detection, sending or
printing, so AUTO mode keeps working underneath.
"""

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QEvent, Signal
from PySide6.QtGui import QPainter, QPainterPath, QColor, QPen
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
                               QHBoxLayout, QScrollArea)

from .theme import C
from .widgets import IconLabel
from .i18n import tr

CARD_W = 340
PAD = 8          # space between a highlighted part and the hole's edge
GAP = 16         # space between the hole and the card


def build_steps(win):
    """The tour for the operator dashboard: [(title, text, targets[, warning])].
    targets is a function returning the widgets to highlight ([] = centred);
    warning is an optional "Take care" line for a risky control."""
    op, sb, tb = win.operator, win.sidebar, win.topbar
    return [
        (tr("Welcome to Keg Palletization"),
         tr("This short tour shows the main parts of the screen. It takes about a "
            "minute. Tap Next to start, or Skip tour if you already know the app."),
         lambda: []),
        (tr("Live camera"),
         tr("The camera watches the pallet. Every keg label it reads gets a green "
            "box with its code."),
         lambda: [op._cam]),
        (tr("Pallet counters"),
         tr("Target = kegs per layer. Detected = kegs seen now. Stability = how "
            "steady the reading is. Status = what the app is doing."),
         lambda: [op._stat_target.parentWidget()]),
        (tr("Scanned kegs"),
         tr("Every keg that is read is listed here. Tap × on a row to remove a wrong "
            "one. Clear empties the list (it asks first)."),
         lambda: [op._scroll.parentWidget()]),
        (tr("What to do next"),
         tr("This box always tells you what is happening and what to do next — for "
            "example \"3 more needed\" or \"Sending pallet…\"."),
         lambda: [op._stack_hint]),
        (tr("AUTO and MANUAL"),
         tr("AUTO sends each full pallet by itself — use it normally. MANUAL is only "
            "for special cases."),
         lambda: [op._auto_btn, op._manual_btn],
         tr("In MANUAL nothing is sent by itself. Switch only when you need to.")),
        (tr("Batch number"),
         tr("Every pallet is recorded with this batch. It never changes by itself — "
            "press NEXT when the batch changes. On a new day the app asks if it is a "
            "new batch."),
         lambda: [op._batch, op._next_batch_btn],
         tr("A wrong batch is recorded on every pallet. Check it at the start of "
            "each batch.")),
        (tr("Kegs per layer and layers per stack"),
         tr("Set how many kegs make one layer and how many layers make one stack. "
            "When a layer has its kegs it is sent, and the next kegs are the next "
            "layer. After the last layer the next kegs start a new stack. Choose 1 "
            "layer if pallets are not stacked."),
         lambda: [op._count, op._layers]),
        (tr("Stack progress"),
         tr("The line tells you what to do now. The picture shows layers done, kegs "
            "scanned on this layer, and every keg in its place. NEW STACK ends the "
            "stack early."),
         lambda: [op._stack_hint, op._stack_map, op._finish_btn]),
        (tr("CAPTURE and SEND"),
         tr("Only needed in MANUAL, or if a pallet was not sent. In AUTO you do not "
            "need them."),
         lambda: [op._capture_btn, op._send_btn],
         tr("Send by hand only if the pallet was NOT sent. SEND asks before sending.")),
        (tr("CLEAR ALL"),
         tr("Empties the scanned list and forgets kegs remembered from earlier layers "
            "and pallets, so only kegs read from now on count. The stack keeps its "
            "layers."),
         lambda: [op._clear_all_btn],
         tr("After clearing, kegs already on a pallet are counted again with no "
            "warning. It asks first.")),
        (tr("Connection"),
         tr("ONLINE means pallets go straight to Keg Management. When OFFLINE, keep "
            "working — pallets are saved and sent automatically later."),
         lambda: [tb._net]),
        (tr("Menu"),
         tr("Pallet Records: sent and saved pallets, and label Reprint. Settings: "
            "printer, camera focus and recent activity."),
         lambda: [b for b, _ in sb._buttons]),
        (tr("Your shift"),
         tr("Your name is shown here. Switch operator lets the next person sign in "
            "when you hand over. Tap Sign out at the end of your shift."),
         lambda: [sb._op_name, sb._switch_btn, sb._sign_btn]),
        (tr("Language"),
         tr("Choose the screen language here. It changes at once, and your choice "
            "comes back when you sign in next time."),
         lambda: [sb._lang_btn]),
        (tr("Restart and Exit"),
         tr("Restart reloads the app. Exit closes it or shuts the camera down."),
         lambda: [tb._restart_btn, tb._exit_btn],
         tr("Exit and Shut Down stop the line until someone starts it again. Both "
            "ask twice first.")),
        (tr("You're ready"),
         tr("That's it. Replay this tour any time with App tour in the menu."),
         lambda: [sb._tour_btn]),
    ]


def build_settings_steps(win):
    """A short tour of the Settings page. Risky controls carry a warning."""
    st = win.settings
    return [
        (tr("Settings"),
         tr("This page is for set-up and checks. You rarely need it during a shift. "
            "Tap Next for a quick look, or Skip tour."),
         lambda: st.show_section("line", [])),
        (tr("Sections"),
         tr("Settings are grouped here: line set-up, label printer, camera and "
            "activity. Tap one to open it."),
         lambda: st.show_section("line", [st._section_nav])),
        # Each step opens the section of the parts it points at.
        (tr("Device identity"),
         tr("Where this camera is: the location and the line number. The camera "
            "number is worked out from the line."),
         lambda: st.show_section("line", [st._loc_edit, st._line_spin, st._cam_edit,
                                          st._save_id_btn]),
         tr("SAVE changes the camera number sent with every pallet. Only change it "
            "when the camera moves to another line.")),
        (tr("Operator"),
         tr("Shows who is on the line. It is set for you when you sign in."),
         lambda: st.show_section("line", [st._operator_combo])),
        (tr("Beer type"),
         tr("Turns the beer type on or off for this line."),
         lambda: st.show_section("line", [st._beer_on_btn, st._beer_off_btn]),
         tr("When ENABLED, a beer type must be chosen before each pallet is sent. "
            "Ask your supervisor before changing it.")),
        (tr("Label printer"),
         tr("Shows whether the label printer is ready. Refresh checks it again."),
         lambda: st.show_section("printer", [st._printer_status, st._printer_refresh_btn,
                                             st._printer_test_btn]),
         tr("Test print uses one real label.")),
        (tr("Camera focus"),
         tr("Use this only if the keg labels look blurry on the camera picture."),
         lambda: st.show_section("camera", [st._focus_btn]),
         tr("Detection pauses while the camera focuses (up to a minute). Keep a "
            "pallet under the camera.")),
        (tr("Audit log"),
         tr("What happened this shift, in plain words: pallets sent, labels printed, "
            "and anything that needs attention."),
         lambda: st.show_section("activity", [st._activity])),
        (tr("That's Settings"),
         tr("Buttons that can cause trouble always ask before doing anything. Use "
            "Back to return to the operator screen."),
         lambda: st.show_section("line", [])),
    ]


class TourOverlay(QWidget):
    """Dimmed overlay with a step card.

    Emits finished(True) after the last step, finished(False) when skipped
    or closed."""
    finished = Signal(bool)

    def __init__(self, parent, steps):
        super().__init__(parent)
        self._steps = steps
        self._i = 0
        self._hole = None
        self._done = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._card = QFrame(self)
        self._card.setObjectName("tourCard")
        self._card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._card.setFixedWidth(CARD_W)
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(6)
        self._step_lbl = QLabel()
        self._step_lbl.setObjectName("tourStep")
        v.addWidget(self._step_lbl)
        self._title = QLabel()
        self._title.setObjectName("tourTitle")
        self._title.setWordWrap(True)
        v.addWidget(self._title)
        self._text = QLabel()
        self._text.setObjectName("tourText")
        self._text.setWordWrap(True)
        v.addWidget(self._text)
        # "Take care" line for risky controls (hidden on ordinary steps).
        self._warn = IconLabel("triangle-alert", "", C["amber_dark"], size=12,
                               weight=600, icon_size=15, bg="rgba(245,158,11,0.12)",
                               radius=6, pad=(10, 7), wrap=True)
        v.addWidget(self._warn)
        v.addSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._skip_btn = QPushButton(tr("Skip tour"))
        self._skip_btn.setObjectName("tourSkip")
        self._skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_btn.clicked.connect(self.skip)
        row.addWidget(self._skip_btn)
        row.addStretch()
        self._back_btn = QPushButton(tr("Back"))
        self._back_btn.setObjectName("ghostBtn")
        self._next_btn = QPushButton(tr("Next"))
        self._next_btn.setObjectName("primaryBtn")
        for b in (self._back_btn, self._next_btn):
            b.setFixedHeight(34)
            b.setMinimumWidth(78)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        self._back_btn.clicked.connect(self.back)
        self._next_btn.clicked.connect(self.next)
        v.addLayout(row)

        parent.installEventFilter(self)
        self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        self.setFocus()
        self._show_step()

    # Navigation
    def next(self):
        if self._i >= len(self._steps) - 1:
            self._close(True)
        else:
            self._i += 1
            self._show_step()

    def back(self):
        if self._i > 0:
            self._i -= 1
            self._show_step()

    def skip(self):
        self._close(False)

    def close_tour(self):
        """Close without counting it as finished (e.g. the shift ended)."""
        self._close(False)

    def _close(self, completed):
        if self._done:
            return
        self._done = True
        p = self.parent()
        if p is not None:
            p.removeEventFilter(self)
        self.hide()
        self.finished.emit(completed)
        self.deleteLater()

    # Layout
    def _show_step(self):
        step = self._steps[self._i]
        title, text = step[0], step[1]
        self._warning = step[3] if len(step) > 3 else ""
        self._warn.setText(self._warning)
        self._warn.setVisible(bool(self._warning))
        n = len(self._steps)
        self._step_lbl.setText(tr("STEP {current} OF {total}", current=self._i + 1, total=n))
        self._title.setText(title)
        self._text.setText(text)
        self._back_btn.setEnabled(self._i > 0)
        last = self._i == n - 1
        self._next_btn.setText(tr("Finish") if last else tr("Next"))
        self._skip_btn.setVisible(not last)
        self._place()

    def _target_rect(self):
        try:
            widgets = self._steps[self._i][2]() or []
        except Exception:
            widgets = []
        rect = QRect()
        for w in widgets:
            if w is None or not w.isVisible():
                continue
            # On a scrolling page, bring the part into view first.
            p = w.parentWidget()
            while p is not None and p is not self.parent():
                if isinstance(p, QScrollArea):
                    p.ensureWidgetVisible(w, 20, 60)
                    break
                p = p.parentWidget()
            r = QRect(w.mapTo(self.parent(), QPoint(0, 0)), w.size())
            rect = r if rect.isNull() else rect.united(r)
        if rect.isNull():
            return None
        return rect.adjusted(-PAD, -PAD, PAD, PAD).intersected(self.rect())

    def _place(self):
        self._hole = self._target_rect()
        self._card.adjustSize()
        cw, ch = self._card.width(), self._card.height()
        W, H = self.width(), self.height()
        hole = self._hole
        if hole is None:
            x, y = (W - cw) // 2, (H - ch) // 2
        elif hole.right() + GAP + cw <= W - 12:                 # right of it
            x, y = hole.right() + GAP, hole.top()
        elif hole.left() - GAP - cw >= 12:                      # left of it
            x, y = hole.left() - GAP - cw, hole.top()
        elif hole.bottom() + GAP + ch <= H - 12:                # below
            x, y = hole.left(), hole.bottom() + GAP
        else:                                                   # above
            x, y = hole.left(), hole.top() - GAP - ch
        x = max(12, min(x, W - cw - 12))
        y = max(12, min(y, H - ch - 12))
        self._card.move(x, y)
        self.update()

    def eventFilter(self, obj, ev):  # noqa: N802 (Qt naming)
        if obj is self.parent() and ev.type() == QEvent.Type.Resize:
            self.setGeometry(self.parent().rect())
            self._place()
        return False

    # Painting and input
    def paintEvent(self, _):  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        if self._hole is not None:
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(self._hole), 8, 8)
            path = path.subtracted(hole)
        p.fillPath(path, QColor(15, 23, 42, 150))
        if self._hole is not None:
            p.setPen(QPen(QColor(C["amber"] if getattr(self, "_warning", "")
                                 else C["ck_pink"]), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self._hole), 8, 8)
        p.end()

    def mousePressEvent(self, e):  # noqa: N802 (Qt naming)
        e.accept()          # the screen underneath is not clickable during the tour

    def keyPressEvent(self, e):  # noqa: N802 (Qt naming)
        k = e.key()
        if k == Qt.Key.Key_Escape:
            self.skip()
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.next()
        elif k == Qt.Key.Key_Left:
            self.back()
        else:
            super().keyPressEvent(e)
