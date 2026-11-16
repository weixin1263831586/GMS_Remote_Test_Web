"""Keep public architecture views aligned with the canonical topology."""

import re
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path

from features.system.topology_view import (
    EDGE_FONT_SIZE,
    NODE_FONT_SIZE,
    NODE_PAD_X,
    _estimate_text_width,
    parse_topology,
    render_topology,
)


ROOT = Path(__file__).resolve().parents[2]
CANONICAL = (ROOT / 'docs/architecture/platform-topology.mmd').read_text(encoding='utf-8').strip()


def _first_mermaid(path: str) -> str:
    content = (ROOT / path).read_text(encoding='utf-8')
    match = re.search(r'```mermaid\n(.*?)\n```', content, re.DOTALL)
    assert match, f'missing Mermaid topology in {path}'
    return match.group(1).strip()


def test_markdown_topologies_match_canonical_definition():
    assert _first_mermaid('README.md') == CANONICAL
    assert _first_mermaid('docs/architecture/overview.md') == CANONICAL


def test_web_architecture_covers_canonical_roles_and_flows():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    for label in (
        'FastAPI Controller',
        'Agent Runtime',
        'Linux Worker A',
        'Linux Worker B',
        'Windows USB Source',
        'Linux USB Source',
        'Heartbeat / Poll / ACK',
        '命令随 Poll 响应返回',
        'USB/IP 接入示例',
        '生产入口为 HTTPS',
    ):
        assert label in content
    assert 'http://&lt;test_host&gt;:5001' not in content
    assert '{{PLATFORM_TOPOLOGY}}' in content


ARCHITECTURE_VIEWS = {
    '{{PLATFORM_TOPOLOGY}}': 'platform-topology.mmd',
    '{{USBIP_TOPOLOGY}}': 'usbip-topology.mmd',
    '{{AOA_TOPOLOGY}}': 'aoa-topology.mmd',
    '{{FIRMWARE_TOPOLOGY}}': 'firmware-topology.mmd',
    '{{TEST_FLOW_TOPOLOGY}}': 'test-flow-topology.mmd',
    '{{AGENT_ANALYSIS_TOPOLOGY}}': 'agent-analysis-topology.mmd',
}


def test_architecture_views_cover_module_topologies():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    for placeholder, source_name in ARCHITECTURE_VIEWS.items():
        assert placeholder in content, f'missing placeholder {placeholder}'
        source = (ROOT / 'docs/architecture' / source_name).read_text(encoding='utf-8')
        assert parse_topology(source), f'{source_name} must define nodes and edges'



def test_business_flows_show_real_ownership_and_monitoring_boundaries():
    sources = ROOT / 'docs' / 'architecture'
    usb_nodes, usb_edges = parse_topology((sources / 'usbip-topology.mmd').read_text(encoding='utf-8'))
    assert '虚拟 USB' in usb_nodes['DevWorker']
    assert any(start == 'DevWorker' and end == 'Tools' for start, end, *_ in usb_edges)
    assert not any(start == 'Tools' and end == 'DevWorker' for start, end, *_ in usb_edges)

    firm_nodes, firm_edges = parse_topology((sources / 'firmware-topology.mmd').read_text(encoding='utf-8'))
    assert 'Windows' in firm_nodes['SourceHost']
    assert any(start == 'SourceHost' and end == 'SourceDev' for start, end, *_ in firm_edges)
    assert not any(start == 'Worker' and end == 'SourceDev' for start, end, *_ in firm_edges)

    assert ('Ctrl', 'WinSrc', 'SSH / bind', '-.->') in usb_edges
    assert ('Ctrl', 'LinuxSrc', 'SSH / bind', '-.->') in usb_edges
    assert ('Ctrl', 'Worker', 'Poll / attach', '-.->') in usb_edges
    _, test_edges = parse_topology((sources / 'test-flow-topology.mmd').read_text(encoding='utf-8'))
    assert ('Worker', 'Job', '事件 / ACK', '-->') in test_edges
    _, agent_edges = parse_topology((sources / 'agent-analysis-topology.mmd').read_text(encoding='utf-8'))
    assert ('References', 'Collect', '引用正文', '-->') in agent_edges
    assert ('Collect', 'Review', '预检证据包', '-->') in agent_edges
    assert ('Planner', 'Monitor', 'run_id 查询', '-.->') in agent_edges

    page = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    for name in ('platform', 'usbip', 'aoa', 'firmware', 'test-flow', 'agent-analysis'):
        assert f'id="arch-tab-{name}"' in page
        assert f'data-arch-pane="{name}"' in page
    assert 'run_id' in page and 'Linux USB Source' in page


def test_architecture_views_render_as_local_svg():
    for source_name in ARCHITECTURE_VIEWS.values():
        source = (ROOT / 'docs/architecture' / source_name).read_text(encoding='utf-8')
        svg = ET.fromstring(render_topology(source, source_name=source_name))
        assert svg.attrib['data-topology-source'] == source_name


def test_architecture_page_script_is_external_for_strict_csp():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    assert '/static/js/pages/architecture-view.js' in content
    assert '<script>' not in content
    script = (ROOT / 'web/static/js/pages/architecture-view.js').read_text(encoding='utf-8')
    for token in (
        'ArrowLeft',
        'ArrowRight',
        'ArrowUp',
        'ArrowDown',
        'embedded-focus-active-tab',
        'embedded-sidebar-arrow',
        'aria-selected',
        'sessionStorage',
    ):
        assert token in script


def test_architecture_page_uses_shared_styles_and_full_width():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    # Shared styles must preserve the full-width page and strict script policy.
    css = (ROOT / 'web/static/css/architecture-layers.css').read_text(encoding='utf-8')
    assert 'main { margin: 0 auto;' in css
    assert 'main { max-width' not in css
    assert '<style>' not in content


def test_architecture_registered_as_embedded_tab_focus_frame():
    switching = (ROOT / 'web/static/js/shell/shell-page-switching.js').read_text(encoding='utf-8')
    assert "architecture: 'architecture-iframe'" in switching


def _svg_text(element: ET.Element | None) -> str:
    if element is None:
        return ''
    return ''.join(element.itertext())


def _box(element: ET.Element) -> tuple[float, float, float, float]:
    x, y = float(element.attrib['x']), float(element.attrib['y'])
    return (x, y, x + float(element.attrib['width']), y + float(element.attrib['height']))


def _overlaps(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    gap: float = 0,
) -> bool:
    return not (a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1])


def _points(group: ET.Element) -> list[tuple[float, float]]:
    path = group.find('{http://www.w3.org/2000/svg}path').attrib['d']
    values = [float(item) for item in re.findall(r'-?\d+(?:\.\d+)?', path)]
    return list(zip(values[::2], values[1::2]))


def _hits_box(a, b, box):
    (x1, y1), (x2, y2) = a, b
    left, top, right, bottom = box
    if x1 == x2:
        return left < x1 < right and min(y1, y2) < bottom and max(y1, y2) > top
    return top < y1 < bottom and min(x1, x2) < right and max(x1, x2) > left


def test_diagram_template_keeps_text_and_routes_clear():
    ns = '{http://www.w3.org/2000/svg}'
    dimensions = set()
    for source_name in ARCHITECTURE_VIEWS.values():
        source = (ROOT / 'docs/architecture' / source_name).read_text(encoding='utf-8')
        svg = ET.fromstring(render_topology(source, source_name=source_name))
        _, _, width, height = map(float, svg.attrib['viewBox'].split())
        assert width == 1240 and height <= 640, source_name
        groups = svg.findall(f'{ns}g')
        nodes = {g.attrib['data-node']: _box(g.find(f'{ns}rect')) for g in groups if 'data-node' in g.attrib}
        edges = [g for g in groups if 'data-source' in g.attrib]
        captions = []
        for group in groups:
            text = group.find(f'{ns}text')
            if text is None:
                continue
            is_node = 'data-node' in group.attrib
            size = NODE_FONT_SIZE if is_node else EDGE_FONT_SIZE
            assert text.attrib['font-size'] == str(size)
            assert 'textLength' not in text.attrib and 'transform' not in text.attrib
            rect = _box(group.find(f'{ns}rect'))
            assert 0 <= rect[0] < rect[2] <= width and 0 <= rect[1] < rect[3] <= height
            for line in text.findall(f'{ns}tspan'):
                estimated = _estimate_text_width(_svg_text(line), size)
                assert estimated <= rect[2] - rect[0] - (NODE_PAD_X * 2 if is_node else 16), (
                    f'{source_name}: text overflows: {_svg_text(line)}'
                )
            if is_node:
                dimensions.add((rect[2] - rect[0], rect[3] - rect[1]))
            else:
                captions.append((group, rect))
                points = _points(group)
                assert group.find(f'{ns}rect').attrib['fill'] == 'none'
                nearest = min(
                    max(rect[0] - max(a[0], b[0]), min(a[0], b[0]) - rect[2], 0)
                    + max(rect[1] - max(a[1], b[1]), min(a[1], b[1]) - rect[3], 0)
                    for a, b in pairwise(points)
                )
                assert nearest == 8, f'{source_name}: caption should sit beside its arrow'
                for node in nodes.values():
                    assert not _overlaps(rect, node), f'{source_name}: caption covers a node: {_svg_text(text)}'
        boxes = list(nodes.values()) + [rect for _, rect in captions]
        for i, rect in enumerate(boxes):
            for other in boxes[i + 1:]:
                assert not _overlaps(rect, other), f'{source_name}: diagram boxes overlap'
        for edge in edges:
            points = _points(edge)
            for a, b in pairwise(points):
                assert a[0] == b[0] or a[1] == b[1], f'{source_name}: diagonal arrow'
                for node in nodes.values():
                    assert not _hits_box(a, b, node), f'{source_name}: arrow crosses a node: {edge.attrib}'
                for _group, rect in captions:
                    assert not _hits_box(a, b, rect), f'{source_name}: arrow crosses a caption: {edge.attrib}'
    assert dimensions == {(208, 72)}


def test_web_topology_preserves_every_node_and_directed_edge():
    for source_name in ARCHITECTURE_VIEWS.values():
        _assert_graph_preserved(source_name)


def _assert_graph_preserved(source_name):
    source = (ROOT / 'docs/architecture' / source_name).read_text(encoding='utf-8')
    nodes, edges = parse_topology(source)
    svg = ET.fromstring(render_topology(source, source_name))
    stem = source_name.removesuffix('-topology.mmd')
    marker = 'topology-arrow' if stem == 'platform' else f'topology-{stem}-arrow'
    groups = svg.findall('{http://www.w3.org/2000/svg}g')
    rendered_nodes = {
        group.attrib['data-node']: _svg_text(group.find('{http://www.w3.org/2000/svg}text'))
        for group in groups
        if 'data-node' in group.attrib
    }
    rendered_edges = [
        (
            group.attrib['data-source'],
            group.attrib['data-target'],
            _svg_text(group.find('{http://www.w3.org/2000/svg}text')),
            group.attrib['data-edge-style'],
        )
        for group in groups
        if 'data-source' in group.attrib
    ]
    assert rendered_nodes == nodes
    assert rendered_edges == edges
    # 单向边与实线回报通道用蓝色箭头；虚线命令通道用紫色专属箭头。
    expected_markers = {
        (start, end): (f'url(#{marker}-cmd)' if style == '-.->' else f'url(#{marker})')
        for start, end, _, style in edges
    }
    assert {
        (group.attrib['data-source'], group.attrib['data-target']): group.find(
            '{http://www.w3.org/2000/svg}path'
        ).attrib['marker-end']
        for group in groups
        if 'data-source' in group.attrib
    } == expected_markers
