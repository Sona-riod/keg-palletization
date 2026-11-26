# modules/serial_match.py - last-resort read of a damaged QR: match it against the brewery's serials
"""
Serial matching: identify a keg whose QR code is too damaged to decode.

The camera does not need to decode an unknown code. It only has to decide which
of the brewery's registered serials this is. The squares it can still see are
compared with the QR pattern of every serial on the list; a match is accepted
only when one serial fits clearly better than every other.

Runs only after every normal decoder has failed on a YOLO box (zxing, the pyzbar
chain, QReader in capture). With an empty serial list it does nothing at all.

    sample   locate the code in the crop (OpenCV finder-pattern detector), pick
             the code size from the sizes on the list, sample every square as
             dark / light, and mark clipped glare as unknown.
    compare  remove the mask (read from the format area, or all 8 tried), keep
             the visible squares that differ between serials, count the
             disagreements with every candidate pattern.
    accept   best within SERIAL_MATCH_MAX_MISMATCH of the compared squares, the
             runner-up (another serial) at least SERIAL_MATCH_MIN_GAP worse, and
             the best serial not blacklisted.

Reference patterns are built from the serial text with the qrcode library,
without a mask: the codewords placed in the standard QR order. That makes the
match independent of which mask the label printer chose, as long as text,
error-correction level, version and encoding mode agree.

Candidates: every serial the brewery's labels can carry. Its shapes are learned
from the labels the camera reads normally (CHECKOLOGY-2026-0592 -> stem
"CHECKOLOGY-2026-" + 4 digits, 2025-H-0000161 -> "2025-H-" + 7 digits); each
shape gives every number from 0 to the highest seen + SERIAL_MATCH_NUMBER_MARGIN.
Only stems that start with one of the brewery's keg-ID prefixes count
(GET /api/camera/{mac}/qr-prefixes, the endpoint the monoblock camera already
uses), so pallet labels and other breweries' kegs teach nothing. What the labels
carry (the plain serial or a web link ending in it) is learned the same way.
Candidates and pre-built patterns are kept in SERIAL_LIST_DB, so a restart or a
cloud outage keeps them and nothing is rebuilt per frame.
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
from functools import lru_cache

import cv2
import numpy as np
from qrcode import util as qr_util
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_M

try:
    import config
except ImportError:                     # tools/qr_damage_test.py without the app
    config = None


def _cfg(name, default):
    return getattr(config, name, default) if config is not None else default


log = logging.getLogger("SerialMatch")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
    log.addHandler(_h)

SOURCE = "serial_match"                 # 'source' of a result found this way
LEVELS = {"M": ERROR_CORRECT_M, "H": ERROR_CORRECT_H}
GLARE_LEVEL = 250                       # a square this bright is clipped: unknown
SAMPLE_PX = 6                           # pixels per square in the straightened code
MIN_FIXED_AGREE = 0.85                  # fixed squares (finders, timing) that must agree
SURE_FIXED_AGREE = 0.97                 # this good: stop trying other sizes
TRACK_GAP_S = 0.5                       # live: longest gap a remembered box may have
MIN_VISIBLE = 40
MAX_MISMATCH = 0.25
MIN_GAP = 25


def _settings():
    """Acceptance values, read at call time so config edits need no restart."""
    return (int(_cfg("SERIAL_MATCH_MIN_VISIBLE", MIN_VISIBLE)),
            float(_cfg("SERIAL_MATCH_MAX_MISMATCH", MAX_MISMATCH)),
            int(_cfg("SERIAL_MATCH_MIN_GAP", MIN_GAP)))


# ---------------------------------------------------------------- QR geometry
def size_of(version):
    return 17 + 4 * version


def version_of(size):
    return (size - 17) // 4


@lru_cache(maxsize=None)
def function_map(version):
    """True for every square that is not data: finders with separators and the
    format area, timing lines, alignment patterns, version area, dark module.
    Same areas and order as the qrcode library (qrcode.main.QRCode.makeImpl)."""
    n = size_of(version)
    f = np.zeros((n, n), bool)
    f[0:9, 0:9] = True
    f[0:9, n - 8:n] = True
    f[n - 8:n, 0:9] = True
    finders = f.copy()
    for r in qr_util.pattern_position(version):
        for c in qr_util.pattern_position(version):
            if not finders[r, c]:
                f[r - 2:r + 3, c - 2:c + 3] = True
    f[6, :] = True
    f[:, 6] = True
    if version >= 7:
        f[0:6, n - 11:n - 8] = True
        f[n - 11:n - 8, 0:6] = True
    f.flags.writeable = False
    return f


@lru_cache(maxsize=None)
def fixed_squares(version):
    """(flat indices, expected dark) of the squares that look the same on every
    code of this size: finders, separators, timing lines, alignment patterns,
    dark module. Used to choose size and orientation of a sampled grid."""
    n = size_of(version)
    known = np.zeros((n, n), bool)
    dark = np.zeros((n, n), bool)

    def finder(r0, c0):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < n and 0 <= cc < n:
                    known[rr, cc] = True
                    dark[rr, cc] = (0 <= r <= 6 and c in (0, 6)) or \
                                   (0 <= c <= 6 and r in (0, 6)) or \
                                   (2 <= r <= 4 and 2 <= c <= 4)
    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    in_finder = known.copy()
    for r in qr_util.pattern_position(version):
        for c in qr_util.pattern_position(version):
            if in_finder[r, c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    known[r + dr, c + dc] = True
                    dark[r + dr, c + dc] = max(abs(dr), abs(dc)) != 1
    for i in range(8, n - 8):
        known[6, i] = known[i, 6] = True
        dark[6, i] = dark[i, 6] = i % 2 == 0
    known[n - 8, 8] = dark[n - 8, 8] = True
    idx = np.flatnonzero(known)
    return idx, dark.ravel()[idx]


@lru_cache(maxsize=None)
def data_order(version):
    """Flat indices of the data squares in the order the bits are placed
    (qrcode.main.QRCode.map_data, which follows the QR standard)."""
    n = size_of(version)
    func = function_map(version)
    order = []
    inc, row = -1, n - 1
    for col in range(n - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if not func[row, c]:
                    order.append(row * n + c)
            row += inc
            if row < 0 or row >= n:
                row -= inc
                inc = -inc
                break
    return np.array(order, np.int32)


@lru_cache(maxsize=None)
def mask_flat(version, mask):
    """Flat bool array: True where mask `mask` inverts a data square."""
    n = size_of(version)
    fn = qr_util.mask_func(mask)
    m = np.array([[bool(fn(r, c)) for c in range(n)] for r in range(n)])
    m &= ~function_map(version)
    return m.ravel()


@lru_cache(maxsize=None)
def format_positions(size):
    """(vertical copy, horizontal copy) of the 15 format bits, bit i at [i]."""
    n = size
    v, h = [], []
    for i in range(15):
        r = i if i < 6 else (i + 1 if i < 8 else n - 15 + i)
        v.append(r * n + 8)
        c = n - i - 1 if i < 8 else (15 - i if i < 9 else 15 - i - 1)
        h.append(8 * n + c)
    return np.array(v), np.array(h)


@lru_cache(maxsize=None)
def format_table():
    """(level, mask, 15 bits as a bool array, bit i at [i]) for all 32 codes."""
    rows = []
    for name, ec in LEVELS.items():
        for mask in range(8):
            bits = qr_util.BCH_type_info((ec << 3) | mask)
            rows.append((name, mask, np.array([(bits >> i) & 1 for i in range(15)], bool)))
    for name, ec in (("L", 1), ("Q", 3)):     # other levels, so they are not misread as M/H
        for mask in range(8):
            bits = qr_util.BCH_type_info((ec << 3) | mask)
            rows.append((name, mask, np.array([(bits >> i) & 1 for i in range(15)], bool)))
    return rows


def read_format(grid, unknown, size):
    """(level, mask) from the format area, or (None, None) if it is not readable.

    Both copies are compared with the 32 valid format codes (any two differ in
    at least 7 bits). Accepted only when the best is within 20% of the visible
    format squares and the next best is at least 3 squares worse."""
    v, h = format_positions(size)
    pos = np.concatenate([v, h])
    seen = ~unknown[pos]
    if seen.sum() < 8:
        return None, None
    got = grid[pos][seen]
    scored = sorted((int((np.concatenate([bits, bits])[seen] != got).sum()), lvl, mask)
                    for lvl, mask, bits in format_table())
    (d0, lvl, mask), (d1, _, _) = scored[0], scored[1]
    if d0 <= 0.2 * seen.sum() and d1 - d0 >= 3:
        return lvl, mask
    return None, None


# ---------------------------------------------------------------- reference patterns
_MODE_BITS = {  # character-count bits per mode for versions 1-9, 10-26, 27-40
    qr_util.MODE_NUMBER: (10, 12, 14),
    qr_util.MODE_ALPHA_NUM: (9, 11, 13),
    qr_util.MODE_8BIT_BYTE: (8, 16, 16),
}
_WIDER = {  # modes that can also carry a run of a given mode
    qr_util.MODE_NUMBER: (qr_util.MODE_NUMBER, qr_util.MODE_ALPHA_NUM, qr_util.MODE_8BIT_BYTE),
    qr_util.MODE_ALPHA_NUM: (qr_util.MODE_ALPHA_NUM, qr_util.MODE_8BIT_BYTE),
    qr_util.MODE_8BIT_BYTE: (qr_util.MODE_8BIT_BYTE,),
}


def _data_bits(mode, length):
    if mode == qr_util.MODE_NUMBER:
        return 10 * (length // 3) + (0, 4, 7)[length % 3]
    if mode == qr_util.MODE_ALPHA_NUM:
        return 11 * (length // 2) + 6 * (length % 2)
    return 8 * length


def _runs(data):
    """Maximal runs of digits, of other alphanumeric characters, and of the rest."""
    runs = []
    for ch in data:
        c = bytes([ch])
        mode = (qr_util.MODE_NUMBER if c.isdigit() else
                qr_util.MODE_ALPHA_NUM if qr_util.RE_ALPHA_NUM.match(c) else
                qr_util.MODE_8BIT_BYTE)
        if runs and runs[-1][0] == mode:
            runs[-1][1] += c
        else:
            runs.append([mode, c])
    return runs


def _optimal_segments(data, group):
    """The shortest split of `data` into segments, the way an optimising
    encoder (the JavaScript `qrcode` library the cloud prints with) does it:
    each run of digits / alphanumerics / other bytes is carried in its own mode
    or a wider one, neighbouring runs in the same mode share one segment.
    `group` 0/1/2 = versions 1-9 / 10-26 / 27-40 (character-count field size)."""
    # state: mode of the open segment -> (bits so far, length of open segment, segments)
    states = {None: (0, 0, [])}
    for mode, chunk in _runs(data):
        nxt = {}
        for m in _WIDER[mode]:
            best = None
            for prev, (bits, open_len, segs) in states.items():
                if prev == m:               # extend the open segment
                    cost = bits - _data_bits(m, open_len) + _data_bits(m, open_len + len(chunk))
                    cand = (cost, open_len + len(chunk), segs[:-1] + [(m, segs[-1][1] + chunk)])
                else:                       # start a new segment
                    cost = bits + 4 + _MODE_BITS[m][group] + _data_bits(m, len(chunk))
                    cand = (cost, len(chunk), segs + [(m, chunk)])
                if best is None or cand[0] < best[0]:
                    best = cand
            nxt[m] = best
        states = nxt
    _, _, segs = min(states.values(), key=lambda st: st[0])
    return [qr_util.QRData(chunk, mode) for mode, chunk in segs]


def _segment_variants(text):
    """Ways a label printer may split `text` into QR segments.

    'one': the whole text in one segment, in the most compact mode for it.
    'optimal': the shortest split into segments, as an optimising encoder
    makes it (the cloud's labels: HTTPS://.../S/2025-H- alphanumeric, then
    0000179 numeric). Computed for the version ranges 1-9 and 10-26.
    'split' / 'numtail': byte start + alphanumeric tail, and any start + a
    numeric tail of trailing digits; other splits an encoder may pick when two
    splits cost the same number of bits."""
    data = text.encode("utf-8")
    out = [("one", [qr_util.QRData(data)])]
    for group in (0, 1):
        out.append((f"optimal{group}", _optimal_segments(data, group)))
    if qr_util.optimal_mode(data) == qr_util.MODE_8BIT_BYTE:
        k = len(data)
        while k > 0 and qr_util.RE_ALPHA_NUM.match(data[k - 1:k]):
            k -= 1
        if 0 < k <= len(data) - 8:
            out.append(("split", [qr_util.QRData(data[:k], qr_util.MODE_8BIT_BYTE),
                                  qr_util.QRData(data[k:], qr_util.MODE_ALPHA_NUM)]))
    k = len(data)
    while k > 0 and data[k - 1:k].isdigit():
        k -= 1
    if 0 < k < len(data):
        out.append(("numtail", [qr_util.QRData(data[:k]),
                                qr_util.QRData(data[k:], qr_util.MODE_NUMBER)]))
    # The same split found twice is built once.
    seen, unique = set(), []
    for name, segs in out:
        key = tuple((sg.mode, sg.data) for sg in segs)
        if key not in seen:
            seen.add(key)
            unique.append((name, segs))
    return unique


def _best_version(segments, ec):
    from qrcode.main import QRCode
    qr = QRCode(error_correction=ec)
    qr.data_list = segments
    return qr.best_fit()


def build_bits(segments, ec):
    """(size, unmasked flat bool grid) for the given segments at level `ec`.
    Only data squares are meaningful; everything else is False."""
    version = _best_version(segments, ec)
    words = qr_util.create_data(version, ec, segments)
    bits = np.unpackbits(np.array(words, np.uint8)).astype(bool)
    order = data_order(version)
    n = size_of(version)
    grid = np.zeros(n * n, bool)
    grid[order[:len(bits)]] = bits
    return n, grid


# Label forms learned from labels read normally (see SerialList.note_label).
_learned_forms = ()


def text_forms():
    """What the labels carry, as templates: SERIAL_MATCH_TEXT_FORMS if set,
    else the forms learned from labels read normally, else the plain serial."""
    fixed = _cfg("SERIAL_MATCH_TEXT_FORMS", None)
    if fixed:
        return tuple(fixed)
    return _learned_forms or ("{serial}",)


def levels():
    return tuple(l for l in _cfg("SERIAL_MATCH_LEVELS", ("M", "H")) if l in LEVELS)


_SHAPE_RE = re.compile(r"^(.*?)(\d+)$")


def shape_of(serial):
    """(shape key, number) of a serial: CHECKOLOGY-2026-0592 ->
    ("CHECKOLOGY-2026-|4", 592). None when it does not end in a number."""
    m = _SHAPE_RE.match(serial or "")
    if not m or not m.group(1):
        return None
    return f"{m.group(1)}|{len(m.group(2))}", int(m.group(2))


def shape_limit(key, highest):
    """Numbers 0 .. limit-1 are candidates for a shape whose highest seen is `highest`."""
    digits = int(key.rsplit("|", 1)[1])
    return min(10 ** digits, highest + int(_cfg("SERIAL_MATCH_NUMBER_MARGIN", 2000)) + 1)


def candidates(prefixes, shapes):
    """Every serial of the learned shapes whose stem starts with a prefix."""
    out = []
    for key, highest in sorted(shapes.items()):
        stem, digits = key.rsplit("|", 1)
        if any(stem.startswith(p) for p in prefixes):
            out += [f"{stem}{i:0{int(digits)}d}" for i in range(shape_limit(key, highest))]
    return out


def pattern_spec():
    """Everything the patterns depend on; a change means rebuilding them all."""
    return "v2|" + ",".join(levels()) + "|" + "\n".join(text_forms())


def build_patterns(serial):
    """[(variant, level, size, unmasked bits)] for one serial: every text form,
    level and segmentation the labels may use. Identical patterns appear once."""
    out, seen = [], set()
    for form in text_forms():
        text = form.format(serial=serial)
        for seg_name, segments in _segment_variants(text):
            for lvl in levels():
                try:
                    n, bits = build_bits(segments, LEVELS[lvl])
                except Exception as e:      # too long for a QR code at this level
                    log.debug("no pattern for %r at %s: %s", text, lvl, e)
                    continue
                key = (n, bits.tobytes())
                if key in seen:
                    continue
                seen.add(key)
                out.append((f"{form}|{seg_name}|{lvl}", lvl, n, bits))
    return out


# ---------------------------------------------------------------- pattern index
class _SizeIndex:
    """All candidate patterns of one code size, as one matrix."""
    __slots__ = ("bits", "row_serial", "row_level", "informative")

    def __init__(self, bits, row_serial, row_level):
        self.bits = bits                    # rows x n*n bool
        self.row_serial = row_serial        # rows -> index into PatternIndex.serials
        self.row_level = row_level          # rows -> "M" / "H"
        # Data squares that differ between at least two candidates. The rest
        # (fixed areas, squares equal on every pattern) only dilute the result.
        self.informative = np.flatnonzero(bits.any(0) & ~bits.all(0))


class PatternIndex:
    """Immutable snapshot used by the matcher; replaced whole on every change."""

    def __init__(self, serials, patterns):
        """serials: list of serial strings; patterns: {serial: [(lvl, n, bits)]}."""
        self.serials = list(serials)
        pos = {s: i for i, s in enumerate(self.serials)}
        by_size = {}
        for s, rows in patterns.items():
            if s not in pos:
                continue
            for lvl, n, bits in rows:
                by_size.setdefault(n, []).append((pos[s], lvl, bits))
        self.sizes = {}
        for n, rows in by_size.items():
            self.sizes[n] = _SizeIndex(np.stack([r[2] for r in rows]),
                                       np.array([r[0] for r in rows], np.int32),
                                       np.array([r[1] for r in rows]))

    def __len__(self):
        return len(self.serials)


# ---------------------------------------------------------------- sampling
_local = threading.local()


def locate(gray):
    """The four outer corners of the code in `gray`, or None.

    Clipped glare is painted the paper's grey first: a white patch next to a
    corner marker breaks the marker's dark-light-dark pattern for the detector,
    while paper grey reads as the light ring it usually is (100 of 100
    simulated labels with 40% glare located, against 96 without)."""
    if not hasattr(_local, "det"):
        _local.det = cv2.QRCodeDetector()
    glare = gray >= GLARE_LEVEL
    if glare.any():
        rest = gray[~glare]
        if rest.size == 0:
            return None
        gray = gray.copy()
        gray[glare] = int(np.median(rest))
    try:
        ok, pts = _local.det.detect(gray)
    except Exception:
        return None
    if ok and pts is not None and len(pts.reshape(-1, 2)) == 4:
        return pts.reshape(4, 2).astype(np.float32)
    return None


def _orientations(cells):
    """The 8 rotations / mirrors of an n x n array, as flat arrays."""
    for k in range(4):
        r = np.rot90(cells, k)
        yield r.ravel()
        yield r.T.ravel()


def sample_grid(gray, sizes):
    """Sample the code in `gray` as (size, dark, unknown) flat arrays, or None.

    The code is straightened from its corners at each candidate size; the size
    and orientation kept are those whose fixed squares (finders, timing lines,
    alignment) agree best with what a QR code must show there."""
    pts = locate(gray)
    if pts is None:
        return None
    best = None
    for n in sorted(sizes):
        side = n * SAMPLE_PX
        dst = np.array([[0, 0], [side, 0], [side, side], [0, side]], np.float32)
        warp = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(pts, dst),
                                   (side, side), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_REPLICATE)
        c0, c1 = SAMPLE_PX // 2 - 1, SAMPLE_PX // 2 + 1
        cells = warp.reshape(n, SAMPLE_PX, n, SAMPLE_PX)[:, c0:c1, :, c0:c1].mean(axis=(1, 3))
        glare = cells >= GLARE_LEVEL
        seen = cells[~glare]
        if seen.size < n:                   # nothing left to threshold
            continue
        thr, _ = cv2.threshold(seen.astype(np.uint8).reshape(1, -1), 0, 255,
                               cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        dark = cells < thr
        idx, want = fixed_squares(version_of(n))
        for d, g in zip(_orientations(dark), _orientations(glare)):
            vis = ~g[idx]
            if vis.sum() < 0.5 * len(idx):
                continue
            agree = float((d[idx][vis] == want[vis]).mean())
            if best is None or agree > best[0]:
                best = (agree, n, d.copy(), g.copy())
        if best is not None and best[0] >= SURE_FIXED_AGREE:
            break                           # a wrong size never fits this well
    if best is None or best[0] < MIN_FIXED_AGREE:
        return None
    return best[1], best[2], best[3]


# ---------------------------------------------------------------- matching
def match_grid(index, size, grid, unknown, excluded=frozenset()):
    """Match one sampled grid against the index.

    Returns a dict: result ('matched' or the reject reason), serial (or None),
    compared, best, runner_up (disagreement counts)."""
    min_visible, max_mismatch, min_gap = _settings()
    out = {"result": "", "serial": None, "compared": 0, "best": None, "runner_up": None}
    si = index.sizes.get(size) if index is not None else None
    if si is None:
        out["result"] = "no serial of this size"
        return out
    version = version_of(size)
    level, mask = read_format(grid, unknown, size)
    if level not in ("M", "H"):
        # L or Q is not a level the labels use: the format area was misread,
        # so it is treated as unreadable (all masks, all levels).
        level, mask = None, None
    rows = np.arange(len(si.row_serial))
    if level is not None:
        rows = rows[si.row_level == level]
    if rows.size == 0:
        out["result"] = "no serial of this size"
        return out
    cols = si.informative[~unknown[si.informative]]
    out["compared"] = int(cols.size)
    if cols.size < min_visible:
        out["result"] = "too few squares"
        return out
    cand = si.bits[np.ix_(rows, cols)]
    seen = grid[cols]
    bad = None
    for m in ([mask] if mask is not None else range(8)):
        b = (cand != (seen ^ mask_flat(version, m)[cols])).sum(axis=1)
        bad = b if bad is None else np.minimum(bad, b)
    a = int(np.argmin(bad))
    best_serial = si.row_serial[rows[a]]
    others = bad[si.row_serial[rows] != best_serial]
    runner = int(others.min()) if others.size else cols.size
    out["best"], out["runner_up"] = int(bad[a]), runner
    serial = index.serials[best_serial]
    if bad[a] > max_mismatch * cols.size:
        out["result"] = "too many disagreements"
    elif runner - bad[a] < min_gap:
        out["result"] = "runner-up too close"
    elif serial in excluded:
        out["result"] = "best serial not in the current list"
    else:
        out["result"], out["serial"] = "matched", serial
    return out


def _iou(a, b):
    """Overlap of two (x1, y1, x2, y2) boxes, 0..1."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- serial list store
class SerialList:
    """Candidate serials with pre-built patterns, kept in SQLite.

    match_crop() / match_box() may be called from any thread; they read the
    current PatternIndex snapshot, which is replaced whole after every change."""

    MAX_FORMS = 3                           # label forms kept (newest)
    MAX_SHAPES = 10                         # serial shapes kept

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.index = PatternIndex([], {})
        self.excluded = frozenset()
        self.prefixes = []
        self.shapes = {}                    # shape key -> highest number seen
        self._spec_loaded = None
        self._db_lock = threading.Lock()
        self._api = None
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._fail_logged = None
        self._stats = self._new_stats()
        self._last_line = {}
        self._tracks = []                   # live boxes: see match_box()
        self._init_db()

    # -- storage
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self):
        with self._db_lock, self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS serials (serial_no TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS patterns (
                    serial_no TEXT, variant TEXT, level TEXT, size INTEGER, bits BLOB,
                    PRIMARY KEY (serial_no, variant));
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)

    def _meta(self, key, default=None):
        with self._db_lock, self._connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_meta(self, key, value):
        with self._db_lock, self._connect() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def load(self, build_missing=True):
        """Load the saved candidates and patterns. Patterns built for other
        settings (label forms, levels) are dropped and rebuilt. With
        build_missing False (start-up, on the caller's thread) serials without
        patterns are left out until the refresh thread builds them. The index
        in use is replaced only at the end, so matching carries on meanwhile."""
        global _learned_forms
        _learned_forms = tuple(self._meta("forms", []))
        self.prefixes = self._meta("prefixes", [])
        self.shapes = self._meta("shapes", {})
        spec = pattern_spec()
        with self._db_lock, self._connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key='spec'").fetchone()
            if row is None or json.loads(row[0]) != spec:
                con.execute("DELETE FROM patterns")
                con.execute("INSERT OR REPLACE INTO meta VALUES ('spec', ?)", (json.dumps(spec),))
            serials = [r[0] for r in con.execute("SELECT serial_no FROM serials ORDER BY serial_no")]
            patterns = {}
            for s, lvl, n, blob in con.execute("SELECT serial_no, level, size, bits FROM patterns"):
                bits = np.unpackbits(np.frombuffer(blob, np.uint8))[:n * n].astype(bool)
                patterns.setdefault(s, []).append((lvl, n, bits))
        missing = [s for s in serials if s not in patterns]
        if missing and build_missing:
            patterns.update(self._store_patterns(missing))
        self.index = PatternIndex([s for s in serials if s in patterns], patterns)
        self._spec_loaded = spec
        log.info("Serial match: %d candidate(s), prefixes %s, serial shapes %s, label forms %s%s",
                 len(self.index), self.prefixes,
                 {k: shape_limit(k, v) for k, v in self.shapes.items()}, list(text_forms()),
                 f", {len(serials) - len(self.index)} waiting for patterns"
                 if len(self.index) < len(serials) else "")
        return len(self.index)

    def _store_patterns(self, serials):
        """Build and save the patterns of `serials` (about 1 ms each for the
        plain serial, 13 ms for a web link, on a PC). Saved every 500 serials,
        so a shutdown in the middle keeps what is done; stops on stop()."""
        t0 = time.perf_counter()
        log.info("Serial match: building patterns for %d serial(s), forms %s...",
                 len(serials), list(text_forms()))
        built, rows, count = {}, [], 0
        for i, s in enumerate(serials, 1):
            if self._stop.is_set():
                break
            for variant, lvl, n, bits in build_patterns(s):
                built.setdefault(s, []).append((lvl, n, bits))
                rows.append((s, variant, lvl, n, np.packbits(bits).tobytes()))
            if i % 500 == 0 or i == len(serials):
                with self._db_lock, self._connect() as con:
                    con.executemany("INSERT OR REPLACE INTO patterns VALUES (?,?,?,?,?)", rows)
                count += len(rows)
                rows = []
            # Building is pure Python: let the detection thread have the
            # interpreter between serials.
            time.sleep(0.001)
        if rows:                            # stopped part-way: keep what is built
            with self._db_lock, self._connect() as con:
                con.executemany("INSERT OR REPLACE INTO patterns VALUES (?,?,?,?,?)", rows)
            count += len(rows)
        log.info("Serial match: built %d pattern(s) for %d serial(s) in %.1f s",
                 count, len(built), time.perf_counter() - t0)
        return built

    def set_prefixes(self, prefixes):
        """Make the candidates every serial of the learned shapes under
        `prefixes`, and load them (new candidates get their patterns built)."""
        wanted = set(candidates(prefixes, self.shapes))
        with self._db_lock, self._connect() as con:
            known = {r[0] for r in con.execute("SELECT serial_no FROM serials")}
            drop, add = known - wanted, wanted - known
            con.executemany("DELETE FROM serials WHERE serial_no=?", [(x,) for x in drop])
            con.executemany("DELETE FROM patterns WHERE serial_no=?", [(x,) for x in drop])
            con.executemany("INSERT INTO serials VALUES (?)", [(x,) for x in add])
        self._set_meta("prefixes", list(prefixes))
        self.prefixes = list(prefixes)
        if drop or add or self._spec_loaded != pattern_spec() or len(self.index) < len(wanted):
            self.load()

    # -- learning from labels read normally
    def note_label(self, raw, serial):
        """A label read normally: learn what it carries and the serial's shape.
        `raw` is the decoded text, `serial` the keg ID taken from it. Only keg
        serials count (they start with one of the brewery's prefixes). A new
        form, a new shape or a number near the end of a shape's range makes the
        refresh thread build the extra patterns. Called for every read, so the
        usual case (nothing new) does no I/O."""
        global _learned_forms
        if not any(serial.startswith(p) for p in self.prefixes):
            return                          # not a keg label (pallet label, other brewery)
        changed = False
        if raw == serial:
            form = "{serial}"
        elif raw.endswith(serial) and len(raw) <= 120:
            form = raw[:-len(serial)] + "{serial}"
        else:
            form = None
        if form and form not in _learned_forms and not _cfg("SERIAL_MATCH_TEXT_FORMS", None):
            _learned_forms = (tuple(_learned_forms) + (form,))[-self.MAX_FORMS:]
            self._set_meta("forms", list(_learned_forms))
            log.info("Serial match: labels carry %r - building patterns for it", form)
            changed = True
        shape = shape_of(serial)
        if shape is not None:
            key, number = shape
            old = self.shapes.get(key)
            margin = int(_cfg("SERIAL_MATCH_NUMBER_MARGIN", 2000))
            if old is None and len(self.shapes) < self.MAX_SHAPES:
                self.shapes[key] = number
                log.info("Serial match: new serial shape %r (%s) - adding its serials", key, serial)
                changed = True
            elif old is not None and number > old and \
                    shape_limit(key, old) - number < margin // 2 and \
                    shape_limit(key, old) < 10 ** int(key.rsplit("|", 1)[1]):
                self.shapes[key] = number   # close to the end of the range: extend it
                changed = True
            if changed:
                self._set_meta("shapes", self.shapes)
        if changed:
            self._wake.set()

    # -- cloud refresh
    def start(self, api_sender):
        """Load the saved candidates, then refresh the prefixes from the cloud
        on a side thread every SERIAL_LIST_REFRESH_S seconds."""
        self._api = api_sender
        try:
            self.load(build_missing=False)
        except Exception:
            log.exception("Serial match: could not load the saved candidates")
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="SerialList", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("Serial match refresh failed")
            self._wake.wait(float(_cfg("SERIAL_LIST_REFRESH_S", 300)))
            self._wake.clear()

    def refresh(self):
        """Fetch the prefixes; on failure keep the last ones. Also rebuilds the
        patterns after a new label form was learned."""
        prefixes = self.prefixes
        if self._api is not None:
            got, error = self._api.get_qr_prefixes()
            if got is None:
                if error != self._fail_logged:
                    log.warning("Keg-ID prefixes not refreshed (%s); using the last ones %s",
                                error, self.prefixes)
                self._fail_logged = error
            else:
                self._fail_logged = None
                if not got and got != self.prefixes:
                    log.warning("The brewery has no keg-ID prefixes: serial matching "
                                "has no candidates")
                prefixes = got
        self.set_prefixes(prefixes)

    def set_excluded(self, codes):
        """Blacklisted serials: never accepted as a match."""
        self.excluded = frozenset(codes or ())

    # -- matching
    def _attempt(self, crop):
        """Sample and match one crop. Returns the serial or None."""
        index = self.index
        t0 = time.perf_counter()
        gray = crop if crop.ndim == 2 else cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        st = self._stats
        st["attempts"] += 1
        sampled = sample_grid(gray, index.sizes.keys())
        if sampled is None:
            st["not_located"] += 1
            st["ms"] += (time.perf_counter() - t0) * 1000
            self._maybe_summary()
            return None
        size, grid, unknown = sampled
        res = match_grid(index, size, grid, unknown, self.excluded)
        ms = (time.perf_counter() - t0) * 1000
        st["ms"] += ms
        st[res["result"]] = st.get(res["result"], 0) + 1
        self._log_attempt(res, size, ms)
        self._maybe_summary()
        return res["serial"]

    @staticmethod
    def _result(serial, crop):
        h, w = crop.shape[:2]
        return [{"data": serial, "bbox": (0, 0, w, h), "source": SOURCE}]

    def forget_boxes(self):
        """Drop every remembered box. A new layer goes down where the last one
        stood, so a box remembered from it would hand its serial to a keg that
        was never read. Replaced in one assignment: the detection thread sees
        the old list or the empty one, never half of either."""
        self._tracks = []

    def match_crop(self, crop):
        """[{'data': serial, 'source': 'serial_match'}] or [] for one crop of a
        saved frame (short-layer detection, capture): always a full attempt."""
        if not len(self.index):
            return []
        serial = self._attempt(crop)
        return self._result(serial, crop) if serial else []

    def match_box(self, crop, bbox, may_attempt=True):
        """Live stream: (results, attempted) for one unread YOLO box.

        A full attempt costs tens of milliseconds, and an unreadable label
        usually stays under the camera for many rounds, so each box position is
        remembered:
          - matched: the serial is reported again while the box is seen with no
            gap longer than TRACK_GAP_S, and re-checked by a full attempt every
            SERIAL_MATCH_REVERIFY_S. A box that disappeared and came back (a
            different keg put in the same place) always gets a full attempt.
          - not matched: no new attempt for SERIAL_MATCH_RETRY_S.
        `may_attempt` False (the round's attempts are used up) reports only
        what is remembered."""
        if not len(self.index):
            return [], False
        now = time.monotonic()
        self._tracks = [t for t in self._tracks if now - t["seen"] <= 5.0]
        track = next((t for t in self._tracks if _iou(t["bbox"], bbox) >= 0.5), None)
        if track is not None:
            gap = now - track["seen"]
            track["seen"], track["bbox"] = now, bbox
            if track["serial"] is not None:
                if gap <= TRACK_GAP_S and now - track["tried"] < float(
                        _cfg("SERIAL_MATCH_REVERIFY_S", 3.0)):
                    return self._result(track["serial"], crop), False
            elif now - track["tried"] < float(_cfg("SERIAL_MATCH_RETRY_S", 1.0)):
                return [], False
        if not may_attempt:
            return [], False
        serial = self._attempt(crop)
        if track is None:
            track = {"bbox": bbox, "seen": now}
            self._tracks.append(track)
        track["serial"], track["tried"] = serial, now
        return (self._result(serial, crop) if serial else []), True

    def _log_attempt(self, res, size, ms):
        """One line per attempt; the same result for the same serial is repeated
        at most every 10 s, so a damaged keg resting under the camera does not
        fill the log."""
        key = (res["result"], res["serial"], size)
        now = time.monotonic()
        if now - self._last_line.get(key, 0) < 10:
            return
        if len(self._last_line) > 500:
            self._last_line.clear()
        self._last_line[key] = now
        log.info("[MATCH] %s%s | %dx%d, compared %d, best %s, runner-up %s | %.1f ms",
                 res["result"], f" {res['serial']}" if res["serial"] else "",
                 size, size, res["compared"], res["best"], res["runner_up"], ms)

    @staticmethod
    def _new_stats():
        return {"attempts": 0, "not_located": 0, "ms": 0.0, "started": time.monotonic()}

    def _maybe_summary(self):
        st = self._stats
        if time.monotonic() - st["started"] < 60:
            return
        n = max(st["attempts"], 1)
        reasons = ", ".join(f"{k} {v}" for k, v in st.items()
                            if k not in ("attempts", "not_located", "ms", "started"))
        log.info("[MATCH] last %.0f s: %d attempt(s), not located %d, %s | %.1f ms avg",
                 time.monotonic() - st["started"], st["attempts"], st["not_located"],
                 reasons or "nothing sampled", st["ms"] / n)
        self._stats = self._new_stats()


# ---------------------------------------------------------------- shared instance
_store = None
_store_lock = threading.Lock()


def db_path():
    """SERIAL_LIST_DB, or serial_list.db in the app folder."""
    return _cfg("SERIAL_LIST_DB", None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "serial_list.db")


def enabled():
    # tools/run_timing_test.sh ... match sets QR_SERIAL_MATCH=0/1 per run, so
    # off and on can be compared without editing config.py.
    env = os.environ.get("QR_SERIAL_MATCH")
    if env in ("0", "1"):
        return env == "1"
    # On unless config.py says otherwise: with no serial list it does nothing,
    # so a camera whose config.py predates this feature needs no edit.
    return bool(_cfg("QR_SERIAL_MATCH", True))


def store():
    """The process-wide SerialList (created on first use), or None when off."""
    global _store
    if not enabled():
        return None
    with _store_lock:
        if _store is None:
            _store = SerialList(db_path())
        return _store


def start(api_sender):
    s = store()
    if s is not None:
        s.start(api_sender)


def stop():
    if _store is not None:
        _store.stop()


def note_label(raw, serial):
    """Detector: a label read normally (teaches the label form)."""
    s = _store
    if s is not None and raw and serial:
        try:
            s.note_label(raw, serial)
        except Exception:
            log.exception("Serial match: could not learn the label form")


def forget_boxes():
    """Operator page: a layer was sent or the operator cleared everything."""
    s = _store
    if s is not None:
        s.forget_boxes()


def set_excluded(codes):
    s = store()
    if s is not None:
        s.set_excluded(codes)


def match_crop(crop, bbox=None, may_attempt=True):
    """Entry point for the detector: (results, attempted).

    results is [] when the feature is off, the list is empty, or there is no
    confident match. With `bbox` (live stream) the box position is remembered,
    see SerialList.match_box(); without it every call is a full attempt."""
    s = _store
    if s is None or not enabled():
        return [], False
    try:
        if bbox is None:
            return s.match_crop(crop), True
        return s.match_box(crop, bbox, may_attempt)
    except Exception:
        log.exception("Serial match failed")
        return [], False
