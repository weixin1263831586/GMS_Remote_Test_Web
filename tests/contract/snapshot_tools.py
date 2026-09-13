from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(__file__).with_name('snapshots')

# 页面 handler 名单：CSP 收紧后 inline onXXX 已全部迁移为 act-bridge
# 的 data-* 声明（见 web/static/js/shell/act-bridge.js），名单改从
# data-click/data-change/... 抽取，继续用于冻结契约与完整性校验。
INLINE_HANDLER_RE = re.compile(
    r'data-(?:click|change|input|submit|keydown|keyup|keypress|dblclick|'
    r'dragstart|dragend|dragover|dragenter|drop|toggle|blur|focus|error|load|'
    r'mouseover|mouseout)=["\']'
    r'([^"\']+)["\']'
)
ID_RE = re.compile(r'\bid=["\']([^"\']+)["\']')
SCRIPT_SRC_RE = re.compile(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)


def write_json(name: str, value: Any) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    (SNAPSHOTS / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def read_json(name: str) -> Any:
    return json.loads((SNAPSHOTS / name).read_text(encoding='utf-8'))


def flatten_app_routes(app) -> list[Any]:
    """Deprecated alias; canonical implementation lives in
    ``foundation/routing_introspection.py`` (importable by features and
    product code, not just tests)."""
    from foundation.routing_introspection import flatten_app_routes as _flatten

    return _flatten(app)


def read_shell_bundle() -> str:
    """shell.html 模板 + 模板实际引用的本地 shell 脚本。

    CSP 前置迁移后 shell 主脚本外置到 ``web/static/js/shell/*.js``：
    "模板里应包含某标记"类断言可能命中模板或外置脚本，统一用本函数
    读取，避免每个测试各自内联拼接样板。只读取模板真实引用的脚本，
    防止孤儿文件让 wiring 测试产生假阳性。
    """
    html = (ROOT / 'web/shell/shell.html').read_text(encoding='utf-8')
    parts = [html]
    for source in SCRIPT_SRC_RE.findall(html):
        source_path = source.split('?', 1)[0]
        if not source_path.startswith('/static/js/shell/'):
            continue
        path = ROOT / 'web/static' / source_path.removeprefix('/static/')
        if path.is_file():
            parts.append(path.read_text(encoding='utf-8'))
    return '\n'.join(parts)


def read_page_bundle(html_relative: str) -> str:
    """嵌入式页面 page.html + 同目录 page.js 的组合文本。

    页面脚本外置后，"HTML 里的 API/标记引用"部分落在 page.js；
    需要跨两者断言时用本函数。
    """
    html = ROOT / html_relative
    html_text = html.read_text(encoding='utf-8')
    parts = [html_text]
    js = html.with_suffix('.js')
    references_page_js = any(
        source.split('?', 1)[0].endswith('/page.js')
        for source in SCRIPT_SRC_RE.findall(html_text)
    )
    if references_page_js and js.is_file():
        parts.append(js.read_text(encoding='utf-8'))
    return '\n'.join(parts)

def normalized_routes(app) -> list[dict[str, Any]]:
    result = []
    for route in flatten_app_routes(app):
        methods = sorted(
            method
            for method in (getattr(route, 'methods', None) or [])
            if method != 'HEAD'
        )
        if not methods and route.__class__.__name__ != 'APIWebSocketRoute':
            continue
        result.append(
            {
                'path': route.path,
                'methods': methods or ['WEBSOCKET'],
            }
        )
    return sorted(result, key=lambda item: (item['path'], item['methods']))


def _remove_internal_openapi_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_internal_openapi_fields(item)
            for key, item in sorted(value.items())
            if key != 'operationId'
        }
    if isinstance(value, list):
        return [_remove_internal_openapi_fields(item) for item in value]
    return value


def normalized_openapi(app) -> dict[str, Any]:
    schema = dict(app.openapi())
    schema.pop('servers', None)
    return _remove_internal_openapi_fields(schema)


def config_shape(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        if path in {('client_hosts',), ('firmware_shares', 'hosts')}:
            sample = next(iter(value.values()), '')
            return {'<dynamic-key>': config_shape(sample, (*path, '<dynamic-key>'))}
        return {
            key: config_shape(item, (*path, key))
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [config_shape(value[0], (*path, '[]'))] if value else []
    return type(value).__name__


def ui_source_groups() -> dict[str, list[Path]]:
    automation_ui = ROOT / 'features/automation/ui'
    devices_console_ui = ROOT / 'features/devices/ui'
    redmine_ui = ROOT / 'features/redmine/ui'
    return {
        'shell': [
            ROOT / 'web/shell/shell.html',
            # 2026-08 shell 拆分后，页面内联 HTML/JS 也来自 web/static/js/shell/。
            *sorted((ROOT / 'web/static/js').glob('*.js')),
            *sorted((ROOT / 'web/static/js/shell').glob('*.js')),
        ],
        'redmine-agent': sorted(redmine_ui.glob('*.*')),
        'gerrit-dashboard': [
            ROOT / 'features/gerrit/ui/page.html',
            ROOT / 'features/gerrit/ui/page.js',
        ],
        'gms-update-monitor': [
            ROOT / 'features/system/update_monitor/ui/page.html',
            ROOT / 'features/system/update_monitor/ui/page.js',
        ],
        'mainline-known-issues': [
            ROOT / 'features/system/mainline_issues/ui/page.html',
            ROOT / 'features/system/mainline_issues/ui/page.js',
        ],
        'automation': (
            sorted(automation_ui.glob('*.*'))
            if automation_ui.exists()
            else []
        ),
        'devices-console': (
            sorted(devices_console_ui.glob('*.*'))
            if devices_console_ui.exists()
            else []
        ),
    }


def ui_controls(sources: dict[str, list[Path]]) -> dict[str, Any]:
    result = {}
    for page, paths in sorted(sources.items()):
        text = '\n'.join(
            path.read_text(encoding='utf-8', errors='ignore') for path in paths
        )
        result[page] = {
            'ids': sorted(set(ID_RE.findall(text))),
            'handlers': sorted(set(INLINE_HANDLER_RE.findall(text))),
        }
    return result
