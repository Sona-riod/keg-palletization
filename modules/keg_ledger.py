# modules/keg_ledger.py
"""
Which pallet each keg was last put on, kept across restarts.

keg_detection.db is wiped on every start and exit (modules/local_data.py), so
on its own the "this keg is already on a pallet" warning forgot every pallet
made before a restart or power cut. This file keeps just enough to bring that
warning back: one row per keg code with the label of the cloud-confirmed
pallet it is on.

Kept in keg_ledger.db, separate from keg_detection.db, so the wipe never
clears it. Rows older than config.KEG_LEDGER_KEEP_DAYS are dropped when the
ledger is read. It holds keg codes and pallet labels only, no operator data.

Every function here is best effort and never raises: losing a ledger write
only means one fewer warning, never a lost or blocked pallet.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, Iterable

import config

log = logging.getLogger("keg_ledger")

_lock = threading.Lock()
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _connect():
    conn = sqlite3.connect(str(config.KEG_LEDGER_DB_PATH), timeout=10)
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS pallet_kegs (
            code        TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            session_id  TEXT,
            updated_at  TEXT NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_pallet_kegs_session '
                 'ON pallet_kegs(session_id)')
    return conn


def _clean_codes(codes: Iterable) -> list:
    out = []
    for qr in codes or []:
        code = qr.strip() if isinstance(qr, str) else str(qr).strip()
        if code:
            out.append(code)
    return out


def record(session_id: str, label: str, codes: Iterable) -> None:
    """Note that `codes` are now on the pallet `label` (the newest pallet wins)."""
    codes = _clean_codes(codes)
    label = str(label or session_id or "").strip()
    if not codes or not label:
        return
    now = datetime.now().strftime(_TS_FORMAT)
    rows = [(c, label, session_id, now) for c in codes]
    try:
        with _lock:
            conn = _connect()
            try:
                conn.executemany('''
                    INSERT OR REPLACE INTO pallet_kegs
                        (code, label, session_id, updated_at)
                    VALUES (?, ?, ?, ?)
                ''', rows)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        log.warning("Could not record %d keg(s) for %s: %s", len(rows), label, e)


def forget_session(session_id: str) -> None:
    """Drop the kegs of a pallet record the operator removed from this device."""
    if not session_id:
        return
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute('DELETE FROM pallet_kegs WHERE session_id = ?',
                             (session_id,))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        log.warning("Could not forget kegs of %s: %s", session_id, e)


def load() -> Dict[str, str]:
    """{keg code: pallet label} for every keg recorded within the keep window.

    Older rows are deleted on the way, so the file stays small."""
    keep_days = max(int(getattr(config, "KEG_LEDGER_KEEP_DAYS", 30) or 0), 1)
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime(_TS_FORMAT)
    try:
        with _lock:
            conn = _connect()
            try:
                conn.execute('DELETE FROM pallet_kegs WHERE updated_at < ?', (cutoff,))
                conn.commit()
                rows = conn.execute('SELECT code, label FROM pallet_kegs').fetchall()
            finally:
                conn.close()
    except Exception as e:
        log.warning("Could not read the keg ledger: %s", e)
        return {}
    return {code: label for code, label in rows}
