# modules/batch_history.py
"""
Batch History: every pallet report the cloud confirmed on this camera, with the
batch it recorded.

The camera's own view of what Keg Management shows on its Pallets page and
pallet details: the pallet, the batch, the layer, the operator, the keg counts,
and the scan, send and receive times. All of it comes from the report we sent
and the cloud's reply to it; the pallet-report API has no read endpoint.

Kept in batch_history.db, separate from keg_detection.db, so the start/exit
wipe never clears it. Written once per confirmed report on the upload thread
(one small insert), read only when the operator opens Batch History. Trimmed
to config.BATCH_HISTORY_KEEP_DAYS / BATCH_HISTORY_KEEP_ROWS.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("batch_history")

_lock = threading.Lock()
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_TS_DISPLAY = "%d-%m-%Y %H:%M:%S"     # same as the Pallet Records tables


def _connect():
    conn = sqlite3.connect(str(config.BATCH_HISTORY_DB_PATH), timeout=10)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS reports (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     TEXT UNIQUE,
            pallet_id      TEXT,
            batch_sent     TEXT,
            batch_recorded TEXT,
            layer          INTEGER,
            kegs_sent      INTEGER,
            palletised     INTEGER,
            changed        INTEGER,
            unmatched      TEXT,
            operator       TEXT,
            camera_name    TEXT,
            line_number    TEXT,
            scanned_time   TEXT,
            sent_time      TEXT,
            received_at    TEXT
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_reports_received ON reports(received_at)')
    return conn


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record(session_id, payload, response):
    """Keep one confirmed report. Never raises: the pallet exists either way."""
    try:
        payload = payload or {}
        resp = response if isinstance(response, dict) else {}
        pallet_id = resp.get("paletteId") or resp.get("palletId") or resp.get("id")
        row = (
            session_id,
            str(pallet_id) if pallet_id else None,
            payload.get("batchNumber"),
            resp.get("batchNumber"),
            _int_or_none(resp.get("layer", payload.get("layer"))),
            len(payload.get("kegIds") or []),
            _int_or_none(resp.get("palletisedCount")),
            _int_or_none(resp.get("changedCount")),
            json.dumps(list(resp.get("unmatched") or [])),
            payload.get(config.PALLET_OPERATOR_NAME_FIELD) or "",
            payload.get("cameraName") or "",
            payload.get("lineNumber") or "",
            payload.get("scannedTime"),
            payload.get("timestamp"),
            datetime.now().strftime(_TS_FORMAT),
        )
        cutoff = (datetime.now() - timedelta(days=config.BATCH_HISTORY_KEEP_DAYS)
                  ).strftime(_TS_FORMAT)
        with _lock:
            conn = _connect()
            try:
                with conn:
                    # A repeat of the same report (a retry the cloud answered with
                    # changedCount 0) updates its row instead of adding a second.
                    conn.execute('''
                        INSERT OR REPLACE INTO reports
                        (session_id, pallet_id, batch_sent, batch_recorded, layer,
                         kegs_sent, palletised, changed, unmatched, operator,
                         camera_name, line_number, scanned_time, sent_time, received_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', row)
                    conn.execute('DELETE FROM reports WHERE received_at < ?', (cutoff,))
                    conn.execute('''
                        DELETE FROM reports WHERE id NOT IN (
                            SELECT id FROM reports ORDER BY id DESC LIMIT ?)
                    ''', (int(config.BATCH_HISTORY_KEEP_ROWS),))
            finally:
                conn.close()
    except Exception:
        log.exception(f"{session_id}: could not save to the batch history")


def _local_time(value):
    """A stored timestamp (ISO-8601 UTC from the payload, or local from
    received_at) in operator-readable local time; passed through if unparseable."""
    if not value:
        return ""
    text = str(value).strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime(_TS_DISPLAY)
        return datetime.strptime(text, _TS_FORMAT).strftime(_TS_DISPLAY)
    except ValueError:
        return text


def pallet_ids():
    """Every pallet ID in the history, as a set. Never raises."""
    if not config.BATCH_HISTORY_DB_PATH.exists():
        return set()
    try:
        with _lock:
            conn = _connect()
            try:
                rows = conn.execute('SELECT DISTINCT pallet_id FROM reports '
                                    'WHERE pallet_id IS NOT NULL').fetchall()
            finally:
                conn.close()
        return {str(r[0]) for r in rows if r[0]}
    except Exception:
        log.exception("could not read the pallet IDs from the batch history")
        return set()


def recent(limit=1000):
    """Confirmed reports, newest first, ready to show. Never raises."""
    if not config.BATCH_HISTORY_DB_PATH.exists():
        return []
    try:
        with _lock:
            conn = _connect()
            try:
                rows = conn.execute('''
                    SELECT session_id, pallet_id, batch_sent, batch_recorded, layer,
                           kegs_sent, palletised, changed, unmatched, operator,
                           camera_name, line_number, scanned_time, sent_time,
                           received_at
                    FROM reports ORDER BY id DESC LIMIT ?
                ''', (int(limit),)).fetchall()
            finally:
                conn.close()
    except Exception:
        log.exception("could not read the batch history")
        return []

    out = []
    for r in rows:
        try:
            unmatched = json.loads(r[8] or "[]")
        except (TypeError, ValueError):
            unmatched = []
        batch_sent, batch_recorded = r[2] or "", r[3]
        out.append({
            "session_id":   r[0] or "",
            "pallet_id":    r[1] or "",
            # What the cloud recorded on the kegs; what we sent if it did not say.
            "batch":        batch_recorded if batch_recorded is not None else batch_sent,
            "batch_sent":   batch_sent,
            "batch_differs": batch_recorded is not None and bool(batch_sent)
                             and str(batch_recorded) != str(batch_sent),
            "layer":        r[4] if r[4] is not None else "",
            "kegs_sent":    r[5] or 0,
            "palletised":   r[6],
            "changed":      r[7],
            "unmatched":    unmatched,
            "operator":     r[9] or "",
            "camera_name":  r[10] or "",
            "line_number":  r[11] or "",
            "scanned":      _local_time(r[12]),
            "sent":         _local_time(r[13]),
            "received":     _local_time(r[14]),
            # As stored (YYYY-MM-DD HH:MM:SS, local), for date filtering.
            "received_at":  r[14] or "",
        })
    return out
