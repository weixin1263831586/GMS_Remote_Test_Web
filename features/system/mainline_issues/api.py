"""Mainline known issues database viewer APIs."""

import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from features.auth import CurrentUser, require_role_when_auth_required
from foundation.config import settings

from .repository import escape_like, init_db


router = APIRouter()
DB_PATH = settings.data_root / 'mainline_known_issues.sqlite3'
_sync_lock = threading.Lock()


def _initial_sync_status(**overrides):
    base = {
        'running': False, 'mode': None, 'started_at': None,
        'finished_at': None, 'returncode': None, 'stdout': '',
        'stderr': '', 'error': None,
    }
    base.update(overrides)
    return base


_sync_status = _initial_sync_status()


def _connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _db_exists_response():
    _connect_db().close()
    return None


def _run_sync_job(mode: str):
    global _sync_status
    command = [
        sys.executable,
        '-m',
        'features.system.mainline_issues.cli',
        '--verbose',
    ]
    if mode == 'full':
        command.append('--force')
    elif mode == 'incremental':
        command.append('--new-only')

    with _sync_lock:
        _sync_status.update(_initial_sync_status(
            running=True, mode=mode, started_at=datetime.now().isoformat(),
        ))
    try:
        result = subprocess.run(
            command,
            cwd=str(settings.project_root),
            text=True,
            capture_output=True,
            timeout=3600,
            check=False,
        )
        with _sync_lock:
            _sync_status.update(_initial_sync_status(
                running=False, finished_at=datetime.now().isoformat(),
                returncode=result.returncode,
                stdout=result.stdout[-4000:], stderr=result.stderr[-4000:],
                error=None if result.returncode == 0 else f'sync exited with {result.returncode}',
            ))
    except Exception as exc:
        with _sync_lock:
            _sync_status.update(_initial_sync_status(
                running=False, finished_at=datetime.now().isoformat(),
                returncode=None, error=str(exc),
            ))


@router.post('/api/mainline-known-issues/sync')
async def start_mainline_known_issues_sync(
    mode: str = Query('incremental', pattern='^(full|incremental)$'),
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    with _sync_lock:
        if _sync_status['running']:
            return JSONResponse(
                status_code=409,
                content={'success': False, 'error': 'sync already running', 'status': dict(_sync_status)},
            )
        _sync_status.update(_initial_sync_status(
            running=True, mode=mode, started_at=datetime.now().isoformat(),
        ))
        thread = threading.Thread(target=_run_sync_job, args=(mode,), daemon=True)
        thread.start()
        status = dict(_sync_status)
    return {'success': True, 'status': status}


@router.get('/api/mainline-known-issues/sync/status')
async def mainline_known_issues_sync_status(
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    with _sync_lock:
        status = dict(_sync_status)
    status['db_exists'] = DB_PATH.exists()
    return {'success': True, 'status': status}


@router.get('/api/mainline-known-issues')
async def list_mainline_known_issues(
    q: str = Query('', description='Keyword search across module, testcase, exemption, issue text, and source URL'),
    issue_type: str = Query('', description='MTS, CTS, or GTS'),
    product_section: str = Query('', description='Android or Android Go'),
    test_module: str = Query('', description='Exact test module'),
    test_case: str = Query('', description='Exact test case'),
    exemption_id: str = Query('', description='Exact internal bug/exemption id'),
    release_year: int | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    missing = _db_exists_response()
    if missing:
        return missing

    where = []
    params: list[str | int] = []
    if q:
        like = f'%{escape_like(q)}%'
        where.append(
            '('
            'test_module LIKE ? OR test_case LIKE ? OR exemption_id LIKE ? OR '
            'issue_text LIKE ? OR source_url LIKE ? OR release_label LIKE ? OR '
            'android_versions LIKE ? OR category LIKE ? OR product_section LIKE ? OR issue_type LIKE ?'
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
    with _connect_db() as conn:
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


@router.get('/api/mainline-known-issues/summary')
async def mainline_known_issues_summary():
    missing = _db_exists_response()
    if missing:
        return missing
    with _connect_db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM mainline_known_issues').fetchone()[0]
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
            SELECT release_year, pages_scanned, pages_skipped, issues_found, finished_at
            FROM mainline_known_issue_sync_runs
            ORDER BY id DESC
            LIMIT 1
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
        'by_type': [dict(row) for row in by_type],
        'last_sync': dict(last_sync) if last_sync else None,
        'year_range': dict(year_range) if year_range and year_range['min_year'] else None,
        'db_path': str(DB_PATH),
    }


@router.get('/mainline-known-issues', response_class=HTMLResponse)
async def mainline_known_issues_page():
    page_path = Path(__file__).with_name('ui') / 'page.html'
    return HTMLResponse(page_path.read_text(encoding='utf-8'))
