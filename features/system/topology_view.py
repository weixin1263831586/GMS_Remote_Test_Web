"""Render canonical Mermaid sources as local SVG using one diagram template.

Graph content stays in docs/architecture; topology_layouts owns presentation.
No browser graph engine, external fonts or scripts are needed.
"""

import html
import math
import re
from itertools import pairwise

from .topology_layouts import ACCESS, CONTROL, LAYOUTS, RESOURCE, WORKER, Point, Port


NODE = re.compile(r'^(\w+)\[\(?"([^"]+)"\)?\]$')
EDGE = re.compile(r'^(\w+)\s+(-->|-\.->)(?:\|"([^"]+)"\|)?\s+(\w+)$')
FONT_FAMILY = "'Noto Sans CJK SC', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', sans-serif"
NODE_FONT_SIZE = 14
EDGE_FONT_SIZE = 12
NODE_W, NODE_H = 208, 72
NODE_PAD_X = 16
NODE_LINE_H, EDGE_LINE_H = 20, 16
CANVAS_W = 1240
DATA_STROKE = 'var(--arch-data, #38bdf8)'
CMD_STROKE = 'var(--arch-command, #a78bfa)'


def _estimate_text_width(text: str, font_size: int = NODE_FONT_SIZE, ascii_em: float = 0.66) -> int:
    return math.ceil(sum(font_size * (ascii_em if ord(ch) < 128 else 1) for ch in text))


def _wrap_label(text: str, font_size: int, max_width: int) -> list[str]:
    """Wrap at words / CJK characters, preserving the canonical label verbatim."""
    if _estimate_text_width(text, font_size) <= max_width:
        return [text]
    # Balance two lines and prefer phrase boundaries over a one-character tail.
    candidates = []
    for i in range(1, len(text)):
        before, after = text[i - 1], text[i]
        if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
            continue
        left, right = text[:i], text[i:]
        a, b = _estimate_text_width(left, font_size), _estimate_text_width(right, font_size)
        if max(a, b) <= max_width and left.strip() and right.strip():
            penalty = -60 if before in ' /：' else 0
            if after in '/）' or before == '（':
                penalty += 100
            candidates.append((abs(a - b) + penalty, i))
    if candidates:
        _, split = min(candidates)
        return [text[:split], text[split:]]
    lines, line = [], ''
    for token in re.findall(r'[A-Za-z0-9_.+/-]+|\s+|.', text):
        if line and _estimate_text_width(line + token, font_size) > max_width:
            lines.append(line)
            line = ''
        line += token
    if line:
        lines.append(line)
    if any(_estimate_text_width(line, font_size) > max_width for line in lines):
        raise ValueError(f'Topology label needs a shorter word: {text}')
    return lines


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


def _port(center: Point, port: Port) -> Point:
    x, y = center
    side, offset = port
    if side == 'left':
        return x - NODE_W / 2, y + offset
    if side == 'right':
        return x + NODE_W / 2, y + offset
    if side == 'top':
        return x + offset, y - NODE_H / 2
    if side == 'bottom':
        return x + offset, y + NODE_H / 2
    raise ValueError(f'Unknown topology port: {side}')


def _text(x: float, y: float, lines: list[str], font_size: int, line_h: int, weight: int = 500) -> str:
    attrs = (f'x="{x:g}" y="{y:g}" text-anchor="middle" dominant-baseline="central" '
             f'font-family="{FONT_FAMILY}" font-size="{font_size}" font-weight="{weight}" '
             'fill="var(--arch-text, #f1f5f9)"')
    spans = ''.join(
        f'<tspan x="{x:g}" dy="{-line_h * (len(lines) - 1) / 2 if i == 0 else line_h:g}">'
        f'{html.escape(line)}</tspan>' for i, line in enumerate(lines)
    )
    return f'<text {attrs}>{spans}</text>'


def _marker(ident: str, color: str) -> str:
    return (f'<marker id="{ident}" viewBox="0 0 10 8" markerWidth="10" markerHeight="8" '
            'refX="9" refY="4" orient="auto" markerUnits="userSpaceOnUse">'
            f'<path d="M0,0 L9,4 L0,8 Z" fill="{color}"/></marker>')


def _caption_candidates(points, anchor, width, height, nodes, shafts, max_y, label_side=None):
    """Seat transparent captions beside a shaft, clear of nodes and all arrows."""
    candidates = []
    for a, b in pairwise(points):
        horizontal = a[1] == b[1]
        axis = 0 if horizontal else 1
        low, high = sorted((a[axis], b[axis]))
        half = (width if horizontal else height) / 2
        if high == low:
            continue
        stations = (max(low, min(high, anchor[axis])), (low + high) / 2,
                    min(high, low + half), max(low, high - half), low, high)
        for station in stations:
            for side in (-1, 1):
                position = ('above' if side < 0 else 'below') if horizontal else ('left' if side < 0 else 'right')
                if label_side is not None and position != label_side:
                    continue
                x = station if horizontal else a[0] + side * (width / 2 + 8)
                y = a[1] + side * (height / 2 + 8) if horizontal else station
                box = (x - width / 2, y - height / 2, x + width / 2, y + height / 2)
                if box[0] < 4 or box[2] > CANVAS_W - 4 or box[1] < 4 or box[3] > max_y:
                    continue
                padded = (box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4)
                if any(_boxes_overlap(padded, node) for node in nodes):
                    continue
                if any(_shaft_hits_box(start, end, padded) for start, end in shafts):
                    continue
                rank = abs(x - anchor[0]) + abs(y - anchor[1])
                candidates.append((rank, x, y, box))
    return sorted(set(candidates))


def _boxes_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _shaft_hits_box(a, b, box):
    left, top, right, bottom = box
    if a[0] == b[0]:
        return left < a[0] < right and min(a[1], b[1]) < bottom and max(a[1], b[1]) > top
    return top < a[1] < bottom and min(a[0], b[0]) < right and max(a[0], b[0]) > left


def _place_captions(nodes, edges, layout, max_y):
    paths = {
        (start, end): (_port(layout.positions[start], route.start), *route.via,
                       _port(layout.positions[end], route.end))
        for (start, end), route in layout.routes.items()
    }
    obstacles = [(x - NODE_W / 2, y - NODE_H / 2, x + NODE_W / 2, y + NODE_H / 2)
                 for name, (x, y) in layout.positions.items() if name in nodes]
    shafts = [segment for points in paths.values() for segment in pairwise(points)]
    options = {}
    for start, end, label, _ in edges:
        if not label:
            continue
        route = layout.routes[start, end]
        if route.label is None:
            raise ValueError(f'Topology route needs a caption position: {start} → {end}')
        lines = _wrap_label(label, EDGE_FONT_SIZE, route.label_width - 16)
        width = max(_estimate_text_width(line, EDGE_FONT_SIZE) for line in lines) + 16
        height = len(lines) * EDGE_LINE_H + 8
        seats = _caption_candidates(
            paths[start, end], route.label, width, height, obstacles, shafts, max_y, route.label_side,
        )
        options[start, end] = (lines, width, height, seats)
    placed, boxes = {}, []
    for edge, (lines, width, height, seats) in sorted(options.items(), key=lambda item: len(item[1][3])):
        for _, x, y, box in seats:
            padded = (box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4)
            if not any(_boxes_overlap(padded, other) for other in boxes):
                placed[edge] = (lines, width, height, x, y)
                boxes.append(box)
                break
        else:
            raise ValueError(f'Topology caption needs a clearer lane: {edge}')
    return placed


def render_topology(source: str, source_name: str = 'platform-topology.mmd') -> str:
    layout = LAYOUTS[source_name]
    nodes, edges = parse_topology(source)
    if set(nodes) != set(layout.positions) or {(a, b) for a, b, *_ in edges} != set(layout.routes):
        raise ValueError(f'Topology presentation must cover every node and edge: {source_name}')
    stem = source_name.removesuffix('-topology.mmd')
    ident = 'topology' if stem == 'platform' else f'topology-{stem}'
    arrow_id = f'{ident}-arrow'
    title_id = f'{ident}-title'
    edge_markup = []
    max_y = max(y + NODE_H / 2 for _, y in layout.positions.values())
    max_y = max([max_y, *(y for route in layout.routes.values() for _, y in route.via)])
    captions = _place_captions(nodes, edges, layout, max_y + 24)
    for start, end, label, style in edges:
        route = layout.routes[start, end]
        points = (_port(layout.positions[start], route.start), *route.via,
                  _port(layout.positions[end], route.end))
        if any(x1 != x2 and y1 != y2 for (x1, y1), (x2, y2) in pairwise(points)):
            raise ValueError(f'Topology route must be orthogonal: {start} → {end}')
        max_y = max(max_y, *(y for _, y in points))
        path = ' '.join(f'{"M" if i == 0 else "L"}{x:g},{y:g}' for i, (x, y) in enumerate(points))
        command = style == '-.->'
        stroke = CMD_STROKE if command else DATA_STROKE
        marker = f'{arrow_id}-cmd' if command else arrow_id
        dashed = ' stroke-dasharray="6 5"' if command else ''
        caption = ''
        if label:
            lines, w, h, x, y = captions[start, end]
            max_y = max(max_y, y + h / 2)
            caption = (f'<rect x="{x - w / 2:g}" y="{y - h / 2:g}" width="{w}" height="{h}" '
                       'rx="5" fill="none"/>'
                       f'{_text(x, y, lines, EDGE_FONT_SIZE, EDGE_LINE_H)}')
        edge_markup.append(
            f'<g data-source="{start}" data-target="{end}" data-edge-style="{style}">'
            f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="1.75" '
            f'stroke-linejoin="round" marker-end="url(#{marker})"{dashed}/>{caption}</g>'
        )
    height = max_y + 24
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS_W} {height:g}" '
        f'width="{CANVAS_W}" height="{height:g}" role="img" aria-labelledby="{title_id}" '
        f'data-topology-source="{html.escape(source_name, quote=True)}">',
        f'<title id="{title_id}">GMS Remote Test 拓扑：{html.escape(source_name)}</title>',
        (f'<defs>{_marker(arrow_id, DATA_STROKE)}{_marker(arrow_id + "-cmd", CMD_STROKE)}'
         + ''.join(
             f'<linearGradient id="{ident}-node-{kind}" x1="0" y1="0" x2="1" y2="1">'
             f'<stop offset="0%" stop-color="{top}"/>'
             f'<stop offset="100%" stop-color="#141627"/>'
             '</linearGradient>'
             for kind, top in (('access', '#36234f'), ('control', '#23365d'),
                               ('worker', '#403323'), ('resource', '#1a3b36'))
         )
         + f'<radialGradient id="{ident}-halo"><stop offset="0%" stop-color="#7353e3" '
         'stop-opacity=".24"/><stop offset="100%" stop-color="#7353e3" stop-opacity="0"/>'
         '</radialGradient></defs>'),
        (f'<ellipse cx="620" cy="{height - 34:g}" rx="420" ry="34" '
         f'fill="url(#{ident}-halo)" aria-hidden="true"/>'),
        *edge_markup,
    ]
    for name, label in nodes.items():
        lines = _wrap_label(label, NODE_FONT_SIZE, NODE_W - NODE_PAD_X * 2)
        if len(lines) > 3:
            raise ValueError(f'Topology node needs a shorter label: {label}')
        x, y = layout.positions[name]
        stroke = layout.kinds.get(name, RESOURCE)
        kind = ('access' if stroke == ACCESS else 'control' if stroke == CONTROL
                else 'worker' if stroke == WORKER else 'resource')
        parts.append(
            f'<g data-node="{name}">'
            f'<rect x="{x - NODE_W / 2:g}" y="{y - NODE_H / 2:g}" width="{NODE_W}" height="{NODE_H}" '
            f'rx="12" fill="url(#{ident}-node-{kind})" stroke="{stroke}" stroke-width="2"/>'
            f'<circle cx="{x - NODE_W / 2 + 16:g}" cy="{y - NODE_H / 2 + 15:g}" '
            f'r="4" fill="{stroke}" fill-opacity=".78"/>'
            f'{_text(x, y, lines, NODE_FONT_SIZE, NODE_LINE_H, 600)}</g>'
        )
    return ''.join([*parts, '</svg>'])
