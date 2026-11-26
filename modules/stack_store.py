# modules/stack_store.py
"""
Stack history: which pallets were stacked together, and which kegs are on which
layer, in which cell.

Every physical pallet is one layer of a stack and keeps its own pallet report,
pallet ID and label. A row is written just before a layer is sent, so a restart
mid-stack carries on at the right layer, and the pallet ID is filled in when the
cloud confirms the report (live or from the upload queue).

Each stack has a local id (STK_...), known from the first layer on, and the
cloud's stack id (2026-11-000019-stack), known once the cloud has answered
layer 1. Reports are built before that answer can exist (a layer queued
offline), so the cloud id is put into each later layer's report at the moment
it is sent (attach_stack_id). A stack closed here before the cloud closed it is
closed there with POST /api/camera/stack-close once its layers are delivered
(stacks_to_close).

Kept in stack_history.db, separate from keg_detection.db, so the start/exit wipe
never clears it. Closed stacks are trimmed to config.BATCH_HISTORY_KEEP_DAYS.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

import config

log = logging.getLogger("stack_store")

_lock = threading.Lock()
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

STATUS_OPEN = "open"
STATUS_COMPLETE = "complete"
STATUS_SHORT = "short"           # finished with fewer layers than planned

LAYER_HELD = "held"              # kept on the camera until the whole stack is sent
LAYER_SENT = "sent"              # handed to the send path, or queued
LAYER_CONFIRMED = "confirmed"    # the cloud issued its pallet ID
LAYER_REJECTED = "rejected"      # refused by the cloud from the upload queue

# Columns added after the first release; added in place on an older file.
_NEW_COLUMNS = (
    ("stacks", "cloud_stack_id", "TEXT"),
    ("stacks", "cloud_closed", "INTEGER DEFAULT 0"),
    ("stacks", "stack_qr", "TEXT"),
    ("stacks", "close_reason", "TEXT"),
    ("stack_layers", "positions", "TEXT"),
    ("stack_layers", "layout", "TEXT"),
    ("stack_layers", "expected", "INTEGER"),
    ("stack_layers", "scanned_time", "TEXT"),
    ("stack_layers", "batch", "TEXT"),
    ("stacks", "report_id", "TEXT"),       # whole-stack report (stack-report)
    ("stacks", "report_json", "TEXT"),
    ("stacks", "report_status", "TEXT"),   # pending / sent / rejected
    ("stacks", "report_error", "TEXT"),
)

REPORT_PENDING = "pending"
REPORT_SENT = "sent"
REPORT_REJECTED = "rejected"



def _connect():
    conn = sqlite3.connect(str(config.STACK_HISTORY_DB_PATH), timeout=10)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stacks (
            stack_id      TEXT PRIMARY KEY,
            status        TEXT NOT NULL,
            layers_target INTEGER,
            started_at    TEXT,
            closed_at     TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS stack_layers (
            session_id    TEXT PRIMARY KEY,
            stack_id      TEXT NOT NULL,
            layer         INTEGER,
            pallet_id     TEXT,
            keg_ids       TEXT,
            status        TEXT,
            measured_size REAL
        )
    ''')
    for table, column, kind in _NEW_COLUMNS:
        # table / column / kind come from the _NEW_COLUMNS constant above.
        have = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}  # nosec B608
        if column not in have:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')  # nosec B608
    conn.execute('CREATE INDEX IF NOT EXISTS idx_layers_stack ON stack_layers(stack_id)')
    return conn


def _run(what, fn):
    """Run fn(conn) in a transaction. Never raises: the pallet report matters
    more than this record, so a failure here must not stop the line."""
    try:
        with _lock:
            conn = _connect()
            try:
                with conn:
                    return fn(conn)
            finally:
                conn.close()
    except Exception:
        log.exception(f"could not {what}")
        return None


def _kegs(text):
    try:
        return list(json.loads(text or "[]"))
    except (TypeError, ValueError):
        return []


def _positions(text):
    """{code: (row, col)} from the stored JSON."""
    try:
        return {str(k): tuple(v) for k, v in (json.loads(text or "{}") or {}).items()
                if isinstance(v, (list, tuple)) and len(v) == 2}
    except (TypeError, ValueError, AttributeError):
        return {}


def add_layer(stack_id, layers_target, layer, session_id, keg_ids, size=None,
              positions=None, layout=None, expected=None, status=LAYER_SENT,
              scanned_time=None, batch=None):
    """Record a layer about to be sent (or, status LAYER_HELD, kept on the
    camera for the whole-stack report), opening its stack on the first layer.
    positions: {code: (row, col)}; layout: "2x3"; expected: kegs planned."""
    now = datetime.now().strftime(_TS_FORMAT)

    def write(conn):
        conn.execute('''
            INSERT OR IGNORE INTO stacks (stack_id, status, layers_target, started_at)
            VALUES (?, ?, ?, ?)
        ''', (stack_id, STATUS_OPEN, int(layers_target), now))
        conn.execute('''
            INSERT OR REPLACE INTO stack_layers
            (session_id, stack_id, layer, keg_ids, status, measured_size,
             positions, layout, expected, scanned_time, batch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (session_id, stack_id, int(layer), json.dumps(list(keg_ids or [])),
              status, size,
              json.dumps({k: list(v) for k, v in (positions or {}).items()}),
              layout or None, int(expected) if expected else None,
              scanned_time, batch or None))
    _run(f"record layer {layer} of {stack_id}", write)


def remove_layer(session_id):
    """Forget a layer the cloud refused while it was still on the screen. It
    stays loaded and will be sent again. An emptied stack goes with it."""
    def write(conn):
        row = conn.execute('SELECT stack_id FROM stack_layers WHERE session_id = ?',
                           (session_id,)).fetchone()
        if not row:
            return
        conn.execute('DELETE FROM stack_layers WHERE session_id = ?', (session_id,))
        left = conn.execute('SELECT COUNT(*) FROM stack_layers WHERE stack_id = ?',
                            (row[0],)).fetchone()[0]
        if not left:
            conn.execute('DELETE FROM stacks WHERE stack_id = ?', (row[0],))
    _run(f"remove layer {session_id}", write)


def confirm_layer(session_id, pallet_id):
    """The cloud confirmed this report. No-op for a report outside any stack."""
    _run(f"confirm layer {session_id}", lambda conn: conn.execute(
        'UPDATE stack_layers SET pallet_id = ?, status = ? WHERE session_id = ?',
        (str(pallet_id) if pallet_id else None, LAYER_CONFIRMED, session_id)))


def reject_layer(session_id):
    """The cloud refused a queued report. No-op for a report outside any stack."""
    _run(f"reject layer {session_id}", lambda conn: conn.execute(
        'UPDATE stack_layers SET status = ? WHERE session_id = ?',
        (LAYER_REJECTED, session_id)))


def set_layers_target(stack_id, layers_target):
    _run(f"update {stack_id}", lambda conn: conn.execute(
        'UPDATE stacks SET layers_target = ? WHERE stack_id = ?',
        (int(layers_target), stack_id)))


def close_stack(stack_id, status, reason=None):
    now = datetime.now()
    cutoff = (now - timedelta(days=config.BATCH_HISTORY_KEEP_DAYS)).strftime(_TS_FORMAT)

    def write(conn):
        conn.execute('UPDATE stacks SET status = ?, closed_at = ?, close_reason = ? '
                     'WHERE stack_id = ?',
                     (status, now.strftime(_TS_FORMAT), reason, stack_id))
        conn.execute('''
            DELETE FROM stack_layers WHERE stack_id IN (
                SELECT stack_id FROM stacks WHERE status != ? AND closed_at < ?)
        ''', (STATUS_OPEN, cutoff))
        conn.execute('DELETE FROM stacks WHERE status != ? AND closed_at < ?',
                     (STATUS_OPEN, cutoff))
    _run(f"close {stack_id}", write)


# The cloud's side of a stack
def _layer_row(conn, session_id):
    """(local stack id, layer, layers_target, cloud stack id) of a report."""
    return conn.execute('''
        SELECT l.stack_id, l.layer, s.layers_target, s.cloud_stack_id
        FROM stack_layers l JOIN stacks s ON s.stack_id = l.stack_id
        WHERE l.session_id = ?
    ''', (session_id,)).fetchone()


def attach_stack_id(session_id, payload):
    """Put the cloud's stack id into a layer-2-or-later report just before it
    is sent. Layer 1 goes without one: the cloud opens the stack and answers
    with its id. Returns the id put in, or None. Changes nothing for a report
    outside any stack."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return None

    def read(conn):
        return _layer_row(conn, session_id)
    row = _run(f"read the stack of {session_id}", read)
    if not row or row[1] <= 1:
        return None
    if not row[3]:
        # Layer 1 was refused or never answered. Sent alone, the cloud opens a
        # new stack with it, and the next layers follow that one.
        log.warning(f"{session_id}: layer {row[1]} of {row[0]} has no cloud stack id "
                    f"yet — sent as the start of a new stack")
        payload.update(restart_fields(session_id) or {})
        payload.pop("stackId", None)
        payload.pop("layer", None)
        return None
    payload["stackId"] = row[3]
    return row[3]


def restart_fields(session_id):
    """Stack fields for a layer the cloud must take as the first of a new
    stack (its stack was closed or unknown there): no stackId and no layer
    number, and the layers still to come as the plan."""
    def read(conn):
        return _layer_row(conn, session_id)
    row = _run(f"read the stack of {session_id}", read)
    if not row:
        return None
    left = max(1, int(row[2] or 1) - int(row[1]) + 1)
    fields = {"layersPlanned": left}
    if left <= 1:
        fields["stackComplete"] = True
    return fields


def note_cloud_reply(session_id, reply):
    """Keep what the cloud said about the stack of a delivered layer.

    Returns {"stack_id", "cloud_id", "closed", "qr"} when the reply carries a
    stack; "closed" is True only the first time the cloud reports it closed,
    so its label is printed once. None otherwise."""
    if not isinstance(reply, dict) or not config.STACK_HISTORY_DB_PATH.exists():
        return None
    stack = reply.get("stack") if isinstance(reply.get("stack"), dict) else {}
    cloud_id = reply.get("stackId") or stack.get("stackId")
    if not cloud_id:
        return None
    closed = bool(stack.get("complete")) or stack.get("status") in (STATUS_COMPLETE,
                                                                    STATUS_SHORT)
    qr = ((stack.get("qr") or {}).get("value") if isinstance(stack.get("qr"), dict)
          else None) or cloud_id

    def write(conn):
        row = _layer_row(conn, session_id)
        if not row:
            return None
        local, _, _, before = row
        if before and before != cloud_id:
            log.warning(f"{local}: the cloud moved this stack from {before} to "
                        f"{cloud_id} (the first was closed there)")
        was_closed = conn.execute('SELECT cloud_closed FROM stacks WHERE stack_id = ?',
                                  (local,)).fetchone()[0]
        newly = closed and (not was_closed or before != cloud_id)
        conn.execute('UPDATE stacks SET cloud_stack_id = ?, stack_qr = ?, '
                     'cloud_closed = ? WHERE stack_id = ?',
                     (cloud_id, qr, 1 if closed else 0, local))
        return {"stack_id": local, "cloud_id": cloud_id, "closed": bool(newly), "qr": qr}
    return _run(f"note the cloud stack of {session_id}", write)


def stacks_to_close():
    """Stacks finished here that the cloud still has open, once every layer is
    delivered or refused: [(local id, cloud id, status, reason)]."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return []

    def read(conn):
        return conn.execute('''
            SELECT s.stack_id, s.cloud_stack_id, s.status, s.close_reason FROM stacks s
            WHERE s.status != ? AND s.cloud_stack_id IS NOT NULL
              AND COALESCE(s.cloud_closed, 0) = 0
              AND NOT EXISTS (SELECT 1 FROM stack_layers l
                              WHERE l.stack_id = s.stack_id AND l.status = ?)
            ORDER BY s.closed_at
        ''', (STATUS_OPEN, LAYER_SENT)).fetchall()
    return _run("read the stacks to close", read) or []


def mark_cloud_closed(stack_id, qr=None):
    _run(f"mark {stack_id} closed in the cloud", lambda conn: conn.execute(
        'UPDATE stacks SET cloud_closed = 1, stack_qr = COALESCE(?, stack_qr) '
        'WHERE stack_id = ?', (qr, stack_id)))


def cloud_id(stack_id):
    """The cloud's id for a local stack, or None before layer 1 is answered."""
    if not stack_id or not config.STACK_HISTORY_DB_PATH.exists():
        return None
    row = _run(f"read {stack_id}", lambda conn: conn.execute(
        'SELECT cloud_stack_id FROM stacks WHERE stack_id = ?', (stack_id,)).fetchone())
    return row[0] if row else None


# Editing a stack kept on the camera (whole-stack mode)
def remove_keg(session_id, code):
    """Take one keg off a layer that is still kept on the camera. Returns the
    kegs left on it, or None when the layer is not there or already sent."""
    def write(conn):
        row = conn.execute('SELECT keg_ids, positions, status FROM stack_layers '
                           'WHERE session_id = ?', (session_id,)).fetchone()
        if not row or row[2] != LAYER_HELD:
            return None
        kegs = [k for k in _kegs(row[0]) if k != code]
        pos = {k: list(v) for k, v in _positions(row[1]).items() if k != code}
        conn.execute('UPDATE stack_layers SET keg_ids = ?, positions = ? '
                     'WHERE session_id = ?', (json.dumps(kegs), json.dumps(pos), session_id))
        return kegs
    return _run(f"remove {code} from {session_id}", write)


def delete_stack(stack_id):
    """Forget a stack none of whose layers has left the camera (CLEAR STACK).
    Returns False when one has (it is in Keg Management and stays on record)."""
    def write(conn):
        sent = conn.execute('SELECT COUNT(*) FROM stack_layers WHERE stack_id = ? '
                            'AND status != ?', (stack_id, LAYER_HELD)).fetchone()[0]
        reported = conn.execute('SELECT report_status FROM stacks WHERE stack_id = ?',
                                (stack_id,)).fetchone()
        if sent or (reported and reported[0]):
            return False
        conn.execute('DELETE FROM stack_layers WHERE stack_id = ?', (stack_id,))
        conn.execute('DELETE FROM stacks WHERE stack_id = ?', (stack_id,))
        return True
    return bool(_run(f"delete {stack_id}", write))


def queue_report(stack_id, report_id, report):
    """Keep the whole-stack report until the cloud has answered it. Kept in
    stack_history.db, so it survives being offline and restarts."""
    def write(conn):
        conn.execute('UPDATE stacks SET report_id = ?, report_json = ?, report_status = ?, '
                     'report_error = NULL WHERE stack_id = ?',
                     (report_id, json.dumps(report), REPORT_PENDING, stack_id))
        conn.execute('UPDATE stack_layers SET status = ? WHERE stack_id = ? AND status = ?',
                     (LAYER_SENT, stack_id, LAYER_HELD))
    _run(f"queue the report of {stack_id}", write)


def pending_reports():
    """[(stack_id, report dict)] waiting for the cloud, oldest first."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return []

    def read(conn):
        rows = conn.execute('SELECT stack_id, report_json FROM stacks WHERE report_status = ? '
                            'ORDER BY closed_at, started_at', (REPORT_PENDING,)).fetchall()
        out = []
        for stack_id, text in rows:
            try:
                out.append((stack_id, json.loads(text)))
            except (TypeError, ValueError):
                log.error(f"{stack_id}: stored stack report is unreadable")
        return out
    return _run("read the pending stack reports", read) or []


def update_report(stack_id, report, error=None):
    """Keep the report changed for a safe resend (stackId, pallet ids)."""
    _run(f"update the report of {stack_id}", lambda conn: conn.execute(
        'UPDATE stacks SET report_json = ?, report_error = ? WHERE stack_id = ?',
        (json.dumps(report), error, stack_id)))


def update_report_error(stack_id, error):
    """Why the report is still waiting (shown in Stack History)."""
    _run(f"note the wait of {stack_id}", lambda conn: conn.execute(
        'UPDATE stacks SET report_error = ? WHERE stack_id = ?',
        (str(error)[:500], stack_id)))


def apply_report_result(stack_id, cloud_id, qr, pallets, closed=True):
    """The cloud built the stack: its id, and {layer number: paletteId}."""
    def write(conn):
        conn.execute('UPDATE stacks SET cloud_stack_id = ?, stack_qr = ?, report_status = ?, '
                     'report_error = NULL, cloud_closed = ? WHERE stack_id = ?',
                     (cloud_id, qr or cloud_id, REPORT_SENT, 1 if closed else 0, stack_id))
        for layer, pallet in (pallets or {}).items():
            conn.execute('UPDATE stack_layers SET pallet_id = ?, status = ? '
                         'WHERE stack_id = ? AND layer = ?',
                         (pallet, LAYER_CONFIRMED, stack_id, int(layer)))
    _run(f"apply the cloud answer for {stack_id}", write)


def reject_report(stack_id, error):
    """The cloud refused the whole report: nothing of it was written there."""
    def write(conn):
        conn.execute('UPDATE stacks SET report_status = ?, report_error = ? WHERE stack_id = ?',
                     (REPORT_REJECTED, str(error)[:500], stack_id))
        conn.execute('UPDATE stack_layers SET status = ? WHERE stack_id = ?',
                     (LAYER_REJECTED, stack_id))
    _run(f"reject the report of {stack_id}", write)


# Reading
def _layer_dict(r):
    return {"layer": r[0], "session_id": r[1], "pallet_id": r[2] or "",
            "keg_ids": _kegs(r[3]), "status": r[4], "size": r[5],
            "positions": _positions(r[6]), "layout": r[7] or "",
            "expected": r[8], "scanned_time": r[9] or "", "batch": r[10] or ""}


_LAYER_COLS = ('layer, session_id, pallet_id, keg_ids, status, measured_size, '
               'positions, layout, expected, scanned_time, batch')


def open_stack():
    """The stack left unfinished by the last run, or None."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return None

    def read(conn):
        row = conn.execute('''
            SELECT stack_id, layers_target, cloud_stack_id FROM stacks WHERE status = ?
            ORDER BY started_at DESC LIMIT 1
        ''', (STATUS_OPEN,)).fetchone()
        if not row:
            return None
        layers = conn.execute(f'SELECT {_LAYER_COLS} FROM stack_layers '  # nosec B608
                              f'WHERE stack_id = ? ORDER BY layer', (row[0],)).fetchall()
        return {"stack_id": row[0], "layers_target": row[1], "cloud_stack_id": row[2],
                "layers": [_layer_dict(r) for r in layers]}
    return _run("read the open stack", read)


def recent_stacks(limit=2000):
    """Stacks with their layers, newest first, for the Stack History screen."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return []

    def read(conn):
        stacks = conn.execute('''
            SELECT stack_id, status, layers_target, started_at, closed_at,
                   cloud_stack_id, stack_qr, report_status, report_error
            FROM stacks ORDER BY started_at DESC, stack_id DESC LIMIT ?
        ''', (int(limit),)).fetchall()
        layers = conn.execute(f'SELECT stack_id, {_LAYER_COLS} FROM stack_layers '  # nosec B608
                              'ORDER BY layer').fetchall()
        grouped = {}
        for r in layers:
            grouped.setdefault(r[0], []).append(_layer_dict(r[1:]))
        return [{"stack_id": s[0], "status": s[1], "layers_target": s[2],
                 "started_at": s[3] or "", "closed_at": s[4] or "",
                 "cloud_stack_id": s[5] or "", "stack_qr": s[6] or "",
                 "report_status": s[7] or "", "report_error": s[8] or "",
                 "layers": grouped.get(s[0], [])} for s in stacks]
    return _run("read the stacks", read) or []


def get_stack(stack_id):
    """One stack in recent_stacks() form, or None."""
    if not stack_id or not config.STACK_HISTORY_DB_PATH.exists():
        return None

    def read(conn):
        s = conn.execute('''
            SELECT stack_id, status, layers_target, started_at, closed_at,
                   cloud_stack_id, stack_qr, report_status, report_error
            FROM stacks WHERE stack_id = ?
        ''', (stack_id,)).fetchone()
        if not s:
            return None
        layers = conn.execute(f'SELECT {_LAYER_COLS} FROM stack_layers '  # nosec B608
                              f'WHERE stack_id = ? ORDER BY layer', (stack_id,)).fetchall()
        return {"stack_id": s[0], "status": s[1], "layers_target": s[2],
                "started_at": s[3] or "", "closed_at": s[4] or "",
                "cloud_stack_id": s[5] or "", "stack_qr": s[6] or "",
                "report_status": s[7] or "", "report_error": s[8] or "",
                "layers": [_layer_dict(r) for r in layers]}
    return _run(f"read {stack_id}", read)


def stack_totals(stack):
    """(pallets, kegs) on a stack. A layer the cloud refused is not on it."""
    layers = [l for l in stack["layers"] if l["status"] != LAYER_REJECTED]
    return len(layers), sum(len(l["keg_ids"]) for l in layers)


def daily_summary(stacks):
    """Totals per day, newest first. A stack counts on the day it was started,
    so one that runs past midnight is not counted twice. "built" is every
    finished stack; the ones still open are listed apart."""
    days = {}
    for stack in stacks:
        day = stack["started_at"][:10]
        if not day:
            continue
        row = days.setdefault(day, {"day": day, "built": 0, STATUS_COMPLETE: 0,
                                    STATUS_SHORT: 0, STATUS_OPEN: 0,
                                    "pallets": 0, "kegs": 0})
        row[stack["status"]] = row.get(stack["status"], 0) + 1
        if stack["status"] != STATUS_OPEN:
            row["built"] += 1
        pallets, kegs = stack_totals(stack)
        row["pallets"] += pallets
        row["kegs"] += kegs
    return [days[d] for d in sorted(days, reverse=True)]


def layers_by_session():
    """{session_id: {stack_id, layer, keg_ids}} for the Batch History screen.
    stack_id is the cloud's id once known, else the local one."""
    if not config.STACK_HISTORY_DB_PATH.exists():
        return {}

    def read(conn):
        rows = conn.execute('''
            SELECT l.session_id, COALESCE(s.cloud_stack_id, l.stack_id), l.layer, l.keg_ids
            FROM stack_layers l LEFT JOIN stacks s ON s.stack_id = l.stack_id
        ''').fetchall()
        return {r[0]: {"stack_id": r[1], "layer": r[2], "keg_ids": _kegs(r[3])}
                for r in rows}
    return _run("read the stack history", read) or {}
