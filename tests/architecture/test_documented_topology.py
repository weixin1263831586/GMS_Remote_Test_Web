"""Keep public architecture views aligned with the canonical topology."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from features.system.topology_view import parse_topology, render_topology


ROOT = Path(__file__).resolve().parents[2]
CANONICAL = (ROOT / "docs/architecture/platform-topology.mmd").read_text(
    encoding="utf-8"
).strip()


def _first_mermaid(path: str) -> str:
    content = (ROOT / path).read_text(encoding="utf-8")
    match = re.search(r"```mermaid\n(.*?)\n```", content, re.DOTALL)
    assert match, f"missing Mermaid topology in {path}"
    return match.group(1).strip()


def test_markdown_topologies_match_canonical_definition():
    assert _first_mermaid("README.md") == CANONICAL
    assert _first_mermaid("docs/architecture/overview.md") == CANONICAL


def test_web_architecture_covers_canonical_roles_and_flows():
    content = (ROOT / "web/templates/architecture.html").read_text(encoding="utf-8")
    for label in (
        "FastAPI Controller",
        "Agent Runtime",
        "Linux Worker A",
        "Linux Worker B",
        "Windows USB Source",
        "Linux USB Source",
        "Heartbeat / Poll / ACK",
        "命令随 Poll 响应返回",
        "USB/IP 接入示例",
        "生产入口为 HTTPS",
    ):
        assert label in content
    assert "http://&lt;test_host&gt;:5001" not in content
    assert "{{PLATFORM_TOPOLOGY}}" in content


def test_web_topology_preserves_every_node_and_directed_edge():
    nodes, edges = parse_topology(CANONICAL)
    svg = ET.fromstring(render_topology(CANONICAL))
    groups = svg.findall("{http://www.w3.org/2000/svg}g")
    rendered_nodes = {
        group.attrib["data-node"]: group.find("{http://www.w3.org/2000/svg}text").text
        for group in groups if "data-node" in group.attrib
    }
    rendered_edges = [
        (
            group.attrib["data-source"], group.attrib["data-target"],
            group.find("{http://www.w3.org/2000/svg}text").text or "",
            group.attrib["data-edge-style"],
        )
        for group in groups if "data-source" in group.attrib
    ]
    assert rendered_nodes == nodes
    assert rendered_edges == edges
    assert all(
        group.find("{http://www.w3.org/2000/svg}path").attrib["marker-end"]
        == "url(#topology-arrow)"
        for group in groups if "data-source" in group.attrib
    )
