# modules/utils.py
# Timestamp and last-batch-number helpers.

import json
import logging
import os
from datetime import datetime
from pathlib import Path

CONFIG_FILENAME = "config.py"

def create_timestamp():
    """Return the current time as YYYYMMDD_HHMMSS."""
    return datetime.now().strftime('%Y%m%d_%H%M%S')

def extract_keg_id(text):
    """Return the keg ID from a decoded QR text.

    Newer labels hold a link such as
    https://dev-keg-management.checkology-cloud.io/s/CHECKOLOGY-2026-6015
    where only the last part of the path is the keg ID. Older labels hold
    the keg ID alone and are returned unchanged.
    """
    text = (text or "").strip()
    if text.lower().startswith(("http://", "https://")):
        path = text.split("://", 1)[1].split("?", 1)[0].split("#", 1)[0]
        parts = [p for p in path.split("/")[1:] if p]
        if parts:
            return parts[-1]
    return text

def write_private_file(path, text: str):
    """Write `text` to `path` atomically, readable by this user only.

    Written to a temporary file in the same folder, flushed to disk and then
    renamed over `path`, so a power cut leaves either the old or the new
    content, never an empty or half-written file. Mode 0600 on the device
    (ignored on the Windows dev box). Raises OSError on failure.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_private_json(path, data):
    """write_private_file() for a JSON document."""
    write_private_file(path, json.dumps(data, indent=2))


def save_last_batch(batch_number: str):
    """Save the last batch number to file (atomic, so a power cut while the
    operator types never leaves it empty)."""
    try:
        config_path = Path(__file__).parent.parent / CONFIG_FILENAME
        config_dir = config_path.parent

        batch_file = config_dir / "last_batch.txt"
        write_private_file(batch_file, batch_number)

        logging.info(f"Saved last batch: {batch_number}")
        return True
    except Exception:
        logging.exception("Failed to save last batch")
        return False

def load_last_batch() -> str:
    """Load the last batch number from file"""
    try:
        config_path = Path(__file__).parent.parent / CONFIG_FILENAME
        config_dir = config_path.parent
        batch_file = config_dir / "last_batch.txt"

        if batch_file.exists():
            with open(batch_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    return content

        # Default when the file is missing or empty
        return "BATCH-001"
    except Exception:
        logging.exception("Failed to load last batch")
        return "BATCH-001"
