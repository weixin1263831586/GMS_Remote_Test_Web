"""Mainline known issues database viewer APIs."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from .mainline_records import escape_like
from .repository import DB_PATH, connect
from .update_records import query_bulletin_patches


router = APIRouter()
def _connect_db() -> sqlite3.Connection:
    return connect(DB_PATH)


@router.get('/api/gms-update-center/exemptions')
async def list_mainline_known_issues(
    q: str = Query('', max_length=256, description='Keyword search across module, testcase, exemption, issue text, and source URL'),
    issue_type: str = Query('', description='MTS, CTS, or GTS'),
    product_section: str = Query('', description='Android or Android Go'),
    test_module: str = Query('', description='Exact test module'),
    test_case: str = Query('', description='Exact test case'),
    exemption_id: str = Query('', description='Exact internal bug/exemption id'),
    release_year: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):

    where = []
    params: list[str | int] = []
    if q:
        like = f'%{escape_like(q)}%'
        # escape_like 转义了 %/_/\,LIKE 必须带 ESCAPE 子句转义才生效;
        # 缺失时含 %/_ 的查询会把转义符按字面匹配,既漏数据又可被通配符滥用。
        where.append(
            '('
            'test_module LIKE ? ESCAPE "\\" OR test_case LIKE ? ESCAPE "\\" OR '
            'exemption_id LIKE ? ESCAPE "\\" OR '
            'issue_text LIKE ? ESCAPE "\\" OR source_url LIKE ? ESCAPE "\\" OR '
            'release_label LIKE ? ESCAPE "\\" OR '
            'android_versions LIKE ? ESCAPE "\\" OR category LIKE ? ESCAPE "\\" OR '
            'product_section LIKE ? ESCAPE "\\" OR issue_type LIKE ? ESCAPE "\\"'
            ')'
        )
        params.extend([like] * 10)
    for col, val in [
        ('issue_type', issue_type.upper() if issue_type else ''),
        ('product_section', product_section),
        ('test_module', test_module),
        ('test_case', test_case),
        ('exemption_id', exemption_id),
    ]:
        if val:
            suffix = ' COLLATE NOCASE' if col == 'product_section' else ''
            where.append(f'{col} = ?{suffix}')
            params.append(val)
    if release_year is not None:
        where.append('release_year = ?')
        params.append(release_year)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(_connect_db()) as conn, conn:
        total = conn.execute(f'SELECT COUNT(*) FROM mainline_known_issues {where_sql}', params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT
                id, issue_type, product_section, release_year, release_label,
                exemption_id, test_module, test_case, android_versions, category,
                source_url, issue_text, last_seen_at
            FROM mainline_known_issues
            {where_sql}
            ORDER BY release_year DESC, source_url DESC, issue_type, product_section, test_module, test_case
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        m = re.search(r'/release-notes/\d{4}/([a-z-]+?)(?:/|$)', d.get('source_url', ''))
        d['release_month'] = m.group(1) if m else ''
        items.append(d)
    return {
        'success': True,
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': items,
    }


@router.get('/api/gms-update-center/exemptions/summary')
async def mainline_known_issues_summary():
    with closing(_connect_db()) as conn, conn:
        total = conn.execute('SELECT COUNT(*) FROM mainline_known_issues').fetchone()[0]
        patch_total = conn.execute('SELECT COUNT(*) FROM mainline_security_patches').fetchone()[0]
        by_type = conn.execute(
            """
            SELECT issue_type, product_section, COUNT(*) AS count
            FROM mainline_known_issues
            GROUP BY issue_type, product_section
            ORDER BY issue_type, product_section
            """
        ).fetchall()
        last_sync = conn.execute(
            """
            SELECT s.mode, s.finished_at, r.fetched-1 AS pages_scanned,
                   0 AS pages_skipped,
                   (SELECT COUNT(*) FROM mainline_known_issues) AS issues_found,
                   (SELECT COUNT(*) FROM mainline_security_patches) AS patches_found
            FROM gms_update_scan_runs s JOIN gms_update_source_results r ON s.id=r.run_id
            WHERE r.source_key='mainline_preload' AND r.state IN ('ready','unchanged')
            ORDER BY s.id DESC LIMIT 1
            """
        ).fetchone()
        year_range = conn.execute(
            """
            SELECT MIN(release_year) AS min_year, MAX(release_year) AS max_year
            FROM mainline_known_issues
            """
        ).fetchone()
    return {
        'success': True,
        'total': total,
        'patch_total': patch_total,
        'by_type': [dict(row) for row in by_type],
        'last_sync': dict(last_sync) if last_sync else None,
        'year_range': dict(year_range) if year_range and year_range['min_year'] else None,
        'db_path': str(DB_PATH),
    }


@router.get('/api/gms-update-center/security-patches')
async def list_mainline_security_patches(
    q: str = Query('', max_length=256, description='CVE id, module, or note keyword'),
    release_year: int | None = Query(None),
    mainline_module: str = Query('', max_length=128),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    where = []
    params: list[str | int] = []
    if release_year is not None:
        where.append('release_year = ?')
        params.append(release_year)
    if mainline_module:
        where.append('mainline_module = ? COLLATE NOCASE')
        params.append(mainline_module)
    if q:
        like = f'%{escape_like(q)}%'
        where.append(
            '(cve_id LIKE ? ESCAPE "\\" OR mainline_module LIKE ? ESCAPE "\\" OR '
            'version_note LIKE ? ESCAPE "\\" OR release_label LIKE ? ESCAPE "\\" OR '
            'source_url LIKE ? ESCAPE "\\" OR product_section LIKE ? ESCAPE "\\" OR '
            'source_title LIKE ? ESCAPE "\\")'
        )
        params.extend([like] * 7)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ''
    with closing(_connect_db()) as conn, conn:
        total = conn.execute(
            f'SELECT COUNT(*) FROM mainline_security_patches {where_sql}', params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, cve_id, mainline_module, version_note, product_section,
                   release_year, release_label, source_url, source_title, last_seen_at
            FROM mainline_security_patches
            {where_sql}
            ORDER BY release_year DESC, source_url DESC, cve_id, mainline_module
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        d = dict(row)
        m = re.search(r'/release-notes/\d{4}/([a-z-]+?)(?:/|$)', d.get('source_url', ''))
        d['release_month'] = m.group(1) if m else ''
        items.append(d)
    return {
        'success': True,
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': items,
    }


@router.get('/api/gms-update-center/critical-patches')
async def list_mainline_critical_patches(
    q: str = Query('', max_length=256, description='Patch id, description, Android version, month, or deadline keyword'),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Critical Fix Supplementary Bulletins 补丁（数据源：GMS Update Monitor 库）。

    patch_id 形如 ``A-568181732``，数字段与 Mainline 豁免表的
    exemption_id 同属 Google bug ID 空间；命中豁免时附带
    ``exemptions``（issue_type / test_module / test_case），供分析
    「补丁修复 ↔ 豁免条目」关联。库不存在时优雅降级为空集。
    """
    updates_db = DB_PATH
    if not updates_db.exists():
        return {'success': True, 'available': False, 'total': 0, 'limit': limit, 'offset': offset, 'items': []}

    items = []
    with closing(_connect_db()) as mainline_conn, mainline_conn:
        mainline_conn.execute('BEGIN')
        result = query_bulletin_patches(mainline_conn, q=q, limit=limit, offset=offset)
        rows = result['items']
        total = result['total']
        for row in rows:
            item = dict(row)
            numeric_id = str(row['patch_id'])[2:] if str(row['patch_id']).startswith(('A-', 'P-', 'R-')) else ''
            exemptions = []
            if numeric_id:
                exemptions = [
                    dict(m) for m in mainline_conn.execute(
                        """
                        SELECT issue_type, test_module, test_case, exemption_id
                        FROM mainline_known_issues
                        WHERE exemption_id = ?
                        ORDER BY issue_type, test_module, test_case
                        """,
                        (numeric_id,),
                    ).fetchall()
                ]
            item['exemptions'] = exemptions
            item['release_month'] = str(row['bulletin_month'] or '')
            items.append(item)
    return {
        'success': True,
        'available': True,
        'coverage_complete': result['coverage_complete'],
        'total': total,
        'limit': limit,
        'offset': offset,
        'items': items,
    }


def _critical_patch_evidence(q: str) -> tuple[bool, list[dict[str, str]]]:
    """Load bulletin ids and detail text for the Gerrit check.

    The text stays on the server. Callers must not put it in the response.
    """
    updates_db = DB_PATH
    if not updates_db.exists():
        return False, []

    conn = connect(updates_db)
    try:
        rows = []
        while True:
            result = query_bulletin_patches(conn, q=q, limit=500, offset=len(rows))
            rows.extend(result['items'])
            if not result['items'] or len(rows) >= result['total']:
                break
    finally:
        conn.close()
    patches = [
        {
            'patch_id': str(row.get('patch_id') or ''),
            'record_key': str(row.get('item_key') or row.get('patch_id') or ''),
            'detail_text': str(row.get('detail_text') or ''),
            'android_versions': str(row.get('android_versions') or ''),
            'patch_rows_json': str(row.get('patch_rows_json') or ''),
            'detail_status': str(row.get('detail_status') or ''),
        }
        for row in rows
    ]
    return True, patches


_gerrit_config_provider = None
_gerrit_query_provider = None


def configure_gerrit_match_access(*, config_provider, query_provider):
    """Wire owner-scoped Gerrit capabilities from the composition root."""
    global _gerrit_config_provider, _gerrit_query_provider
    _gerrit_config_provider = config_provider
    _gerrit_query_provider = query_provider


@router.get('/api/gms-update-center/critical-patches/gerrit')
async def mainline_critical_patch_gerrit(
    request: Request,
    q: str = Query('', max_length=256, description='Same search as the Critical Fix list'),
    refresh: int = Query(0, ge=0, le=1, description='1 skips the in-memory Gerrit cache'),
):
    """Report whether our Gerrit has merged each Critical Fix patch.

    A bulletin row matches a change when the change is that revision,
    names that full SHA, or has the same project and commit subject.
    Mentioning the bug number is not a match. The response does not
    include bulletin detail text.
    """
    from features.users import owner_id_from_request

    from .gerrit_match import match_critical_patches

    try:
        available, patches = _critical_patch_evidence(q)
        owner_id = owner_id_from_request(request)
        cfg = dict(_gerrit_config_provider(owner_id)) if _gerrit_config_provider else {}
        cfg["_match_owner"] = str(owner_id)

        async def query_changes(config, query, limit, page_size, commit_message=False):
            if _gerrit_query_provider is None:
                raise RuntimeError('Gerrit query provider is not configured')
            return await _gerrit_query_provider(
                config, query, limit, page_size, commit_message=commit_message,
            )

        payload = await match_critical_patches(
            patches, cfg, query_changes, refresh=bool(refresh),
        )
    except Exception as exc:
        return {
            'success': True,
            'available': True,
            'configured': True,
            'cached': False,
            'checked_at': '',
            'error': str(exc)[:300],
            'items': {},
        }
    return {'success': True, 'available': available, **payload}


@router.get('/gms-update-center', response_class=HTMLResponse)
async def gms_update_center_page():
    page_path = Path(__file__).with_name('ui') / 'page.html'
    return HTMLResponse(page_path.read_text(encoding='utf-8'), headers={'Cache-Control': 'no-cache'})


@router.get('/gms-update-center/page.js')
def gms_update_center_page_js():
    """页面脚本走静态资源（CSP 收紧后禁止 inline <script>）。"""
    page_path = Path(__file__).with_name('ui') / 'page.html'
    js = page_path.parent / 'page.js'
    return Response(
        js.read_text(encoding='utf-8'), media_type='application/javascript',
        headers={'Cache-Control': 'no-cache'},
    )
