"""Render the repository's Mermaid topology subset as a local, script-free SVG."""

import html
import re


NODE = re.compile(r'^(\w+)\[\(?"([^"]+)"\)?\]$')
EDGE = re.compile(r'^(\w+)\s+(-->|-\.->)(?:\|"([^"]+)"\|)?\s+(\w+)$')
# Coordinates are presentation only. Nodes, labels, and directed edges come
# exclusively from platform-topology.mmd; extra nodes get an overflow row.
POSITIONS = {
    "Browser": (250, 70), "Agent": (740, 70), "Controller": (740, 300),
    "DB": (1190, 300), "WorkerA": (400, 560), "WorkerB": (900, 560),
    "Source": (160, 390), "Devices": (240, 810), "Suite": (720, 810),
    "Build": (1190, 510), "External": (1190, 760),
}


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


def render_topology(source: str) -> str:
    nodes, edges = parse_topology(source)
    positions = dict(POSITIONS)
    extra = [name for name in nodes if name not in positions]
    for index, name in enumerate(extra):
        positions[name] = (200 + index % 3 * 430, 980 + index // 3 * 160)
    height = max(900, 1100 + ((len(extra) - 1) // 3) * 160) if extra else 900
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1400 {height}" role="img" aria-labelledby="topology-title" data-topology-source="platform-topology.mmd">',
        '<title id="topology-title">GMS Remote Test 平台拓扑</title>',
        '<defs><marker id="topology-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto"><path d="M0,0 L10,4 L0,8" fill="#38bdf8"/></marker></defs>',
    ]
    for start, end, label, arrow in edges:
        x1, y1 = positions[start]
        x2, y2 = positions[end]
        dx, dy = x2 - x1, y2 - y1
        scale = min(125 / abs(dx) if dx else 1, 35 / abs(dy) if dy else 1)
        sx, sy = x1 + dx * scale, y1 + dy * scale
        tx, ty = x2 - dx * scale, y2 - dy * scale
        offset = 35 if arrow == "-.->" else -35
        mx, my = (sx + tx) / 2, (sy + ty) / 2 + offset
        dashed = ' stroke-dasharray="7 5"' if arrow == "-.->" else ""
        parts.append(
            f'<g data-source="{start}" data-target="{end}" data-edge-style="{arrow}">'
            f'<path d="M{sx:.1f},{sy:.1f} Q{mx:.1f},{my:.1f} {tx:.1f},{ty:.1f}" fill="none" stroke="#38bdf8" stroke-width="2" marker-end="url(#topology-arrow)"{dashed}/>'
            f'<text x="{mx:.1f}" y="{(sy + ty) / 2 + offset / 2 - 8:.1f}" text-anchor="middle" fill="#cbd5e1" font-size="12" stroke="#0f172a" stroke-width="4" paint-order="stroke">{html.escape(label)}</text></g>'
        )
    for name, label in nodes.items():
        x, y = positions[name]
        parts.append(
            f'<g data-node="{name}"><rect x="{x - 125}" y="{y - 35}" width="250" height="70" rx="10" fill="#0f172a" stroke="#64748b"/>'
            f'<text x="{x}" y="{y + 5}" text-anchor="middle" fill="#e2e8f0" font-size="14">{html.escape(label)}</text></g>'
        )
    parts.append("</svg>")
    return "".join(parts)
