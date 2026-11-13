"""Render the repository's Mermaid topology subset as a local, script-free SVG.

Node and edge copy comes only from ``docs/architecture/*-topology.mmd``.
Coordinates and chrome (box size, arrow lanes, label chips) are presentation.
"""

import html
import math
import re
from dataclasses import dataclass, field


NODE = re.compile(r'^(\w+)\[\(?"([^"]+)"\)?\]$')
EDGE = re.compile(r'^(\w+)\s+(-->|-\.->)(?:\|"([^"]+)"\|)?\s+(\w+)$')

SLATE = '#64748b'
NODE_FILL = '#0f172a'
NODE_TEXT = '#f8fafc'
PILL_FILL = '#0b1220'
DATA_STROKE = '#38bdf8'
CMD_STROKE = '#a78bfa'
DATA_LABEL = '#e0f2fe'
CMD_LABEL = '#ede9fe'
FONT_FAMILY = "'Noto Sans CJK SC', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif"

NODE_FONT_SIZE = 16
EDGE_FONT_SIZE = 14
NODE_LINE_LIMIT = 228
EDGE_LINE_LIMIT = 210
NODE_PAD_X = 18
NODE_PAD_Y = 14
NODE_LINE_H = 22
EDGE_PAD_X = 10
EDGE_PAD_Y = 6
EDGE_LINE_H = 18
MIN_NODE_W = 168
MIN_NODE_H = 60


def _estimate_text_width(text: str, font_size: int = NODE_FONT_SIZE, ascii_em: float = 0.66) -> int:
    """Conservative advance at the given CSS pixel size (sans + CJK).

    ASCII is estimated wide enough for semibold UI text; every other scalar
    (CJK, fullwidth punctuation, arrows) takes a full em. The estimate is
    intentionally a little high so boxes and chips are not tighter than the
    glyphs.
    """
    width = sum(font_size * (ascii_em if ord(ch) < 128 else 1.0) for ch in text)
    return math.ceil(width)


def _wrap_label(text: str, font_size: int, max_width: int, ascii_em: float) -> list[str]:
    """Split a label on a natural break so each line stays near ``max_width``.

    The pieces concatenate back to ``text`` (spaces stay on the first line).
    When no balanced break exists the label stays one line and the box grows.
    """
    if _estimate_text_width(text, font_size, ascii_em) <= max_width or len(text) < 4:
        return [text]
    best: tuple[int, int] | None = None
    for index in range(2, len(text) - 1):
        left, right = text[:index], text[index:]
        left_w = _estimate_text_width(left, font_size, ascii_em)
        right_w = _estimate_text_width(right, font_size, ascii_em)
        if left_w > max_width or right_w > max_width:
            continue
        prev, nxt = text[index - 1], text[index]
        if nxt == '（' or prev == '：':
            bonus = -140
        elif prev == ' ' and nxt != '/':
            bonus = -90
        elif prev == '/':
            bonus = -30
        else:
            bonus = 40
        rank = abs(left_w - right_w) + bonus
        if best is None or rank < best[0]:
            best = (rank, index)
    if best is None:
        return [text]
    split_at = best[1]
    while split_at < len(text) and text[split_at] == ' ':
        split_at += 1
    left, right = text[:split_at], text[split_at:]
    if not right or not left.strip():
        return [text]
    return [left, right]


def _node_metrics(label: str) -> tuple[list[str], float, float]:
    lines = _wrap_label(label, NODE_FONT_SIZE, NODE_LINE_LIMIT, 0.66)
    inner = max(_estimate_text_width(line, NODE_FONT_SIZE, 0.66) for line in lines)
    width = float(max(MIN_NODE_W, inner + NODE_PAD_X * 2))
    height = float(max(MIN_NODE_H, NODE_PAD_Y * 2 + NODE_LINE_H * len(lines)))
    return lines, width, height


def _pill_metrics(label: str, max_width: int = EDGE_LINE_LIMIT) -> tuple[list[str], float, float]:
    lines = _wrap_label(label, EDGE_FONT_SIZE, max_width, 0.62)
    inner = max(_estimate_text_width(line, EDGE_FONT_SIZE, 0.62) for line in lines)
    width = float(inner + EDGE_PAD_X * 2)
    height = float(EDGE_PAD_Y * 2 + EDGE_LINE_H * len(lines))
    return lines, width, height


def _rect_for(cx: float, cy: float, width: float, height: float) -> tuple[float, float, float, float]:
    return (cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2)


def _expand(rect: tuple[float, float, float, float], pad: float) -> tuple[float, float, float, float]:
    return (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)


def _overlap_amount(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    if ox > 0 and oy > 0:
        return ox + oy
    return 0.0


def _segments_intersect(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
    x4: float,
    y4: float,
) -> bool:
    def orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
        return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)

    o1 = orient(x1, y1, x2, y2, x3, y3)
    o2 = orient(x1, y1, x2, y2, x4, y4)
    o3 = orient(x3, y3, x4, y4, x1, y1)
    o4 = orient(x3, y3, x4, y4, x2, y2)
    if o1 == 0 and o2 == 0:
        return (
            min(x1, x2) <= max(x3, x4)
            and min(x3, x4) <= max(x1, x2)
            and min(y1, y2) <= max(y3, y4)
            and min(y3, y4) <= max(y1, y2)
        )
    return o1 * o2 < 0 and o3 * o4 < 0


def _segment_hits_rect(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    rect: tuple[float, float, float, float],
    pad: float = 0,
) -> bool:
    left, top, right, bottom = _expand(rect, pad)

    def inside(x: float, y: float) -> bool:
        return left <= x <= right and top <= y <= bottom

    if inside(x1, y1) or inside(x2, y2):
        return True
    edges = (
        (left, top, right, top),
        (right, top, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, top),
    )
    return any(_segments_intersect(x1, y1, x2, y2, ex1, ey1, ex2, ey2) for ex1, ey1, ex2, ey2 in edges)


def _pair_offset(rect: tuple[float, float, float, float], px: float, py: float) -> float:
    """How far a parallel shaft can sit and still meet this box."""
    left, top, right, bottom = rect
    half = abs(px) * (right - left) / 2 + abs(py) * (bottom - top) / 2
    return max(10.0, half - 8.0)


def _parallel_border(
    rect: tuple[float, float, float, float],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    px: float,
    py: float,
    distance: float,
) -> tuple[float, float] | None:
    """Where a shaft parallel to the center line leaves the face toward the other node.

    ``t`` runs from this node's center toward the other node. The accepted hit is
    the furthest boundary crossing still in front of the center, so the shaft
    starts on the near face instead of the back of the box.
    """
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    ox, oy = x1 + px * distance, y1 + py * distance
    left, top, right, bottom = rect
    hits: list[tuple[float, float, float]] = []
    if abs(ux) > 1e-6:
        for edge_x in (left, right):
            tee = (edge_x - ox) / ux
            y = oy + tee * uy
            if top - 0.6 <= y <= bottom + 0.6:
                hits.append((tee, edge_x, min(max(y, top), bottom)))
    if abs(uy) > 1e-6:
        for edge_y in (top, bottom):
            tee = (edge_y - oy) / uy
            x = ox + tee * ux
            if left - 0.6 <= x <= right + 0.6:
                hits.append((tee, min(max(x, left), right), edge_y))
    forward = [hit for hit in hits if hit[0] > 0.5]
    if not forward:
        return None
    _, x, y = max(forward, key=lambda item: item[0])
    return x, y


def _paired_ends(
    start_rect: tuple[float, float, float, float],
    end_rect: tuple[float, float, float, float],
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    px: float,
    py: float,
    bow: int,
) -> tuple[float, float, float, float]:
    """Parallel attachments on opposite sides of the center line."""
    limit = min(_pair_offset(start_rect, px, py), _pair_offset(end_rect, px, py))
    # 36px a side leaves a two-line caption (48px) clear of the opposite arrow.
    requested = min(limit, 36.0)
    for scale in (1.0, 0.82, 0.64, 0.46):
        distance = requested * scale * bow
        start_hit = _parallel_border(start_rect, x1, y1, x2, y2, px, py, distance)
        end_hit = _parallel_border(end_rect, x2, y2, x1, y1, px, py, distance)
        if start_hit and end_hit:
            return start_hit[0], start_hit[1], end_hit[0], end_hit[1]
    return (
        *_border_point(start_rect, x1, y1, x2, y2),
        *_border_point(end_rect, x2, y2, x1, y1),
    )


def _border_point(
    rect: tuple[float, float, float, float],
    cx: float,
    cy: float,
    tx: float,
    ty: float,
) -> tuple[float, float]:
    """First intersection of the ray from a node center toward ``(tx, ty)``."""
    left, top, right, bottom = rect
    dx, dy = tx - cx, ty - cy
    hits: list[tuple[float, float, float]] = []
    if abs(dx) > 1e-6:
        for edge_x in (left, right):
            t = (edge_x - cx) / dx
            if t > 1e-4:
                y = min(max(cy + t * dy, top), bottom)
                if top - 0.6 <= cy + t * dy <= bottom + 0.6:
                    hits.append((t, edge_x, y))
    if abs(dy) > 1e-6:
        for edge_y in (top, bottom):
            t = (edge_y - cy) / dy
            if t > 1e-4:
                x = min(max(cx + t * dx, left), right)
                if left - 0.6 <= cx + t * dx <= right + 0.6:
                    hits.append((t, x, edge_y))
    if not hits:
        return cx, cy
    _, x, y = min(hits, key=lambda item: item[0])
    return x, y


def _shorten(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    start_gap: float,
    end_gap: float,
) -> tuple[float, float, float, float]:
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= start_gap + end_gap + 1:
        return x1, y1, x2, y2
    ux, uy = dx / length, dy / length
    return (
        x1 + ux * start_gap,
        y1 + uy * start_gap,
        x2 - ux * end_gap,
        y2 - uy * end_gap,
    )


def _unit_perp(dx: float, dy: float) -> tuple[float, float]:
    length = math.hypot(dx, dy) or 1.0
    return -dy / length, dx / length


def _preferred_perp(dx: float, dy: float) -> tuple[float, float]:
    """Unit perpendicular whose positive direction is up, or right when vertical."""
    px, py = _unit_perp(dx, dy)
    if abs(dx) >= abs(dy):
        if py > 0:
            return -px, -py
    elif px < 0:
        return -px, -py
    return px, py


def _canonical_perp(x1: float, y1: float, x2: float, y2: float) -> tuple[float, float]:
    """Same perpendicular for both directions of a node pair."""
    if (y1, x1) <= (y2, x2):
        return _unit_perp(x2 - x1, y2 - y1)
    return _unit_perp(x1 - x2, y1 - y2)


@dataclass(frozen=True)
class TopologyLayout:
    """Presentation-only layout for one topology diagram."""

    positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)


# Coordinates are presentation only. Nodes, labels, and directed edges come
# exclusively from docs/architecture/*-topology.mmd. A unidirectional caption
# sits in a gap on its shaft. A bidirectional caption rides its own parallel
# shaft and reads along that arrow.
LAYOUTS = {
    'platform-topology.mmd': TopologyLayout(
        positions={
            # Columns keep each Worker↔Controller pair on its own vertical
            # lane, so heartbeat / command captions stack in a clear gap.
            'Browser': (190, 70),
            'Agent': (560, 70),
            'Controller': (560, 300),
            'DB': (980, 300),
            'Build': (980, 110),
            'Source': (190, 300),
            'WorkerA': (190, 560),
            'WorkerB': (560, 560),
            'Devices': (190, 820),
            'Suite': (560, 820),
            'External': (1120, 520),
        },
        kinds={
            'Browser': '#38bdf8',
            'Agent': '#a78bfa',
            'Controller': '#38bdf8',
            'DB': '#34d399',
            'WorkerA': '#fbbf24',
            'WorkerB': '#fbbf24',
            'Source': '#34d399',
            'Devices': '#34d399',
            'Suite': '#34d399',
        },
    ),
    'usbip-topology.mmd': TopologyLayout(
        positions={
            'DevSrc': (360, 70),
            'WinSrc': (120, 270),
            'LinuxSrc': (600, 270),
            'Worker': (360, 490),
            'Ctrl': (1040, 490),
            'Tools': (360, 700),
            'DevWorker': (360, 900),
        },
        kinds={
            'DevSrc': '#34d399',
            'DevWorker': '#34d399',
            'WinSrc': '#34d399',
            'LinuxSrc': '#34d399',
            'Worker': '#fbbf24',
            'Tools': '#fbbf24',
            'Ctrl': '#38bdf8',
        },
    ),
    'aoa-topology.mmd': TopologyLayout(
        positions={
            'Browser': (190, 80),
            'Ctrl': (700, 80),
            'Store': (1180, 80),
            'Worker': (700, 360),
            'Acc': (340, 590),
            'Dev': (340, 830),
            'Adb': (980, 830),
        },
        kinds={
            'Browser': '#38bdf8',
            'Ctrl': '#38bdf8',
            'Store': '#34d399',
            'Worker': '#fbbf24',
            'Acc': '#fbbf24',
            'Dev': '#34d399',
            'Adb': '#a78bfa',
        },
    ),
    'firmware-topology.mmd': TopologyLayout(
        positions={
            'Browser': (210, 80),
            'Ctrl': (730, 80),
            'Worker': (730, 360),
            'Loader': (300, 600),
            'Dev': (300, 850),
            'AoaRec': (1060, 600),
        },
        kinds={
            'Browser': '#38bdf8',
            'Ctrl': '#38bdf8',
            'Worker': '#fbbf24',
            'Loader': '#fbbf24',
            'Dev': '#34d399',
            'AoaRec': '#a78bfa',
        },
    ),
}
DEFAULT_LAYOUT = TopologyLayout()


@dataclass
class _EdgeDraw:
    start: str
    end: str
    label: str
    arrow: str
    lines: list[str]
    x1: float
    y1: float
    x2: float
    y2: float
    ctrl: tuple[float, float] | None
    bow: int
    prefer_along: float
    out_x: float
    out_y: float
    stroke: str
    marker: str
    label_fill: str
    pill_w: float
    pill_h: float
    segments: list[tuple[float, float, float, float]]
    angle: float = 0.0
    label_at: tuple[float, float] | None = None
    gap: tuple[float, float] | None = None
    pill: tuple[float, float, float, float] | None = None
    gapped: bool = False


def parse_topology(source: str) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    nodes = {}
    edges = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith('%%') or line == 'flowchart TB':
            continue
        if match := NODE.fullmatch(line):
            nodes[match[1]] = match[2]
        elif match := EDGE.fullmatch(line):
            edges.append((match[1], match[4], match[3] or '', match[2]))
        else:
            raise ValueError(f'Unsupported topology statement: {line}')
    if any(start not in nodes or end not in nodes for start, end, _, _ in edges):
        raise ValueError('Topology edge references an undefined node')
    return nodes, edges


def _edge_style(arrow: str, arrow_id: str) -> tuple[str, str, str]:
    # Solid sky-blue is the report/data channel toward the Controller.
    # Dashed violet is a Controller-initiated command lane (poll response / SSH).
    if arrow == '-.->':
        return CMD_STROKE, f'{arrow_id}-cmd', CMD_LABEL
    return DATA_STROKE, arrow_id, DATA_LABEL


def _build_edge(
    start: str,
    end: str,
    label: str,
    arrow: str,
    positions: dict[str, tuple[int, int]],
    rects: dict[str, tuple[float, float, float, float]],
    styles: dict[tuple[str, str], str],
    arrow_id: str,
) -> _EdgeDraw:
    x1, y1 = positions[start]
    x2, y2 = positions[end]
    stroke, marker, label_fill = _edge_style(arrow, arrow_id)
    lines, pill_w, pill_h = _pill_metrics(label) if label else ([], 0.0, 0.0)
    paired = (end, start) in styles
    px, py = _canonical_perp(x1, y1, x2, y2)
    if not paired:
        bow = 0
    elif arrow != styles[(end, start)]:
        bow = 1 if arrow == '-->' else -1
    else:
        bow = 1 if (start, end) < (end, start) else -1
    # Paired directions run on parallel shafts so each caption can follow its
    # own arrow without covering the opposite one.
    if bow:
        sx, sy, tx, ty = _paired_ends(rects[start], rects[end], x1, y1, x2, y2, px, py, bow)
    else:
        sx, sy = _border_point(rects[start], x1, y1, x2, y2)
        tx, ty = _border_point(rects[end], x2, y2, x1, y1)
    sx, sy, tx, ty = _shorten(sx, sy, tx, ty, 3, 6)
    out_x, out_y = _preferred_perp(tx - sx, ty - sy)
    angle = 0.0
    if bow and label:
        shaft = math.hypot(tx - sx, ty - sy)
        # One line stays thin enough for a shallow pair (a horizontal lane only
        # has the box height to separate the two shafts). Wrap when that line
        # cannot keep stubs at both ends of the shaft.
        lines, pill_w, pill_h = _pill_metrics(label, max_width=10**6)
        if pill_w > shaft - 100:
            budget = max(128, min(176, int(shaft - 64)))
            lines, pill_w, pill_h = _pill_metrics(label, max_width=budget)
        angle = _reading_angle(tx - sx, ty - sy)
    # Vertical pairs stack both captions in the lower half of the shaft, clear
    # of arrows that leave the upper node toward the side. Diagonal pairs
    # stagger toward opposite ends. Other pairs stay near mid-shaft.
    dx, dy = x2 - x1, y2 - y1
    if bow == 0:
        prefer_along = 0.0
    elif abs(dx) < 100 and abs(dy) > 150:
        toward_lower = -1 if y1 > y2 else 1
        prefer_along = toward_lower * (0.28 if bow > 0 else 0.1)
    elif abs(dx) > 120 and abs(dy) > 120:
        prefer_along = -0.22 if bow > 0 else 0.22
    else:
        prefer_along = -0.18 if bow > 0 else 0.18
    return _EdgeDraw(
        start=start,
        end=end,
        label=label,
        arrow=arrow,
        lines=lines,
        x1=sx,
        y1=sy,
        x2=tx,
        y2=ty,
        ctrl=None,
        bow=bow,
        prefer_along=prefer_along,
        out_x=out_x,
        out_y=out_y,
        stroke=stroke,
        marker=marker,
        label_fill=label_fill,
        pill_w=pill_w,
        pill_h=pill_h,
        segments=[(sx, sy, tx, ty)],
        angle=angle,
    )


def _reading_angle(dx: float, dy: float) -> float:
    """Degrees that run a caption along its own arrow.

    SVG ``rotate()`` is clockwise and y grows downward: 0° reads to the right,
    90° reads downward, and -90° reads upward. A shaft that points almost due
    left would turn the whole line upside down, so that one case is flipped
    and the caption stays on the shaft. Downward and diagonal return arrows
    keep the arrow's own reading direction.
    """
    angle = math.degrees(math.atan2(dy, dx))
    # A return arrow that points mostly left puts the glyphs upside down.
    # Flip only that case. Downward and down-left arrows keep the reading
    # direction, so each caption still runs with its own arrowhead.
    if angle > 155 or angle < -155:
        angle += 180 if angle < 0 else -180
    return angle


def _rotated_corners(cx: float, cy: float, width: float, height: float, angle: float) -> list[tuple[float, float]]:
    """World-space corners of a chip rotated clockwise by ``angle`` (SVG)."""
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    corners = []
    for lx, ly in (
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ):
        corners.append((cx + lx * cos_a - ly * sin_a, cy + lx * sin_a + ly * cos_a))
    return corners


def _rotated_aabb(cx: float, cy: float, width: float, height: float, angle: float) -> tuple[float, float, float, float]:
    corners = _rotated_corners(cx, cy, width, height, angle)
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _point_in_rotated(px: float, py: float, cx: float, cy: float, width: float, height: float, angle: float) -> bool:
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = px - cx, py - cy
    local_x = dx * cos_a + dy * sin_a
    local_y = -dx * sin_a + dy * cos_a
    return abs(local_x) <= width / 2 and abs(local_y) <= height / 2


def _rotated_hits_rect(
    cx: float,
    cy: float,
    width: float,
    height: float,
    angle: float,
    rect: tuple[float, float, float, float],
    pad: float = 0,
) -> bool:
    left, top, right, bottom = _expand(rect, pad)
    corners = _rotated_corners(cx, cy, width, height, angle)
    if any(left <= x <= right and top <= y <= bottom for x, y in corners):
        return True
    for x, y in ((left, top), (right, top), (right, bottom), (left, bottom)):
        if _point_in_rotated(x, y, cx, cy, width, height, angle):
            return True
    node_edges = (
        (left, top, right, top),
        (right, top, right, bottom),
        (right, bottom, left, bottom),
        (left, bottom, left, top),
    )
    for index, (x1, y1) in enumerate(corners):
        x2, y2 = corners[(index + 1) % 4]
        if any(_segments_intersect(x1, y1, x2, y2, ex1, ey1, ex2, ey2) for ex1, ey1, ex2, ey2 in node_edges):
            return True
    return False


def _rotated_hits_rotated(
    ax: float,
    ay: float,
    aw: float,
    ah: float,
    a_angle: float,
    bx: float,
    by: float,
    bw: float,
    bh: float,
    b_angle: float,
    pad: float = 0,
) -> bool:
    a_corners = _rotated_corners(ax, ay, aw + pad, ah + pad, a_angle)
    b_corners = _rotated_corners(bx, by, bw + pad, bh + pad, b_angle)
    if any(_point_in_rotated(x, y, bx, by, bw + pad, bh + pad, b_angle) for x, y in a_corners):
        return True
    if any(_point_in_rotated(x, y, ax, ay, aw + pad, ah + pad, a_angle) for x, y in b_corners):
        return True
    for index, (x1, y1) in enumerate(a_corners):
        x2, y2 = a_corners[(index + 1) % 4]
        for other, (u1, v1) in enumerate(b_corners):
            u2, v2 = b_corners[(other + 1) % 4]
            if _segments_intersect(x1, y1, x2, y2, u1, v1, u2, v2):
                return True
    return False


def _half_extent(width: float, height: float, ux: float, uy: float) -> float:
    """How far an axis-aligned chip reaches along a unit vector, from its center."""
    return abs(ux) * width / 2 + abs(uy) * height / 2


def _inside_interval(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    rect: tuple[float, float, float, float],
) -> tuple[float, float] | None:
    """Parametric span of the segment that lies inside ``rect`` (Liang–Barsky)."""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rect[0]), (dx, rect[2] - x1), (-dy, y1 - rect[1]), (dy, rect[3] - y1)):
        if abs(p) < 1e-9:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return t0, t1


def _obstacle_score(
    rect: tuple[float, float, float, float],
    node_rects: list[tuple[float, float, float, float]],
    placed: list[tuple[float, float, float, float]],
    foreign: list[tuple[float, float, float, float]],
    foreign_penalty: float,
) -> float:
    score = 0.0
    for node in node_rects:
        amount = _overlap_amount(rect, _expand(node, 8))
        if amount:
            score += 2500 + amount * 8
    for pill in placed:
        amount = _overlap_amount(rect, _expand(pill, 6))
        if amount:
            score += 2500 + amount * 8
    if any(_segment_hits_rect(sx, sy, tx, ty, rect, pad=5) for sx, sy, tx, ty in foreign):
        score += foreign_penalty
    return score


def _place_pill(
    edge: _EdgeDraw,
    node_rects: list[tuple[float, float, float, float]],
    placed: list[tuple[float, float, float, float]],
    foreign: list[tuple[float, float, float, float]],
) -> tuple[tuple[float, float, float, float], bool]:
    """Seat a horizontal chip in a gap on the arrow, or beside it if it cannot.

    On-arrow chips are preferred: the stroke breaks around the words, so the
    caption stays attached to its arrow. Beside-arrow chips are only used when
    the shaft is too short to hold the caption.
    """
    dx, dy = edge.x2 - edge.x1, edge.y2 - edge.y1
    length = math.hypot(dx, dy) or 1.0
    mid_x, mid_y = (edge.x1 + edge.x2) / 2, (edge.y1 + edge.y2) / 2
    best: tuple[float, tuple[float, float, float, float], bool] | None = None

    def consider(score: float, rect: tuple[float, float, float, float], gapped: bool) -> None:
        nonlocal best
        if best is None or score < best[0]:
            best = (score, rect, gapped)

    seen: set[float] = set()
    for along in (edge.prefer_along, 0.0, -0.18, 0.18, -0.32, 0.32, -0.44, 0.44):
        if along in seen:
            continue
        seen.add(along)
        rect = _rect_for(mid_x + dx * along, mid_y + dy * along, edge.pill_w, edge.pill_h)
        interval = _inside_interval(edge.x1, edge.y1, edge.x2, edge.y2, _expand(rect, 8))
        if interval is None:
            continue
        t0, t1 = interval
        if t0 * length < 18 or (1 - t1) * length < 20:
            continue
        score = (
            _obstacle_score(rect, node_rects, placed, foreign, foreign_penalty=140)
            + abs(along - edge.prefer_along) * 36
        )
        consider(score, rect, True)

    clear = _half_extent(edge.pill_w, edge.pill_h, edge.out_x, edge.out_y) + 14
    for along in (0.0, 0.16, -0.16, 0.3, -0.3):
        for sign in (1, -1):
            for extra in (0, 26, 52):
                dist = clear + extra
                cx = mid_x + dx * along + edge.out_x * sign * dist
                cy = mid_y + dy * along + edge.out_y * sign * dist
                rect = _rect_for(cx, cy, edge.pill_w, edge.pill_h)
                score = (
                    _obstacle_score(rect, node_rects, placed, foreign, foreign_penalty=700)
                    + abs(along) * 36
                    + extra * 0.3
                    + (0 if sign > 0 else 24)
                    + 520
                )
                consider(score, rect, False)
    assert best is not None
    return best[1], best[2]


def _place_aligned(
    edge: _EdgeDraw,
    node_rects: list[tuple[float, float, float, float]],
    placed: list[tuple[float, float, float, float, float]],
) -> tuple[float, tuple[float, float, float, float], tuple[float, float], tuple[float, float]]:
    """Seat a caption on its own shaft, rotated to read along that arrow.

    The score is below 2500 when the chip clears nodes and captions already
    seated. Callers keep the lowest score.
    """
    dx, dy = edge.x2 - edge.x1, edge.y2 - edge.y1
    length = math.hypot(dx, dy) or 1.0
    span = edge.pill_w + 14
    best: tuple[float, tuple[float, float], tuple[float, float], tuple[float, float, float, float]] | None = None
    candidates = [edge.prefer_along, 0.0, -edge.prefer_along]
    for step in (0.08, 0.16, 0.24, 0.32, 0.4):
        candidates.append(edge.prefer_along + step)
        candidates.append(edge.prefer_along - step)
    for along in candidates:
        cx = (edge.x1 + edge.x2) / 2 + dx * along
        cy = (edge.y1 + edge.y2) / 2 + dy * along
        t_center = 0.5 + along
        half_t = (span / 2) / length
        t0, t1 = t_center - half_t, t_center + half_t
        if t0 * length < 12 or (1 - t1) * length < 14:
            continue
        aabb = _rotated_aabb(cx, cy, edge.pill_w, edge.pill_h, edge.angle)
        score = abs(along - edge.prefer_along) * 80
        if any(_rotated_hits_rect(cx, cy, edge.pill_w, edge.pill_h, edge.angle, node, pad=4) for node in node_rects):
            score += 2500
        for ox, oy, ow, oh, o_angle in placed:
            if _rotated_hits_rotated(cx, cy, edge.pill_w, edge.pill_h, edge.angle, ox, oy, ow, oh, o_angle, pad=4):
                score += 2500
        if best is None or score < best[0]:
            best = (score, (cx, cy), (t0, t1), aabb)
    if best is None:
        cx = (edge.x1 + edge.x2) / 2
        cy = (edge.y1 + edge.y2) / 2
        aabb = _rotated_aabb(cx, cy, edge.pill_w, edge.pill_h, edge.angle)
        return 5000.0, aabb, (cx, cy), (0.2, 0.8)
    return best[0], best[3], best[1], best[2]


def _seat_on_shaft(
    edge: _EdgeDraw,
    node_rects: list[tuple[float, float, float, float]],
    placed: list[tuple[float, float, float, float, float]],
) -> tuple[tuple[float, float, float, float], tuple[float, float], tuple[float, float]]:
    """Place the caption, wrapping it shorter when the long form has nowhere clear."""
    best = _place_aligned(edge, node_rects, placed)
    if best[0] >= 2500:
        for max_width in (168, 140, 120):
            lines, width, height = _pill_metrics(edge.label, max_width=max_width)
            if width >= edge.pill_w - 1:
                continue
            edge.lines, edge.pill_w, edge.pill_h = lines, width, height
            candidate = _place_aligned(edge, node_rects, placed)
            if candidate[0] < best[0]:
                best = candidate
            if candidate[0] < 2500:
                break
    return best[1], best[2], best[3]


def _text_element(
    x: float,
    y: float,
    lines: list[str],
    font_size: int,
    weight: int,
    fill: str,
    line_h: int,
    transform: str = '',
) -> str:
    attrs = (
        f'x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
        f'fill="{fill}" font-size="{font_size}" font-weight="{weight}" font-family="{FONT_FAMILY}"'
        f'{transform}'
    )
    if len(lines) <= 1:
        body = html.escape(lines[0]) if lines else ''
        return f'<text {attrs}>{body}</text>'
    first_dy = -line_h * (len(lines) - 1) / 2
    spans = []
    for index, line in enumerate(lines):
        dy = first_dy if index == 0 else line_h
        spans.append(f'<tspan x="{x:.1f}" dy="{dy:.1f}">{html.escape(line)}</tspan>')
    return f'<text {attrs}>{"".join(spans)}</text>'


def _marker_defs(arrow_id: str) -> str:
    def marker(suffix: str, color: str) -> str:
        ident = arrow_id if not suffix else f'{arrow_id}-{suffix}'
        return (
            f'<marker id="{ident}" viewBox="0 0 12 10" markerWidth="13" markerHeight="11" '
            f'refX="11" refY="5" orient="auto" markerUnits="userSpaceOnUse">'
            f'<path d="M0,1 L11,5 L0,9 Z" fill="{color}"/></marker>'
        )

    return marker('', DATA_STROKE) + marker('cmd', CMD_STROKE)


def render_topology(source: str, source_name: str = 'platform-topology.mmd') -> str:
    layout = LAYOUTS.get(source_name, DEFAULT_LAYOUT)
    nodes, edges = parse_topology(source)
    positions = dict(layout.positions)
    extra = [name for name in nodes if name not in positions]
    for index, name in enumerate(extra):
        positions[name] = (220 + index % 3 * 460, 1040 + index // 3 * 180)
    metrics = {name: _node_metrics(label) for name, label in nodes.items()}
    rects = {
        name: _rect_for(positions[name][0], positions[name][1], metrics[name][1], metrics[name][2]) for name in nodes
    }
    stem = source_name.removesuffix('-topology.mmd') or 'topology'
    arrow_id = 'topology-arrow' if stem == 'platform' else f'topology-arrow-{stem}'
    title_id = 'topology-title' if stem == 'platform' else f'topology-title-{stem}'
    styles = {(start, end): arrow for start, end, _, arrow in edges}
    drawn = [
        _build_edge(start, end, label, arrow, positions, rects, styles, arrow_id) for start, end, label, arrow in edges
    ]
    node_rects = list(rects.values())
    placed_boxes: list[tuple[float, float, float, float]] = []
    placed_oriented: list[tuple[float, float, float, float, float]] = []

    def _place_order(index: int) -> tuple[int, float, float]:
        edge = drawn[index]
        if not edge.bow:
            return (1, 0.0, -edge.pill_w)
        # Short paired shafts have almost one legal station, so they claim it
        # before a long diagonal caption can cover that lane.
        shaft = math.hypot(edge.x2 - edge.x1, edge.y2 - edge.y1)
        return (0, shaft, -edge.pill_w)

    order = sorted(range(len(drawn)), key=_place_order)
    for index in order:
        edge = drawn[index]
        if not edge.label:
            continue
        if edge.bow:
            edge.pill, edge.label_at, edge.gap = _seat_on_shaft(edge, node_rects, placed_oriented)
            edge.gapped = True
            placed_oriented.append((edge.label_at[0], edge.label_at[1], edge.pill_w, edge.pill_h, edge.angle))
        else:
            foreign = [seg for other, item in enumerate(drawn) if other != index for seg in item.segments]
            edge.pill, edge.gapped = _place_pill(edge, node_rects, placed_boxes, foreign)
            left, top, right, bottom = edge.pill
            placed_oriented.append(((left + right) / 2, (top + bottom) / 2, edge.pill_w, edge.pill_h, 0.0))
        placed_boxes.append(edge.pill)

    bounds = list(rects.values()) + [edge.pill for edge in drawn if edge.pill is not None]
    pad = 36
    min_x = min(item[0] for item in bounds) - pad
    min_y = min(item[1] for item in bounds) - pad
    width = max(item[2] for item in bounds) + pad - min_x
    height = max(item[3] for item in bounds) + pad - min_y
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x:.1f} {min_y:.1f} {width:.1f} {height:.1f}" '
            f'role="img" aria-labelledby="{title_id}" data-topology-source="{html.escape(source_name)}" '
            f'font-family="{FONT_FAMILY}">'
        ),
        f'<title id="{title_id}">GMS Remote Test 拓扑：{html.escape(source_name)}</title>',
        f'<defs>{_marker_defs(arrow_id)}</defs>',
    ]
    for name in nodes:
        lines, box_w, box_h = metrics[name]
        cx, cy = positions[name]
        stroke = layout.kinds.get(name, SLATE)
        parts.append(
            f'<g data-node="{name}">'
            f'<rect x="{cx - box_w / 2:.1f}" y="{cy - box_h / 2:.1f}" width="{box_w:.1f}" height="{box_h:.1f}" '
            f'rx="12" fill="{NODE_FILL}" stroke="{stroke}" stroke-width="2"/>'
            f'{_text_element(cx, cy, lines, NODE_FONT_SIZE, 600, NODE_TEXT, NODE_LINE_H)}</g>'
        )
    for edge in drawn:
        dashed = ' stroke-dasharray="7 6"' if edge.arrow == '-.->' else ''
        if edge.ctrl is not None:
            path = f'M{edge.x1:.1f},{edge.y1:.1f} Q{edge.ctrl[0]:.1f},{edge.ctrl[1]:.1f} {edge.x2:.1f},{edge.y2:.1f}'
        elif edge.gap is not None:
            t0, t1 = edge.gap
            dx, dy = edge.x2 - edge.x1, edge.y2 - edge.y1
            path = (
                f'M{edge.x1:.1f},{edge.y1:.1f} L{edge.x1 + dx * t0:.1f},{edge.y1 + dy * t0:.1f} '
                f'M{edge.x1 + dx * t1:.1f},{edge.y1 + dy * t1:.1f} L{edge.x2:.1f},{edge.y2:.1f}'
            )
        elif edge.gapped and edge.pill is not None:
            interval = _inside_interval(edge.x1, edge.y1, edge.x2, edge.y2, _expand(edge.pill, 8))
            if interval is None:
                path = f'M{edge.x1:.1f},{edge.y1:.1f} L{edge.x2:.1f},{edge.y2:.1f}'
            else:
                t0, t1 = interval
                dx, dy = edge.x2 - edge.x1, edge.y2 - edge.y1
                path = (
                    f'M{edge.x1:.1f},{edge.y1:.1f} L{edge.x1 + dx * t0:.1f},{edge.y1 + dy * t0:.1f} '
                    f'M{edge.x1 + dx * t1:.1f},{edge.y1 + dy * t1:.1f} L{edge.x2:.1f},{edge.y2:.1f}'
                )
        else:
            path = f'M{edge.x1:.1f},{edge.y1:.1f} L{edge.x2:.1f},{edge.y2:.1f}'
        if edge.label and edge.label_at is not None:
            cx, cy = edge.label_at
            transform = f' transform="rotate({edge.angle:.1f} {cx:.1f} {cy:.1f})"'
            label_markup = (
                f'<rect x="{cx - edge.pill_w / 2:.1f}" y="{cy - edge.pill_h / 2:.1f}" '
                f'width="{edge.pill_w:.1f}" height="{edge.pill_h:.1f}" rx="8" fill="{PILL_FILL}" '
                f'stroke="{edge.stroke}" stroke-width="1.25"{transform}/>'
                f'{_text_element(cx, cy, edge.lines, EDGE_FONT_SIZE, 500, edge.label_fill, EDGE_LINE_H, transform)}'
            )
        elif edge.label and edge.pill is not None:
            left, top, right, bottom = edge.pill
            cx, cy = (left + right) / 2, (top + bottom) / 2
            label_markup = (
                f'<rect x="{left:.1f}" y="{top:.1f}" width="{edge.pill_w:.1f}" height="{edge.pill_h:.1f}" '
                f'rx="8" fill="{PILL_FILL}" stroke="{edge.stroke}" stroke-width="1.25"/>'
                f'{_text_element(cx, cy, edge.lines, EDGE_FONT_SIZE, 500, edge.label_fill, EDGE_LINE_H)}'
            )
        else:
            label_markup = _text_element(0, 0, [], EDGE_FONT_SIZE, 500, edge.label_fill, EDGE_LINE_H)
        parts.append(
            f'<g data-source="{edge.start}" data-target="{edge.end}" data-edge-style="{edge.arrow}">'
            f'<path d="{path}" fill="none" stroke="{edge.stroke}" stroke-width="2.5" '
            f'stroke-linecap="round" marker-end="url(#{edge.marker})"{dashed}/>'
            f'{label_markup}</g>'
        )
    parts.append('</svg>')
    return ''.join(parts)
