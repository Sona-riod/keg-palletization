# modules/database.py
"""
Lean persistence layer for the palletization HMI.

Three tables:

  detection_sessions
      The main record: one row per detection/decode capture. It becomes a
      pallet once sent to the cloud (batch_status='api_sent'). `qr_list` holds
      the decoded keg QR codes and `pallet_id` the cloud pallet id.
      This table covers:
        • detected / decoded data            → qr_list, decodedqrcodes
        • "is this keg already in a pallet?" → get_used_kegs()  (api_sent rows)
        • keg-under-camera-too-long warning  → UI timer reading get_used_kegs()

  retry_queue
      Failed cloud sends awaiting retry (offline resilience). A pallet only
      counts as created once it reaches the cloud, so this table is what
      eventually turns an offline capture into an api_sent pallet.

      Queued reports are never given up on. An entry leaves the queue only when
      the cloud accepts it (mark_pallet_sent), rejects it permanently
      (400/404/409, which no replay can fix), or an operator deletes the record.

batch_status on a detection_sessions row is the one authority on whether a
pallet exists. Three values are written:

  api_sent     the cloud acknowledged it and issued a paletteId. Only this is a
               pallet: it is what Pallet Records lists and what get_used_kegs()
               counts as "this keg is already palletized".
  api_pending  captured and queued, waiting for the network. Shown separately as
               pending upload, never as a pallet. Not flagged for attention;
               waiting out an outage is normal.
  api_failed   the cloud refused it permanently. Flagged require_attention,
               because only a human can resolve it.

  keg_moves
      Audit trail for kegs the operator moved from one pallet onto another. The
      move edits the old pallet's `qr_list` in place, so this table is the only
      record that the old pallet ever held that keg: which pallet it came off,
      which it went onto, and when.

Legacy tables (decoded_data, processed_pallets, system_alerts, pallet_lifecycle,
system_events) are dropped on startup if an old DB still has them.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
import threading
from config import DB_PATH, RETRY_BACKOFF_MINUTES

# Thread-safe DB access (shared with api_sender.py via its own import)
db_lock = threading.RLock()

# Retired tables, dropped on startup if an old DB still has them.
_LEGACY_TABLES = ("decoded_data", "processed_pallets", "system_alerts",
                  "pallet_lifecycle", "system_events")

# How the UI shows a timestamp. SQLite returns whatever str() the datetime was
# written with ("2026-09-21 10:14:02.123456"), so they are reformatted here once
# rather than in each table that displays them.
_TS_DISPLAY = "%d-%m-%Y %H:%M:%S"


def _fmt_ts(value) -> str:
    """A stored timestamp in operator-readable form; passed through if unparseable."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime(_TS_DISPLAY)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime(_TS_DISPLAY)
        except ValueError:
            continue
    return text


def retry_backoff_minutes(attempts: int) -> int:
    """Minutes to wait before queue attempt `attempts` (1-based).

    Walks config.RETRY_BACKOFF_MINUTES and then stays on its last entry, so the
    wait has a ceiling (an uncapped exponential would leave a deliverable report
    untried for days). Shared by both writers of next_retry.
    """
    ladder = RETRY_BACKOFF_MINUTES or [1]
    idx = max(int(attempts), 1) - 1
    return int(ladder[min(idx, len(ladder) - 1)])


def _qr_count(qr_json) -> int:
    """How many kegs a stored qr_list actually holds."""
    try:
        codes = json.loads(qr_json or '[]')
    except (json.JSONDecodeError, TypeError):
        return 0
    return len(codes) if isinstance(codes, list) else 0


# The first DatabaseManager of the process starts the DB fresh. This lives here,
# not in main(), because modules/__init__ imports process_worker, whose
# module-level DatabaseManager() runs before main().
_fresh_start_done = False


class DatabaseManager:
    def __init__(self):
        global _fresh_start_done
        self.db_path = str(DB_PATH)
        with db_lock:
            first = not _fresh_start_done
            _fresh_start_done = True
            owner = False
            if first:
                # Fresh DB on every start. Exit wipes too, but a power cut or
                # crash never reaches that, so this covers those cases.
                # Skipped when another running process owns the data.
                from modules.local_data import wipe_local_data
                owner = wipe_local_data("startup")
            self._init_db()
            self._migrate_schema()
            if first and owner:
                # Not when another process owns the data: its queue already
                # holds these reports, and a second copy would send them twice.
                self._restore_outbox()

    def _restore_outbox(self):
        """Put the reports that never reached the cloud back in the queue."""
        from modules import outbox
        conn = sqlite3.connect(self.db_path, timeout=60)
        try:
            outbox.restore_into(conn.cursor())
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[DB] Restoring the offline outbox failed: {e}")
        finally:
            conn.close()

    # Schema
    def _init_db(self):
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            cur.execute('PRAGMA journal_mode=WAL;')
            cur.execute('PRAGMA foreign_keys = ON;')

            # Main detection / decode / pallet table.
            cur.execute('''
                CREATE TABLE IF NOT EXISTS detection_sessions (
                    slno INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE,
                    source_image TEXT,
                    target_keg_count INTEGER DEFAULT 6,
                    detectionstart INTEGER DEFAULT 1,
                    adimgp INTEGER DEFAULT 0,
                    totaldetection INTEGER,
                    decodedqrcodes INTEGER,
                    processing_time REAL,
                    session_timestamp DATETIME,
                    advanced_detection_used INTEGER DEFAULT 0,
                    advanced_qr_found INTEGER DEFAULT 0,
                    qr_list TEXT DEFAULT '[]',
                    batch TEXT,
                    -- ISO-8601 time the layer was READ (payload "scannedTime").
                    -- This is what the cloud writes onto each keg.
                    scanned_time TEXT,
                    batch_status TEXT DEFAULT 'captured',
                    api_status TEXT DEFAULT 'pending',
                    api_attempts INTEGER DEFAULT 0,
                    last_api_attempt DATETIME,
                    last_error TEXT,
                    api_payload TEXT,
                    pallet_id TEXT,
                    require_attention INTEGER DEFAULT 0,
                    attention_reason TEXT,
                    -- Kegs approved to MOVE onto this pallet, held until the
                    -- report is accepted (see _migrate_detection_sessions).
                    move_kegs TEXT,
                    CONSTRAINT valid_status CHECK (
                        batch_status IN ('captured', 'processing', 'api_pending',
                                         'api_sent', 'api_failed', 'duplicate',
                                         'manual_resolved')
                    )
                )
            ''')

            # Retry queue for failed cloud sends (survives power loss).
            cur.execute('''
                CREATE TABLE IF NOT EXISTS retry_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE,
                    payload TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_attempt DATETIME,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    next_retry DATETIME,
                    error_message TEXT,
                    FOREIGN KEY (session_id) REFERENCES detection_sessions(session_id) ON DELETE CASCADE
                )
            ''')

            # Audit trail for operator-approved keg moves between pallets.
            # One row per keg per move. Deliberately not a foreign key on
            # detection_sessions: the history must survive if a pallet row is
            # later pruned.
            cur.execute('''
                CREATE TABLE IF NOT EXISTS keg_moves (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keg_code TEXT NOT NULL,
                    from_pallet TEXT,
                    from_session_id TEXT,
                    to_pallet TEXT,
                    to_session_id TEXT,
                    batch TEXT,
                    moved_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cur.execute('CREATE INDEX IF NOT EXISTS idx_session_ts ON detection_sessions(session_timestamp)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_batch_status ON detection_sessions(batch_status)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_require_attention ON detection_sessions(require_attention)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_next_retry ON retry_queue(next_retry)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_keg_moves_code ON keg_moves(keg_code)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_keg_moves_at ON keg_moves(moved_at)')

            conn.commit()
            conn.close()
            print(f"[DB] Initialized {self.db_path}")

    def _migrate_schema(self):
        """Bring an existing DB up to date: add any missing detection_sessions
        columns, then drop the retired legacy tables."""
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                self._migrate_detection_sessions(cur)
                self._drop_legacy_tables(cur)
                conn.commit()
            finally:
                conn.close()

    def _migrate_detection_sessions(self, cur):
        cur.execute("PRAGMA table_info(detection_sessions)")
        columns = [row[1] for row in cur.fetchall()]
        new_columns = [
            # start_session_complete() writes beer_type on every batch, but
            # _init_db does not create it. Without this, every pallet write on
            # a fresh database fails with "no column named beer_type".
            ('beer_type',        "ALTER TABLE detection_sessions ADD COLUMN beer_type TEXT"),
            ('batch_status',     "ALTER TABLE detection_sessions ADD COLUMN batch_status TEXT DEFAULT 'captured'"),
            ('api_attempts',     "ALTER TABLE detection_sessions ADD COLUMN api_attempts INTEGER DEFAULT 0"),
            ('last_api_attempt', "ALTER TABLE detection_sessions ADD COLUMN last_api_attempt DATETIME"),
            ('last_error',       "ALTER TABLE detection_sessions ADD COLUMN last_error TEXT"),
            ('api_payload',      "ALTER TABLE detection_sessions ADD COLUMN api_payload TEXT"),
            ('require_attention',"ALTER TABLE detection_sessions ADD COLUMN require_attention INTEGER DEFAULT 0"),
            ('attention_reason', "ALTER TABLE detection_sessions ADD COLUMN attention_reason TEXT"),
            ('target_keg_count', "ALTER TABLE detection_sessions ADD COLUMN target_keg_count INTEGER DEFAULT 6"),
            ('api_status',       "ALTER TABLE detection_sessions ADD COLUMN api_status TEXT DEFAULT 'pending'"),
            ('batch',            "ALTER TABLE detection_sessions ADD COLUMN batch TEXT"),
            ('scanned_time',     "ALTER TABLE detection_sessions ADD COLUMN scanned_time TEXT"),
            ('pallet_id',        "ALTER TABLE detection_sessions ADD COLUMN pallet_id TEXT"),
            # Kegs the operator approved to move onto this pallet, parked here
            # until the report is accepted. A queued send may be delivered hours
            # later and the move must still be applied then, or the keg stays
            # listed on its old pallet. Local only; never sent to the cloud.
            ('move_kegs',        "ALTER TABLE detection_sessions ADD COLUMN move_kegs TEXT"),
        ]
        # Older DBs may still carry a `filling_date` column. It is nullable and
        # unused; SQLite cannot drop a column portably, so it is left in place.
        for col_name, sql in new_columns:
            if col_name not in columns:
                try:
                    cur.execute(sql)
                    print(f"[DB] Added column to detection_sessions: {col_name}")
                except Exception as e:
                    print(f"[DB] Error adding {col_name}: {e}")

    def _drop_legacy_tables(self, cur):
        """Remove tables retired in the cleanup if a pre-existing DB still has them."""
        for tbl in _LEGACY_TABLES:
            try:
                cur.execute(f"DROP TABLE IF EXISTS {tbl}")  # nosec B608
            except Exception as e:
                print(f"[DB] Could not drop legacy table {tbl}: {e}")

    # Writes
    def start_session_complete(self, session_data: dict):
        """Write the session once, at the end of processing (success or failure).

        INSERT OR REPLACE, so a re-send on retry updates the existing row.
        """
        qr_json      = json.dumps(session_data.get('qr_list', []))
        payload      = session_data.get('payload')
        payload_json = json.dumps(payload, default=str) if payload else None

        session_id   = session_data.get('session_id')
        batch_status = session_data.get('batch_status')
        error_msg    = session_data.get('error_msg')
        decoded_cnt  = session_data.get('decoded_cnt', 0)
        target_count = session_data.get('target_count', 0)
        elapsed      = session_data.get('elapsed', 0.0)
        api_status   = session_data.get('api_status')
        move_kegs    = session_data.get('move_kegs') or []

        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            cur.execute('''
                INSERT OR REPLACE INTO detection_sessions
                    (session_id, source_image, session_timestamp, detectionstart,
                     target_keg_count, batch, scanned_time,
                     qr_list, decodedqrcodes, totaldetection,
                     advanced_detection_used, advanced_qr_found, adimgp,
                     processing_time, api_status, batch_status,
                     pallet_id, last_error, api_payload,
                     require_attention, attention_reason,
                     last_api_attempt, beer_type, move_kegs)
                VALUES
                    (?, ?, ?, 0,
                     ?, ?, ?,
                     ?, ?, ?,
                     ?, ?, ?,
                     ?, ?, ?,
                     ?, ?, ?,
                     ?, ?,
                     ?, ?, ?)
            ''', (
                session_id, session_data.get('source_image'), datetime.now(), target_count,
                session_data.get('batch'), session_data.get('scanned_time'),
                qr_json, decoded_cnt, decoded_cnt,
                session_data.get('adv_used', 0), session_data.get('adv_found', 0), session_data.get('adimgp', 0),
                elapsed, api_status, batch_status,
                session_data.get('pallet_id'), error_msg, payload_json,
                # Only a permanent rejection needs a human. A report queued
                # because the network is down is normal; flagging it would bury
                # the ones that really need attention.
                1 if batch_status == 'api_failed' else 0,
                error_msg if batch_status == 'api_failed' else None,
                datetime.now(),
                # beer_type column (added by _migrate_detection_sessions if missing).
                (session_data.get('beer_type') or "").strip() or None,
                # Parked for a late delivery; see the column comment.
                json.dumps(move_kegs) if move_kegs else None,
            ))
            conn.commit()
            conn.close()

        if batch_status == 'api_sent':
            from modules import keg_ledger
            keg_ledger.record(session_id, session_data.get('pallet_id') or session_id,
                              session_data.get('qr_list', []))

        print(f"[DB] WROTE {session_id} | {decoded_cnt} QRs | {batch_status} | {elapsed:.2f}s")

    # Cloud confirmation
    def mark_pallet_sent(self, session_id: str, pallet_id: str = None) -> bool:
        """Promote a queued report the cloud has now accepted to a real pallet.

        Without this the row would stay api_pending after a successful retry:
        get_used_kegs() would not see its kegs (no duplicate-keg warning), the
        pallet would not appear in Pallet Records, and the cloud's paletteId
        would be lost, so no label could be printed.

        The update and the queue delete run in one transaction, so a report is
        either still queued or a confirmed pallet, never both or neither.

        Returns True when a session row was actually promoted.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('''
                    UPDATE detection_sessions
                    SET batch_status      = 'api_sent',
                        api_status        = 'success',
                        -- COALESCE: a 2xx with an unparseable body carries no
                        -- paletteId, and that must not wipe one we already have.
                        pallet_id         = COALESCE(?, pallet_id),
                        last_error        = NULL,
                        require_attention = 0,
                        attention_reason  = NULL,
                        last_api_attempt  = ?
                    WHERE session_id = ?
                ''', ((str(pallet_id).strip() or None) if pallet_id else None,
                      datetime.now(), session_id))
                updated = cur.rowcount
                cur.execute('SELECT pallet_id, qr_list FROM detection_sessions '
                            'WHERE session_id = ?', (session_id,))
                sent_row = cur.fetchone()
                cur.execute('DELETE FROM retry_queue WHERE session_id = ?', (session_id,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[DB] mark_pallet_sent({session_id}) failed: {e}")
                return False
            finally:
                conn.close()

        from modules import outbox
        outbox.remove(session_id)
        if updated and sent_row:
            from modules import keg_ledger
            try:
                codes = json.loads(sent_row[1] or '[]')
            except (json.JSONDecodeError, TypeError):
                codes = []
            keg_ledger.record(session_id, sent_row[0] or session_id, codes)
        if updated:
            print(f"[DB] CONFIRMED {session_id} -> pallet {pallet_id or '(no id)'}")
        return updated > 0

    def take_pending_moves(self, session_id: str) -> List[str]:
        """Read and clear the move list parked on a queued report.

        Cleared in the same transaction it is read in, so a move is applied at
        most once even if two sweeps race on the same session.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('SELECT move_kegs FROM detection_sessions WHERE session_id = ?',
                            (session_id,))
                row = cur.fetchone()
                if not row or not row[0]:
                    return []
                cur.execute('UPDATE detection_sessions SET move_kegs = NULL WHERE session_id = ?',
                            (session_id,))
                conn.commit()
                codes = json.loads(row[0])
            except Exception as e:
                conn.rollback()
                print(f"[DB] take_pending_moves({session_id}) failed: {e}")
                return []
            finally:
                conn.close()
        return [str(c) for c in codes if c] if isinstance(codes, list) else []

    def requeue_all_now(self) -> int:
        """Make every queued report due now. Called when the network comes back.

        The backoff ladder protects a server that is refusing us; once the
        network is back it only delays the backlog. Returns how many were made due.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            cur.execute('UPDATE retry_queue SET next_retry = ?', (datetime.now(),))
            n = cur.rowcount
            conn.commit()
            conn.close()
        return max(n, 0)

    # Reads
    def get_pallets(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Cloud-confirmed pallets, newest first, for the Pallet Records table.

        Only batch_status='api_sent' rows. Showing an unacknowledged report here
        would let an operator believe it was recorded upstream.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('''
                    SELECT session_id, pallet_id, batch, session_timestamp, qr_list
                    FROM detection_sessions
                    WHERE batch_status = 'api_sent'
                    ORDER BY session_timestamp DESC
                    LIMIT ?
                ''', (limit,))
                rows = cur.fetchall()
            except Exception as e:
                print(f"[DB] get_pallets error: {e}")
                rows = []
            conn.close()

        return [{'session_id': r[0],
                 'pallet_id':  r[1] or '',
                 'batch':      r[2] or '',
                 'created_at': _fmt_ts(r[3]),
                 # len(qr_list) rather than decodedqrcodes: a move rewrites the
                 # list, and the list is what the pallet holds now.
                 'keg_count':  _qr_count(r[4]),
                 'status':     'CREATED'} for r in rows]

    def get_pending_pallets(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Reports captured locally but not yet acknowledged by the cloud.

        Oldest first, the same order the queue delivers them. Includes both the
        waiting (api_pending, in the queue) and the rejected (api_failed, needs
        a human), told apart by `queued`.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('''
                    SELECT d.session_id, d.batch, d.session_timestamp, d.qr_list,
                           d.batch_status, d.last_error,
                           q.attempts, q.next_retry, q.session_id
                    FROM detection_sessions d
                    LEFT JOIN retry_queue q ON q.session_id = d.session_id
                    WHERE d.batch_status IN ('api_pending', 'api_failed')
                    ORDER BY d.session_timestamp ASC
                    LIMIT ?
                ''', (limit,))
                rows = cur.fetchall()
            except Exception as e:
                print(f"[DB] get_pending_pallets error: {e}")
                rows = []
            conn.close()

        return [{'session_id': r[0],
                 'batch':      r[1] or '',
                 'created_at': _fmt_ts(r[2]),
                 'keg_count':  _qr_count(r[3]),
                 'queued':     r[8] is not None,
                 'attempts':   r[6] or 0,
                 'next_retry': _fmt_ts(r[7]) if r[7] else '',
                 'error':      r[5] or ''} for r in rows]

    def get_session_summary(self, session_id: str) -> Dict[str, Any]:
        """Batch and keg count of one report, as recorded on its label job."""
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('SELECT batch, qr_list FROM detection_sessions '
                            'WHERE session_id = ?', (session_id,))
                row = cur.fetchone()
            except Exception:
                row = None
            conn.close()
        if not row:
            return {'batch': '', 'keg_count': ''}
        return {'batch': row[0] or '', 'keg_count': _qr_count(row[1])}

    def get_pending_count(self) -> int:
        """How many reports are waiting in the upload queue right now."""
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('SELECT COUNT(*) FROM retry_queue')
                n = cur.fetchone()[0] or 0
            except Exception:
                n = 0
            conn.close()
        return n

    def delete_pallet(self, session_id: str) -> bool:
        """Remove one pallet/report record from this device, completely.

        Local only. The pallet-report API has no delete, so a pallet the cloud
        already accepted still exists there; the caller should tell the operator.

        The queue row is deleted explicitly, not via ON DELETE CASCADE:
        `PRAGMA foreign_keys` is per-connection and only set where the schema is
        created, so the cascade would not fire here and the next sweep would
        re-create the pallet from the leftover payload.

        keg_moves is left alone. It is the only record that a keg came off
        another pallet, and the audit trail matters for pallets still here.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('DELETE FROM retry_queue WHERE session_id = ?', (session_id,))
                cur.execute('DELETE FROM detection_sessions WHERE session_id = ?', (session_id,))
                deleted = cur.rowcount
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[DB] delete_pallet({session_id}) failed: {e}")
                return False
            finally:
                conn.close()

        from modules import outbox, keg_ledger
        outbox.remove(session_id)
        keg_ledger.forget_session(session_id)
        if deleted:
            # Its kegs stop appearing in get_used_kegs() from the next reload, so
            # they are free to be palletized again on this device.
            print(f"[DB] DELETED pallet record {session_id} (local only)")
        return deleted > 0

    def get_stuck_batches(self, timeout_minutes: int = 10) -> List[str]:
        """Session ids stuck mid-processing, surfaced for recovery on startup.

        Rows in retry_queue are not stuck; they are waiting their turn to upload.
        Counting them would report a whole overnight backlog as stuck.
        """
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            timeout_time = datetime.now() - timedelta(minutes=timeout_minutes)
            cur.execute('''
                SELECT session_id FROM detection_sessions
                WHERE batch_status IN ('processing', 'api_pending')
                AND session_timestamp < ?
                AND session_id NOT IN (SELECT session_id FROM retry_queue)
            ''', (timeout_time,))
            results = cur.fetchall()
            conn.close()
        return [row[0] for row in results]

    def get_used_kegs(self) -> Dict[str, str]:
        """Map every keg QR on a cloud-sent pallet to that pallet's label.

        Reads detection_sessions rows with batch_status = 'api_sent' (a keg only
        counts as used once its pallet reached the cloud). The label is the cloud
        pallet_id when available, otherwise the local session_id.

        The operator page uses this to warn when an already-palletized keg
        reappears, and to raise the "kegs already made a pallet" warning if it lingers.

        Starts from the keg ledger (modules/keg_ledger.py), which survives the
        start/exit wipe of this DB, so pallets made before a restart still
        count. Rows in this DB are applied on top, being the same or newer.
        """
        from modules import keg_ledger
        used: Dict[str, str] = keg_ledger.load()
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('''
                    SELECT session_id, pallet_id, qr_list
                    FROM detection_sessions
                    WHERE batch_status = 'api_sent'
                    ORDER BY session_timestamp ASC
                ''')
                rows = cur.fetchall()
            except Exception as e:
                print(f"[DB] get_used_kegs error: {e}")
                rows = []
            conn.close()

        for session_id, pallet_id, qr_json in rows:
            label = pallet_id or session_id
            try:
                codes = json.loads(qr_json or '[]')
            except (json.JSONDecodeError, TypeError):
                continue
            for qr in codes:
                code = qr.strip() if isinstance(qr, str) else str(qr).strip()
                if code:
                    used[code] = label   # last (most recent) pallet wins
        return used

    # Keg moves between pallets
    def move_kegs_to_pallet(self, codes: List[str], to_session_id: str,
                            to_pallet: str = None, batch: str = None) -> List[Dict[str, Any]]:
        """Remove `codes` from every other cloud-sent pallet, recording each move.

        The local half of the operator's move decision. The cloud reassigns a
        re-reported keg to the newest pallet itself, but this device's record of
        the old pallet would still list the keg, and get_used_kegs() would keep
        naming a pallet the keg has left.

        Call it only after the new pallet's report succeeded. A crash in between
        then leaves the keg on both pallets (visible, correctable) rather than
        on neither.

        Only `api_sent` rows are touched. Rewriting an unsent report would
        desync it from the payload still in retry_queue.

        Returns
        -------
        [{'code', 'from_pallet', 'from_session_id'}, ...]
            One entry per keg actually removed from a previous pallet, so the
            caller can report exactly what moved. Empty when no local pallet
            record still held any of them.
        """
        wanted = {str(c).strip() for c in (codes or []) if c and str(c).strip()}
        if not wanted:
            return []

        moved: List[Dict[str, Any]] = []
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            cur = conn.cursor()
            try:
                cur.execute('''
                    SELECT session_id, pallet_id, qr_list
                    FROM detection_sessions
                    WHERE batch_status = 'api_sent' AND session_id != ?
                ''', (to_session_id,))
                rows = cur.fetchall()

                for session_id, pallet_id, qr_json in rows:
                    try:
                        stored = json.loads(qr_json or '[]')
                    except (json.JSONDecodeError, TypeError):
                        continue

                    kept, removed = [], []
                    for qr in stored:
                        code = qr.strip() if isinstance(qr, str) else str(qr).strip()
                        if code in wanted:
                            removed.append(code)
                        else:
                            kept.append(qr)      # kept as stored, not rewritten
                    if not removed:
                        continue

                    from_label = pallet_id or session_id
                    cur.execute('''
                        UPDATE detection_sessions
                        SET qr_list = ?, decodedqrcodes = ?, totaldetection = ?
                        WHERE session_id = ?
                    ''', (json.dumps(kept), len(kept), len(kept), session_id))

                    for code in removed:
                        cur.execute('''
                            INSERT INTO keg_moves
                                (keg_code, from_pallet, from_session_id,
                                 to_pallet, to_session_id, batch, moved_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (code, from_label, session_id, to_pallet,
                              to_session_id, batch, datetime.now()))
                        moved.append({'code': code,
                                      'from_pallet': from_label,
                                      'from_session_id': session_id})

                conn.commit()
            except Exception as e:
                # All-or-nothing: a half-applied move would leave a keg listed on
                # a pallet it was already logged as having left.
                conn.rollback()
                print(f"[DB] move_kegs_to_pallet failed, rolled back: {e}")
                moved = []
            finally:
                conn.close()

        if moved:
            from modules import keg_ledger
            keg_ledger.record(to_session_id, to_pallet or to_session_id,
                              [m['code'] for m in moved])
            print(f"[DB] MOVED {len(moved)} keg(s) onto {to_pallet or to_session_id}")
        return moved

    # Retry queue
    def add_to_retry_queue(self, session_id: str, payload: dict, error_msg: str = None,
                           held: bool = False):
        """Add a failed send to the retry queue with capped exponential backoff,
        and keep a copy in the offline outbox so it survives the DB wipe.

        held=True is a report that was never attempted; it went straight into
        the queue behind earlier ones (see process_worker._hold_reason). There is
        no failure to back off from, so it is due at once with 0 attempts.
        """
        payload_json = json.dumps(payload, default=str)
        with db_lock:
            conn = sqlite3.connect(self.db_path, timeout=60)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            cur.execute('SELECT attempts FROM retry_queue WHERE session_id = ?', (session_id,))
            result = cur.fetchone()
            if held:
                attempts = result[0] if result else 0
                next_retry = datetime.now()
            else:
                attempts = result[0] + 1 if result else 1
                next_retry = datetime.now() + timedelta(minutes=retry_backoff_minutes(attempts))

            cur.execute('''
                INSERT OR REPLACE INTO retry_queue
                (session_id, payload, last_attempt, attempts, next_retry, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (session_id, payload_json, datetime.now(),
                  attempts, next_retry, error_msg))

            conn.commit()
            cur.execute('SELECT created_at FROM retry_queue WHERE session_id = ?', (session_id,))
            created_at = cur.fetchone()[0]
            cur.execute('SELECT * FROM detection_sessions WHERE session_id = ?', (session_id,))
            row = cur.fetchone()
            conn.close()

        from modules import outbox
        outbox.save(session_id, dict(row) if row else None, payload_json,
                    created_at, error_msg)
