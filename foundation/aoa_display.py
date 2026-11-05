"""Explicit display and application frames for certified semantic AOA input."""
from __future__ import annotations

import math
import re


def input_displays(dump):
    """Only expose unambiguous viewport identities, never the first display found."""
    candidates = {}
    # DisplayManager emits Rect(...) and may put several viewports on one
    # line; InputReader uses Viewport INTERNAL with an unquoted uniqueId.
    block_pattern = r"DisplayViewport\{[^{}]*\}"
    blocks = re.findall(block_pattern, dump)
    blocks.extend(line for line in re.sub(block_pattern, "", dump).splitlines()
                  if re.search(r"\bViewport\b", line))
    frame_patterns = (
        r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*(?:,|\]\s*\[)\s*(-?\d+)\s*,\s*(-?\d+)\s*\]",
        r"Rect\(\s*(-?\d+)\s*,\s*(-?\d+)\s*-\s*(-?\d+)\s*,\s*(-?\d+)\s*\)",
    )
    for block in blocks:
        if re.search(r"\b(?:valid|isActive)\s*[=:]\s*false\b", block):
            continue
        display_id = re.search(r"\bdisplayId\s*[=:]\s*(\d+)", block)
        route = re.search(r'''\buniqueId\s*[=:]\s*(?:'([^']+)'|"([^"]+)"|([^'",\s}\]]+))''', block)
        rotation = re.search(r"\borientation\s*[=:]\s*([0-3])\b", block)
        frame = next((match for pattern in frame_patterns
                      if (match := re.search(r"\blogicalFrame\s*[=:]\s*" + pattern, block))), None)
        if not display_id or not route or not rotation or not frame:
            continue
        left, top, right, bottom = map(int, frame.groups())
        if left != 0 or top != 0 or right <= 0 or bottom <= 0:
            continue
        value = {"display_id": int(display_id[1]), "display_route": next(value for value in route.groups() if value),
                 "rotation": int(rotation[1]), "display": [right, bottom]}
        candidates.setdefault(value["display_id"], []).append(value)
    return [values[0] for _, values in sorted(candidates.items()) if all(value == values[0] for value in values)]


def display_contexts(context):
    contexts = [context]
    for item in context.get("displays") or []:
        candidate = {**context, **item}
        if candidate not in contexts:
            contexts.append(candidate)
    return contexts


def rect(value, *, normalized=False):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in value)):
        raise ValueError("display rectangle requires four finite coordinates")
    left, top, right, bottom = value
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise ValueError("invalid display rectangle")
    if normalized and max(value) > 1:
        raise ValueError("normalized display rectangle is outside 0..1")
    return tuple(value)


def standard_point(u, v, display, axis_rotation=0, viewport=None):
    """HID to logical display pixels; flips use the last display pixel."""
    width, height = display
    left, top, right, bottom = rect(viewport or [0, 0, width, height])
    if right > width or bottom > height or type(axis_rotation) is not int or axis_rotation not in range(4):
        raise ValueError("invalid certified viewport or rotation")
    x, y, w, h = u / 361, v / 641, right - left, bottom - top
    points = ((x * w, y * h), (w - 1 - y * w, x * h),
              (w - 1 - x * w, h - 1 - y * h), (y * w, h - 1 - x * h))
    px, py = points[axis_rotation]
    return left + px, top + py


def standard_inverse(x, y, display, axis_rotation=0, viewport=None):
    width, height = display
    left, top, right, bottom = rect(viewport or [0, 0, width, height])
    if not (left <= x < right <= width and top <= y < bottom <= height):
        raise ValueError("semantic target is outside the certified viewport")
    px, py, w, h = x - left, y - top, right - left, bottom - top
    transforms = ((px / w, py / h), (py / h, (w - 1 - px) / w),
                  ((w - 1 - px) / w, (h - 1 - py) / h), ((h - 1 - py) / h, px / w))
    if type(axis_rotation) is not int or axis_rotation not in range(4):
        raise ValueError("invalid certified axis rotation")
    u, v = transforms[axis_rotation]
    u, v = u * 361, v * 641
    if not (-1e-6 <= u <= 360 + 1e-6 and -1e-6 <= v <= 640 + 1e-6):
        raise ValueError("semantic target is outside the reachable HID region")
    return round(u), round(v)


def target_point(target, frame, display, *, rotate_quarters=0, max_error_px=0):
    """Layout rotation is an explicit opt-in, separate from HID orientation."""
    left, top, right, bottom = rect(frame, normalized=True)
    point = target.get("point")
    bounds = rect(target.get("bounds"), normalized=True)
    if (not isinstance(point, list) or len(point) != 2
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in point)):
        raise ValueError("semantic target requires a normalized reference point")
    x, y = point
    if not (bounds[0] < x < bounds[2] and bounds[1] < y < bounds[3]):
        raise ValueError("semantic reference point must be inside its control bounds")
    if type(rotate_quarters) is not int or rotate_quarters not in range(4):
        raise ValueError("invalid explicit layout rotation")
    dx, dy = min(x - bounds[0], bounds[2] - x), min(y - bounds[1], bounds[3] - y)
    if rotate_quarters % 2:
        dx, dy = dy, dx
    margin = dx * (right - left) * display[0]
    margin_y = dy * (bottom - top) * display[1]
    tolerance = min(8.0, margin / 2, margin_y / 2)
    if max_error_px > tolerance:
        raise ValueError("verified mapping error exceeds this control's safe click margin")
    x, y = ((x, y), (1 - y, x), (1 - x, 1 - y), (y, 1 - x))[rotate_quarters]
    return (left + x * (right - left)) * display[0], (top + y * (bottom - top)) * display[1]
