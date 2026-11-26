# modules/focus_zone.py
"""
Which heights above the floor are in focus, and so how many layers of a stack
the camera can read without refocusing.

The camera reports a lens motor position, not a distance, so the zone is worked
out from the setup instead: the camera's height, the height of one layer, the
layer the focus was set on, and the lens. Standard depth of field:

    hyperfocal  H    = f² / (N·c) + f
    nearest sharp    = d·H / (H + (d − f))
    farthest sharp   = d·H / (H − (d − f))      (no limit once d − f ≥ H)

with d the distance focused on, f the focal length, N the f-number and c the
circle of confusion. The lens values in config.py are placeholders until they
are taken from the camera's datasheet, and the screen says so.
"""

import config


def _setup():
    return {
        "camera_h": float(config.CAMERA_MOUNT_HEIGHT_M),
        "layer_h": float(config.STACK_LAYER_HEIGHT_M),
        "focus_layer": max(1, int(config.FOCUS_ZONE_FOCUS_LAYER)),
        "focal_mm": float(config.LENS_FOCAL_MM),
        "f_number": float(config.LENS_F_NUMBER),
        "coc_mm": float(config.LENS_COC_MM),
        "min_distance": float(config.CAMERA_MIN_DISTANCE_M),
    }


def sharp_heights(camera_h, focus_h, focal_mm, f_number, coc_mm):
    """(lowest, highest) height above the floor in focus, in metres, for a lens
    focused on height `focus_h`. lowest is 0 when the focus reaches the floor."""
    d = (camera_h - focus_h) * 1000.0                 # distance focused on, mm
    f = focal_mm
    if d <= f:
        return focus_h, focus_h
    hyper = f * f / (f_number * coc_mm) + f
    near = d * hyper / (hyper + (d - f))
    far = d * hyper / (hyper - (d - f)) if hyper > d - f else float("inf")
    return max(0.0, camera_h - far / 1000.0), camera_h - near / 1000.0


def focus_zone(layers_planned=1, **overrides):
    """Everything the Focus zone panel shows.

    {"camera_h", "layer_h", "focus_layer", "focus_h", "zone": (low, high),
     "layers": [(n, top_h, "sharp" | "blurred" | "too close")],
     "in_focus": layers from the bottom that read without refocusing,
     "estimated": True while the lens values are placeholders}
    """
    s = _setup()
    s.update(overrides)
    camera_h, layer_h = s["camera_h"], s["layer_h"]
    focus_h = min(s["focus_layer"] * layer_h, camera_h)
    low, high = sharp_heights(camera_h, focus_h, s["focal_mm"], s["f_number"], s["coc_mm"])

    # Every layer that fits under the camera, and at least the planned ones and
    # two, so the panel always answers "how high can we build".
    shown = max(2, int(layers_planned), s["focus_layer"])
    layers = []
    for n in range(1, shown + 1):
        top = n * layer_h
        if top >= camera_h:
            break
        if camera_h - top < s["min_distance"]:
            status = "too close"
        elif low <= top <= high:
            status = "sharp"
        else:
            status = "blurred"
        layers.append((n, top, status))

    in_focus = 0
    for _, _, status in layers:
        if status != "sharp":
            break
        in_focus += 1
    return {"camera_h": camera_h, "layer_h": layer_h, "focus_layer": s["focus_layer"],
            "focus_h": focus_h, "zone": (low, high), "layers": layers,
            "in_focus": in_focus, "estimated": bool(config.LENS_VALUES_ESTIMATED)}
