# modules/outbox.py
"""
Offline outbox: reports that could not reach the cloud, kept safe from the
local-data wipe.

keg_detection.db is wiped on every start and exit (modules/local_data.py), and
its retry_queue with it. So every queued report is also copied here, into
offline_outbox.db, which the wipe never touches:

  • sending:      process_worker saves the report here just before the live
                  send and removes it once the cloud accepts it, so a power cut
                  or crash mid-send cannot lose it (it is re-sent on the next
                  start). Runs on the upload worker thread, not the UI thread;
  • network down: add_to_retry_queue saves the report (its detection_sessions
                  row + payload);
  • rejected:     a report the cloud refused for good is kept, marked failed.
                  It is not re-sent, but it comes back after a restart as a
                  record needing attention until the operator discards it;
  • next start:   the fresh DB is refilled from here (restore_into) and the
                  retry monitor delivers the backlog;
  • delivered:    the entry is removed. When the last one goes the file is
                  deleted, so an online device has no outbox at all.

Lock order: database.db_lock may be held while taking _lock, never the reverse.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime

import config

log = logging.getLogger("outbox")

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(str(config.OUTBOX_DB_PATH), timeout=10)
    # Rollback journal + synchronous=FULL (sqlite's default for it): each save
    # is on disk before it returns, so a power cut cannot lose a queued report.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pending_reports (
            session_id    TEXT PRIMARY KEY,
            session_row   TEXT,
            payload       TEXT NOT NULL,
            created_at    TEXT,
            error_message TEXT
        )
    ''')
    # failed = 1: the cloud rejected it permanently. Kept for the operator,
    # never re-sent. Added to outbox files written by older versions.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(pending_reports)")}
    if "failed" not in columns:
        conn.execute("ALTER TABLE pending_reports ADD COLUMN failed INTEGER DEFAULT 0")
    return conn


def save(session_id, session_row, payload_json, created_at, error_message=None,
         failed=False) -> bool:
    """Keep one report. Never raises; returns False if it could not be saved."""
    try:
        with _lock:
            conn = _connect()
            try:
                with conn:
                    conn.execute('''
                        INSERT OR REPLACE INTO pending_reports
                        (session_id, session_row, payload, created_at, error_message, failed)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (session_id,
                          json.dumps(session_row, default=str) if session_row else None,
                          payload_json, str(created_at), error_message,
                          1 if failed else 0))
            finally:
                conn.close()
        return True
    except Exception:
        log.exception(f"{session_id}: could not save to the offline outbox")
        return False


def mark_failed(session_id, error_message):
    """The cloud rejected a queued report for good: stop re-sending it, but keep
    it so it is still listed for the operator after a restart."""
    if not config.OUTBOX_DB_PATH.exists():
        return
    try:
        with _lock:
            conn = _connect()
            try:
                with conn:
                    conn.execute('UPDATE pending_reports SET failed = 1, error_message = ? '
                                 'WHERE session_id = ?', (error_message, session_id))
            finally:
                conn.close()
    except Exception:
        log.exception(f"{session_id}: could not mark as rejected in the offline outbox")


def remove(session_id):
    """Drop a report that was delivered, rejected or deleted; delete the file
    once nothing is left. Just a file-exists check while online."""
    if not config.OUTBOX_DB_PATH.exists():
        return
    try:
        with _lock:
            conn = _connect()
            try:
                with conn:
                    conn.execute('DELETE FROM pending_reports WHERE session_id = ?',
                                 (session_id,))
                left = conn.execute('SELECT COUNT(*) FROM pending_reports').fetchone()[0]
            finally:
                conn.close()
            if left == 0:
                _delete_file()
                log.info("Offline outbox empty — all queued reports delivered; file removed")
    except Exception:
        log.exception(f"{session_id}: could not remove from the offline outbox")


def count() -> int:
    """How many reports are waiting to reach the cloud (rejected ones excluded)."""
    if not config.OUTBOX_DB_PATH.exists():
        return 0
    try:
        with _lock:
            conn = _connect()
            try:
                return conn.execute('SELECT COUNT(*) FROM pending_reports '
                                    'WHERE failed = 0').fetchone()[0]
            finally:
                conn.close()
    except Exception:
        return 0


def restore_into(cur) -> int:
    """Refill a freshly created main DB (cursor `cur`, caller holds db_lock and
    commits) with every report still waiting. Returns how many."""
    if not config.OUTBOX_DB_PATH.exists():
        return 0
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute('''
                SELECT session_id, session_row, payload, created_at, error_message, failed
                FROM pending_reports ORDER BY created_at ASC
            ''').fetchall()
        finally:
            conn.close()
    if not rows:
        with _lock:
            _delete_file()
        return 0

    cur.execute("PRAGMA table_info(detection_sessions)")
    columns = {r[1] for r in cur.fetchall()} - {"slno"}   # slno is re-assigned
    now = datetime.now()
    rejected = 0
    for session_id, session_row, payload, created_at, error_message, failed in rows:
        if session_row:
            row = json.loads(session_row)
            if failed:
                # Listed for the operator as refused, the same as before the restart.
                row.update(batch_status="api_failed", api_status="failed",
                           require_attention=1, attention_reason=error_message,
                           last_error=error_message)
            # Only names the table really has (PRAGMA above) reach the SQL text;
            # every value is bound.
            row = {k: v for k, v in row.items() if k in columns}
            names = ", ".join(row)
            marks = ", ".join("?" * len(row))
            cur.execute(f"INSERT OR IGNORE INTO detection_sessions ({names}) VALUES ({marks})",  # nosec B608
                        tuple(row.values()))
        if failed:
            rejected += 1
            continue
        # Due now, oldest first, so the retry monitor delivers them in capture order.
        cur.execute('''
            INSERT OR IGNORE INTO retry_queue
            (session_id, payload, created_at, attempts, next_retry, error_message)
            VALUES (?, ?, ?, 0, ?, ?)
        ''', (session_id, payload, created_at, now, error_message))
    log.info(f"Restored {len(rows) - rejected} unsent report(s) from the offline outbox"
             f"{f' and {rejected} rejected one(s) needing attention' if rejected else ''}")
    return len(rows)


def _delete_file():
    """Caller holds _lock."""
    for suffix in ("", "-journal"):
        path = config.OUTBOX_DB_PATH.with_name(config.OUTBOX_DB_PATH.name + suffix)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning(f"Could not delete {path.name}: {e}")
