"""Render the repository's Mermaid topology subset as a local, script-free SVG."""

import html
import math
import re
from dataclasses import dataclass, field


NODE = re.compile(r'^(\w+)\[\(?"([^"]+)"\)?\]$')
EDGE = re.compile(r'^(\w+)\s+(-->|-\.->)(?:\|"([^"]+)"\|)?\s+(\w+)$')

SLATE = "#64748b"
# 250px box minus ~10px padding each side. Kept below the theoretical 230:
# the estimator is approximate, so borderline labels get compressed too.
NODE_TEXT_WIDTH = 220


def _estimate_text_width(text: str) -> int:
    """Rough advance estimate at font-size 14 (monospace UI font stack).

    Latin monospace advance is ~8.4px per char (rounded up to 9); CJK and
    full-width punctuation (>= U+2000, incl. arrows like →) render ~14px.
    """
    return sum(9 if ord(ch) < 0x2000 else 14 for ch in text)


@dataclass(frozen=True)
class TopologyLayout:
    """Presentation-only layout for one topology diagram."""

    positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)


# Coordinates are presentation only. Nodes, labels, and directed edges come
# exclusively from docs/architecture/*-topology.mmd; extra nodes get an
# overflow row.
LAYOUTS = {
    "platform-topology.mmd": TopologyLayout(
        positions={
            "Browser": (250, 70), "Agent": (740, 70), "Controller": (740, 300),
            "DB": (1190, 300), "WorkerA": (400, 560), "WorkerB": (820, 560),
            "Source": (160, 390), "Devices": (400, 810), "Suite": (720, 810),
            "Build": (1190, 460), "External": (1200, 810),
        },
        kinds={
            "Browser": "#38bdf8", "Agent": "#a78bfa", "Controller": "#38bdf8",
            "DB": "#34d399", "WorkerA": "#fbbf24", "WorkerB": "#fbbf24",
            "Source": "#34d399", "Devices": "#34d399", "Suite": "#34d399",
        },
    ),
    "usbip-topology.mmd": TopologyLayout(
        positions={
            "DevSrc": (340, 70), "WinSrc": (140, 220), "LinuxSrc": (560, 220),
            "Worker": (340, 370), "Ctrl": (1000, 370), "Tools": (340, 520),
            "DevWorker": (140, 670),
        },
        kinds={
            "DevSrc": "#34d399", "DevWorker": "#34d399", "WinSrc": "#34d399",
            "LinuxSrc": "#34d399", "Worker": "#fbbf24", "Tools": "#fbbf24",
            "Ctrl": "#38bdf8",
        },
    ),
    "aoa-topology.mmd": TopologyLayout(
        positions={
            "Browser": (200, 70), "Ctrl": (700, 70), "Store": (1180, 70),
            "Worker": (700, 230), "Acc": (380, 390), "Dev": (380, 550),
            "Adb": (950, 550),
        },
        kinds={
            "Browser": "#38bdf8", "Ctrl": "#38bdf8", "Store": "#34d399",
            "Worker": "#fbbf24", "Acc": "#fbbf24", "Dev": "#34d399",
            "Adb": "#a78bfa",
        },
    ),
    "firmware-topology.mmd": TopologyLayout(
        positions={
            "Browser": (220, 70), "Ctrl": (720, 70), "Worker": (720, 220),
            "Loader": (340, 370), "Dev": (340, 520), "AoaRec": (1000, 370),
        },
        kinds={
            "Browser": "#38bdf8", "Ctrl": "#38bdf8", "Worker": "#fbbf24",
            "Loader": "#fbbf24", "Dev": "#34d399", "AoaRec": "#a78bfa",
        },
    ),
}
DEFAULT_LAYOUT = TopologyLayout()


def parse_topology(source: str) -> tuple[dict[str, str], list[tuple[str, str, str, str]]]:
    nodes = {}
    edges = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("%%") or line == "flowchart TB":
            continue
        if match := NODE.fullmatch(line):
            nodes[match[1]] = match[2]
        elif match := EDGE.fullmatch(line):
            edges.append((match[1], match[4], match[3] or "", match[2]))
        else:
            raise ValueError(f"Unsupported topology statement: {line}")
    if any(start not in nodes or end not in nodes for start, end, _, _ in edges):
        raise ValueError("Topology edge references an undefined node")
    return nodes, edges


def render_topology(source: str, source_name: str = "platform-topology.mmd") -> str:
    layout = LAYOUTS.get(source_name, DEFAULT_LAYOUT)
    nodes, edges = parse_topology(source)
    positions = dict(layout.positions)
    extra = [name for name in nodes if name not in positions]
    for index, name in enumerate(extra):
        positions[name] = (200 + index % 3 * 430, 980 + index // 3 * 160)
    # Canvas hugs the drawn content with one uniform margin so the diagram
    # fills its container instead of floating in an oversized viewBox.
    pad = 40
    min_x = min(x for x, _ in positions.values()) - 125 - pad
    min_y = min(y for _, y in positions.values()) - 35 - pad
    width = max(x for x, _ in positions.values()) + 125 + pad - min_x
    height = max(y for _, y in positions.values()) + 35 + pad - min_y
    stem = source_name.removesuffix("-topology.mmd") or "topology"
    arrow_id = "topology-arrow" if stem == "platform" else f"topology-arrow-{stem}"
    title_id = "topology-title" if stem == "platform" else f"topology-title-{stem}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x} {min_y} {width} {height}" role="img" aria-labelledby="{title_id}" data-topology-source="{html.escape(source_name)}">',
        f'<title id="{title_id}">GMS Remote Test 拓扑：{html.escape(source_name)}</title>',
        f'<defs><marker id="{arrow_id}" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8" fill="#38bdf8"/></marker>'
        f'<marker id="{arrow_id}-cmd" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8" fill="#a78bfa"/></marker></defs>',
    ]
    directed_edges = {(start, end) for start, end, _, _ in edges}
    for start, end, label, arrow in edges:
        x1, y1 = positions[start]
        x2, y2 = positions[end]
        dx, dy = x2 - x1, y2 - y1
        scale = min(125 / abs(dx) if dx else 1, 35 / abs(dy) if dy else 1)
        sx, sy = x1 + dx * scale, y1 + dy * scale
        tx, ty = x2 - dx * scale, y2 - dy * scale
        dashed = ' stroke-dasharray="7 5"' if arrow == "-.->" else ""
        # Color semantics across every diagram: solid sky-blue = report/data
        # channel toward the Controller; dashed violet = Controller-initiated
        # command channel (poll response / SSH), with a matching arrowhead
        # and label tint so paired directions read as two distinct lanes.
        if arrow == "-.->":
            stroke_color, marker_ref, label_fill = "#a78bfa", f"{arrow_id}-cmd", "#c4b5fd"
        else:
            stroke_color, marker_ref, label_fill = "#38bdf8", arrow_id, "#cbd5e1"
        # Opposite-direction node pairs keep opposing bows so both arrows and
        # their labels stay separated; single edges are straight. Labels ride
        # the curve apex (quadratic-bezier midpoint) and rotate to the chord
        # direction — flipped 180° when that would render them upside down —
        # so the text reads along the arrow like the rest of the diagram.
        if (end, start) in directed_edges:
            offset = 45 if arrow == "-.->" else -40
            # Paired lines share both boxes: shift each line's attachment
            # points onto its own side of the center line (opposite chords
            # get opposite perpendiculars) and re-clamp them to the box
            # borders, so both arrow heads stay visibly separate at each box
            # instead of merging with the other line's arrow head.
            length = math.hypot(dx, dy) or 1
            side = 18 if offset > 0 else -18
            ux, uy = -dy / length * side, dx / length * side
            sx2, sy2, tx2, ty2 = sx + ux, sy + uy, tx + ux, ty + uy
            scale_s = min(
                125 / abs(sx2 - x1) if abs(sx2 - x1) else 1,
                35 / abs(sy2 - y1) if abs(sy2 - y1) else 1,
            )
            sx, sy = x1 + (sx2 - x1) * scale_s, y1 + (sy2 - y1) * scale_s
            scale_t = min(
                125 / abs(tx2 - x2) if abs(tx2 - x2) else 1,
                35 / abs(ty2 - y2) if abs(ty2 - y2) else 1,
            )
            tx, ty = x2 + (tx2 - x2) * scale_t, y2 + (ty2 - y2) * scale_t
            mx, my = (sx + tx) / 2, (sy + ty) / 2 + offset
            # Paired arrows flank each other: each label rides OUTSIDE its
            # own bow (above the upper bow, below the lower one) so no text
            # sits between the two lines.
            label_dy = 20 if offset > 0 else -8
        else:
            mx, my = (sx + tx) / 2, (sy + ty) / 2
            label_dy = -8
        lx, ly = (sx + 2 * mx + tx) / 4, (sy + 2 * my + ty) / 4
        angle = math.degrees(math.atan2(ty - sy, tx - sx))
        if angle > 90:
            angle -= 180
        elif angle < -90:
            angle += 180
        parts.append(
            f'<g data-source="{start}" data-target="{end}" data-edge-style="{arrow}">'
            f'<path d="M{sx:.1f},{sy:.1f} Q{mx:.1f},{my:.1f} {tx:.1f},{ty:.1f}" fill="none" stroke="{stroke_color}" stroke-width="2" marker-end="url(#{marker_ref})"{dashed}/>'
            f'<text x="{lx:.1f}" y="{ly:.1f}" dy="{label_dy}" transform="rotate({angle:.1f} {lx:.1f} {ly:.1f})" text-anchor="middle" fill="{label_fill}" font-size="12" stroke="#0f172a" stroke-width="4" paint-order="stroke">{html.escape(label)}</text></g>'
        )
    for name, label in nodes.items():
        x, y = positions[name]
        stroke = layout.kinds.get(name, SLATE)
        text_attrs = f'x="{x}" y="{y + 5}" text-anchor="middle" fill="#e2e8f0" font-size="14"'
        if _estimate_text_width(label) > NODE_TEXT_WIDTH:
            text_attrs += f' textLength="{NODE_TEXT_WIDTH}" lengthAdjust="spacingAndGlyphs"'
        parts.append(
            f'<g data-node="{name}"><rect x="{x - 125}" y="{y - 35}" width="250" height="70" rx="10" fill="#0f172a" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text {text_attrs}>{html.escape(label)}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)
