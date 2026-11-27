"""GMS/CTS update monitor APIs."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, Query, Request, Response

from features.auth import require_elevated_admin, require_human_principal

from .mainline_records import escape_like
from .repository import DB_PATH, connect
from .requirement_lookup import lookup_requirement_sections, read_requirement_index_status
from .sources import SOURCES
from .update_records import query_bulletin_patches


router = APIRouter(prefix='/api/gms-update-center')

_DOWNLOAD_API = {'method': 'POST', 'path': '/api/test/suites/download-url', 'body_template': {'url': '<download_url>'}}
_VERSION_TO_API = {'13': '33', '14': '34', '15': '35', '16': '36', '17': '37'}

def _connect_db() -> sqlite3.Connection:
    return connect(DB_PATH)


def _get_db() -> sqlite3.Connection:
    """Return an initialized database connection."""
    return _connect_db()


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _like_param(value: str) -> str:
    return f'%{escape_like(value)}%'


def _artifact_release_number(item: dict) -> str:
    for value in (item.get('release_name', ''), item.get('android_version', ''), item.get('file_name', '')):
        match = re.search(r'(?:CTS|VTS|GTS)?[-_ ]?(1[3-7](?:\.\d+)?)', value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r'Android\s+(1[3-7])(?:\s+QPR2)?', value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ''


def _artifact_api_level(item: dict, release: str = '') -> str:
    target_platform = item.get('target_platform', '')
    if item.get('suite_type') == 'GTS' and target_platform:
        range_match = re.search(r'Android\s+(1[3-7])\s*-\s*(1[3-7])', target_platform, flags=re.IGNORECASE)
        versions = list(range_match.groups()) if range_match else re.findall(r'Android\s+(1[3-7])', target_platform, flags=re.IGNORECASE)
        api_map_full = _VERSION_TO_API
        apis = [api_map_full[v] for v in versions if v in api_map_full]
        if len(apis) >= 2:
            return f'{apis[0]}-{apis[-1]}'
        if len(apis) == 1:
            return apis[0]
    if not release:
        release = _artifact_release_number(item)
    if not release:
        return ''
    # A suite release decimal is not a platform API level.
    return _VERSION_TO_API.get(release.partition('.')[0], '')


def _artifact_section_url(item: dict, release: str = '') -> str:
    source_key = item.get('source_key', '')
    if not release:
        release = _artifact_release_number(item)
    if source_key == 'cts_downloads' and release:
        anchor = 'android-' + release.partition('.')[0]
        return f'https://source.android.com/docs/compatibility/cts/downloads#{anchor}'
    if source_key == 'vts_downloads' and release:
        anchor = 'android-' + release.partition('.')[0]
        return f'https://docs.partner.android.com/gms/testing/vts#{anchor}'
    if source_key == 'gts_downloads':
        return 'https://docs.partner.android.com/gms/testing/gts#download-gts'
    return ''


def _enrich_artifact_item(item: dict) -> dict:
    release = _artifact_release_number(item)
    item['api_level'] = _artifact_api_level(item, release)
    item['section_url'] = _artifact_section_url(item, release)
    return item


@router.get('/sources')
async def gms_update_sources():
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
        with closing(conn), conn:
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


@router.get('/summary')
async def gms_update_summary():
    conn = _get_db()
    with closing(conn), conn:
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
                   COALESCE(m.count, 0) AS mainline_packages,
                   COALESCE(b.count, 0) AS bulletin_patches,
                   COALESCE(r.count, 0) AS requirement_sections
            FROM gms_update_sources s
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_artifacts GROUP BY source_key
            ) a ON a.source_key = s.source_key
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_packages GROUP BY source_key
            ) p ON p.source_key = s.source_key
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_mainline_packages GROUP BY source_key
            ) m ON m.source_key = s.source_key
            LEFT JOIN (
                SELECT source_key, COUNT(*) AS count FROM gms_update_bulletin_patches GROUP BY source_key
            ) b ON b.source_key = s.source_key
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


@router.get('/scan/runs')
async def list_gms_update_scan_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
    with closing(conn), conn:
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
async def list_gms_update_changes(
    run_id: int | None = Query(None),
    source_key: str = Query(''),
    entity_type: str = Query(''),
    change_type: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
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
        where.append('(entity_key LIKE ? ESCAPE "\\" OR before_json LIKE ? ESCAPE "\\" OR after_json LIKE ? ESCAPE "\\")')
        like = _like_param(q)
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
        total = conn.execute(f'SELECT COUNT(*) FROM gms_update_change_events {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, run_id, source_key, entity_type, entity_key, change_type, origin,
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
async def list_gms_update_artifacts(
    source_key: str = Query(''),
    suite_type: str = Query(''),
    android_version: str = Query(''),
    arch: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
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
            '(release_name LIKE ? ESCAPE "\\" OR file_name LIKE ? ESCAPE "\\" OR download_url LIKE ? ESCAPE "\\" OR '
            'description LIKE ? ESCAPE "\\" OR target_platform LIKE ? ESCAPE "\\" OR section_path LIKE ? ESCAPE "\\")'
        )
        params.extend([like, like, like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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
async def list_new_gms_update_artifacts(
    run_id: int | None = Query(None),
    source_key: list[str] = Query(default=[]),
    limit: int = Query(20, ge=1, le=100),
):
    conn = _get_db()
    with closing(conn), conn:
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
        where = ['run_id = ?', "entity_type = 'artifact'", "change_type = 'added'", "origin = 'source_changed'", "after_json != ''"]
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
async def list_gms_update_packages(
    android_version: str = Query(''),
    section: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
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
        where.append('(file_name LIKE ? ESCAPE "\\" OR partner_gerrit_tag LIKE ? ESCAPE "\\" OR description LIKE ? ESCAPE "\\" OR download_url LIKE ? ESCAPE "\\")')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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


@router.get('/bulletins')
async def list_gms_update_bulletins(
    year: str = Query(''),
    kind: str = Query(''),
    android_version: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
    with closing(conn), conn:
        result = query_bulletin_patches(conn, q=q, year=year, kind=kind, android_version=android_version,
                                        limit=limit, offset=offset)
    return {'success': True, 'data': result, 'meta': {'total': result['total'], 'limit': limit, 'offset': offset}}


@router.get('/mainline')
async def list_gms_update_mainline(
    year: str = Query(''),
    month: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
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
        where.append('(preload_version LIKE ? ESCAPE "\\" OR partner_zip_build_id LIKE ? ESCAPE "\\" OR notes_url LIKE ? ESCAPE "\\" OR ci_build_url LIKE ? ESCAPE "\\")')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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


def _require_human_policy_read(request: Request, response: Response):
    require_human_principal(request)
    response.headers['Cache-Control'] = 'no-store'


@router.get('/requirements/sections', dependencies=[Depends(_require_human_policy_read)])
async def list_gms_update_requirement_sections(
    level: int | None = Query(None, ge=1, le=6),
    top_only: bool = Query(False, description='Only return top-level numbered chapters such as 1, 2, 3'),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
    where = []
    params: list[str | int] = []
    if level is not None:
        where.append('level = ?')
        params.append(level)
    if top_only:
        where.append("number != '' AND number NOT LIKE '%.%'")
    if q:
        like = _like_param(q)
        where.append('(number LIKE ? ESCAPE "\\" OR title LIKE ? ESCAPE "\\" OR path LIKE ? ESCAPE "\\" OR text_excerpt LIKE ? ESCAPE "\\")')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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


@router.get('/requirements/version-summary', dependencies=[Depends(_require_human_policy_read)])
async def gms_update_requirement_version_summary():
    conn = _get_db()
    with closing(conn), conn:
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


@router.get('/requirements', dependencies=[Depends(_require_human_policy_read)])
async def list_gms_update_requirement_version_tags(
    android_version: str = Query('', description='Android 15, Android 16, or Android 17'),
    change_kind: str = Query('', description='added, changed, or specific'),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
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
        where.append('(section_title LIKE ? ESCAPE "\\" OR requirement_ids LIKE ? ESCAPE "\\" OR text_excerpt LIKE ? ESCAPE "\\")')
        params.extend([like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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


@router.get('/requirements/table-rows', dependencies=[Depends(_require_human_policy_read)])
async def list_gms_update_requirement_table_rows(
    section_key: str = Query(''),
    q: str = Query(''),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    conn = _get_db()
    where = []
    params: list[str] = []
    if section_key:
        where.append('section_key = ?')
        params.append(section_key)
    if q:
        like = _like_param(q)
        where.append('(section_title LIKE ? ESCAPE "\\" OR row_text LIKE ? ESCAPE "\\" OR headers_json LIKE ? ESCAPE "\\" OR values_json LIKE ? ESCAPE "\\")')
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(conn), conn:
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


@router.get('/requirements/coverage')
def gms_requirement_coverage(request: Request, response: Response):
    require_elevated_admin(request)
    response.headers['Cache-Control'] = 'no-store'
    with closing(_get_db()) as conn, conn:
        conn.execute("BEGIN")
        data = read_requirement_index_status(conn)
    return {'success': True, 'data': data}


@router.get('/requirements/clauses')
def list_gms_requirement_clauses(request: Request, response: Response, q: str = Query('', max_length=256),
                                 requirement_id: str = Query('', max_length=128),
                                 section_key: str = Query('', max_length=128),
                                 change_version: str = Query('', max_length=40),
                                 limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    require_elevated_admin(request)
    response.headers['Cache-Control'] = 'no-store'
    if q.startswith(("https://", "http://", "section:")):
        with closing(_get_db()) as conn, conn:
            conn.execute("BEGIN")
            data = lookup_requirement_sections(conn, q)
        return {'success': True, 'data': data}
    where = ['source_key=?']
    params: list[str | int] = ['gms_requirements']
    if requirement_id:
        where.append('requirement_id=? COLLATE NOCASE')
        params.append(requirement_id)
    if section_key:
        where.append('section_key=?')
        params.append(section_key)
    if change_version:
        where.append('change_version=? COLLATE NOCASE')
        params.append(change_version)
    if q:
        like = _like_param(q)
        where.append('(original_text LIKE ? ESCAPE "\\" OR context_text LIKE ? ESCAPE "\\" OR section_title LIKE ? ESCAPE "\\")')
        params.extend([like, like, like])
    predicate = ' AND '.join(where)
    with closing(_get_db()) as conn, conn:
        conn.execute("BEGIN")
        total = conn.execute(f'SELECT count(*) FROM gms_update_requirement_clauses WHERE {predicate}', params).fetchone()[0]
        rows = conn.execute(f"""SELECT * FROM gms_update_requirement_clauses WHERE {predicate}
            ORDER BY section_title,requirement_id,clause_key LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
    items = _rows_to_dicts(rows)
    for item in items:
        item.update(source='gms_requirements', reference=item['clause_key'],
                    evidence_level='requirement_candidate', applicability_verified=False,
                    original_document_verified=False)
    return {'success': True, 'data': {'items': items, 'coverage_complete': False,
                                      'scope': 'authorized_fetched_html_candidates'},
            'meta': {'total': total, 'limit': limit, 'offset': offset}}
