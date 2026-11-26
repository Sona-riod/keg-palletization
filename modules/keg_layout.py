# modules/keg_layout.py
"""
Where each keg stands in its layer: a grid cell from its QR box in the frame.

The robot puts a layer down as a regular grid (2 x 3 for six kegs, 2 x 2 for
four), so every keg is given the nearest free cell of that grid laid over the
kegs seen. Row 1 is the top of the camera frame and column 1 the left, as the
cloud contract defines them. A keg read without a box (advanced decode, serial
match) gets no cell; the cloud records its position as absent.

No Qt and no I/O: the operator page calls it live, and tests load it alone.
"""

import math

import config


def _parse(text):
    """"2x3" -> (2, 3), or None."""
    try:
        rows, cols = (int(v) for v in str(text).lower().replace(" ", "").split("x"))
    except (TypeError, ValueError):
        return None
    return (rows, cols) if rows > 0 and cols > 0 else None


def _centre(bbox):
    x1, y1, x2, y2 = bbox
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def grid_for(count, boxes=None):
    """(rows, cols) of a layer of `count` kegs.

    config.PALLET_LAYOUT wins when set. Otherwise the grid closest to square
    that holds them (4 -> 2x2, 6 -> 2x3, 5 -> 2x3), with the long side along
    the long side of the kegs seen, so a layer lying tall in the frame is 3x2.
    """
    fixed = _parse(getattr(config, "PALLET_LAYOUT", ""))
    if fixed:
        return fixed
    count = max(1, int(count or 1))
    # The largest divisor up to the square root fills the grid exactly; a long
    # thin grid (a prime like 5 or 7) is worse than a square with a gap.
    rows = next(r for r in range(int(math.sqrt(count)), 0, -1) if count % r == 0)
    if count // rows > 3 * rows:
        rows = max(1, round(math.sqrt(count)))
    cols = math.ceil(count / rows)
    rows, cols = min(rows, cols), max(rows, cols)
    centres = [_centre(b) for b in (boxes or {}).values() if b]
    if len(centres) >= 2 and rows != cols:
        xs, ys = [c[0] for c in centres], [c[1] for c in centres]
        if max(ys) - min(ys) > max(xs) - min(xs):
            rows, cols = cols, rows
    return rows, cols


def assign_positions(boxes, layout):
    """{code: (row, col)}, 1-based, for every code with a box.

    The grid is laid over the centres of the kegs seen and each keg takes the
    nearest free cell, closest pairs first, so two kegs never share a cell. A
    keg left over (more kegs than cells) gets no cell.
    """
    rows, cols = layout
    centres = {code: _centre(b) for code, b in (boxes or {}).items() if b}
    if not centres:
        return {}
    xs = [c[0] for c in centres.values()]
    ys = [c[1] for c in centres.values()]
    x0, y0 = min(xs), min(ys)
    # One cell pitch per axis. With the whole layer in one line the other axis
    # has no spread, so it borrows the pitch of the axis that has one.
    px = (max(xs) - x0) / (cols - 1) if cols > 1 else 0.0
    py = (max(ys) - y0) / (rows - 1) if rows > 1 else 0.0
    px = px or py or 1.0
    py = py or px

    pairs = []
    for code, (cx, cy) in centres.items():
        for r in range(rows):
            for c in range(cols):
                d = ((cx - (x0 + c * px)) / px) ** 2 + ((cy - (y0 + r * py)) / py) ** 2
                pairs.append((d, code, r, c))
    pairs.sort(key=lambda t: (t[0], t[1]))
    placed, taken = {}, set()
    for _, code, r, c in pairs:
        if code in placed or (r, c) in taken:
            continue
        placed[code] = (r + 1, c + 1)
        taken.add((r, c))
    return placed


def layout_text(layout):
    return f"{layout[0]}x{layout[1]}" if layout else ""


def parse_layout(text):
    return _parse(text)


def stack_map_data(stack=None, target=1, building=None):
    """What StackMap draws, from a stack_store stack (or None) and the layer
    being scanned now (or None): {"target", "layers"}, bottom layer first.

    building: {"codes", "boxes" {code: bbox}, "expected", "flagged"}; flagged are
    the kegs already on another pallet (drawn amber).
    """
    layers = []
    for l in (stack or {}).get("layers") or []:
        codes = list(l.get("keg_ids") or [])
        layout = parse_layout(l.get("layout")) or grid_for(l.get("expected") or len(codes))
        grid, loose = grid_cells(codes, l.get("positions") or {}, layout)
        state = {"confirmed": "done", "rejected": "refused",
                 "held": "held"}.get(l.get("status"), "queued")
        layers.append({"layer": l["layer"], "state": state, "grid": grid, "loose": loose,
                       "count": len(codes), "expected": l.get("expected") or len(codes),
                       "pallet": l.get("pallet_id") or ""})
    if building is not None:
        codes = list(building.get("codes") or [])
        boxes = {c: b for c, b in (building.get("boxes") or {}).items() if c in set(codes)}
        expected = building.get("expected") or len(codes)
        layout = grid_for(expected, boxes)
        grid, loose = grid_cells(codes, assign_positions(boxes, layout), layout)
        layers.append({"layer": (layers[-1]["layer"] + 1) if layers else 1,
                       "state": "building", "grid": grid, "loose": loose,
                       "count": len(codes), "expected": expected, "pallet": "",
                       "flagged": set(building.get("flagged") or ())})
    planned = int((stack or {}).get("layers_target") or target or 1)
    return {"target": max(planned, len(layers)), "layers": layers}


def grid_cells(codes, positions, layout):
    """The layer as rows of cells for drawing: [[code or None, ...], ...], plus
    the codes with no cell. A code without a position fills the first free
    cell in reading order, as the cloud does for a plain list."""
    rows, cols = layout
    grid = [[None] * cols for _ in range(rows)]
    loose = []
    for code in codes:
        pos = positions.get(code)
        if pos and 1 <= pos[0] <= rows and 1 <= pos[1] <= cols and grid[pos[0] - 1][pos[1] - 1] is None:
            grid[pos[0] - 1][pos[1] - 1] = code
        else:
            loose.append(code)
    rest = []
    for code in loose:
        free = next(((r, c) for r in range(rows) for c in range(cols) if grid[r][c] is None), None)
        if free is None:
            rest.append(code)
        else:
            grid[free[0]][free[1]] = code
    return grid, rest
