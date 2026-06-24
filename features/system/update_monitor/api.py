"""GMS/CTS update monitor APIs."""

from __future__ import annotations

# ruff: noqa: F403, F405
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse

from .api_support import *


@page_router.get('/gms-update-monitor', response_class=HTMLResponse)
async def gms_update_monitor_page():
    page_path = Path(__file__).with_name('ui') / 'page.html'
    return HTMLResponse(page_path.read_text(encoding='utf-8'))

@router.get('/sources')
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
        conn = _connect_db()
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


@router.post('/sync')
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


@router.get('/sync/status')
async def gms_update_monitor_sync_status():
    with _sync_lock:
        status = dict(_sync_status)
    status['db_exists'] = DB_PATH.exists()
    return {'success': True, 'data': {'status': status}}


@router.get('/summary')
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


@router.get('/scan-runs')
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


@router.get('/changes')
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


@router.get('/artifacts')
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


@router.get('/artifacts/new')
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
                    'download_api': _DOWNLOAD_API,
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
            'download_api': _DOWNLOAD_API,
        },
        'meta': {'total': len(items), 'limit': limit},
    }


@router.get('/packages')
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


_MAINLINE_MONTH_ORDER_SQL = (
    "CASE month "
    "WHEN 'jan' THEN 1 WHEN 'feb' THEN 2 WHEN 'mar' THEN 3 WHEN 'apr' THEN 4 "
    "WHEN 'may' THEN 5 WHEN 'jun' THEN 6 WHEN 'jul' THEN 7 WHEN 'aug' THEN 8 "
    "WHEN 'sep' THEN 9 WHEN 'oct' THEN 10 WHEN 'nov' THEN 11 WHEN 'dec' THEN 12 "
    "ELSE 0 END"
)


@router.get('/mainline')
async def list_gms_update_monitor_mainline(
    year: str = Query(''),
    month: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn, missing = _get_db()
    if missing:
        return missing
    where = []
    params: list[str] = []
    if year:
        where.append('year = ?')
        params.append(year)
    if month:
        where.append('month = ? COLLATE NOCASE')
        params.append(month.lower())
    if q:
        like = _like_param(q)
        where.append('(preload_version LIKE ? OR partner_zip_build_id LIKE ? OR notes_url LIKE ? OR ci_build_url LIKE ?)')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_mainline_packages {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT *
            FROM gms_update_mainline_packages
            {where_sql}
            ORDER BY CAST(year AS INTEGER) DESC, {_MAINLINE_MONTH_ORDER_SQL} DESC, preload_version DESC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    return {'success': True, 'data': {'items': _rows_to_dicts(rows)}, 'meta': {'total': total, 'limit': limit, 'offset': offset}}


@router.get('/requirements/sections')
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


@router.get('/requirements/version-summary')
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


@router.get('/requirements/version-tags')
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


@router.get('/requirements/table-rows')
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

