# modules/disk_check.py
"""
Free disk space check for the drive the app writes to (config.BASE_DIR).

Warn only: nothing in the workflow is stopped or changed. Captures, sending,
printing, the outbox and the logs carry on whatever the level. The operator
sees a toast and an Activity Log line when the level changes, and the app log
gets one line per change.

    ok        free >= DISK_WARN_FREE_MB
    low       free <  DISK_WARN_FREE_MB
    critical  free <  DISK_CRITICAL_FREE_MB

At "critical" the only action is cleaning keg_frames/ harder than usual: every
frame older than FRAMES_MIN_AGE_S goes, since no frame is read again after its
send attempt (see modules/frame_store.py). Frames younger than that are kept.

No Qt here: main.py calls check() from a QTimer.
"""

import logging
import shutil

import config

log = logging.getLogger("disk_check")

OK, LOW, CRITICAL = "ok", "low", "critical"


def check():
    """{"level", "free_mb", "total_mb", "used_pct"}; level is None if the
    drive could not be read. Never raises."""
    try:
        du = shutil.disk_usage(str(config.BASE_DIR))
    except OSError as e:
        log.warning("Could not read free disk space: %s", e)
        return {"level": None, "free_mb": None, "total_mb": None, "used_pct": None}
    free_mb = du.free / 1048576
    warn = float(getattr(config, "DISK_WARN_FREE_MB", 2000))
    crit = float(getattr(config, "DISK_CRITICAL_FREE_MB", 500))
    level = CRITICAL if free_mb < crit else LOW if free_mb < warn else OK
    return {"level": level,
            "free_mb": free_mb,
            "total_mb": du.total / 1048576,
            "used_pct": 100.0 * du.used / du.total if du.total else None}


def free_space_now():
    """At critical: delete every keg_frames/ frame older than FRAMES_MIN_AGE_S.
    Returns the number deleted. Never raises."""
    try:
        from modules import frame_store
        return frame_store.prune("disk space critical", max_mb=0)
    except Exception:
        log.exception("keg_frames cleanup on low disk failed")
        return 0
