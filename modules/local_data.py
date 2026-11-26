# modules/local_data.py
"""
Wipe the device's local data so every app start begins with a new, empty DB.

Removed: the SQLite database (keg_detection.db and its -wal / -shm / -journal
files), and any cloud cache (beer types, operators, blacklist, keg registration
form) that is older than config.CLOUD_CACHE_MAX_AGE_DAYS, belongs to another
camera MAC or cannot be read. A fresh cache for this camera is kept, so a
device that boots with no network still has its blacklist and operator list.
Everything else stays: camera_credentials.json and device_state.json are
configuration, keg_ledger.db keeps the "already on a pallet" warning working
across restarts, and the logs are kept for troubleshooting.

Called twice:
  • on exit: at the end of MainWindow._cleanup(), which runs for the Exit
    button, power-off, the window close and SIGTERM from systemd;
  • on startup: by the first DatabaseManager(), before anything opens the DB.
    A power cut or crash never reaches _cleanup(), so this covers those cases.

Reports that never reached the cloud are not lost. They live in the offline
outbox (modules/outbox.py, a separate file this never deletes) and go back into
the queue on the next start.

Only the process that owns the local data wipes it. The first process to wipe
takes an exclusive lock on .local_data.lock and holds it until it exits. Any
other process (a second copy of the app, or a maintenance script that imports
modules/) skips the wipe instead of deleting the files under the running app.
"""

import json
import logging
import time

import config
from modules import outbox
from modules.database import db_lock

log = logging.getLogger("local_data")

_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")

_owner_lock = None   # open file holding the lock while this process owns the data


def _own_local_data() -> bool:
    """True if this process owns the local data (no other live process does)."""
    global _owner_lock
    if _owner_lock is not None:
        return True
    try:
        import fcntl
    except ImportError:
        return True   # Windows development machine: no second-process guard
    path = config.DB_PATH.with_name(".local_data.lock")
    try:
        fh = open(path, "a")
    except OSError as e:
        log.warning("Could not open %s (%s); wiping without the guard", path.name, e)
        return True
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _owner_lock = fh   # kept open: the lock is released when the process exits
    return True


def _cloud_cache_files():
    return [config.BEER_TYPES_CACHE_FILE,
            config.OPERATORS_CACHE_FILE,
            config.BLACKLIST_CACHE_FILE,
            config.KEG_REG_FORM_CACHE_FILE]


def _cache_is_stale(path) -> bool:
    """True when a cloud cache must not survive the wipe.

    Kept: a readable copy saved for this camera within
    config.CLOUD_CACHE_MAX_AGE_DAYS, so the device still has its blacklist and
    operator list when it boots offline. Deleted: anything older, saved for
    another camera MAC, or unreadable.
    """
    try:
        age_s = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return True
    max_days = float(getattr(config, "CLOUD_CACHE_MAX_AGE_DAYS", 0) or 0)
    if max_days <= 0 or age_s > max_days * 86400:
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return True
    stored = config.parse_mac(data.get("macId", "")) if isinstance(data, dict) else ""
    current = config.CAMERA_MAC_ID
    return bool(stored and current and stored != current)


def _local_files():
    db = config.DB_PATH
    files = [db.with_name(db.name + s) for s in _DB_SUFFIXES]
    files += [p for p in _cloud_cache_files() if _cache_is_stale(p)]
    return files


def count_unsent_reports() -> int:
    """Reports still waiting to reach the cloud (kept across the wipe)."""
    return outbox.count()


def wipe_local_data(reason: str) -> bool:
    """Delete the DB and any stale cloud cache. Never raises.

    Returns False, and deletes nothing, when another running process owns the
    local data."""
    if not _own_local_data():
        log.warning("Local data NOT cleared on %s: another process (the running "
                    "app?) is using it", reason)
        return False
    removed, failed = [], []
    with db_lock:   # no DatabaseManager call can run while the files go
        for path in _local_files():
            try:
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                pass
            except OSError as e:
                failed.append(f"{path.name} ({e})")
    if removed:
        log.info("Local data cleared on %s: %s", reason, ", ".join(removed))
    if failed:
        log.warning("Could not clear on %s: %s", reason, "; ".join(failed))
    return True
