# modules/process_worker.py
"""
Background worker for pallet-report processing.

One captured layer becomes one POST /api/camera/pallet-report: every keg in the
report goes onto one pallet under one batch number, and the cloud answers with
the paletteId the label is printed from.

Design notes:
  - The APISender is injected by main.py via set_api_sender(); none is created here.
  - submit_batch() does not call db.start_session(), which avoids a session_id race.
  - qr_codes are passed in, so processing never reads the DB.
  - Each batch is written to the DB once, at the end (_write_completed_batch()).
  - A retryable failure goes to retry_queue so it survives power loss. A
    permanent 400/404/409 is flagged for the operator instead of retried forever.
  - While the live send runs, the report is already in the offline outbox, so
    a power cut or crash mid-send cannot lose it.
"""

import os
import json
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from modules import outbox, stack_store
from modules.database import DatabaseManager
from modules.api_sender import build_pallet_report, clean_keg_ids, iso_utc
from modules.print_queue import queue_confirmed_label

log = logging.getLogger(__name__)

# Shared resources
db           = DatabaseManager()
executor     = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ProcessWorker")

# In-flight batch tracking. ThreadPoolExecutor only exposes the queued backlog
# (executor._work_queue.qsize()), not tasks already running in its threads. We
# track live futures ourselves so the shutdown screen can show an accurate
# "N background task(s) still finishing" count and the app does not close
# mid-upload.
_inflight       = set()
_inflight_lock  = threading.Lock()

# The app's APISender, injected by main.py after deferred_init()
_api_sender = None


def set_api_sender(instance):
    """
    Inject the app's APISender. Called once from main.py deferred_init(), so
    importing this module does not start a second retry-monitor thread.
    """
    global _api_sender
    _api_sender = instance
    log.info("APISender injected into process_worker")


# Public entry point

def submit_batch(frame_path: str,
                 image_name: str,
                 session_id: str,
                 qr_codes: List[str],
                 required_count: int = 6,
                 batch: str = "",
                 scanned_time: str = None,
                 palette_id: str = None,
                 layer: int = None,
                 reassign_kegs: List[str] = None,
                 beer_type: str = "",
                 beer_type_id: str = "",
                 operator_id: str = "",
                 operator_name: str = "",
                 stack: dict = None,
                 positions: dict = None,
                 layout=None) -> Future:
    """
    Submit a pallet report for background processing.

    Parameters
    ----------
    frame_path     : absolute path to the saved JPEG
    image_name     : filename portion (for DB record)
    session_id     : unique timestamp-based ID created once in main.py. Not
                     created here, to avoid a race condition.
    qr_codes       : QR strings already decoded by the live detector, passed in
                     from SessionStore so the DB is never read here
    required_count : target keg count
    batch          : batch number string from UI -> payload "batchNumber"
    scanned_time   : ISO-8601 time the layer was read -> payload "scannedTime"
    palette_id     : add this layer to an existing pallet (None = start a new one)
    layer          : layer number within the pallet (defaults to PALLET_DEFAULT_LAYER)
    reassign_kegs  : kegs the operator chose to move onto this pallet. On success
                     they are taken off their previous pallet locally, with an
                     audit row. The cloud reassigns them itself.
    beer_type      : selected beer type name -> payload "beerType". "" when the
                     feature is off or nothing is selected; then neither beer
                     key is sent.
    beer_type_id   : its catalogue _id -> payload "beerTypeId" (traceability).
    operator_id    : the operator picked in Settings -> payload "operatorId".
                     "" when nobody is selected; then no operator key is sent.
    operator_name  : their name -> payload "operatorName".
    stack          : the layer's stack fields from StackTracker.begin_layer, or
                     None when pallets are not stacked (see build_pallet_report).
    positions      : {code: (row, col)}, where each keg stood in the layer.
    layout         : (rows, cols) of the layer's grid.
    """
    print("\n" + "=" * 60)
    print("SUBMIT_BATCH CALLED")
    print("=" * 60)
    print(f"  Session ID     : {session_id}")
    print(f"  Frame Path     : {frame_path}")
    print(f"  QR codes in    : {len(qr_codes)} (from SessionStore)")
    print(f"  Required Count : {required_count}")
    print(f"  Batch Number   : {batch}")
    print(f"  Scanned Time   : {scanned_time}")
    print(f"  Palette ID     : {palette_id or '(new pallet)'}")
    print(f"  Move off old   : {len(reassign_kegs or [])} keg(s)")
    print(f"  Beer type      : {beer_type or '(not sent)'}")
    print(f"  Operator       : {operator_name or operator_id or '(not sent)'}")

    log.info(f"Submitting pallet report {session_id} | {len(qr_codes)} QR(s) | batch={batch}")

    # Same UTC-Z shape the payload uses, so the DB record and the wire agree.
    scanned_time = iso_utc() if scanned_time is None else scanned_time

    future: Future = executor.submit(
        _process_one,
        frame_path,
        image_name,
        session_id,
        qr_codes,
        required_count,
        batch,
        scanned_time,
        palette_id,
        layer,
        reassign_kegs,
        beer_type,
        beer_type_id,
        operator_id,
        operator_name,
        stack,
        positions,
        layout,
    )

    with _inflight_lock:
        _inflight.add(future)

    def _callback(f: Future):
        with _inflight_lock:
            _inflight.discard(f)
        try:
            f.result()
            log.debug(f"[{session_id}] Background processing completed")
        except Exception:
            log.exception(f"[{session_id}] Background processing error")

    future.add_done_callback(_callback)
    return future


# Internal worker

def _process_one(frame_path: str,
                 image_name: str,
                 session_id: str,
                 qr_codes: List[str],
                 required_count: int,
                 batch: str,
                 scanned_time: str,
                 palette_id: str = None,
                 layer: int = None,
                 reassign_kegs: List[str] = None,
                 beer_type: str = "",
                 beer_type_id: str = "",
                 operator_id: str = "",
                 operator_name: str = "",
                 stack: dict = None,
                 positions: dict = None,
                 layout=None):
    """
    Runs inside the ThreadPoolExecutor.

    Flow:
    1. Take the QR codes from memory (no DB read).
    2. Run advanced detection on the saved frame only if the count is short.
    3. Build the pallet-report payload.
    4. Send it to the cloud.
    5. One DB write at the end: success goes to detection_sessions,
       a retryable failure goes to retry_queue.
    6. On success only, take operator-approved moved kegs off their
       previous pallets.
    """
    start = time.time()
    log.info(f"[{session_id}] Processing started")

    try:
        # 1. QR codes from SessionStore; advanced detection only if short.
        # Cleaned first so the short-layer test counts distinct kegs; otherwise a
        # duplicate decode pads the count and skips advanced detection.
        final_qrs = clean_keg_ids(qr_codes)
        adv_used = 0
        adv_found = 0

        if len(final_qrs) < required_count:
            log.info(f"[{session_id}] {len(final_qrs)}/{required_count} QRs — running advanced detection")
            try:
                from modules.detector import detect_qr_advanced
                extra_qrs, adv_found = detect_qr_advanced(frame_path)
                extra_strings = [q['data'] if isinstance(q, dict) else q for q in extra_qrs]
                # Merge in first-read order. Do not use a set() union: it
                # randomises the keg order, so the payload and the operator's
                # on-screen list would disagree.
                final_qrs = clean_keg_ids(final_qrs + extra_strings)
                adv_used = 1
                log.info(f"[{session_id}] Advanced detection: {adv_found} found, combined total: {len(final_qrs)}")
            except Exception:
                log.exception(f"[{session_id}] Advanced detection failed")

        # 2. Build the payload (POST /api/camera/pallet-report).
        # build_pallet_report() defines the wire format. Identity fields are read
        # from config at call time, so Settings edits apply immediately.
        # Only kegs actually going onto this pallet can be moved onto it. A code
        # the operator approved that is not in the layer must stay on its pallet.
        # Note: the move is not part of the payload. The report body is the same
        # for MOVE and KEEP; the move is applied to the local pallet records
        # after the send succeeds.
        moving = [c for c in clean_keg_ids(reassign_kegs) if c in set(final_qrs)]

        payload = build_pallet_report(
            keg_ids      = final_qrs,
            batch_number = batch,
            scanned_time = scanned_time,
            palette_id   = palette_id,
            layer        = layer,
            beer_type    = beer_type,
            beer_type_id = beer_type_id,
            operator_id  = operator_id,
            operator_name= operator_name,
            stack        = stack,
            positions    = positions,
            layout       = layout,
            expected_count = required_count,
        )

        log.debug(f"Payload built: {len(payload.get('kegIds', []))} kegs, "
                  f"batch={payload.get('batchNumber')}")
        # Log the exact keg IDs sent so a client-vs-cloud gap (codes the cloud
        # could not match) can be diagnosed against the response body.
        log.info(f"[{session_id}] Sending {len(final_qrs)} keg ID(s): {sorted(final_qrs)}")

        # 3. Send to the API, or hold it behind the backlog
        hold_reason = _hold_reason()
        if hold_reason:
            log.info(f"[{session_id}] Not sent live — {hold_reason}")
            result = {"success": False, "pallet_id": None, "error": hold_reason,
                      "retryable": True, "response": None, "held": True}
        else:
            # On disk before the send, so a power cut or crash during it (a slow
            # network can keep it in retries for minutes) does not lose the
            # layer: the next start re-sends it from the outbox.
            outbox_row = _outbox_row(session_id, image_name, final_qrs, batch,
                                     scanned_time, required_count, payload,
                                     beer_type, moving)
            payload_json = json.dumps(payload, default=str)
            queued_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            outbox.save(session_id, outbox_row, payload_json, queued_at,
                        "Interrupted while sending, re-sent on start-up")
            result = _send_to_api(session_id, payload)
            if result["success"]:
                outbox.remove(session_id)
                stack_store.confirm_layer(session_id, result["pallet_id"])
                resp_body = result.get("response")
                palletised = (resp_body.get("palletisedCount")
                              if isinstance(resp_body, dict) else None)
                queue_confirmed_label(result["pallet_id"], session_id=session_id,
                                      batch=batch,
                                      keg_count=palletised or len(final_qrs))
            elif not result.get("retryable", True):
                # Refused for good: not re-sent, but kept so it is still listed
                # for the operator after a restart.
                outbox.save(session_id, outbox_row, payload_json, queued_at,
                            result["error"], failed=True)
            # A retryable failure is re-saved by add_to_retry_queue below.
        api_success = result["success"]
        pallet_id   = result["pallet_id"]
        error_msg   = result["error"]
        resp        = result.get("response") or {}

        # 4. Single DB write at the end
        elapsed = time.time() - start
        _write_completed_batch({
            "session_id":   session_id,
            "image_name":   image_name,
            "qr_codes":     final_qrs,
            "batch":        batch,
            "scanned_time": scanned_time,
            "required_count": required_count,
            "adv_used":     adv_used,
            "adv_found":    adv_found,
            "elapsed":      elapsed,
            "api_success":  api_success,
            "retryable":    result.get("retryable", True),
            "held":         result.get("held", False),
            "pallet_id":    pallet_id,
            "error_msg":    error_msg,
            "payload":      payload,
            "beer_type":    beer_type,
            # Stored with the row if the send failed, so the move is still
            # applied when the queue later delivers this pallet.
            "move_kegs":    moving,
        })

        # 5. Operator-approved moves, after the new pallet is on record
        moved = []
        if api_success and moving:
            moved = _move_kegs_off_old_pallets(session_id, pallet_id, batch, moving)

        # Delete the frame on success
        if api_success and os.path.exists(frame_path):
            try:
                os.remove(frame_path)
                log.debug(f"[{session_id}] Frame removed after successful send")
            except Exception as e:
                log.warning(f"[{session_id}] Could not remove frame: {e}")

        status = "SUCCESS" if api_success else "FAILED"
        log.info(f"[{session_id}] {len(final_qrs)}/{required_count} QRs | API:{status} | {elapsed:.2f}s")

        # Result for the caller (future.result() in the UI worker).
        # The cloud can palletise fewer kegs than were sent (a code that matches
        # no keg is recorded but not put on the pallet), so pass those counts up
        # for the operator instead of only reporting "sent".
        return {
            "success":          api_success,
            "pallet_id":        pallet_id,
            "error":            error_msg,
            "retryable":        result.get("retryable", True),
            # The layer is on disk and in the upload queue, so the UI can tell
            # the operator it is held, not lost. This separates "waiting for
            # the network" from "the cloud said no".
            "queued":           (not api_success) and result.get("retryable", True),
            # Queued without being attempted: earlier reports were still waiting,
            # or the network was already known to be down.
            "held":             result.get("held", False),
            "session_id":       session_id,
            "qr_count":         len(final_qrs),
            "palletised_count": resp.get("palletisedCount"),
            "unmatched":        list(resp.get("unmatched") or []),
            "moved":            moved,
        }

    except Exception as e:
        elapsed = time.time() - start
        error_msg = f"Unexpected processing error: {str(e)}"
        log.critical(f"[{session_id}] {error_msg}")
        log.exception("Processing error details")

        # Write a failure record; the frame is kept for manual recovery.
        # retryable=False because we failed before building a payload, so there
        # is nothing to queue. Marking it pending would leave a row waiting for
        # an upload that never happens, so it is flagged for attention instead.
        _write_completed_batch({
            "session_id": session_id, "image_name": image_name,
            "qr_codes": list(qr_codes), "batch": batch,
            "scanned_time": scanned_time, "required_count": required_count,
            "adv_used": 0, "adv_found": 0, "elapsed": elapsed,
            "api_success": False, "retryable": False, "pallet_id": None,
            "error_msg": error_msg, "payload": None,
        })
        return {"success": False, "pallet_id": None, "error": error_msg,
                "retryable": False, "queued": False, "session_id": session_id,
                "qr_count": 0,
                "palletised_count": None, "unmatched": [], "moved": []}


def _outbox_row(session_id, image_name, qr_codes, batch, scanned_time,
                required_count, payload, beer_type, move_kegs) -> Dict[str, Any]:
    """The detection_sessions row a report restored from the outbox starts as:
    queued (api_pending), the same row _write_completed_batch writes for a send
    that failed and will be retried."""
    count = len(qr_codes)
    return {
        "session_id":        session_id,
        "source_image":      image_name,
        "session_timestamp": str(datetime.now()),
        "detectionstart":    0,
        "target_keg_count":  required_count,
        "batch":             batch,
        "scanned_time":      scanned_time,
        "qr_list":           json.dumps(qr_codes),
        "decodedqrcodes":    count,
        "totaldetection":    count,
        "adimgp":            1 if count < required_count else 0,
        "api_status":        "queued",
        "batch_status":      "api_pending",
        "api_payload":       json.dumps(payload, default=str),
        "beer_type":         (beer_type or "").strip() or None,
        "move_kegs":         json.dumps(move_kegs) if move_kegs else None,
    }


# Keg move helper

def _move_kegs_off_old_pallets(session_id: str, pallet_id: Optional[str],
                               batch: str, codes: List[str]) -> List[Dict[str, Any]]:
    """Take the operator-approved kegs off their previous pallets.

    Runs only after the report succeeded and the new pallet is on record. The
    keg is added to the new pallet before it leaves the old one, so a failure
    here leaves it listed on both (visible and fixable) rather than on neither.

    Does not raise: the pallet exists and the label is about to print, and a
    bookkeeping problem here must not turn that into a failed batch.
    """
    try:
        moved = db.move_kegs_to_pallet(codes, session_id, pallet_id, batch)
    except Exception:
        log.exception(f"[{session_id}] Could not move kegs off their previous pallets")
        return []

    to_label = pallet_id or session_id
    if moved:
        for m in moved:
            log.info(f"[{session_id}] MOVED keg {m['code']}: pallet "
                     f"{m['from_pallet']} → {to_label}")
    else:
        # Not an error: the cloud decides which pallet a keg is on and may have
        # moved it already. No local pallet record still claimed these codes.
        log.info(f"[{session_id}] MOVE approved for {len(codes)} keg(s), but no local "
                 f"pallet record still held them — nothing to update")
    return moved


# Queue ordering

def _hold_reason() -> Optional[str]:
    """Return why this report must be queued instead of sent live, or None.

    Two cases:
      - Earlier reports are still waiting. Sending this one live would overtake
        them, so pallets and labels would come out of build order.
      - The network is already known to be down. A live attempt would only make
        the operator wait through every retry and timeout before it is queued
        anyway.
    """
    if _api_sender is None:
        return None
    try:
        waiting = int(_api_sender.pending_count())
    except Exception:
        waiting = 0
    if waiting:
        return (f"held behind {waiting} earlier report(s) still waiting to upload "
                f"— it will be sent in order")
    try:
        if _api_sender.is_known_offline():
            return "network is offline — held in the upload queue"
    except Exception:
        pass
    return None


# API send helper

def _send_to_api(session_id: str, payload: dict) -> Dict[str, Any]:
    """
    Send through the injected APISender.
    Returns its result dict: {success, pallet_id, error, retryable, response}.
    """
    if _api_sender is None:
        msg = "APISender not injected — call set_api_sender() first"
        log.error(msg)
        return {"success": False, "pallet_id": None, "error": msg,
                "retryable": True, "response": None}

    print("\n" + "-" * 40)
    print("CALLING API_SENDER.send_batch()")
    print("-" * 40)

    try:
        result = _api_sender.send_batch(
            batch_id  = session_id,
            qr_codes  = payload["kegIds"],
            payload   = payload,
        )
        print(f"  api_sender returned: success={result['success']}, "
              f"pallet_id={result['pallet_id']}, retryable={result['retryable']}")
        if result["success"]:
            log.info(f"[{session_id}] API send OK — pallet_id={result['pallet_id']}")
        else:
            log.error(f"[{session_id}] API send failed — {result['error']}")
        return result

    except Exception as e:
        error_msg = f"API send exception: {str(e)}"
        log.critical(f"[{session_id}] {error_msg}")
        log.exception("API send error details")
        return {"success": False, "pallet_id": None, "error": error_msg,
                "retryable": True, "response": None}


# Single DB write

def _write_completed_batch(ctx: Dict[str, Any]):
    """
    Write the batch to the database in one call at the end of processing.
    Uses the detection_sessions and retry_queue tables.
    """
    session_id     = ctx["session_id"]
    qr_codes       = ctx["qr_codes"]
    required_count = ctx["required_count"]
    api_success    = ctx["api_success"]
    payload        = ctx["payload"]
    error_msg      = ctx["error_msg"]

    decoded_cnt = len(qr_codes)
    adimgp      = 1 if decoded_cnt < required_count else 0
    retryable   = ctx.get("retryable", True)

    # Three outcomes, not two. Pallet Records, the pending-upload table,
    # get_used_kegs() and require_attention all depend on this. Treating
    # "waiting for the network" like "the cloud refused it" would make an
    # offline pallet look failed.
    if api_success:
        api_status, batch_status = "success", "api_sent"
    elif retryable:
        api_status, batch_status = "queued", "api_pending"
    else:
        api_status, batch_status = "failed", "api_failed"

    try:
        # Insert the session record
        db.start_session_complete({
            "session_id"   : session_id,
            "source_image" : ctx["image_name"],
            "qr_list"      : qr_codes,
            "batch"        : ctx["batch"],
            "scanned_time" : ctx["scanned_time"],
            "target_count" : required_count,
            "decoded_cnt"  : decoded_cnt,
            "adv_used"     : ctx["adv_used"],
            "adv_found"    : ctx["adv_found"],
            "adimgp"       : adimgp,
            "elapsed"      : ctx["elapsed"],
            "api_status"   : api_status,
            "batch_status" : batch_status,
            "pallet_id"    : ctx["pallet_id"],
            "error_msg"    : error_msg,
            "payload"      : payload,
            "beer_type"    : ctx.get("beer_type", ""),
            # Stored only when the report failed. On success the caller applies
            # the move right away, so storing it could apply it twice.
            "move_kegs"    : [] if api_success else ctx.get("move_kegs") or [],
        })
        log.debug(f"[{session_id}] DB write complete | status={batch_status}")
    except Exception:
        log.exception(f"[{session_id}] DB write failed")

    # A retryable failure goes to retry_queue so it survives power loss.
    # A permanent fault (400 bad payload, 404 camera not registered, 409 nothing
    # matched) fails the same way on every retry, so it is not queued. The row
    # is already flagged require_attention above for the operator.
    if not api_success and payload:
        if retryable:
            held = bool(ctx.get("held"))
            try:
                db.add_to_retry_queue(session_id, payload, error_msg or "Send failed",
                                      held=held)
                log.info(f"[{session_id}] Added to retry_queue"
                         f"{' (held, due now)' if held else ''}")
            except Exception:
                log.exception(f"[{session_id}] Failed to add to retry_queue")
            # A held report is due now, so wake the monitor instead of waiting
            # for its next tick (it sends nothing while offline).
            if held and _api_sender is not None:
                try:
                    _api_sender.wake_retry_monitor()
                except Exception:
                    pass
        else:
            log.error(f"[{session_id}] NOT queued for retry — permanent failure "
                      f"needing operator action: {error_msg}")


# Utilities

def get_active_tasks() -> int:
    """Number of batch uploads not yet finished, queued or running. The
    shutdown screen uses it to decide whether to wait before releasing
    resources."""
    with _inflight_lock:
        inflight = sum(1 for f in _inflight if not f.done())
    try:
        queued = executor._work_queue.qsize()
    except Exception:
        queued = 0
    # Queued futures are a subset of _inflight, so take the max, not the sum.
    return max(inflight, queued)


def shutdown():
    """Shut down cleanly: finish in-flight uploads, stop the retry monitor and
    close the APISender. Safe to call more than once, from the exit workflow,
    aboutToQuit, or a SIGTERM/SIGINT handler."""
    log.info("Shutting down process_worker...")
    if _api_sender:
        try:
            _api_sender.stop_retry_monitor()
        except Exception:
            log.exception("stop_retry_monitor error")
    # wait=True blocks until every queued and in-flight batch has finished, so
    # no upload is lost on exit. The send paths have network timeouts, so this
    # cannot hang forever.
    try:
        executor.shutdown(wait=True)
    except Exception:
        log.exception("executor shutdown error")
    if _api_sender:
        try:
            _api_sender.close()
        except Exception:
            log.exception("api_sender close error")
    log.info("process_worker shutdown complete")


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
