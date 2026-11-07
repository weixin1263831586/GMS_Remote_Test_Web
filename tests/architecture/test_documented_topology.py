"""Keep public architecture views aligned with the canonical topology."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from features.system.topology_view import (
    NODE_TEXT_WIDTH,
    _estimate_text_width,
    parse_topology,
    render_topology,
)


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


ARCHITECTURE_VIEWS = {
    "{{PLATFORM_TOPOLOGY}}": "platform-topology.mmd",
    "{{USBIP_TOPOLOGY}}": "usbip-topology.mmd",
    "{{AOA_TOPOLOGY}}": "aoa-topology.mmd",
    "{{FIRMWARE_TOPOLOGY}}": "firmware-topology.mmd",
}


def test_architecture_views_cover_module_topologies():
    content = (ROOT / "web/templates/architecture.html").read_text(encoding="utf-8")
    for placeholder, source_name in ARCHITECTURE_VIEWS.items():
        assert placeholder in content, f"missing placeholder {placeholder}"
        source = (ROOT / "docs/architecture" / source_name).read_text(
            encoding="utf-8"
        )
        assert parse_topology(source), f"{source_name} must define nodes and edges"


def test_architecture_views_render_as_local_svg():
    for source_name in ARCHITECTURE_VIEWS.values():
        source = (ROOT / "docs/architecture" / source_name).read_text(
            encoding="utf-8"
        )
        svg = ET.fromstring(render_topology(source, source_name=source_name))
        assert svg.attrib["data-topology-source"] == source_name


def test_architecture_page_script_is_external_for_strict_csp():
    content = (ROOT / "web/templates/architecture.html").read_text(encoding="utf-8")
    assert '/static/js/pages/architecture-view.js' in content
    assert "<script>" not in content
    script = (ROOT / "web/static/js/pages/architecture-view.js").read_text(
        encoding="utf-8"
    )
    for token in (
        "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
        "embedded-focus-active-tab", "embedded-sidebar-arrow",
        "aria-selected", "sessionStorage",
    ):
        assert token in script


def test_architecture_page_uses_full_width_without_inline_max_width():
    content = (ROOT / "web/templates/architecture.html").read_text(encoding="utf-8")
    assert "main { margin: 0 auto; }" in content
    assert "main { max-width" not in content


def test_architecture_registered_as_embedded_tab_focus_frame():
    switching = (ROOT / "web/static/js/shell/shell-page-switching.js").read_text(
        encoding="utf-8"
    )
    assert "architecture: 'architecture-iframe'" in switching


def test_architecture_node_labels_stay_inside_node_boxes():
    for source_name in ARCHITECTURE_VIEWS.values():
        source = (ROOT / "docs/architecture" / source_name).read_text(
            encoding="utf-8"
        )
        svg = ET.fromstring(render_topology(source, source_name=source_name))
        ns = "{http://www.w3.org/2000/svg}"
        for group in svg.findall(f"{ns}g"):
            if "data-node" not in group.attrib:
                continue
            text = group.find(f"{ns}text")
            label = text.text or ""
            estimated = _estimate_text_width(label)
            if estimated > NODE_TEXT_WIDTH:
                assert text.attrib.get("textLength") == str(NODE_TEXT_WIDTH), (
                    f"{source_name}:{label} overflows without textLength"
                )


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
    # 单向边与实线回报通道用蓝色箭头；虚线命令通道用紫色专属箭头。
    expected_markers = {
        (start, end): ("url(#topology-arrow-cmd)" if style == "-.->" else "url(#topology-arrow)")
        for start, end, _, style in edges
    }
    assert {
        (group.attrib["data-source"], group.attrib["data-target"]): group.find(
            "{http://www.w3.org/2000/svg}path"
        ).attrib["marker-end"]
        for group in groups
        if "data-source" in group.attrib
    } == expected_markers
