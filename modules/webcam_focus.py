# modules/webcam_focus.py - automatic focus for the layer-1 USB webcam
"""
Focus test for the layer-1 USB webcam (Settings -> USB webcam focus).

The webcam's autofocus is off and its focus is a fixed value (layer 1 is
always the same distance away). When layer 1 reads badly, the operator puts
keg QR codes where layer 1 stands and presses START AUTO FOCUS. auto_focus()
moves the focus through its range by itself, watching how many codes are
found and decoded at each value, and keeps the middle of the range where they
all decode. The live picture shows it working (green box: code decoded,
amber: seen, not decoded). The focus found is saved
(config.save_layer1_webcam_focus) and used each time the webcam opens.

No Qt here: ui_pyside/workers.py runs this on its own thread.
"""
import logging
import re
import shutil
import subprocess  # nosec B404
import time

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Generic UVC webcam focus range, when the driver cannot be asked.
DEFAULT_RANGE = (0, 255, 1)

# What focus_range() passes to v4l2-ctl -d: /dev/videoN, a /dev/v4l/by-id or
# by-path link, or a bare index.
_VIDEO_DEVICE = re.compile(r"\d{1,3}|/dev/video\d{1,3}|/dev/v4l/by-(?:id|path)/[\w.:+-]+")

# YOLO looks at a picture this wide; the crops are cut from the full frame.
DETECT_WIDTH = 1280
# Every crop is resized to this before measuring sharpness, so a big code does
# not score higher than a small one only because it is bigger.
SHARP_SIZE = 160


def focus_range(device):
    """(min, max, step, current or None) of the webcam's focus.

    Asked from the driver with v4l2-ctl (Linux); DEFAULT_RANGE when that is not
    possible (no v4l2-ctl, not Linux, a webcam without a focus control)."""
    lo, hi, step = DEFAULT_RANGE
    tool = shutil.which("v4l2-ctl")
    device = str(device or "")
    # Only a video device node or index: never something v4l2-ctl would read
    # as an option.
    if not tool or not _VIDEO_DEVICE.fullmatch(device):
        return lo, hi, step, None
    try:
        out = subprocess.run([tool, "-d", device, "-l"],  # nosec B603
                             capture_output=True, text=True, timeout=3, check=False).stdout
    except Exception:
        log.debug("v4l2-ctl failed", exc_info=True)
        return lo, hi, step, None
    for line in out.splitlines():
        if "focus_absolute" not in line:
            continue
        nums = dict(re.findall(r"(min|max|step|value)=(-?\d+)", line))
        try:
            return (int(nums["min"]), int(nums["max"]), max(1, int(nums.get("step", 1))),
                    int(nums["value"]) if "value" in nums else None)
        except (KeyError, ValueError):
            break
    return lo, hi, step, None


def sharpness(gray):
    """Variance of the Laplacian of a grey crop, resized to SHARP_SIZE."""
    if gray is None or gray.size == 0:
        return 0.0
    small = cv2.resize(gray, (SHARP_SIZE, SHARP_SIZE), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(small, cv2.CV_64F).var())


def measure(frame, detector=None):
    """How well the codes in `frame` read now.

    Returns {"boxes": [(x1, y1, x2, y2, read)], "found", "read", "codes",
    "sharpness", "frame_size"}. Boxes are full-frame pixels. Without a
    detector (or no code found) sharpness is that of the middle of the picture,
    so the focus can still be judged on any target."""
    out = {"boxes": [], "found": 0, "read": 0, "codes": [], "sharpness": 0.0,
           "frame_size": None}
    if frame is None:
        return out
    h, w = frame.shape[:2]
    out["frame_size"] = (w, h)
    gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    boxes = []
    if detector is not None and hasattr(detector, "locate_boxes"):
        scale = max(1, w // DETECT_WIDTH)
        small = frame if scale == 1 else cv2.resize(frame, (w // scale, h // scale),
                                                    interpolation=cv2.INTER_AREA)
        try:
            boxes = detector.locate_boxes(small, scale=scale)
        except Exception:
            log.debug("locate_boxes failed", exc_info=True)

    scores = []
    for x1, y1, x2, y2 in boxes:
        pad = max(8, (x2 - x1) // 10)
        cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
        cx2, cy2 = min(w, x2 + pad), min(h, y2 + pad)
        crop = gray[cy1:cy2, cx1:cx2]
        if crop.size == 0:
            continue
        texts = []
        if hasattr(detector, "read_crop"):
            try:
                texts = detector.read_crop(crop)
            except Exception:
                log.debug("read_crop failed", exc_info=True)
        scores.append(sharpness(gray[y1:y2, x1:x2]))
        out["boxes"].append((x1, y1, x2, y2, bool(texts)))
        for t in texts:
            if t not in out["codes"]:
                out["codes"].append(t)

    out["found"] = len(out["boxes"])
    out["read"] = sum(1 for b in out["boxes"] if b[4])
    if scores:
        out["sharpness"] = float(np.mean(scores))
    else:
        ch, cw = h // 3, w // 3
        out["sharpness"] = sharpness(gray[ch:2 * ch, cw:2 * cw])
    return out


def score(m):
    """Bigger is better: more codes read first, then sharper."""
    return (m["read"], m["sharpness"])


def sweep_positions(lo, hi, step, points=16):
    """About `points` evenly spaced focus values over lo..hi, on the step grid."""
    stride = max(step, ((hi - lo) // max(1, points - 1)) // step * step)
    values = list(range(lo, hi + 1, stride))
    if values[-1] != hi:
        values.append(hi)
    return values


def all_read(m):
    """Every code in view decoded (and at least one in view)."""
    return bool(m) and m["read"] > 0 and m["read"] == m["found"]


def _pick(tried):
    """The focus to keep from {value: measure}.

    Values where the most codes read form the readable band. The middle of the
    band run that holds the sharpest picture is kept: an edge of the band would
    lose the codes with the smallest change (a keg a little higher or lower).
    With nothing read, the sharpest value."""
    values = sorted(tried)
    most = max(tried[v]["read"] for v in values)
    if most == 0:
        return max(values, key=lambda v: tried[v]["sharpness"])
    runs, run = [], []
    for v in values:
        if tried[v]["read"] == most:
            run.append(v)
        elif run:
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    best_run = max(runs, key=lambda r: max(tried[v]["sharpness"] for v in r))
    return best_run[len(best_run) // 2]


def auto_focus(set_focus, read_measure, lo, hi, step, on_point=None, cancelled=None,
               settle_s=0.35):
    """Focus the webcam on the codes in view, with no one touching anything.

    1. Coarse: the whole range in ~16 steps, stopping once the codes have read
       and then stopped reading again (the readable band has been passed).
    2. Fine: around the band (or the sharpest value when nothing read yet),
       in quarter steps.
    3. The middle of the readable band is kept (_pick).

    set_focus(value) -> bool moves the lens (False: no focus control);
    read_measure() -> measure() dict for a picture taken after the move;
    on_point(value, m) after each one; cancelled() -> True stops early.
    Returns (value or None, its measure, every (value, measure) tried)."""
    tried = {}
    refused = []

    def visit(value):
        """The measure at `value`, or None when cancelled / refused."""
        if cancelled and cancelled():
            return None
        if not set_focus(value):
            refused.append(value)
            return None
        time.sleep(settle_s)
        m = read_measure()
        tried[value] = m
        if on_point:
            on_point(value, m)
        return m

    coarse = sweep_positions(lo, hi, step)
    stride = coarse[1] - coarse[0] if len(coarse) > 1 else step
    seen_read = False
    for v in coarse:
        m = visit(v)
        if m is None:
            if refused and not tried:
                return None, None, []          # fixed lens: no focus control
            if cancelled and cancelled():
                break
            continue
        if all_read(m):
            seen_read = True
        elif seen_read:
            break                              # past the readable band
    if not tried:
        return None, None, []

    if not (cancelled and cancelled()):
        most = max(m["read"] for m in tried.values())
        band = ([v for v in tried if tried[v]["read"] == most] if most
                else [_pick(tried)])
        fine = max(step, (stride // 4) // step * step)
        for v in range(max(lo, min(band) - stride), min(hi, max(band) + stride) + 1, fine):
            if v in tried:
                continue
            if visit(v) is None and cancelled and cancelled():
                break

    best = _pick(tried)
    return best, tried[best], sorted(tried.items())
