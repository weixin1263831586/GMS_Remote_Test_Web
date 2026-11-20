"""Direct CLI and Web-initiated scans preserve data across partial refreshes."""
import sqlite3
import sys
from dataclasses import replace

import pytest
import requests

from features.system.mainline_issues import cli
from features.system.mainline_issues.parser import FetchedPage, ReleasePage
from features.system.mainline_issues.repository import init_db
from features.system.update_monitor import cli as update_cli
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock


@pytest.fixture
def fake_partner(monkeypatch):
    page = ReleasePage(2026, 'Release', 'https://example.test/2026')
    monkeypatch.setattr(cli, 'build_session', lambda args: requests.Session())
    monkeypatch.setattr(cli, 'fetch_doc', lambda *args: None)
    monkeypatch.setattr(cli, 'extract_release_years', lambda doc: [page.year])
    monkeypatch.setattr(cli, 'extract_release_pages', lambda *args: [page])
    monkeypatch.setattr(cli, 'fetch_page', lambda *args: FetchedPage(None, 'digest', 200, page.url))
    monkeypatch.setattr(cli, 'extract_known_issues', lambda *args: [])
    monkeypatch.setattr(cli, 'extract_security_patches', lambda *args: [])


def test_preview_does_not_create_a_database_or_directory(tmp_path, monkeypatch, fake_partner):
    path = tmp_path / 'missing' / 'notes.db'
    monkeypatch.setattr(sys, 'argv', ['scan', '--db', str(path), '--dry-run'])
    assert cli.main() == 0
    assert not path.parent.exists()


@pytest.mark.parametrize('failure', [False, True])
def test_force_preserves_other_years_and_failed_pages(tmp_path, monkeypatch, fake_partner, failure):
    path = tmp_path / 'notes.db'
    with sqlite3.connect(path) as conn:
        init_db(conn)
        conn.execute("""INSERT INTO mainline_security_patches
            (source_url,source_title,release_year,release_label,product_section,cve_id,first_seen_at,last_seen_at)
            VALUES ('prior','prior',2025,'prior','Android','prior','','')""")
    args = ['scan', '--force', '--db', str(path)]
    if failure:
        def unavailable(*args):
            raise requests.ConnectionError('source unavailable')
        monkeypatch.setattr(cli, 'fetch_page', unavailable)
    else:
        args += ['--year', '2026']
    monkeypatch.setattr(sys, 'argv', args)
    assert cli.main() == int(failure)
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT release_year FROM mainline_security_patches').fetchall() == [(2025,)]


@pytest.mark.parametrize('module', [cli, update_cli])
def test_direct_scan_contends_with_web_and_inherited_lock_stays_held(tmp_path, monkeypatch, module):
    path = tmp_path / 'test.db'
    lock = path.with_suffix('.sync.lock')
    descriptor = try_acquire_sync_lock(lock)
    try:
        calls = []
        monkeypatch.setattr(module, 'run_sync', lambda args: calls.append(args.db) or 0)
        args = ['scan', '--db', str(path)]
        monkeypatch.setattr(sys, 'argv', args)
        assert module.main() == 1
        assert calls == []
        monkeypatch.setattr(sys, 'argv', [*args, '--sync-lock-fd', str(descriptor)])
        assert module.main() == 0
        assert calls == [path]
        assert try_acquire_sync_lock(lock) is None
    finally:
        release_sync_lock(descriptor)


def test_incremental_scan_transfers_an_issue_between_updated_pages(tmp_path, monkeypatch, fake_partner):
    path = tmp_path / 'notes.db'
    origin = ReleasePage(2026, 'Old', 'https://example.test/old')
    target = ReleasePage(2026, 'New', 'https://example.test/new')
    monkeypatch.setattr(cli, 'extract_release_pages', lambda *args: [target, origin])
    issue = cli.KnownIssue(target.url, 'New', 2026, 'New', 'Android', 'CTS', '16', 'cat', 'M', 'C', 'E', 'text')
    monkeypatch.setattr(cli, 'extract_known_issues', lambda page, doc: [issue] if page == target else [])
    with sqlite3.connect(path) as conn:
        init_db(conn)
        cli.upsert_issues(conn, [replace(issue, source_url=origin.url)], 'previous')
    monkeypatch.setattr(sys, 'argv', ['scan', '--db', str(path)])
    assert cli.main() == 0
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT source_url FROM mainline_known_issues').fetchall() == [(target.url,)]
