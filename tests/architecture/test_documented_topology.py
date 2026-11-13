"""Keep public architecture views aligned with the canonical topology."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from features.system.topology_view import (
    EDGE_FONT_SIZE,
    NODE_FONT_SIZE,
    NODE_PAD_X,
    _estimate_text_width,
    _reading_angle,
    _rotated_hits_rect,
    _rotated_hits_rotated,
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


def test_architecture_page_uses_full_width_without_inline_max_width():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    # Tab 行模板化后 main 仍须全宽（只允许 margin auto + 内边距，禁止 max-width）。
    assert 'main { margin: 0 auto;' in content
    assert 'main { max-width' not in content


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


def test_architecture_node_labels_stay_inside_node_boxes():
    ns = '{http://www.w3.org/2000/svg}'
    for source_name in ARCHITECTURE_VIEWS.values():
        source = (ROOT / 'docs/architecture' / source_name).read_text(encoding='utf-8')
        svg = ET.fromstring(render_topology(source, source_name=source_name))
        node_rects: list[tuple[float, float, float, float]] = []
        pills: list[tuple[float, float, float, float, float, float]] = []
        edge_dirs: dict[tuple[str, str], tuple[float, float]] = {}
        for group in svg.findall(f'{ns}g'):
            if 'data-node' not in group.attrib:
                continue
            rect = _box(group.find(f'{ns}rect'))
            node_rects.append(rect)
            text = group.find(f'{ns}text')
            assert text.attrib['font-size'] == str(NODE_FONT_SIZE)
            assert 'textLength' not in text.attrib
            assert 'rotate(' not in text.attrib.get('transform', '')
            for line in text.findall(f'{ns}tspan') or [text]:
                estimated = _estimate_text_width(_svg_text(line), NODE_FONT_SIZE)
                assert estimated <= (rect[2] - rect[0]) - NODE_PAD_X * 2, (
                    f'{source_name}:{_svg_text(line)} overflows its box'
                )
        for index, rect in enumerate(node_rects):
            for other in node_rects[index + 1 :]:
                assert not _overlaps(rect, other), f'{source_name} node boxes overlap'
        for group in svg.findall(f'{ns}g'):
            if 'data-source' not in group.attrib:
                continue
            text = group.find(f'{ns}text')
            label = _svg_text(text)
            if not label:
                continue
            assert text.attrib['font-size'] == str(EDGE_FONT_SIZE)
            pill = group.find(f'{ns}rect')
            transform = text.attrib.get('transform', '')
            source_name_edge = (group.attrib['data-source'], group.attrib['data-target'])
            path = group.find(f'{ns}path').attrib['d']
            numbers = [float(item) for item in re.findall(r'-?\d+(?:\.\d+)?', path)]
            dx, dy = numbers[-2] - numbers[0], numbers[-1] - numbers[1]
            edge_dirs[source_name_edge] = (dx, dy)
            paired = (source_name_edge[1], source_name_edge[0]) in {
                (group.attrib['data-source'], group.attrib['data-target'])
                for group in svg.findall(f'{ns}g')
                if 'data-source' in group.attrib
            }
            if paired:
                assert 'rotate(' in transform, f'{source_name}:{label} should run along its arrow'
                assert transform == pill.attrib.get('transform', '')
                reading = _reading_angle(dx, dy)
                assert f'rotate({reading:.1f} ' in transform
            else:
                assert 'rotate(' not in transform
            box = _box(pill)
            cx = (box[0] + box[2]) / 2
            cy = (box[1] + box[3]) / 2
            angle = _reading_angle(dx, dy) if paired else 0.0
            pills.append((cx, cy, box[2] - box[0], box[3] - box[1], angle, label))
            for rect in node_rects:
                assert not _rotated_hits_rect(cx, cy, box[2] - box[0], box[3] - box[1], angle, rect, pad=0), (
                    f'{source_name}:{label} covers a node'
                )
        for index, pill in enumerate(pills):
            for other in pills[index + 1 :]:
                assert not _rotated_hits_rotated(*pill[:5], *other[:5], pad=0), (
                    f'{source_name} arrow labels overlap: {pill[5]} / {other[5]}'
                )


def test_web_topology_preserves_every_node_and_directed_edge():
    nodes, edges = parse_topology(CANONICAL)
    svg = ET.fromstring(render_topology(CANONICAL))
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
        (start, end): ('url(#topology-arrow-cmd)' if style == '-.->' else 'url(#topology-arrow)')
        for start, end, _, style in edges
    }
    assert {
        (group.attrib['data-source'], group.attrib['data-target']): group.find(
            '{http://www.w3.org/2000/svg}path'
        ).attrib['marker-end']
        for group in groups
        if 'data-source' in group.attrib
    } == expected_markers
