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
    page = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    assert 'architecture-scene-data.js' in page
    assert 'architecture-scenes.js' in page
    for term in ('Controller', 'Linux Worker A', 'Windows Source', 'Linux Source',
                 'USB/IP', 'Redmine', 'Gerrit', 'Claim / Lease', 'AOAv2',
                 'Tradefed', 'run_id'):
        assert term in data, term
    assert '<details' not in page
    assert '{{' not in page
    assert 'http://&lt;test_host&gt;:5001' not in page

ARCHITECTURE_VIEWS = {
    'platform': 'platform-topology.mmd',
    'usbip': 'usbip-topology.mmd',
    'aoa': 'aoa-topology.mmd',
    'firmware': 'firmware-topology.mmd',
    'test-flow': 'test-flow-topology.mmd',
    'agent-analysis': 'agent-analysis-topology.mmd',
}


def test_architecture_views_cover_module_topologies():
    content = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    for view_id, source_name in ARCHITECTURE_VIEWS.items():
        assert f'data-arch-pane="{view_id}"' in content
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
    assert ('Ctrl', 'Worker', 'Poll 响应 / attach', '-.->') in usb_edges
    assert ('Worker', 'Ctrl', 'Poll / ACK', '-->') in usb_edges
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
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    assert 'run_id' in data and 'Linux Source' in data


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

def test_six_views_use_one_visual_scene_without_duplicate_expanders():
    page = (ROOT / 'web/templates/architecture.html').read_text(encoding='utf-8')
    renderer = (ROOT / 'web/static/js/pages/architecture-scenes.js').read_text(encoding='utf-8')
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    css = (ROOT / 'web/static/css/architecture-scenes.css').read_text(encoding='utf-8')
    assert '<body class="architecture-v2">' in page
    assert 'workspace-tabs.css' in page
    assert 'architecture-scenes.css' in page
    assert 'architecture-scene-data.js' in page
    assert 'architecture-scenes.js' in page
    assert page.count('data-arch-pane=') == 6
    assert page.count('arch-v2-flow-bottom') == 5
    assert '<details' not in page and '{{' not in page
    for view in ARCHITECTURE_VIEWS:
        assert (f"'{view}':" in data or f'{view}:' in data), view
    assert '<p class="lead">' not in page
    assert 'arch-v2-heading' not in page
    assert 'arch-v2-intro' not in page
    assert '逻辑架构示意 · 不代表实时健康状态' not in page
    assert 'function fitScenes()' in renderer
    assert "canvas.style.zoom" in renderer
    assert 'arch-scene-controller-emblem' in renderer
    assert 'const blocked=graph.nodes.some' in renderer
    assert '.workspace-tab{' not in css and '.workspace-tabs{' not in css


def test_scene_data_respects_poll_and_worker_source_boundaries():
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    for token in ('Poll / 命令', 'Poll / attach', '二选一接入',
                  '本地 Worker / Windows Source', 'Redmine 后台执行',
                  '设备执行 Worker', '按需登记 / 分析'):
        assert token in data, token
    assert "'M698 314 H552','report','Poll / ACK'" in data


def test_scene_labels_do_not_overlap_one_another_and_marker_starts_reverse():
    js = (ROOT / 'web/static/js/pages/architecture-scenes.js').read_text(encoding='utf-8')
    css = (ROOT / 'web/static/css/architecture-scenes.css').read_text(encoding='utf-8')
    assert 'labelBounds.some' in js
    assert 'auto-start-reverse' in js
    assert 'arch-scene-chip .arch-scene-icon' in css
    assert 'data-scene-view="platform"' in css

def test_agent_runtime_is_not_embedded_in_controller_scene():
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    for token in ('Controller / 工具网关', 'kkagent / CLI', 'Service Token',
                  '权限与审计', 'Redmine 后台执行', '设备执行 Worker'):
        assert token in data, token

def test_architecture_routes_have_fixed_size_arrowheads_and_soft_elbows():
    js = (ROOT / 'web/static/js/pages/architecture-scenes.js').read_text(encoding='utf-8')
    css = (ROOT / 'web/static/css/architecture-scenes.css').read_text(encoding='utf-8')
    assert 'markerUnits="userSpaceOnUse"' in js
    assert 'function roundedRoute(value)' in js
    assert "d+=' L'+fmt(x1)" in js
    assert 'arch-scene-path-halo--command' not in css
    assert 'arch-scene-path-halo--report' not in css
    assert 'arch-scene-ground' not in css
    assert 'arch-scene-path-halo' not in js
    assert 'arch-scene-ground' not in js
    assert 'stroke-width:1.9;filter:none' in css


def test_scene_edges_leave_minimum_arrow_clearance():
    """Compact nodes must still leave readable links between ports.

    The authored M/H/V source paths are axis-aligned and use scene-space
    coordinates. The renderer adds 5px endpoint insets and smooth elbows.
    """
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    routes = re.findall(r"\['(M\d+ \d+(?: [HV]\d+)*)','(?:data|command|report|both)'", data)
    assert len(routes) >= 55, 'expected all six scenes, not a partial extraction'
    for route in routes:
        values = re.findall(r'[MHV]|\d+', route)
        assert values[0] == 'M'
        x, y = int(values[1]), int(values[2])
        total = 0
        for op, value in zip(values[3::2], values[4::2]):
            target = int(value)
            if op == 'H':
                total += abs(target - x)
                x = target
            else:
                assert op == 'V', f'unexpected diagonal route: {route}'
                total += abs(target - y)
                y = target
        assert total >= 55, f'arrow too short ({total}px): {route}'


def test_report_receives_centered_direct_arrow_from_worker_results():
    data = (ROOT / 'web/static/js/pages/architecture-scene-data.js').read_text(encoding='utf-8')
    assert "['logs','Worker 原生结果','results / logs','chart','channel',839,340,237,165" in data
    assert "['report','报告浏览 / 登记','按需汇总与分析','book','control',839,564,237,104" in data
    assert "['M958 505 V564','report','结果索引'" in data
