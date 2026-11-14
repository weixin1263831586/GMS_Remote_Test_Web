"""Scheduling, durable comparisons, and notification failures without external IO."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from features.auth import CurrentUser
from features.system.update_radar import api, scanning
from features.system.update_radar.models import CATEGORIES, RadarConfig, next_scan
from features.system.update_radar.repository import RadarStore
from features.system.update_radar.service import RadarService
from features.system.update_radar.snapshots import compare, snapshot
from foundation.command_result import CommandResult
from foundation.error_model import ApiError, register_api_error_handler


def instant(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def config(**changes):
    return RadarConfig(recipients=['team@example.test'], **changes).model_dump(mode='json')


@pytest.fixture
def store(tmp_path):
    return RadarStore(tmp_path / 'radar.sqlite3')


def result(*, changes=0, errors=None):
    counts = {key: {'before': 1, 'after': 1, 'added': 0, 'modified': 0, 'removed': 0} for key in CATEGORIES}
    counts['packages']['modified'] = changes
    return {'categories': counts, 'changes_total': changes, 'errors': errors or {}, 'initial_baseline': False}


def service(store, summary=None, mailer=None):
    return RadarService(store, scanner=lambda **_: (summary or result(), []),
                        mailer=mailer or Mock(return_value={'sent': True}), reserve=lambda: None, release=lambda _=None: None)


def test_default_schedule_is_disabled_and_uses_beijing_0400(store):
    assert store.settings() == {**RadarConfig().model_dump(mode='json'), 'next_run_at': None}
    assert store.settings()['notify'] == 'changes'
    cfg = config(enabled=True)
    assert next_scan(cfg, instant('2026-11-13T19:59:00+00:00')) == '2026-11-13T20:00:00+00:00'
    assert next_scan(cfg, instant('2026-11-13T20:01:00+00:00')) == '2026-11-14T20:00:00+00:00'


@pytest.mark.parametrize('fields', [
    {'scan_time': '24:00'}, {'timezone': 'Mars/Invalid'}, {'recipients': ['ok@example.test\r\nBcc: bad@example.test']},
    {'recipients': ['bad']}, {'enabled': True}, {'tasks': []}, {'tasks': ['unknown']},
])
def test_invalid_schedule_and_mail_headers_are_rejected(fields):
    with pytest.raises(ValidationError):
        RadarConfig(**fields)


def test_timezone_dst_preserves_local_0400():
    cfg = config(enabled=True, timezone='America/New_York')
    # DST ends on Nov 1: next 04:00 is 09:00 UTC, not a fixed UTC interval.
    assert next_scan(cfg, instant('2026-11-01T03:00:00+00:00')) == '2026-11-01T09:00:00+00:00'


def test_schedule_claim_is_atomic_and_survives_restart(store):
    store.save_settings(config(enabled=True), now=instant('2026-11-13T19:00:00+00:00'))
    due = instant('2026-11-13T20:00:00+00:00')

    def claim(_):
        try:
            return RadarStore(store.path).claim('schedule', now=due)
        except ApiError as exc:
            assert exc.code == 'STATE_CONFLICT'
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = [value for value in pool.map(claim, range(2)) if value]
    assert len(ids) == 1
    store.finish(ids[0], 'completed', result(), [])
    assert RadarStore(store.path).claim('schedule', now=due) is None
    assert store.history()['total'] == 1
    assert store.settings()['next_run_at'] == '2026-11-14T20:00:00+00:00'


def test_missed_schedule_catches_up_once_and_settings_do_not_move_it(store):
    store.save_settings(config(enabled=True), now=instant('2026-11-13T19:00:00+00:00'))
    cfg = config(enabled=True, notify='changes')
    store.save_settings(cfg, now=instant('2026-11-15T06:00:00+00:00'))
    assert store.settings()['next_run_at'] == '2026-11-13T20:00:00+00:00'
    store.recover()
    run_id = store.claim('schedule', now=instant('2026-11-15T06:00:00+00:00'))
    store.finish(run_id, 'completed', result(), [])
    assert store.claim('schedule', now=instant('2026-11-15T06:00:00+00:00')) is None
    assert store.settings()['next_run_at'] == '2026-11-15T20:00:00+00:00'


def test_scheduler_triggers_at_0400_once_without_an_open_browser(store):
    store.save_settings(config(enabled=True, tasks=['packages']), now=instant('2026-11-13T19:00:00+00:00'))
    radar = service(store)
    assert radar.tick(now=instant('2026-11-13T19:59:59+00:00')) is None
    run_id = radar.tick(now=instant('2026-11-13T20:00:00+00:00'))
    radar.worker.join(5)
    assert store.run(run_id)['trigger'] == 'schedule'
    assert store.run(run_id)['summary']['tasks'] == ['packages']
    assert radar.tick(now=instant('2026-11-13T20:00:01+00:00')) is None
    assert store.history()['total'] == 1


def update_db(path, records, sources=7):
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS gms_update_artifacts (
            source_key TEXT,item_key TEXT,file_name TEXT,first_seen_at TEXT,last_seen_at TEXT,content_hash TEXT);
            CREATE TABLE IF NOT EXISTS gms_update_scan_runs(id INTEGER PRIMARY KEY,success INTEGER,sources_scanned INTEGER);
            CREATE TABLE IF NOT EXISTS gms_update_sources(source_key TEXT PRIMARY KEY,status_code INTEGER);
            DELETE FROM gms_update_artifacts; DELETE FROM gms_update_scan_runs;
            INSERT INTO gms_update_scan_runs VALUES(1,1,7);''')
        conn.executemany('INSERT INTO gms_update_artifacts VALUES(?,?,?,?,?,?)',
                         [('cts_downloads', key, name, 'first', 'last', 'hash') for key, name in records])
        conn.execute('UPDATE gms_update_scan_runs SET sources_scanned=?', (sources,))
        conn.executemany('INSERT OR REPLACE INTO gms_update_sources VALUES(?,200)',
                         [(source,) for source in ('cts_downloads', 'vts_downloads', 'gts_downloads',
                                                  'gms_downloads', 'gms_requirements', 'mainline_preload', 'gms_bulletins')])


def exemption_db(path, text):
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS mainline_known_issues (
            id INTEGER PRIMARY KEY,product_section TEXT,issue_type TEXT,test_module TEXT,
            test_case TEXT,exemption_id TEXT,issue_text TEXT,last_seen_at TEXT)''')
        conn.execute('DELETE FROM mainline_known_issues')
        conn.execute('INSERT INTO mainline_known_issues VALUES(1,?,?,?,?,?,?,?)',
                     ('Android', 'CTS', 'Module', 'Case', '123', text, 'now'))


def test_snapshot_diffs_added_modified_removed_and_ignores_bookkeeping(tmp_path):
    path = tmp_path / 'updates.db'
    update_db(path, [('kept', 'old.zip'), ('removed', 'removed.zip')])
    before = snapshot(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE gms_update_artifacts SET last_seen_at='later',content_hash='new-hash'")
    assert compare(before, snapshot(path))[1] == []
    update_db(path, [('kept', 'new.zip'), ('added', 'added.zip')])
    counts, changes = compare(before, snapshot(path))
    assert counts['artifacts'] == {'before': 2, 'after': 2, 'added': 1, 'modified': 1, 'removed': 1}
    assert {item['type'] for item in changes} == {'added', 'modified', 'removed'}
    modified = next(item for item in changes if item['type'] == 'modified')
    assert modified['before']['file_name'] == 'old.zip'
    assert modified['after']['file_name'] == 'new.zip'


def test_incomplete_mainline_scan_keeps_previous_database(tmp_path, monkeypatch):
    updates, exemptions = tmp_path / 'updates.db', tmp_path / 'exemptions.db'
    update_db(updates, [('a', 'old.zip')])
    exemption_db(exemptions, 'original')
    monkeypatch.setattr(scanning.update_api, 'DB_PATH', updates)
    monkeypatch.setattr(scanning.mainline_api, 'DB_PATH', exemptions)
    monkeypatch.setattr(scanning, 'settings', SimpleNamespace(data_root=tmp_path, project_root=tmp_path))

    def command(argv, **kwargs):
        assert kwargs['env']['GMS_PARTNER_ANDROID_AUTO_LOGIN'] == '0'
        target = Path(argv[argv.index('--db') + 1])
        assert target not in (updates, exemptions)
        if 'features.system.update_monitor.cli' in argv:
            assert argv[argv.index('--mode') + 1] == 'full'
            assert argv.count('--source') == 7
            update_db(target, [('a', 'new.zip')])
            return CommandResult()
        assert '--force' in argv and '--new-only' not in argv
        exemption_db(target, 'partial result must not publish')
        return CommandResult(stdout='total_pages=3 pages_scanned=2 pages_skipped=0')

    monkeypatch.setattr(scanning, 'run_local_command', command)
    summary, changes = scanning.full_scan()
    assert summary['categories']['artifacts']['modified'] == 1
    assert summary['categories']['exemptions']['removed'] == 0
    assert set(summary['errors']) == {'exemptions'}
    assert len(changes) == 1
    assert next(iter(snapshot(exemptions)['exemptions'].values()))['issue_text'] == 'original'


def test_scan_timeout_keeps_baseline_and_surfaces_both_failures(tmp_path, monkeypatch):
    updates, exemptions = tmp_path / 'updates.db', tmp_path / 'exemptions.db'
    update_db(updates, [('a', 'old.zip')])
    exemption_db(exemptions, 'original')
    monkeypatch.setattr(scanning.update_api, 'DB_PATH', updates)
    monkeypatch.setattr(scanning.mainline_api, 'DB_PATH', exemptions)
    monkeypatch.setattr(scanning, 'settings', SimpleNamespace(data_root=tmp_path, project_root=tmp_path))
    monkeypatch.setattr(scanning, 'run_local_command', lambda *_, **__: CommandResult(code=-1, stderr='Command timed out'))
    summary, changes = scanning.full_scan()
    assert set(summary['errors']) == {'updates', 'exemptions'}
    assert not changes
    assert next(iter(snapshot(updates)['artifacts'].values()))['file_name'] == 'old.zip'


@pytest.mark.parametrize('tasks,expected_sources', [
    (['packages'], {'gms_downloads'}),
    (['packages', 'artifacts'], {'cts_downloads', 'vts_downloads', 'gts_downloads', 'gms_downloads'}),
])
def test_selected_tasks_filter_notifications_and_deduplicate_shared_sources(tmp_path, monkeypatch, tasks, expected_sources):
    updates = tmp_path / 'updates.db'
    update_db(updates, [('a', 'old.zip')])

    def package(path, name):
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS gms_update_packages(source_key TEXT,item_key TEXT,file_name TEXT)')
            conn.execute('DELETE FROM gms_update_packages')
            conn.execute('INSERT INTO gms_update_packages VALUES(?,?,?)', ('gms_downloads', 'p', name))

    package(updates, 'old-package.zip')
    monkeypatch.setattr(scanning.update_api, 'DB_PATH', updates)
    monkeypatch.setattr(scanning.mainline_api, 'DB_PATH', tmp_path / 'must-not-scan.db')
    monkeypatch.setattr(scanning, 'settings', SimpleNamespace(data_root=tmp_path, project_root=tmp_path))
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        assert 'features.system.update_monitor.cli' in argv
        sources = [argv[index + 1] for index, value in enumerate(argv) if value == '--source']
        assert set(sources) == expected_sources
        assert len(sources) == len(expected_sources)
        target = Path(argv[argv.index('--db') + 1])
        update_db(target, [('a', 'new.zip')], sources=len(expected_sources))
        package(target, 'new-package.zip')
        return CommandResult()

    monkeypatch.setattr(scanning, 'run_local_command', command)
    summary, changes = scanning.full_scan(tasks=tasks)
    assert len(calls) == 1
    assert set(summary['categories']) == set(tasks)
    assert {item['category'] for item in changes} == set(tasks)
    assert summary['errors'] == {}


def test_exemptions_only_never_runs_gms_scanner(tmp_path, monkeypatch):
    exemptions = tmp_path / 'exemptions.db'
    exemption_db(exemptions, 'original')
    updates = tmp_path / 'must-not-create.db'
    monkeypatch.setattr(scanning.update_api, 'DB_PATH', updates)
    monkeypatch.setattr(scanning.mainline_api, 'DB_PATH', exemptions)
    monkeypatch.setattr(scanning, 'settings', SimpleNamespace(data_root=tmp_path, project_root=tmp_path))

    def command(argv, **kwargs):
        assert 'features.system.mainline_issues.cli' in argv
        exemption_db(Path(argv[argv.index('--db') + 1]), 'updated')
        return CommandResult(stdout='total_pages=1 pages_scanned=1 pages_skipped=0')

    monkeypatch.setattr(scanning, 'run_local_command', command)
    summary, changes = scanning.full_scan(tasks=['exemptions'])
    assert not updates.exists()
    assert summary['tasks'] == ['exemptions']
    assert summary['changes_total'] == 1
    assert changes[0]['after']['issue_text'] == 'updated'


def test_running_scan_freezes_task_selection(store):
    store.save_settings(config(tasks=['packages']))
    run_id = store.claim('manual')
    store.save_settings(config(tasks=['exemptions']))
    assert store.run(run_id)['config']['tasks'] == ['packages']
    assert store.settings()['tasks'] == ['exemptions']


def test_email_failure_is_independent_and_retry_does_not_rescan(store):
    store.save_settings(config(notify='always'))
    mailer = Mock(return_value={'sent': False, 'error': 'SMTP unavailable'})
    radar = service(store, mailer=mailer)
    run_id = store.claim('manual', owner_id='admin-id')
    radar.execute(run_id)
    assert store.run(run_id)['status'] == 'completed'
    assert store.run(run_id)['email_status'] == 'failed'
    assert mailer.call_args.kwargs['channel'] == 'update_radar'
    radar.notify(run_id)
    assert mailer.call_count == 1
    cfg = config()
    cfg['recipients'] = ['new@example.test']
    store.save_settings(cfg)
    mailer.return_value = {'sent': True}
    radar.notify(run_id, retry=True)
    assert mailer.call_count == 2
    assert mailer.call_args.args[0] == ['new@example.test']
    assert store.run(run_id)['email_status'] == 'sent'
    assert store.history()['total'] == 1


@pytest.mark.parametrize('notify,summary,expected', [
    ('always', result(), 'sent'), ('changes', result(), 'skipped'),
    ('changes', result(changes=1), 'sent'), ('changes', result(errors={'updates': '登录失效'}), 'sent'),
])
def test_notification_policies_include_scan_errors(store, notify, summary, expected):
    store.save_settings(config(notify=notify))
    radar = service(store, summary)
    run_id = store.claim('manual')
    radar.execute(run_id)
    assert store.run(run_id)['email_status'] == expected


def test_restart_preserves_completed_scan_and_marks_uncertain_delivery(store):
    run_id = store.claim('manual')
    store.finish(run_id, 'completed', result(), [])
    store.mark_email(run_id, 'sending')
    store.recover()
    assert store.run(run_id)['status'] == 'completed'
    assert store.run(run_id)['email_status'] == 'unknown'


def test_background_scan_claim_prevents_overlap_and_finishes_history(store):
    entered, proceed = threading.Event(), threading.Event()
    radar = service(store)

    def scan(**kwargs):
        entered.set()
        assert proceed.wait(5)
        return result(), []

    radar.scanner = scan
    run_id = radar.start_scan()
    try:
        assert entered.wait(5)
        with pytest.raises(ApiError) as raised:
            radar.start_scan()
        assert '正在进行中' in raised.value.message
    finally:
        proceed.set()
        radar.worker.join(5)
    assert store.run(run_id)['status'] == 'completed'
    assert store.run(run_id)['email_status'] == 'skipped'
    assert store.history()['total'] == 1


def test_email_contains_field_differences_and_plain_text_upstream_evidence(store):
    store.save_settings(config(dashboard_url='https://controller.example.test/gms-update-radar'))
    run_id = store.claim('manual')
    changes = [{'category': 'packages', 'key': 'a', 'type': 'modified',
                'before': {'file_name': 'old.zip'}, 'after': {'file_name': '<script>alert(1)</script>'}}]
    store.finish(run_id, 'completed', result(changes=1), changes)
    subject, body = service(store).preview(run_id)
    assert '1 项变更' in subject
    assert 'old.zip' in body and '<script>alert(1)</script>' in body
    assert '?run=' in body and 'GMS包更新' in body


@pytest.fixture
def client(store, monkeypatch):
    from features.auth import access

    monkeypatch.setattr(access, 'authentication_required', lambda: True)
    monkeypatch.setattr(access, 'is_elevated', lambda _: False)

    def principal(request):
        role = request.headers.get('x-test-role')
        return CurrentUser(id=role, username=role, role=role) if role else None

    monkeypatch.setattr(access, 'get_authenticated_user', principal)
    monkeypatch.setattr(api, 'get_service', lambda: service(store))
    app = FastAPI()
    register_api_error_handler(app)
    app.include_router(api.router)
    return TestClient(app)


def test_api_requires_auth_and_admin_for_settings_scan_and_mail(client, store):
    assert client.get('/api/gms-update-radar/runs').status_code == 401
    assert client.get('/api/gms-update-radar/runs', headers={'x-test-role': 'user'}).status_code == 200
    for method, path in [('get', '/settings'), ('put', '/settings'), ('post', '/scan'), ('post', '/runs/1/email')]:
        assert getattr(client, method)('/api/gms-update-radar' + path, headers={'x-test-role': 'user'}).status_code == 403
    assert store.history()['total'] == 0


def test_admin_configuration_roundtrip_and_private_recipient_masking(client, store):
    response = client.put('/api/gms-update-radar/settings', json=config(enabled=True), headers={'x-test-role': 'admin'})
    assert response.status_code == 200
    assert response.json()['data']['next_run_at']
    assert client.get('/api/gms-update-radar/status', headers={'x-test-role': 'user'}).json()['data'].get('recipients') is None
    run_id = store.claim('manual')
    store.finish(run_id, 'completed', result(), [])
    detail = client.get(f'/api/gms-update-radar/runs/{run_id}', headers={'x-test-role': 'user'})
    assert 'recipients' not in detail.text
    assert client.put('/api/gms-update-radar/settings', json={'enabled': True}, headers={'x-test-role': 'admin'}).status_code == 422
