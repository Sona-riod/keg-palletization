# modules/print_queue.py
"""
Label print queue. Every pallet label goes through here, one at a time.

Why a single queue:

  • after an outage the backlog confirms in a burst. Parallel jobs on the one
    USB printer collide (the Zebra path resets and claims the device on every
    job), giving [Errno 5] and garbled labels;
  • if the printer is unplugged or out of labels at confirmation time, the job
    must be remembered and printed later.

How it works:

  • enqueue():        the GUI thread hands a job over in memory and returns at
                      once; it never waits on disk or USB;
  • print thread:     saves the job to print_queue.db (separate from the main DB,
                      so the start/exit wipe never loses an unprinted label),
                      then prints jobs strictly one at a time, oldest first;
  • printer not ready: the job waits (re-checked every PRINT_RETRY_SECONDS) and
                      prints by itself when the printer is back;
  • print error:      retried PRINT_MAX_ATTEMPTS times, then FAILED and left for
                      the operator's REPRINT in Pallet Records;
  • crash mid-print:  a job found in `printing` at start-up is marked CHECK, not
                      reprinted. The device cannot know whether paper came out,
                      so the operator decides (REPRINT or LABEL OK).

Events for the UI travel on a small deque (drain_events), the same pattern as
APISender, so no Qt object crosses threads.
"""

import logging
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import config

log = logging.getLogger("print_queue")

# Job states
PENDING  = "pending"     # waiting to print (or waiting for the printer)
PRINTING = "printing"    # handed to the printer right now
PRINTED  = "printed"
FAILED   = "failed"      # gave up after PRINT_MAX_ATTEMPTS; needs REPRINT
CHECK    = "check"       # interrupted mid-print; operator must look at the label
RESOLVED = "resolved"    # operator confirmed a CHECK label is fine
REPLACED = "replaced"    # a waiting/FAILED/CHECK job superseded by a reprint
REMOVED  = "removed"     # operator took it off the queue; it will not print.
                         # Kept (not deleted) so a late confirmation of the same
                         # pallet is still seen as a duplicate and not re-queued.

# Event kinds (see drain_events)
EV_LABEL_PRINTED = "label_printed"    # {pallet_id, reprint}
EV_LABEL_RETRY   = "label_retry"      # {pallet_id, error, attempts}
EV_LABEL_FAILED  = "label_failed"     # {pallet_id, error}
EV_PRINTER_WAIT  = "printer_waiting"  # {pending, reason, hint, detail, supported}
EV_PRINTER_BACK  = "printer_back"     # {pending}
EV_LABEL_CHECK   = "label_check"      # {pallet_ids}, found at start-up
EV_MAX_EVENTS    = 200

_IDLE_WAIT_SECONDS = 600          # nothing to do: sleep until woken (or this)
_HOUSEKEEPING_EVERY = 3600        # seconds between history clean-ups


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _default_probe():
    from modules.printer import probe_printer
    return probe_printer()


# Labels for pallets the cloud has just confirmed, handed over by the upload
# threads (process_worker and the retry monitor) at the moment of confirmation.
# The screen still queues the same label when it handles the result; _save_job
# skips that as a duplicate. This covers the cases where the screen never gets
# there: exit while an upload is finishing, or a backlog confirming faster than
# the screen drains its events.
_label_queue = None
_early_labels = []            # confirmed before the print queue was created
_label_lock = threading.Lock()


def set_label_queue(queue):
    """Register the app's PrintQueue as the target of queue_confirmed_label()."""
    global _label_queue
    with _label_lock:
        _label_queue = queue
        early = list(_early_labels)
        _early_labels.clear()
    for job in early:
        queue.enqueue(**job)


def queue_confirmed_label(pallet_id, session_id="", batch="", keg_count=""):
    """Queue the label for a cloud-confirmed pallet. Never raises."""
    if not pallet_id:
        return
    job = {"pallet_id": pallet_id, "session_id": session_id or "",
           "batch": batch or "", "keg_count": keg_count or ""}
    try:
        with _label_lock:
            queue = _label_queue
            if queue is None:
                _early_labels.append(job)
                return
        queue.enqueue(**job)
    except Exception:
        log.exception(f"could not queue the label for pallet {pallet_id}")


class PrintQueue:
    def __init__(self, printer, probe=None, db_path=None):
        self._printer = printer
        self._probe = probe or _default_probe
        self._db_path = str(db_path or config.PRINT_QUEUE_DB_PATH)

        self._intake = deque()                 # (command, data) from the GUI
        self._intake_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

        self._events = deque(maxlen=EV_MAX_EVENTS)
        self._events_lock = threading.Lock()

        self._pending = 0                      # cached for the badge
        self._waiting_for_printer = False
        self._wait_reason = ""
        self._last_housekeeping = 0.0

        # Every pallet ID this queue has seen, so the operator page can tell a
        # pallet label under the camera from a keg. Filled from print_queue.db
        # now and by every enqueue(), whichever thread confirmed the pallet.
        self._known_ids = {r[0] for r in self._read(
            'SELECT DISTINCT pallet_id FROM print_jobs') if r[0]}

    # Public API (GUI thread)
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Label_Print_Queue",
                                        daemon=True)
        self._thread.start()
        log.info("Print queue started")

    def stop(self, timeout=8.0):
        """Stop after the job in hand (a USB write is bounded by its 5 s timeout).
        Unprinted jobs stay in print_queue.db for the next start."""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self.flush()
        log.info("Print queue stopped")

    def flush(self):
        """Save labels handed over but not yet written to print_queue.db, without
        printing them. Used at stop and once more after the uploads finish on
        exit, so a label for a pallet confirmed during shutdown prints on the
        next start instead of being lost with the process."""
        try:
            self._take_intake()
        except Exception:
            log.exception("print queue flush failed")

    def enqueue(self, pallet_id, session_id="", batch="", keg_count="", reprint=False):
        """Queue one label. Returns at once; the print thread saves and prints."""
        if not pallet_id:
            return False
        self._known_ids.add(str(pallet_id))
        job = {"pallet_id": str(pallet_id), "session_id": session_id or "",
               "batch": batch or "", "keg_count": str(keg_count or ""),
               "reprint": 1 if reprint else 0}
        self._hand_over("add", job)
        return True

    def mark_label_ok(self, job_id):
        """Operator looked at a CHECK label and it printed fine (or dismissed a
        FAILED one they have dealt with another way)."""
        self._hand_over("resolve", int(job_id))

    def remove_job(self, job_id):
        """Operator took one waiting / failed / interrupted label off the queue."""
        self._hand_over("remove", int(job_id))

    def clear_queue(self):
        """Operator took every label not printed yet off the queue. The label
        printing right now is left to finish."""
        self._hand_over("clear", None)

    def knows_pallet(self, code) -> bool:
        """True if `code` is a pallet ID this queue has had a label for."""
        return code in self._known_ids

    def pending_count(self) -> int:
        """Labels not printed yet, for the operator's badge. No disk access."""
        with self._intake_lock:
            queued = sum(1 for cmd, _ in self._intake if cmd == "add")
        return self._pending + queued

    def is_waiting_for_printer(self) -> bool:
        return self._waiting_for_printer

    def drain_events(self):
        with self._events_lock:
            events = list(self._events)
            self._events.clear()
        return events

    def jobs_needing_attention(self, limit=200):
        """Unprinted, failed and interrupted labels, oldest first (Records page)."""
        rows = self._read('''
            SELECT id, pallet_id, session_id, batch, keg_count, status, attempts,
                   last_error, created_at, reprint
            FROM print_jobs
            WHERE status IN (?, ?, ?, ?)
            ORDER BY id ASC LIMIT ?
        ''', (PENDING, PRINTING, FAILED, CHECK, limit))
        return [{"id": r[0], "pallet_id": r[1], "session_id": r[2], "batch": r[3],
                 "keg_count": r[4], "status": r[5], "attempts": r[6],
                 "error": r[7] or "", "created_at": r[8], "reprint": bool(r[9])}
                for r in rows]

    def label_status_by_session(self):
        """{session_id: status of its newest label job}, for the Records column."""
        rows = self._read('''
            SELECT session_id, status FROM print_jobs
            WHERE id IN (SELECT MAX(id) FROM print_jobs
                         WHERE session_id != '' GROUP BY session_id)
        ''')
        return {r[0]: r[1] for r in rows}

    # Internals
    def _hand_over(self, command, data):
        with self._intake_lock:
            self._intake.append((command, data))
        self._wake.set()

    def _publish(self, kind, **data):
        try:
            with self._events_lock:
                self._events.append({"kind": kind, **data})
        except Exception:
            log.debug("print event publish failed", exc_info=True)

    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS print_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id   TEXT NOT NULL,
                session_id  TEXT DEFAULT '',
                batch       TEXT DEFAULT '',
                keg_count   TEXT DEFAULT '',
                reprint     INTEGER DEFAULT 0,
                status      TEXT NOT NULL,
                attempts    INTEGER DEFAULT 0,
                last_error  TEXT,
                created_at  TEXT,
                updated_at  TEXT,
                printed_at  TEXT
            )
        ''')
        return conn

    def _read(self, sql, args=()):
        """A read for the GUI. Never creates the file and never raises."""
        if not config.PRINT_QUEUE_DB_PATH.exists():
            return []
        try:
            conn = self._connect()
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except Exception:
            log.exception("print queue read failed")
            return []

    def _write(self, sql, args=()):
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(sql, args)
                return cur.rowcount
        finally:
            conn.close()

    # Print thread
    def _run(self):
        try:
            self._recover()
            self._housekeeping()
        except Exception:
            log.exception("print queue start-up failed")

        while not self._stop.is_set():
            try:
                self._take_intake()
                job = self._next_job()
                self._refresh_pending()
                if job is None:
                    if time.monotonic() - self._last_housekeeping > _HOUSEKEEPING_EVERY:
                        self._housekeeping()
                    self._sleep(_IDLE_WAIT_SECONDS)
                    continue

                if not self._printer_ready():
                    self._sleep(config.PRINT_RETRY_SECONDS)
                    continue

                self._print_one(job)
            except Exception:
                log.exception("print queue loop error")
                self._stop.wait(config.PRINT_RETRY_SECONDS)

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _recover(self):
        """A job left in `printing` was interrupted. Ask the operator; don't reprint."""
        conn = self._connect()
        try:
            rows = conn.execute('SELECT pallet_id FROM print_jobs WHERE status = ?',
                                (PRINTING,)).fetchall()
            if rows:
                with conn:
                    conn.execute('''
                        UPDATE print_jobs SET status = ?, updated_at = ?,
                            last_error = 'Interrupted while printing — check the label'
                        WHERE status = ?
                    ''', (CHECK, _now(), PRINTING))
        finally:
            conn.close()
        if rows:
            ids = [r[0] for r in rows]
            log.warning(f"{len(ids)} label(s) were interrupted mid-print: {ids}")
            self._publish(EV_LABEL_CHECK, pallet_ids=ids)

    def _take_intake(self):
        with self._intake_lock:
            items = list(self._intake)
            self._intake.clear()
        for command, data in items:
            try:
                if command == "add":
                    self._save_job(data)
                elif command == "resolve":
                    self._write('UPDATE print_jobs SET status = ?, updated_at = ? '
                                'WHERE id = ? AND status IN (?, ?)',
                                (RESOLVED, _now(), data, CHECK, FAILED))
                elif command == "remove":
                    n = self._write('UPDATE print_jobs SET status = ?, updated_at = ? '
                                    'WHERE id = ? AND status IN (?, ?, ?)',
                                    (REMOVED, _now(), data, PENDING, FAILED, CHECK))
                    log.info(f"Label job {data} removed from the queue by the operator"
                             if n else f"Label job {data} not removed (already printing "
                             "or done)")
                elif command == "clear":
                    n = self._write('UPDATE print_jobs SET status = ?, updated_at = ? '
                                    'WHERE status IN (?, ?, ?)',
                                    (REMOVED, _now(), PENDING, FAILED, CHECK))
                    log.info(f"Print queue cleared by the operator: {n} label(s) removed")
            except Exception:
                log.exception(f"print queue could not apply {command}: {data}")

    def _save_job(self, job):
        conn = self._connect()
        try:
            with conn:
                if job["reprint"]:
                    # The reprint takes over from any waiting, failed or
                    # interrupted job, so the label prints only once.
                    conn.execute('''
                        UPDATE print_jobs SET status = ?, updated_at = ?
                        WHERE pallet_id = ? AND status IN (?, ?, ?)
                    ''', (REPLACED, _now(), job["pallet_id"], PENDING, FAILED, CHECK))
                else:
                    # One original label per pallet, however many times the
                    # confirmation reaches us.
                    dup = conn.execute('SELECT 1 FROM print_jobs WHERE pallet_id = ? '
                                       'AND reprint = 0 LIMIT 1',
                                       (job["pallet_id"],)).fetchone()
                    if dup:
                        log.info(f"Label for {job['pallet_id']} already queued — skipped")
                        return
                conn.execute('''
                    INSERT INTO print_jobs (pallet_id, session_id, batch, keg_count,
                        reprint, status, attempts, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                ''', (job["pallet_id"], job["session_id"], job["batch"],
                      job["keg_count"], job["reprint"], PENDING, _now(), _now()))
        finally:
            conn.close()
        log.info(f"Label queued for pallet {job['pallet_id']}"
                 f"{' (reprint)' if job['reprint'] else ''}")

    def _next_job(self):
        conn = self._connect()
        try:
            return conn.execute('''
                SELECT id, pallet_id, attempts, reprint FROM print_jobs
                WHERE status = ? ORDER BY id ASC LIMIT 1
            ''', (PENDING,)).fetchone()
        finally:
            conn.close()

    def _refresh_pending(self):
        conn = self._connect()
        try:
            self._pending = conn.execute('SELECT COUNT(*) FROM print_jobs WHERE status = ?',
                                         (PENDING,)).fetchone()[0]
        finally:
            conn.close()

    def _printer_ready(self):
        try:
            st = self._probe() or {}
            ready, reason = bool(st.get("ready")), st.get("summary") or "Printer not ready"
            hint, detail = st.get("hint") or "", st.get("device_msg") or ""
            supported = st.get("supported") or ""
        except Exception as e:
            ready, reason = False, f"Printer check failed: {e}"
            hint, detail, supported = "", "", ""
        # Told again when the reason changes (e.g. unplugged, then a printer
        # that is not supported is plugged in), not on every re-check.
        if not ready and (not self._waiting_for_printer or reason != self._wait_reason):
            self._waiting_for_printer = True
            self._wait_reason = reason
            log.warning(f"Printer not ready ({reason}: {detail}) — {self._pending} "
                        f"label(s) waiting")
            self._publish(EV_PRINTER_WAIT, pending=self._pending, reason=reason,
                          hint=hint, detail=detail, supported=supported)
        elif ready and self._waiting_for_printer:
            self._waiting_for_printer = False
            self._wait_reason = ""
            log.info(f"Printer back — printing {self._pending} waiting label(s)")
            self._publish(EV_PRINTER_BACK, pending=self._pending)
        return ready

    def _print_one(self, job):
        job_id, pallet_id, attempts, reprint = job
        # Written before the printer is touched. If the app dies mid-print, this
        # row tells the next start that the label may be half-done.
        self._write('UPDATE print_jobs SET status = ?, updated_at = ? WHERE id = ?',
                    (PRINTING, _now(), job_id))
        try:
            ok, err = self._printer.print_pallet_qr(pallet_id)
        except Exception as e:
            log.exception(f"Printing {pallet_id} raised")
            ok, err = False, str(e)

        if ok:
            self._write('UPDATE print_jobs SET status = ?, updated_at = ?, printed_at = ?, '
                        'last_error = NULL WHERE id = ?',
                        (PRINTED, _now(), _now(), job_id))
            log.info(f"Label printed for pallet {pallet_id}")
            self._publish(EV_LABEL_PRINTED, pallet_id=pallet_id, reprint=bool(reprint))
            return

        attempts += 1
        err = (err or "Print job failed")[:300]
        if attempts >= config.PRINT_MAX_ATTEMPTS:
            self._write('UPDATE print_jobs SET status = ?, attempts = ?, last_error = ?, '
                        'updated_at = ? WHERE id = ?',
                        (FAILED, attempts, err, _now(), job_id))
            log.error(f"Label for {pallet_id} FAILED after {attempts} attempt(s): {err}")
            self._publish(EV_LABEL_FAILED, pallet_id=pallet_id, error=err)
        else:
            self._write('UPDATE print_jobs SET status = ?, attempts = ?, last_error = ?, '
                        'updated_at = ? WHERE id = ?',
                        (PENDING, attempts, err, _now(), job_id))
            log.warning(f"Label for {pallet_id} failed (attempt {attempts}): {err}")
            self._publish(EV_LABEL_RETRY, pallet_id=pallet_id, error=err,
                          attempts=attempts)
            # The same job goes next (order is kept); pause before retrying it.
            self._stop.wait(config.PRINT_RETRY_SECONDS)

    def _housekeeping(self):
        """Trim the history. Unprinted / needs-action jobs are never deleted."""
        self._last_housekeeping = time.monotonic()
        cutoff = (datetime.now() - timedelta(days=config.PRINT_KEEP_DAYS)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        done = (PRINTED, RESOLVED, REPLACED, REMOVED)
        conn = self._connect()
        try:
            with conn:
                n = conn.execute('DELETE FROM print_jobs WHERE status IN (?, ?, ?, ?) '
                                 'AND updated_at < ?', (*done, cutoff)).rowcount
                n += conn.execute('''
                    DELETE FROM print_jobs WHERE status IN (?, ?, ?, ?) AND id NOT IN (
                        SELECT id FROM print_jobs ORDER BY id DESC LIMIT ?)
                ''', (*done, int(config.PRINT_KEEP_ROWS))).rowcount
        finally:
            conn.close()
        if n:
            log.info(f"Print history trimmed: {n} old job(s) removed")
