"""GMS/CTS update monitor APIs."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime

from fastapi import APIRouter

from foundation.config import settings

from .models import SOURCES
from .repository import init_db


router = APIRouter(prefix='/api/gms-update-monitor')
page_router = APIRouter()
DB_PATH = settings.data_root / 'gms_update_monitor.sqlite3'

_DOWNLOAD_API = {'method': 'POST', 'path': '/api/test/suites/download-url', 'body_template': {'url': '<download_url>'}}
_VERSION_TO_API = {'13': '33', '14': '34', '15': '35', '16': '36', '17': '37'}

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

__all__ = [
    "DB_PATH",
    "SOURCES",
    "_DOWNLOAD_API",
    "_artifact_api_level",
    "_artifact_release_number",
    "_artifact_section_url",
    "_connect_db",
    "_enrich_artifact_item",
    "_get_db",
    "_like_param",
    "_rows_to_dicts",
    "_run_sync_job",
    "_sync_lock",
    "_sync_status",
    "page_router",
    "router",
]


def _connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _get_db():
    """Return an initialized database connection."""
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
    major_text, _, minor = release.partition('.')
    api = _VERSION_TO_API.get(major_text)
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
    command = [
        sys.executable,
        '-m',
        'features.system.update_monitor.cli',
        '--mode',
        mode,
        '--verbose',
    ]
    for item in source:
        command.extend(['--source', item])
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
