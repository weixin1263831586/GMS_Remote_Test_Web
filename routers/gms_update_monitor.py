"""GMS/CTS update monitor APIs."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

from core.settings import PROJECT_ROOT
from modules.gms_update_monitor import SOURCES


router = APIRouter()
DB_PATH = Path(PROJECT_ROOT) / 'data' / 'gms_update_monitor.sqlite3'
SYNC_SCRIPT = Path(PROJECT_ROOT) / 'modules' / 'gms_update_monitor.py'

_sync_lock = threading.Lock()
_sync_status = {
    'running': False,
    'mode': None,
    'source': [],
    'started_at': None,
    'finished_at': None,
    'returncode': None,
    'stdout': '',
    'stderr': '',
    'error': None,
}


def _connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _get_db():
    """Return (conn, None) or (None, error_response) if DB is missing."""
    if not DB_PATH.exists():
        return None, JSONResponse(
            status_code=404,
            content={
                'success': False,
                'error': f'Database not found: {DB_PATH}',
                'hint': 'Run: python3 modules/gms_update_monitor.py --mode full --verbose',
            },
        )
    return _connect_db(), None


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _like_param(value: str) -> str:
    safe = value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{safe}%'


def _artifact_release_number(item: dict) -> str:
    for value in (item.get('release_name', ''), item.get('android_version', ''), item.get('file_name', '')):
        match = re.search(r'(?:CTS|VTS|GTS)?[-_ ]?(1[4-7](?:\.\d+)?)', value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r'Android\s+(1[4-7])(?:\s+QPR2)?', value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ''


def _artifact_api_level(item: dict, release: str = '') -> str:
    target_platform = item.get('target_platform', '')
    if item.get('suite_type') == 'GTS' and target_platform:
        range_match = re.search(r'Android\s+(1[3-7])\s*-\s*(1[3-7])', target_platform, flags=re.IGNORECASE)
        versions = list(range_match.groups()) if range_match else re.findall(r'Android\s+(1[3-7])', target_platform, flags=re.IGNORECASE)
        api_map_full = {'13': '33', '14': '34', '15': '35', '16': '36', '17': '37'}
        apis = [api_map_full[v] for v in versions if v in api_map_full]
        if len(apis) >= 2:
            return f'{apis[0]}-{apis[-1]}'
        if len(apis) == 1:
            return apis[0]
    if not release:
        release = _artifact_release_number(item)
    if not release:
        return ''
    major_text, _, minor = release.partition('.')
    api_map = {'14': '34', '15': '35', '16': '36', '17': '37'}
    api = api_map.get(major_text)
    if not api:
        return ''
    return f'{api}.{minor}' if minor else api


def _artifact_section_url(item: dict, release: str = '') -> str:
    source_key = item.get('source_key', '')
    if not release:
        release = _artifact_release_number(item)
    if source_key == 'cts_downloads' and release:
        anchor = 'android-' + release.replace('.', '-')
        return f'https://source.android.com/docs/compatibility/cts/downloads#{anchor}'
    if source_key == 'vts_downloads' and release:
        anchor = 'android-' + release.replace('.', '-')
        return f'https://docs.partner.android.com/gms/testing/vts#{anchor}'
    if source_key == 'gts_downloads':
        return 'https://docs.partner.android.com/gms/testing/gts#download-gts'
    return ''


def _enrich_artifact_item(item: dict) -> dict:
    release = _artifact_release_number(item)
    item['api_level'] = _artifact_api_level(item, release)
    item['section_url'] = _artifact_section_url(item, release)
    return item


def _run_sync_job(mode: str, source: list[str]):
    command = [sys.executable, str(SYNC_SCRIPT), '--mode', mode, '--verbose']
    for item in source:
        command.extend(['--source', item])
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=3600,
            check=False,
        )
        with _sync_lock:
            _sync_status.update(
                {
                    'running': False,
                    'finished_at': datetime.now().isoformat(),
                    'returncode': result.returncode,
                    'stdout': result.stdout[-6000:],
                    'stderr': result.stderr[-6000:],
                    'error': None if result.returncode == 0 else f'sync exited with {result.returncode}',
                }
            )
    except Exception as exc:
        with _sync_lock:
            _sync_status.update(
                {
                    'running': False,
                    'finished_at': datetime.now().isoformat(),
                    'returncode': None,
                    'error': str(exc),
                }
            )


@router.get('/api/v1/gms-update-monitor/sources')
async def gms_update_monitor_sources():
    configured = [
        {
            'source_key': source.key,
            'name': source.name,
            'url': source.url,
            'category': source.category,
            'parser': source.parser,
            'auth_required': source.auth_required,
        }
        for source in SOURCES
    ]
    scanned = []
    if DB_PATH.exists():
        with conn:
            scanned = _rows_to_dicts(
                conn.execute(
                    """
                    SELECT source_key, name, url, final_url, category, parser, auth_required,
                           content_hash, status_code, title, first_seen_at, last_scanned_at, last_changed_at
                    FROM gms_update_sources
                    ORDER BY source_key
                    """
                ).fetchall()
            )
    return {'success': True, 'data': {'configured': configured, 'scanned': scanned}}


@router.post('/api/v1/gms-update-monitor/sync')
async def start_gms_update_monitor_sync(
    mode: str = Query('incremental', pattern='^(full|incremental)$'),
    source: list[str] = Query(default=[]),
):
    known_sources = {item.key for item in SOURCES}
    unknown = sorted(set(source) - known_sources)
    if unknown:
        return JSONResponse(status_code=400, content={'success': False, 'error': f'unknown source: {", ".join(unknown)}'})
    with _sync_lock:
        if _sync_status['running']:
            return JSONResponse(
                status_code=409,
                content={'success': False, 'error': 'sync already running', 'status': dict(_sync_status)},
            )
        _sync_status.update(
            {
                'running': True,
                'mode': mode,
                'source': source,
                'started_at': datetime.now().isoformat(),
                'finished_at': None,
                'returncode': None,
                'stdout': '',
                'stderr': '',
                'error': None,
            }
        )
        thread = threading.Thread(target=_run_sync_job, args=(mode, source), daemon=True)
        thread.start()
        status = dict(_sync_status)
    return {'success': True, 'data': {'status': status}}


@router.get('/api/v1/gms-update-monitor/sync/status')
async def gms_update_monitor_sync_status():
    with _sync_lock:
        status = dict(_sync_status)
    status['db_exists'] = DB_PATH.exists()
    return {'success': True, 'data': {'status': status}}


@router.get('/api/v1/gms-update-monitor/summary')
async def gms_update_monitor_summary():
    conn, missing = _get_db()
    if missing:
        return missing
    with conn:
        last_run = conn.execute(
            """
            SELECT *
            FROM gms_update_scan_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        source_counts = conn.execute(
            """
            SELECT s.source_key, s.name, s.category, s.last_scanned_at, s.last_changed_at,
                   COALESCE(a.count, 0) AS artifacts,
                   COALESCE(p.count, 0) AS packages,
                   COALESCE(r.count, 0) AS requirement_sections
            FROM gms_update_sources s
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_artifacts GROUP BY source_key
            ) a ON a.source_key = s.source_key
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_packages GROUP BY source_key
            ) p ON p.source_key = s.source_key
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_requirement_sections GROUP BY source_key
            ) r ON r.source_key = s.source_key
            ORDER BY s.source_key
            """
        ).fetchall()
        suite_counts = conn.execute(
            """
            SELECT suite_type, android_version, COUNT(*) AS count
            FROM gms_update_artifacts
            GROUP BY suite_type, android_version
            ORDER BY suite_type, android_version DESC
            """
        ).fetchall()
        recent_changes = conn.execute(
            """
            SELECT source_key, entity_type, change_type, COUNT(*) AS count
            FROM gms_update_change_events
            WHERE run_id = COALESCE((SELECT MAX(id) FROM gms_update_scan_runs), 0)
            GROUP BY source_key, entity_type, change_type
            ORDER BY source_key, entity_type, change_type
            """
        ).fetchall()
        requirement_version_summary = conn.execute(
            """
            SELECT android_version, change_kind, COUNT(*) AS count
            FROM gms_update_requirement_version_tags
            GROUP BY android_version, change_kind
            ORDER BY android_version, change_kind
            """
        ).fetchall()
    return {
        'success': True,
        'data': {
            'last_run': dict(last_run) if last_run else None,
            'sources': _rows_to_dicts(source_counts),
            'suite_counts': _rows_to_dicts(suite_counts),
            'recent_changes': _rows_to_dicts(recent_changes),
            'requirement_version_summary': _rows_to_dicts(requirement_version_summary),
            'db_path': str(DB_PATH),
        },
    }


@router.get('/api/v1/gms-update-monitor/scan-runs')
async def list_gms_update_monitor_scan_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    with conn:
        total = conn.execute('SELECT COUNT(*) FROM gms_update_scan_runs').fetchone()[0]
        rows = conn.execute(
            """
            SELECT *
            FROM gms_update_scan_runs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {'success': True, 'data': {'items': _rows_to_dicts(rows)}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/changes')
async def list_gms_update_monitor_changes(
    run_id: int | None = Query(None),
    source_key: str = Query(''),
    entity_type: str = Query(''),
    change_type: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str | int] = []
    if run_id is not None:
        where.append('run_id = ?')
        params.append(run_id)
    if source_key:
        where.append('source_key = ?')
        params.append(source_key)
    if entity_type:
        where.append('entity_type = ?')
        params.append(entity_type)
    if change_type:
        where.append('change_type = ?')
        params.append(change_type)
    if q:
        where.append('(entity_key LIKE ? OR before_json LIKE ? OR after_json LIKE ?)')
        like = _like_param(q)
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_change_events {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, run_id, source_key, entity_type, entity_key, change_type,
                   before_json, after_json, detected_at
            FROM gms_update_change_events
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ('before_json', 'after_json'):
            item[key.replace('_json', '')] = json.loads(item.pop(key) or 'null')
        items.append(item)
    return {'success': True, 'data': {'items': items}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/artifacts')
async def list_gms_update_monitor_artifacts(
    source_key: str = Query(''),
    suite_type: str = Query(''),
    android_version: str = Query(''),
    arch: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str] = []
    if source_key:
        where.append('source_key = ?')
        params.append(source_key)
    if suite_type:
        where.append('suite_type = ? COLLATE NOCASE')
        params.append(suite_type)
    if android_version:
        where.append('android_version = ? COLLATE NOCASE')
        params.append(android_version)
    if arch:
        where.append('arch = ? COLLATE NOCASE')
        params.append(arch)
    if q:
        like = _like_param(q)
        where.append(
            '(release_name LIKE ? OR file_name LIKE ? OR download_url LIKE ? OR '
            'description LIKE ? OR target_platform LIKE ? OR section_path LIKE ?)'
        )
        params.extend([like, like, like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_artifacts {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_artifacts
            {where_sql}
            ORDER BY suite_type, android_version DESC, release_name DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = [_enrich_artifact_item(item) for item in _rows_to_dicts(rows)]
    return {'success': True, 'data': {'items': items}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/artifacts/new')
async def list_new_gms_update_monitor_artifacts(
    run_id: int | None = Query(None),
    source_key: list[str] = Query(default=[]),
    limit: int = Query(20, ge=1, le=100),
):
    conn, missing = _get_db()
    if missing:
        return missing
    with conn:
        target_run_id = run_id
        if target_run_id is None:
            row = conn.execute('SELECT MAX(id) AS id FROM gms_update_scan_runs').fetchone()
            target_run_id = row['id'] if row else None
        if target_run_id is None:
            return {
                'success': True,
                'data': {
                    'run_id': None,
                    'items': [],
                    'download_api': {'method': 'POST', 'path': '/api/test/suites/download-url', 'body_template': {'url': '<download_url>'}},
                },
                'meta': {'total': 0, 'limit': limit},
            }
        where = ['run_id = ?', "entity_type = 'artifact'", "change_type = 'added'", "after_json != ''"]
        params: list[str | int] = [target_run_id]
        if source_key:
            placeholders = ','.join(['?'] * len(source_key))
            where.append(f'source_key IN ({placeholders})')
            params.extend(source_key)
        rows = conn.execute(
            f"""
            SELECT id, run_id, source_key, entity_key, after_json, detected_at
            FROM gms_update_change_events
            WHERE {' AND '.join(where)}
            ORDER BY id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    items = []
    for row in rows:
        artifact = json.loads(row['after_json'])
        if not artifact.get('download_url'):
            continue
        artifact['change_event_id'] = row['id']
        artifact['run_id'] = row['run_id']
        artifact['detected_at'] = row['detected_at']
        artifact['download_request'] = {'url': artifact['download_url']}
        _enrich_artifact_item(artifact)
        items.append(artifact)
    return {
        'success': True,
        'data': {
            'run_id': target_run_id,
            'items': items,
            'download_api': {'method': 'POST', 'path': '/api/test/suites/download-url', 'body_template': {'url': '<download_url>'}},
        },
        'meta': {'total': len(items), 'limit': limit},
    }


@router.get('/api/v1/gms-update-monitor/packages')
async def list_gms_update_monitor_packages(
    android_version: str = Query(''),
    section: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str] = []
    if android_version:
        where.append('android_version = ? COLLATE NOCASE')
        params.append(android_version)
    if section:
        where.append('section = ? COLLATE NOCASE')
        params.append(section)
    if q:
        like = _like_param(q)
        where.append('(file_name LIKE ? OR partner_gerrit_tag LIKE ? OR description LIKE ? OR download_url LIKE ?)')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_packages {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_packages
            {where_sql}
            ORDER BY section, android_version DESC, file_name DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {'success': True, 'data': {'items': _rows_to_dicts(rows)}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/requirements/sections')
async def list_gms_update_monitor_requirement_sections(
    level: int | None = Query(None, ge=2, le=3),
    top_only: bool = Query(False, description='Only return top-level numbered chapters such as 1, 2, 3'),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str | int] = []
    if level is not None:
        where.append('level = ?')
        params.append(level)
    if top_only:
        where.append("number != '' AND number NOT LIKE '%.%'")
    if q:
        like = _like_param(q)
        where.append('(number LIKE ? OR title LIKE ? OR path LIKE ? OR text_excerpt LIKE ?)')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_requirement_sections {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_requirement_sections
            {where_sql}
            ORDER BY number, path
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {'success': True, 'data': {'items': _rows_to_dicts(rows)}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/requirements/version-summary')
async def gms_update_monitor_requirement_version_summary():
    conn, missing = _get_db()
    if missing:
        return missing
    with conn:
        rows = conn.execute(
            """
            SELECT android_version, change_kind, COUNT(*) AS count
            FROM gms_update_requirement_version_tags
            GROUP BY android_version, change_kind
            ORDER BY android_version, change_kind
            """
        ).fetchall()
        by_section = conn.execute(
            """
            SELECT android_version, section_title, COUNT(*) AS count
            FROM gms_update_requirement_version_tags
            GROUP BY android_version, section_title
            ORDER BY android_version, count DESC, section_title
            """
        ).fetchall()
    return {'success': True, 'data': {'summary': _rows_to_dicts(rows), 'by_section': _rows_to_dicts(by_section)}}


@router.get('/api/v1/gms-update-monitor/requirements/version-tags')
async def list_gms_update_monitor_requirement_version_tags(
    android_version: str = Query('', description='Android 15, Android 16, or Android 17'),
    change_kind: str = Query('', description='added, changed, or specific'),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str] = []
    if android_version:
        where.append('android_version = ? COLLATE NOCASE')
        params.append(android_version)
    if change_kind:
        where.append('change_kind = ? COLLATE NOCASE')
        params.append(change_kind)
    if q:
        like = _like_param(q)
        where.append('(section_title LIKE ? OR requirement_ids LIKE ? OR text_excerpt LIKE ?)')
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_requirement_version_tags {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_requirement_version_tags
            {where_sql}
            ORDER BY android_version, change_kind, section_title, requirement_ids
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {'success': True, 'data': {'items': _rows_to_dicts(rows)}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/api/v1/gms-update-monitor/requirements/table-rows')
async def list_gms_update_monitor_requirement_table_rows(
    section_key: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str] = []
    if section_key:
        where.append('section_key = ?')
        params.append(section_key)
    if q:
        like = _like_param(q)
        where.append('(section_title LIKE ? OR row_text LIKE ? OR headers_json LIKE ? OR values_json LIKE ?)')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_requirement_table_rows {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_requirement_table_rows
            {where_sql}
            ORDER BY section_title, table_index, row_index
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['headers'] = json.loads(item.pop('headers_json') or '[]')
        item['values'] = json.loads(item.pop('values_json') or '[]')
        items.append(item)
    return {'success': True, 'data': {'items': items}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/gms-update-monitor', response_class=HTMLResponse)
async def gms_update_monitor_page():
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GMS Update Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#0a0a0f; --card:#111116; --head:#17171d; --border:#2a2a35; --text:#e6e6ee; --muted:#9494a3; --primary:#3b82f6; --ok:#22a06b; }
    body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:var(--bg); min-height:100vh; }
    header { padding:16px 22px 12px; background:var(--card); border-bottom:1px solid var(--border); display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }
    h1 { margin:0 0 8px; font-size:21px; }
    .summary { color:var(--muted); font-size:13px; line-height:1.7; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    button { height:32px; border:0; border-radius:6px; padding:0 11px; background:var(--primary); color:#fff; cursor:pointer; font-weight:600; }
    button.secondary { background:#32323d; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    main { padding:14px 22px 24px; }
    .tabs { display:flex; gap:6px; margin-bottom:12px; flex-wrap:wrap; }
    .tabs button { background:#24242d; }
    .tabs button.active { background:var(--primary); }
    .filters { display:grid; grid-template-columns:minmax(220px,1fr) 150px 140px 100px; gap:8px; margin-bottom:12px; }
    input, select { height:32px; border:1px solid var(--border); border-radius:6px; background:var(--card); color:var(--text); padding:0 10px; }
    table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--border); font-size:12px; table-layout:fixed; }
    th, td { border-bottom:1px solid var(--border); padding:8px 9px; text-align:left; vertical-align:top; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    th { background:var(--head); position:sticky; top:0; z-index:1; }
    td.wrap { white-space:normal; overflow:visible; text-overflow:clip; }
    .table-wrap { max-height:calc(100vh - 240px); overflow:auto; border-radius:6px; }
    .muted { color:var(--muted); }
    a { color:#67a3ff; text-decoration:none; }
    a:hover { text-decoration:underline; }
    .pager { position:fixed; right:14px; bottom:10px; display:flex; gap:6px; align-items:center; background:var(--card); border:1px solid var(--border); border-radius:6px; padding:5px 7px; box-shadow:0 2px 10px rgba(0,0,0,.4); font-size:12px; }
    .pager button { height:24px; padding:0 8px; font-size:12px; }
    .toast { position:fixed; left:50%; bottom:46px; transform:translateX(-50%); max-width:min(720px, calc(100vw - 32px)); background:#1f2937; color:#fff; border:1px solid var(--border); border-radius:6px; padding:9px 12px; box-shadow:0 8px 24px rgba(0,0,0,.35); font-size:13px; z-index:9999; display:none; }
    .toast.error { background:#3b1118; border-color:#7f1d1d; }
    @media (max-width:900px) { header { flex-direction:column; } .filters { grid-template-columns:1fr 1fr; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>GMS Update Monitor</h1>
      <div id="summary" class="summary">Loading...</div>
    </div>
    <div class="actions">
      <button onclick="startSync('incremental')">增量扫描</button>
      <button onclick="startSync('full')" class="secondary">全量扫描</button>
      <button onclick="loadAll()" class="secondary">刷新</button>
    </div>
  </header>
  <main>
    <div class="tabs">
      <button data-tab="changes" class="active" onclick="setTab('changes')">变更</button>
      <button data-tab="artifacts" onclick="setTab('artifacts')">测试套件</button>
      <button data-tab="packages" onclick="setTab('packages')">GMS包</button>
      <button data-tab="requirements" onclick="setTab('requirements')">认证要求章节</button>
    </div>
    <div class="filters">
      <input id="q" placeholder="搜索关键字">
      <select id="source_key">
        <option value="">全部来源</option>
        <option value="cts_downloads">CTS</option>
        <option value="vts_downloads">VTS</option>
        <option value="gts_downloads">GTS</option>
        <option value="gms_downloads">GMS Download</option>
        <option value="gms_requirements">GMS Requirements</option>
      </select>
      <input id="type_filter" placeholder="类型/版本">
      <button onclick="reload(true)">查询</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead id="thead"></thead>
        <tbody id="rows"><tr><td class="muted">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="pager">
      <button onclick="page(-1)">上一页</button>
      <span id="pageinfo" class="muted"></span>
      <button onclick="page(1)">下一页</button>
    </div>
    <div id="toast" class="toast"></div>
  </main>
  <script>
    const initialTab = new URLSearchParams(window.location.search).get('tab');
    let tab = ['changes', 'artifacts', 'packages', 'requirements'].includes(initialTab) ? initialTab : 'changes';
    let offset = 0;
    const limit = 100;
    let total = 0;
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const link = (url, text='下载') => url ? `<a href="${esc(url)}" target="_blank">${esc(text)}</a>` : '';
    const suiteDownloadLink = url => url ? `<a href="#" class="suite-download-link" data-url="${esc(url)}">下载</a>` : '';
    function showToast(message, type='info') {
      const el = document.getElementById('toast');
      if (!el) return;
      el.textContent = message;
      el.className = 'toast' + (type === 'error' ? ' error' : '');
      el.style.display = 'block';
      clearTimeout(showToast._timer);
      showToast._timer = setTimeout(() => { el.style.display = 'none'; }, 3600);
    }
    function setTab(next) {
      tab = next; offset = 0;
      document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
      reload(true);
    }
    async function loadSummary() {
      const r = await fetch('/api/v1/gms-update-monitor/summary');
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'summary failed');
      const d = data.data;
      const parts = [];
      if (d.last_run) parts.push(`最近扫描: ${new Date(d.last_run.finished_at).toLocaleString('zh-CN')}，变更 ${d.last_run.changes_total}`);
      const suiteTotal = (d.sources || []).reduce((sum, s) => sum + (s.source_key === 'gms_requirements' ? 0 : (s.artifacts || 0) + (s.packages || 0)), 0);
      const reqTotal = (d.sources || []).reduce((sum, s) => sum + (s.source_key === 'gms_requirements' ? (s.requirement_sections || 0) : 0), 0);
      if (suiteTotal) parts.push(`测试/GMS包: ${suiteTotal}`);
      if (reqTotal) parts.push(`认证章节: ${reqTotal}`);
      if (Array.isArray(d.requirement_version_summary) && d.requirement_version_summary.length) {
        const versionTotals = {};
        d.requirement_version_summary.forEach(x => { versionTotals[x.android_version] = (versionTotals[x.android_version] || 0) + x.count; });
        parts.push('新版本要求: ' + Object.entries(versionTotals).map(([k, v]) => `${k} ${v}`).join(', '));
      }
      document.getElementById('summary').textContent = parts.join(' | ') || '暂无扫描数据';
    }
    function startSuiteDownload(url) {
      if (!url) return;
      let frame = document.getElementById('suite-direct-download-frame');
      if (!frame) {
        frame = document.createElement('iframe');
        frame.id = 'suite-direct-download-frame';
        frame.name = 'suite-direct-download-frame';
        frame.style.display = 'none';
        document.body.appendChild(frame);
      }
      frame.src = url;
      showToast('已开始下载套件');
    }
    async function startSync(mode) {
      const buttons = document.querySelectorAll('header button');
      buttons.forEach(b => b.disabled = true);
      try {
        const r = await fetch('/api/v1/gms-update-monitor/sync?mode=' + encodeURIComponent(mode), {method:'POST'});
        const data = await r.json();
        if (!r.ok || !data.success) throw new Error(data.error || '启动失败');
        pollSync();
      } catch (e) {
        showToast(e.message, 'error');
        buttons.forEach(b => b.disabled = false);
      }
    }
    async function pollSync() {
      const r = await fetch('/api/v1/gms-update-monitor/sync/status');
      const data = await r.json();
      const status = data.data.status;
      if (status.running) {
        document.getElementById('summary').textContent = `扫描中: ${status.mode}`;
        setTimeout(pollSync, 3000);
        return;
      }
      document.querySelectorAll('header button').forEach(b => b.disabled = false);
      if (status.error) showToast('扫描失败: ' + status.error, 'error');
      loadAll();
    }
    function paramsBase() {
      const params = new URLSearchParams({limit, offset});
      const q = document.getElementById('q').value.trim();
      const source = document.getElementById('source_key').value;
      const type = document.getElementById('type_filter').value.trim();
      if (q) params.set('q', q);
      if (source) params.set('source_key', source);
      return {params, type};
    }
    async function reload(reset=false) {
      if (reset) offset = 0;
      const {params, type} = paramsBase();
      let endpoint = '/api/v1/gms-update-monitor/changes';
      if (tab === 'artifacts') {
        endpoint = '/api/v1/gms-update-monitor/artifacts';
        if (type) params.set(type.match(/^Android/i) ? 'android_version' : 'suite_type', type);
      } else if (tab === 'packages') {
        endpoint = '/api/v1/gms-update-monitor/packages';
        params.delete('source_key');
        if (type) params.set('android_version', type);
      } else if (tab === 'requirements') {
        endpoint = '/api/v1/gms-update-monitor/requirements/version-tags';
        params.delete('source_key');
        if (type && /^Android\s+1[5-7]$/i.test(type)) params.set('android_version', type);
        else if (type && /^(added|changed|specific)$/i.test(type)) params.set('change_kind', type);
      } else if (type) {
        params.set('entity_type', type);
      }
      const r = await fetch(endpoint + '?' + params.toString());
      const data = await r.json();
      if (!data.success) throw new Error(data.error || 'query failed');
      total = data.meta.total;
      renderRows(data.data.items);
      document.getElementById('pageinfo').textContent = `${total ? offset + 1 : 0}-${Math.min(offset + limit, total)} / ${total}`;
    }
    function renderRows(items) {
      if (tab === 'changes') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:95px">来源</th><th style="width:110px">对象</th><th style="width:70px">类型</th><th>内容</th><th style="width:145px">时间</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.source_key)}</td><td>${esc(x.entity_type)}</td><td>${esc(x.change_type)}</td><td class="wrap">${esc(JSON.stringify(x.after || x.before || {})).slice(0, 700)}</td><td>${esc(x.detected_at)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无记录</td></tr>';
      } else if (tab === 'artifacts') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:80px">套件</th><th style="width:250px">Android</th><th style="width:70px">API level</th><th style="width:26%">套件版本</th><th>套件文件名</th><th style="width:60px">架构</th><th style="width:70px">页面</th><th style="width:70px">下载</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => {
          const title = x.release_name || x.file_name || '';
          const androidText = x.suite_type === 'GTS' && x.target_platform ? x.target_platform : x.android_version;
          return `<tr><td>${esc(x.suite_type)}</td><td class="wrap">${esc(androidText)}</td><td>${esc(x.api_level)}</td><td class="wrap">${esc(title)}</td><td class="wrap">${esc(x.file_name)}</td><td>${esc(x.arch)}</td><td>${link(x.section_url, '页面')}</td><td>${suiteDownloadLink(x.download_url)}</td></tr>`;
        }).join('') || '<tr><td colspan="8" class="muted">无记录</td></tr>';
      } else if (tab === 'packages') {
        document.getElementById('thead').innerHTML = '<tr><th style="width:200px">章节</th><th style="width:150px">Android</th><th style="width:30%">文件</th><th style="width:150px">生效日期</th><th>Gerrit Tag</th><th style="width:56px">链接</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.section)}</td><td>${esc(x.android_version)}</td><td class="wrap">${esc(x.file_name || x.description)}</td><td>${esc(x.required_from)}</td><td>${esc(x.partner_gerrit_tag)}</td><td>${link(x.download_url)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">无记录</td></tr>';
      } else {
        document.getElementById('thead').innerHTML = '<tr><th style="width:90px">Android</th><th style="width:70px">类型</th><th style="width:28%">章节</th><th style="width:150px">Requirement ID</th><th>要求摘要</th></tr>';
        document.getElementById('rows').innerHTML = items.map(x => `<tr><td>${esc(x.android_version)}</td><td>${esc(x.change_kind)}</td><td class="wrap">${esc(x.section_title)}</td><td class="wrap">${esc(x.requirement_ids)}</td><td class="wrap">${esc(x.text_excerpt).slice(0, 700)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无记录</td></tr>';
      }
    }
    function page(delta) {
      const next = offset + delta * limit;
      if (next < 0 || next >= total) return;
      offset = next; reload(false);
    }
    function loadAll() { loadSummary().catch(e => document.getElementById('summary').textContent = e.message); reload(false).catch(e => document.getElementById('rows').innerHTML = `<tr><td class="muted">${esc(e.message)}</td></tr>`); }
    document.getElementById('q').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('type_filter').addEventListener('keydown', e => { if (e.key === 'Enter') reload(true); });
    document.getElementById('rows').addEventListener('click', e => {
      const linkEl = e.target.closest('.suite-download-link');
      if (!linkEl) return;
      e.preventDefault();
      startSuiteDownload(linkEl.dataset.url || '');
    });
    document.querySelectorAll('.tabs button').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
    loadAll();
  </script>
</body>
</html>
        """
    )
