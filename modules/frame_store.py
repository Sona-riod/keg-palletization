# modules/frame_store.py
"""
Keeps keg_frames/ (config.SAVE_FOLDER) from filling the disk.

Every capture saves a 4K JPEG there. It is deleted when its pallet is sent on
the first try, or when a stacked layer is kept. Every other path left it behind
for good: a send that failed or was queued offline, a refused report, a capture
cleared or cancelled before SEND, a crash. Over a long network outage that
fills the disk, and then the databases and the log cannot be written either.

Nothing reads a frame after its send attempt: the retry queue and the outbox
keep the report itself, and only the file name as a label. So old frames can go.

prune() deletes, oldest first:
  - frames older than FRAMES_KEEP_DAYS;
  - then more frames until the folder is under FRAMES_MAX_MB.
A frame younger than FRAMES_MIN_AGE_S is never touched, so a capture still
waiting for SEND or for its tiled decode is safe. Only *.jpg files directly in
the folder are considered.

Runs at start-up, and after captures at most once every FRAMES_PRUNE_EVERY_S,
on a background thread. Never raises.
"""

import logging
import threading
import time

import config

log = logging.getLogger("frame_store")

_lock = threading.Lock()
_last_run = 0.0


def _frames():
    """(mtime, size, path) of every frame, oldest first."""
    out = []
    for path in config.SAVE_FOLDER.glob("*.jpg"):
        try:
            st = path.stat()
        except OSError:
            continue   # deleted meanwhile
        out.append((st.st_mtime, st.st_size, path))
    out.sort(key=lambda f: f[0])
    return out


def prune(reason="periodic", max_mb=None):
    """Delete old frames as described above. Returns the number deleted.
    `max_mb` overrides FRAMES_MAX_MB for this run (0: every frame older than
    FRAMES_MIN_AGE_S goes; used when the disk is nearly full)."""
    global _last_run
    if not _lock.acquire(blocking=False):
        return 0   # one already running
    try:
        _last_run = time.monotonic()
        keep_s = float(getattr(config, "FRAMES_KEEP_DAYS", 3)) * 86400
        if max_mb is None:
            max_mb = getattr(config, "FRAMES_MAX_MB", 2000)
        max_bytes = float(max_mb) * 1024 * 1024
        min_age = float(getattr(config, "FRAMES_MIN_AGE_S", 3600))
        now = time.time()
        frames = _frames()
        total = sum(size for _, size, _ in frames)
        deleted, freed = 0, 0
        for mtime, size, path in frames:
            age = now - mtime
            if age < min_age:
                break   # sorted oldest first: the rest are younger still
            if age <= keep_s and total <= max_bytes:
                break
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Could not delete frame %s: %s", path.name, e)
                continue
            total -= size
            deleted += 1
            freed += size
        if deleted:
            log.info("keg_frames cleanup (%s): %d old frame(s) deleted, %.0f MB freed, "
                     "%.0f MB left", reason, deleted, freed / 1048576, total / 1048576)
        return deleted
    except Exception:
        log.exception("keg_frames cleanup failed")
        return 0
    finally:
        _lock.release()


def prune_soon(reason="periodic"):
    """prune() on a background thread, at most once every FRAMES_PRUNE_EVERY_S."""
    every = float(getattr(config, "FRAMES_PRUNE_EVERY_S", 600))
    if _last_run and time.monotonic() - _last_run < every:
        return
    threading.Thread(target=prune, args=(reason,), name="FramePrune",
                     daemon=True).start()
