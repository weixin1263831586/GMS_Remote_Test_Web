from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOTS = Path(__file__).with_name('snapshots')

INLINE_HANDLER_RE = re.compile(
    r'on(?:click|change|input|submit|keydown)=["\']([^"\']+)["\']'
)
ID_RE = re.compile(r'\bid=["\']([^"\']+)["\']')


def write_json(name: str, value: Any) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    (SNAPSHOTS / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )


def read_json(name: str) -> Any:
    return json.loads((SNAPSHOTS / name).read_text(encoding='utf-8'))


def normalized_routes(app) -> list[dict[str, Any]]:
    result = []
    for route in app.routes:
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


def config_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: config_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [config_shape(value[0])] if value else []
    return type(value).__name__


def ui_source_groups() -> dict[str, list[Path]]:
    new_shell = ROOT / 'web/shell/shell.html'
    if new_shell.exists():
        return {
            'shell': [
                new_shell,
                *sorted((ROOT / 'web/static/js').glob('*.js')),
            ],
            'redmine-agent': sorted((ROOT / 'features/redmine/ui').glob('*.*')),
            'gerrit-dashboard': sorted((ROOT / 'features/gerrit/ui').glob('*.*')),
            'gms-update-monitor': sorted(
                (ROOT / 'features/system/update_monitor/ui').glob('*.*')
            ),
            'mainline-known-issues': sorted(
                (ROOT / 'features/system/mainline_issues/ui').glob('*.*')
            ),
            'automation': sorted((ROOT / 'features/automation/ui').glob('*.*')),
        }
    return {
        'shell': [
            ROOT / 'templates/index_fastapi.html',
            *sorted((ROOT / 'static/js').glob('*.js')),
        ],
        'redmine-agent': [ROOT / 'routers/redmine_agent.py'],
        'gerrit-dashboard': [ROOT / 'routers/gerrit_dashboard.py'],
        'gms-update-monitor': [ROOT / 'routers/gms_update_monitor.py'],
        'mainline-known-issues': [ROOT / 'routers/mainline_known_issues.py'],
        'automation': [ROOT / 'routers/automation.py'],
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
