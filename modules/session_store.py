# modules/session_store.py
"""
In-memory state machine for one keg-scanning session.

Holds the scan state in one place instead of these flags in main.py:
    processing, data_ready_to_send, auto_confirm_pending,
    auto_pending_frame, _pending_capture, last_captured_qr_set,
    stability_counter, captured_qr_codes

ScanState transitions:
    SCANNING → (target reached + stable) → READY
    READY    → (user/auto presses SEND)  → SENDING
    SENDING  → (API success)             → SENT
    SENDING  → (API failure)             → READY   (allow retry)
    SENT     → (brief delay)             → SCANNING (reset)
    Any      → (manual clear)            → SCANNING

Continuous flow (client requirement):
    After a batch is sent, kegs still under the camera are ignored. Only new
    kegs count toward the next pallet target. When the next target is reached
    the system sends and prints again without operator input.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Set, Optional, Dict


class ScanState(Enum):
    SCANNING = "scanning"   # live detection, accumulating QR codes
    READY    = "ready"      # target count reached, awaiting Send
    SENDING  = "sending"    # API call in progress
    SENT     = "sent"       # success, brief hold before reset


@dataclass
class SessionStore:
    # Batch config (set from UI fields)
    target_count:  int = 6
    batch_number:  str = ""          # -> pallet-report payload "batchNumber"
    mode:          str = "AUTO"      # "AUTO" or "MANUAL"

    # Live detection state (in memory only)
    qr_codes:          Set[str] = field(default_factory=set)
    state:             ScanState = ScanState.SCANNING
    stability_counter: int = 0
    # last_seen: qr -> when the live detector last reported it. This keeps the
    #            list to what is under the camera now, not everything seen since
    #            the last send. Otherwise kegs still in frame after a send stay
    #            in the list, a fresh layer pushes the count past the target,
    #            and AUTO never fires again.
    last_seen:         Dict[str, datetime] = field(default_factory=dict)

    # Capture snapshot (filled in at the moment of capture)
    frame_path:   Optional[str]      = None
    image_name:   Optional[str]      = None
    captured_at:  Optional[datetime] = None
    session_id:   Optional[str]      = None   # set once; the only copy

    # Keg history (continuous-flow guards)
    last_sent_qr_set:  Set[str] = field(default_factory=set)   # last batch only
    sent_qr_history:   Set[str] = field(default_factory=set)   # every keg ever sent (cumulative)
    # stack_qrs: kegs on the layers below, kept in step by the operator page.
    stack_qrs:         Set[str] = field(default_factory=set)

    # Already-palletized kegs (from the DB, plus operator overrides)
    # used_kegs: qr -> pallet label for kegs already in a cloud-sent pallet.
    #            Loaded from the DB at startup so it survives restarts.
    used_kegs:          Dict[str, str] = field(default_factory=dict)
    # flagged_used: qr -> pallet label for already-palletized kegs currently seen
    #               (excluded from the count, shown to the operator).
    flagged_used:       Dict[str, str] = field(default_factory=dict)
    # accepted_overrides: kegs the operator chose to include despite being used.
    accepted_overrides: Set[str] = field(default_factory=set)
    # rejected_kegs: kegs the operator dismissed; they stay excluded, no re-warning.
    rejected_kegs:      Set[str] = field(default_factory=set)
    # duplicate_action: the operator's decision for this pallet's already-
    #            palletized kegs: "" (not asked yet) | "move" | "keep".
    duplicate_action:   str = ""
    # move_kegs: flagged kegs to take off their previous pallet when this one is
    #            sent. Only filled by the "move" decision.
    move_kegs:          Set[str] = field(default_factory=set)
    # ignore_repeats: test mode (Settings). A keg already on a pallet is taken
    # as a new keg: no reminder, no MOVE / KEEP question, AUTO does not wait.
    ignore_repeats:     bool = False

    # QR helpers

    def add_qr(self, code: str) -> str:
        """
        Classify a detected QR code and add it to the current pallet if allowed.

        Returns one of:
            "new"     - new keg, added to the count.
            "flagged" - keg already belongs to a cloud-sent pallet. It is still
                        counted and sent (never dropped), and recorded in
                        ``flagged_used`` so the operator gets a reminder. Returned
                        only the first time the keg is flagged.
            "ignored" - nothing changed (already counted, operator-rejected, or empty).

        Continuous-flow rule: a keg already on another pallet is remembered and the
        operator is reminded, but it is never dropped from the pallet or payload.
        Every detected keg is counted and sent so the line never stalls.
        """
        code = code.strip() if code else ""
        if not code:
            return "ignored"

        # Operator already chose to include this keg: count it, no reminder.
        if code in self.accepted_overrides:
            if code not in self.qr_codes:
                self.qr_codes.add(code)
                return "new"
            return "ignored"

        # Operator removed this keg: keep it out (manual override only).
        if code in self.rejected_kegs:
            return "ignored"

        # The pallet this device just sent is still under the camera because
        # the conveyor has not moved it yet. These kegs are neither a duplicate
        # to warn about nor part of the next pallet:
        #   - warning would fire after every pallet, for a few seconds, about
        #     the pallet the operator just watched succeed;
        #   - counting them would put the layer just sent onto the next report too.
        # So they are ignored until they leave the view, when prune_absent()
        # forgets them. A keg from any older pallet is still flagged below,
        # since that is a real repeat.
        if code in self.last_sent_qr_set:
            return "ignored"

        # Same reasoning for the rest of the stack: a lower layer glimpsed around
        # the edge of the one above is expected, not a repeat.
        if code in self.stack_qrs:
            return "ignored"

        # Keg already on a previous (cloud-sent) pallet?
        # Remember and remind, but do not drop: still counted (qr_codes) and sent.
        pallet = None if self.ignore_repeats else self.used_kegs.get(code)
        if pallet is None and code in self.sent_qr_history and not self.ignore_repeats:
            pallet = "a previous pallet"
        if pallet is not None:
            newly_flagged = code not in self.flagged_used
            self.flagged_used[code] = pallet
            added = code not in self.qr_codes
            self.qr_codes.add(code)                 # counted and sent, not dropped
            # "move" applies to the whole pallet, not just the kegs seen when it
            # was chosen. A keg flagged later moves too; otherwise part of the
            # layer would silently stay on the old pallet.
            if self.duplicate_action == "move":
                self.move_kegs.add(code)
            if newly_flagged:
                return "flagged"                    # first sighting: remind once
            return "new" if added else "ignored"

        # New keg.
        if code not in self.qr_codes:
            self.qr_codes.add(code)
            return "new"
        return "ignored"

    # Keeping the list in step with the camera

    def touch_seen(self, codes, now: datetime = None):
        """Record that `codes` are visible now.

        Must be called before prune_absent() on the same tick, or a code the
        detector has just reported could be pruned by its own tick.
        """
        now = now or datetime.now()
        for raw in (codes or []):
            code = str(raw).strip() if raw is not None else ""
            if code:
                self.last_seen[code] = now

    def prune_absent(self, grace_seconds: float, now: datetime = None) -> list:
        """Drop kegs the detector has not reported for `grace_seconds`.

        This lets the line run continuously. A keg lifted off the conveyor
        stops being counted, so:
          - the count tracks the kegs actually present instead of climbing past
            the target and blocking AUTO;
          - an already-palletized keg the operator is told to remove clears its
            own warning once removed, with nothing to acknowledge;
          - the pallet that just went out stops getting in the way once it moves.

        `grace_seconds` is needed because a label is often unreadable for a frame
        or two (glare, a passing shadow, a partly hidden code). Dropping on the
        first missed frame would make the count jump and keep resetting the
        stability counter, so AUTO could capture at the wrong moment.

        A code with no last_seen stamp is stamped, not dropped. Codes can enter
        qr_codes without coming from the live stream (the MANUAL tiled decode,
        an operator override) and must not vanish because the live detector
        never saw them.

        Returns the removed codes so the caller can redraw and report them.
        """
        now = now or datetime.now()
        removed = []
        for code in list(self.qr_codes):
            seen = self.last_seen.get(code)
            if seen is None:
                self.last_seen[code] = now
                continue
            if (now - seen).total_seconds() >= grace_seconds:
                removed.append(code)

        for code in removed:
            self.qr_codes.discard(code)
            # The keg is gone, so drop any pending decision about it too:
            # nothing to warn about and nothing to move off an old pallet.
            # accepted_overrides / rejected_kegs are intentionally kept. They are
            # the operator's decision for this pallet and must still apply if
            # the keg comes back.
            self.flagged_used.pop(code, None)
            self.move_kegs.discard(code)
            self.last_seen.pop(code, None)
        return removed

    # Operator decisions on already-palletized kegs

    def choose_move(self) -> list:
        """Operator chose to move the flagged kegs onto this pallet.

        Each one joins this pallet and is taken off the pallet it was on. The
        decision holds for the rest of this pallet, so a keg flagged later moves
        too (see add_qr). Returns the codes now marked to move.
        """
        self.duplicate_action = "move"
        self.move_kegs = set(self.flagged_used)
        return self.move_list()

    def choose_keep(self):
        """Operator chose to keep the flagged kegs on their old pallet as well.

        They still join this pallet (nothing is dropped); the previous pallet
        is left as it is.
        """
        self.duplicate_action = "keep"
        self.move_kegs = set()

    def move_list(self) -> list:
        """Kegs to take off their previous pallet, sorted.

        Intersected with the live QR set so a keg the operator removed from the
        list after deciding is not moved off a pallet it never left.
        """
        return sorted(self.move_kegs & self.qr_codes)

    def has_flagged(self) -> bool:
        """True when one or more already-palletized kegs are awaiting a decision."""
        return bool(self.flagged_used)

    def flagged_list(self) -> list:
        """[(code, pallet_label), ...] sorted, for the warning UI."""
        return sorted(self.flagged_used.items())

    def qr_list(self) -> list:
        """Return QR codes as a plain list (for API payload)."""
        return list(self.qr_codes)

    def count(self) -> int:
        return len(self.qr_codes)

    def target_reached(self) -> bool:
        return len(self.qr_codes) == self.target_count

    def over_target(self) -> bool:
        return len(self.qr_codes) > self.target_count

    # Sent-keg helpers

    def ignored_count(self) -> int:
        """Number of kegs currently being ignored (from previous pallets)."""
        return len(self.sent_qr_history)

    def get_ignored_info(self) -> tuple:
        """
        Status of ignored kegs for the HMI.

        Returns
        -------
        (has_ignored: bool, ignored_count: int, message: str)
        """
        ignored = len(self.sent_qr_history)
        if ignored == 0:
            return False, 0, ""
        new_count = len(self.qr_codes)
        return True, ignored, (
            f"{ignored} keg(s) from previous pallet(s) ignored — "
            f"detecting new kegs only ({new_count} new so far)"
        )

    # State transitions

    def reset(self):
        """
        Reset to SCANNING for the next pallet.

        Clears the QR codes, capture data and the per-pallet duplicate decisions
        (flagged / accepted / rejected / move), so a MOVE approved for one pallet
        never carries over to the next.
        Keeps: target_count, batch_number, mode, last_sent_qr_set,
               sent_qr_history and used_kegs (the already-palletized warning
               depends on these).
        """
        self.qr_codes          = set()
        self.last_seen         = {}
        self.state             = ScanState.SCANNING
        self.stability_counter = 0
        self.frame_path        = None
        self.image_name        = None
        self.captured_at       = None
        self.session_id        = None
        self.flagged_used      = {}
        self.accepted_overrides = set()
        self.rejected_kegs     = set()
        self.duplicate_action  = ""
        self.move_kegs         = set()

    def mark_sent(self, pallet_label: Optional[str] = None):
        """
        Record the QR set that was just sent.

        Adds all kegs to the cumulative history and to ``used_kegs`` so they are
        recognised as already-palletized right away, before the next DB reload.
        Moves to the SENT state.
        """
        label = pallet_label or "this session's pallet"
        self.last_sent_qr_set = set(self.qr_codes)
        self.sent_qr_history |= self.qr_codes       # cumulative
        for code in self.qr_codes:
            self.used_kegs[code] = label
        self.state = ScanState.SENT

    def set_used_kegs(self, used: Dict[str, str]):
        """Replace the DB-backed map of kegs already in a cloud-sent pallet."""
        self.used_kegs = dict(used or {})

    def clear_history(self):
        """
        Clear the local already-palletized memory (operator action).

        Use when all kegs have been removed from the camera area and a fresh
        load is starting. The DB is not touched; used_kegs is rebuilt from it
        on the next reload or restart.
        """
        self.sent_qr_history.clear()
        self.last_sent_qr_set.clear()
        self.stack_qrs.clear()
        self.used_kegs.clear()
        self.flagged_used.clear()
        self.accepted_overrides.clear()
        self.rejected_kegs.clear()
        self.move_kegs.clear()
        self.duplicate_action = ""
