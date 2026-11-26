# modules/stack_tracker.py
"""
The stack being built under the camera.

Pallets are stacked: pallet 1 is layer 1, pallet 2 goes on top of it as layer 2,
and so on. Each layer is scanned and reported on its own before the next one
hides it, so order of arrival gives the layer number and each report's keg list
says which kegs are on that layer.

A closer layer looks bigger to a camera at a fixed height, so the size of the QR
boxes is an independent check. Each layer is compared with the one below it, not
with stored sizes, because any keg type can be stacked. The check assumes one
label size within a stack.

With one layer per stack (the default) this stays out of the way: nothing is
recorded and reports go out exactly as they do without stacking.
"""

import time
from collections import deque
from datetime import datetime
from statistics import median
from typing import List, Optional, Set
from uuid import uuid4

import config
from modules import stack_store

HEIGHT_OK     = "ok"       # send as the expected layer
HEIGHT_MOVING = "moving"   # the pallet is still being lowered: wait
HEIGHT_ASK    = "ask"      # not higher than the layer below: ask the operator

_SAMPLES = 6               # detection rounds that make up one size reading
_MIN_SAMPLES = 3


class StackTracker:
    def __init__(self, layers_target: int = 1):
        self.layers_target = max(1, int(layers_target))
        self.stack_id: Optional[str] = None
        self.layers: List[dict] = []          # finished layers, bottom first
        self._previous_codes: Set[str] = set()   # the stack just closed
        self._samples = deque(maxlen=_SAMPLES)
        self._moving_since = None
        self._accepted = False                # operator said to keep this layer
        self._pending = None                  # layer on its way to the cloud
        self._restore()

    def _restore(self):
        stack = stack_store.open_stack()
        if not stack:
            return
        self.stack_id = stack["stack_id"]
        self.layers = stack["layers"]
        self.layers_target = max(1, int(stack["layers_target"] or 1))

    # What the screen and the report need
    @property
    def active(self) -> bool:
        return self.layers_target > 1 or bool(self.layers)

    @property
    def open(self) -> bool:
        return bool(self.layers)

    @property
    def next_layer(self) -> int:
        return (self.layers[-1]["layer"] + 1) if self.layers else 1

    @property
    def pending_layer(self) -> Optional[int]:
        return self._pending["layer"] if self._pending else None

    def codes(self) -> Set[str]:
        """Kegs on layers already reported, so a glimpse of them is not taken
        for a repeat. The closed stack still counts until the next one has its
        first layer, because it is usually still under the camera."""
        if not self.layers:
            return set(self._previous_codes)
        return {code for layer in self.layers for code in layer["keg_ids"]}

    @property
    def editable(self) -> bool:
        """True while every layer of the open stack is still on the camera."""
        return bool(self.layers) and all(
            l.get("status") == stack_store.LAYER_HELD for l in self.layers)

    def remove_keg(self, layer_no: int, code: str):
        """Take a keg off a layer kept on the camera. Returns (ok, reason):
        reason "sent" (already in Keg Management), "empty" (it is the last keg
        of a layer below the top one: clear the stack instead) or "missing"."""
        layer = next((l for l in self.layers if l["layer"] == layer_no), None)
        if layer is None or code not in layer["keg_ids"]:
            return False, "missing"
        if layer.get("status") != stack_store.LAYER_HELD:
            return False, "sent"
        if len(layer["keg_ids"]) == 1 and layer is not self.layers[-1]:
            return False, "empty"
        if len(layer["keg_ids"]) == 1:
            # The top layer's last keg: the layer goes, and is scanned again.
            stack_store.remove_layer(layer["session_id"])
            self.layers.pop()
            if not self.layers:
                self.stack_id = None
            return True, "layer"
        left = stack_store.remove_keg(layer["session_id"], code)
        if left is None:
            return False, "sent"
        layer["keg_ids"] = left
        layer["positions"] = {k: v for k, v in layer.get("positions", {}).items() if k != code}
        return True, ""

    def discard(self) -> bool:
        """CLEAR STACK: forget the open stack while all of it is still on the
        camera. False when a layer already went to Keg Management."""
        if self.stack_id and self.layers:
            if not self.editable or not stack_store.delete_stack(self.stack_id):
                return False
        self.stack_id, self.layers, self._pending = None, [], None
        self._previous_codes = set()
        return True

    def forget_previous(self):
        """CLEAR ALL: the stack just finished no longer hides its kegs."""
        self._previous_codes = set()

    def layer_of(self, code: str) -> Optional[int]:
        for layer in self.layers:
            if code in layer["keg_ids"]:
                return layer["layer"]
        return None

    # Height check
    def note_sizes(self, sizes):
        """One detection round: the QR box sizes of the kegs being counted."""
        if self.active and sizes:
            self._samples.append(median(sizes))

    def measured_size(self) -> Optional[float]:
        return median(self._samples) if self._samples else None

    def clear_samples(self):
        self._samples.clear()
        self._moving_since = None
        self._accepted = False

    def accept_height(self):
        """The operator confirmed this layer belongs to the stack."""
        self._accepted = True

    def _settled(self, now: float) -> bool:
        """False while the sizes are still changing. Gives up waiting after
        LAYER_SIZE_SETTLE_SECONDS, so a noisy reading can never stall the line."""
        size = median(self._samples)
        spread = (max(self._samples) - min(self._samples)) / size if size else 0
        if len(self._samples) >= _MIN_SAMPLES and spread <= config.LAYER_SIZE_TOLERANCE:
            self._moving_since = None
            return True
        if self._moving_since is None:
            self._moving_since = now
        return now - self._moving_since >= config.LAYER_SIZE_SETTLE_SECONDS

    def check_height(self, now: float = None) -> str:
        """Is the layer under the camera the one expected next?"""
        if (not self.active or self._accepted or not self._samples
                or config.LAYER_SIZE_RATIO <= 0):
            return HEIGHT_OK               # no sizes, or the check is off: arrival order only
        now = time.monotonic() if now is None else now
        if not self._settled(now):
            return HEIGHT_MOVING
        below = self.layers[-1].get("size") if self.layers else None
        if below and median(self._samples) < below * config.LAYER_SIZE_RATIO:
            return HEIGHT_ASK
        return HEIGHT_OK

    def closed_elsewhere(self, status: str = stack_store.STATUS_SHORT):
        """The cloud no longer has this stack open (found at start-up): close it
        here too, without asking the cloud again."""
        sid = self.stack_id
        self.finish(status, "closed-in-cloud")
        if sid:
            stack_store.mark_cloud_closed(sid)

    @property
    def cloud_id(self) -> Optional[str]:
        """The cloud's id for this stack, once it has answered layer 1."""
        return stack_store.cloud_id(self.stack_id)

    # Layer life cycle
    def begin_layer(self, session_id: str, keg_ids, positions=None, layout="",
                    expected=None, held=False, scanned_time=None,
                    batch=None) -> Optional[dict]:
        """A layer is about to be sent, or with held=True kept on the camera
        for the whole-stack report. Returns its stack fields for the report
        ({layer, layers_planned, stack_complete, stack_id}), or None when
        stacking is off, which leaves the report unchanged."""
        if not self.active:
            return None
        if not self.stack_id:
            # The suffix keeps two stacks started within a second apart.
            self.stack_id = f"STK_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:4]}"
        self._pending = {"layer": self.next_layer, "session_id": session_id,
                         "keg_ids": list(keg_ids or []), "size": self.measured_size(),
                         "positions": dict(positions or {}), "layout": layout or "",
                         "expected": expected, "pallet_id": "",
                         "status": stack_store.LAYER_HELD if held else stack_store.LAYER_SENT,
                         "scanned_time": scanned_time or "", "batch": batch or ""}
        stack_store.add_layer(self.stack_id, self.layers_target, self._pending["layer"],
                              session_id, keg_ids, self._pending["size"],
                              positions, layout, expected, self._pending["status"],
                              scanned_time, batch)
        layer = self._pending["layer"]
        return {"layer": layer, "stack_id": self.stack_id,
                "layers_planned": self.layers_target,
                "stack_complete": layer >= self.layers_target}

    def layer_sent(self) -> bool:
        """The layer was delivered or queued. True when that completed the stack."""
        if not self._pending:
            return False
        self.layers.append(self._pending)
        self._pending = None
        self._previous_codes = set()
        if len(self.layers) >= self.layers_target:
            self.finish(stack_store.STATUS_COMPLETE)
            return True
        return False

    def layer_refused(self):
        """Refused while still on the screen: it stays loaded and is sent again."""
        if not self._pending:
            return
        stack_store.remove_layer(self._pending["session_id"])
        self._pending = None
        if not self.layers:
            self.stack_id = None

    def finish(self, status: str = stack_store.STATUS_SHORT, reason: str = None):
        """Close the stack, however it ended. The cloud is told by the last
        layer's stackComplete, or else by stack-close once every layer is
        delivered (APISender.close_finished_stacks)."""
        if self.stack_id and self.layers:
            stack_store.close_stack(self.stack_id, status, reason)
        self._previous_codes = {code for layer in self.layers for code in layer["keg_ids"]}
        self.stack_id = None
        self.layers = []

    def set_layers_target(self, n: int) -> bool:
        """Change the planned height. True if the open stack is now finished."""
        self.layers_target = max(1, int(n))
        if not self.layers:
            return False
        if len(self.layers) >= self.layers_target:
            self.finish(stack_store.STATUS_COMPLETE if self.layers_target > 1
                        else stack_store.STATUS_SHORT, "plan-changed")
            return True
        stack_store.set_layers_target(self.stack_id, self.layers_target)
        return False
