"""Scheduling, durable comparisons, and notification failures without external IO."""

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import html
from pydantic import ValidationError

from features.auth import CurrentUser
from features.gms.radar import api, scanning
from features.gms.radar.models import CATEGORIES, RadarConfig, next_scan
from features.gms.radar.repository import RadarStore
from features.gms.radar.scanning import batch_changes, inventory_counts
from features.gms.radar.service import RadarService
from foundation.error_model import ApiError, register_api_error_handler


def instant(text):
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def config(**changes):
    return RadarConfig(recipients=['team@example.test'], **changes).model_dump(mode='json')


@pytest.fixture
def store(tmp_path):
    return RadarStore(tmp_path / 'radar.sqlite3')


def test_history_reader_remains_read_only_after_database_replacement(store):
    with store.connect_readonly() as reader:
        assert reader.execute('SELECT COUNT(*) FROM radar_runs').fetchone()[0] == 0
    replacement = store.path.with_name('empty.sqlite3')
    replacement.touch()
    replacement.replace(store.path)
    with store.connect_readonly() as reader:
        assert reader.execute('SELECT COUNT(*) FROM radar_runs').fetchone()[0] == 0
        reader.execute('PRAGMA query_only=OFF')
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            reader.execute('DELETE FROM radar_settings')


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


def test_timezone_dst_fallback_schedules_only_once_per_local_date():
    cfg = config(enabled=True, timezone='America/New_York', scan_time='01:30')
    # First 01:30 is EDT (05:30 UTC); second 01:30 is EST (06:30 UTC).
    assert next_scan(cfg, instant('2026-11-01T05:20:00+00:00')) == '2026-11-01T05:30:00+00:00'
    # After the first occurrence, do not return a past instant or schedule
    # a second scan for the same local calendar day.
    assert next_scan(cfg, instant('2026-11-01T06:00:00+00:00')) == '2026-11-02T06:30:00+00:00'


def test_timezone_dst_nonexistent_wall_time_rolls_forward():
    cfg = config(enabled=True, timezone='America/New_York', scan_time='02:30')
    # Spring-forward date has no 02:30; its first forward mapping is 03:30.
    assert next_scan(cfg, instant('2026-03-08T06:00:00+00:00')) == '2026-03-08T07:30:00+00:00'


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
            CREATE TABLE IF NOT EXISTS gms_update_change_events (
                id INTEGER PRIMARY KEY,run_id INTEGER,entity_type TEXT,change_type TEXT,before_json TEXT,after_json TEXT);
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


def test_radar_counts_added_modified_removed_and_ignores_bookkeeping(tmp_path):
    path = tmp_path / 'updates.db'
    update_db(path, [('kept', 'old.zip'), ('removed', 'removed.zip')])
    before = inventory_counts(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE gms_update_artifacts SET last_seen_at='later',content_hash='new-hash'")
    assert batch_changes(path, 1, before, inventory_counts(path), ['artifacts'])[1] == []
    update_db(path, [('kept', 'new.zip'), ('added', 'added.zip')])

    def record(key, name):
        return json.dumps({'source_key': 'cts_downloads', 'item_key': key, 'file_name': name})

    with sqlite3.connect(path) as conn:
        conn.executemany(
            'INSERT INTO gms_update_change_events(run_id,entity_type,change_type,before_json,after_json) '
            "VALUES(1,'artifact',?,?,?)",
            [('changed', record('kept', 'old.zip'), record('kept', 'new.zip')),
             ('added', '', record('added', 'added.zip')),
             ('removed', record('removed', 'removed.zip'), '')])
    counts, changes = batch_changes(path, 1, before, inventory_counts(path), ['artifacts'])
    assert counts['artifacts'] == {'before': 2, 'after': 2, 'added': 1, 'modified': 1, 'removed': 1}
    assert {item['type'] for item in changes} == {'added', 'modified', 'removed'}
    modified = next(item for item in changes if item['type'] == 'modified')
    assert modified['before']['file_name'] == 'old.zip'
    assert modified['after']['file_name'] == 'new.zip'


def test_incomplete_mainline_scan_retains_existing_database_and_reports_scope(tmp_path, monkeypatch):
    db=tmp_path/'gms_update.sqlite3'
    update_db(db,[('a','old.zip')])
    exemption_db(db,'original')
    monkeypatch.setattr(scanning.service,'DB_PATH',db)

    def scan(mode, sources, **kwargs):
        assert mode=='full'
        assert len(sources)==7 and len(set(sources))==7
        update_db(db,[('a','new.zip')])
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO gms_update_change_events VALUES(1,1,?,?,?,?)',
                ('artifact', 'changed',
                 json.dumps({'source_key': 'cts_downloads', 'item_key': 'a', 'file_name': 'old.zip'}),
                 json.dumps({'source_key': 'cts_downloads', 'item_key': 'a', 'file_name': 'new.zip'})))
        return {'id':1,'state':'partial','sources':[
            {'source_key':source,'state':'partial' if source=='mainline_preload' else 'ready',
             'error':'404' if source=='mainline_preload' else '', 'origin':'source_changed'}
            for source in sources]}
    monkeypatch.setattr(scanning.service,'execute_reserved',scan)
    summary,changes=scanning.full_scan()
    assert summary['categories']['artifacts']['modified']==1
    assert summary['categories']['exemptions']['removed']==0
    assert set(summary['errors'])=={'mainline_preload'}
    assert set(summary['category_errors'])=={'mainline','exemptions','security_patches'}
    assert len(changes)==1
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT issue_text FROM mainline_known_issues').fetchone()[0] == 'original'


def test_radar_projects_only_committed_batch_events(tmp_path):
    db = tmp_path / 'updates.db'
    update_db(db, [('a', 'old.zip'), ('b', 'old-other.zip')])
    before = inventory_counts(db)
    update_db(db, [('a', 'new.zip'), ('b', 'new-other.zip')])
    after = inventory_counts(db)
    with sqlite3.connect(db) as conn:
        for run_id, key in ((9, 'a'), (8, 'b')):
            old = {'source_key': 'cts_downloads', 'item_key': key, 'file_name': 'old.zip'}
            new = {**old, 'file_name': 'new.zip', 'content_hash': 'ignored'}
            conn.execute(
                'INSERT INTO gms_update_change_events(run_id,entity_type,change_type,before_json,after_json) '
                'VALUES(?,?,?,?,?)', (run_id, 'artifact', 'changed', json.dumps(old), json.dumps(new)))
    counts, changes = batch_changes(db, 9, before, after, ['artifacts'])
    assert counts['artifacts'] == {'before': 2, 'after': 2, 'added': 0, 'modified': 1, 'removed': 0}
    assert len(changes) == 1
    assert changes[0]['key'].startswith('gms_update_artifacts:')
    assert changes[0]['before']['item_key'] == 'a'
    assert changes[0]['after']['item_key'] == 'a'
    assert 'content_hash' not in changes[0]['after']
    assert batch_changes(db, 9, before, after, ['packages'])[1] == []


def test_scan_timeout_does_not_claim_success(tmp_path,monkeypatch):
    db=tmp_path/'gms_update.sqlite3'
    update_db(db,[('a','old.zip')])
    monkeypatch.setattr(scanning.service,'DB_PATH',db)
    def scan(*args,**kwargs):
        raise RuntimeError('Command timed out')
    monkeypatch.setattr(scanning.service,'execute_reserved',scan)
    with pytest.raises(RuntimeError,match='timed out'):
        scanning.full_scan()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT file_name FROM gms_update_artifacts').fetchone()[0] == 'old.zip'


@pytest.mark.parametrize('tasks,expected_sources',[
    (['packages'],{'gms_downloads'}),
    (['packages','artifacts'],{'cts_downloads','vts_downloads','gts_downloads','gms_downloads'}),
    (['mainline','exemptions','security_patches'],{'mainline_preload'}),
])
def test_selected_tasks_deduplicate_shared_sources(tmp_path,monkeypatch,tasks,expected_sources):
    db=tmp_path/'gms_update.sqlite3'
    update_db(db,[('a','old.zip')])
    monkeypatch.setattr(scanning.service,'DB_PATH',db)
    calls=[]
    def scan(mode,sources,**kwargs):
        calls.append(sources)
        assert set(sources)==expected_sources and len(sources)==len(expected_sources)
        return {'id':1,'state':'completed','sources':[
            {'source_key':source,'state':'ready','error':'','origin':'baseline'} for source in sources]}
    monkeypatch.setattr(scanning.service,'execute_reserved',scan)
    summary,changes=scanning.full_scan(tasks=tasks)
    assert len(calls)==1 and set(summary['categories'])==set(tasks)
    assert summary['errors']=={} and changes==[]


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


def test_late_worker_finish_cannot_overwrite_recovered_interrupted_scan(store):
    run_id = store.claim('manual')
    store.recover()
    assert store.run(run_id)['status'] == 'interrupted'
    store.finish(run_id, 'completed', result(changes=1), [
        {'category': 'packages', 'key': 'late', 'type': 'modified',
         'before': {}, 'after': {'file_name': 'late.zip'}}
    ])
    assert store.run(run_id)['status'] == 'interrupted'
    assert store.changes(run_id)['total'] == 0
    # Neither a duplicated callback nor a conflicting terminal result can
    # append evidence to a scan that has already been recovered.
    store.finish(run_id, 'failed', result(), [])
    assert store.run(run_id)['status'] == 'interrupted'


def test_mail_summary_generation_failure_is_recorded_without_sending(store, monkeypatch):
    from features.gms.radar import service as radar_module

    # A scan freezes its notification config on claim. Configure recipients
    # before the claim so this tests rendering failures, not unconfigured mail.
    store.save_settings(config(notify='always'))
    run_id = store.claim('manual')
    store.finish(run_id, 'completed', result(changes=1), [])
    mailer = Mock(return_value={'sent': True})

    def fail_preview(*_args):
        raise ValueError('mail summary data invalid')

    monkeypatch.setattr(radar_module, 'build_email', fail_preview)
    radar_module.RadarService(
        store, scanner=lambda **_: (result(), []),
        mailer=mailer, reserve=lambda: None, release=lambda _=None: None,
    ).notify(run_id)
    assert store.run(run_id)['email_status'] == 'failed'
    assert 'mail summary data invalid' in store.run(run_id)['email_error']
    mailer.assert_not_called()


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


def _assert_mail_table_alignment(html_body, expected):
    tables = html.fromstring(html_body).xpath('//table')
    assert len(tables) == len(expected)
    for table, alignments in zip(tables, expected, strict=True):
        for row in table.xpath('./thead/tr | ./tbody/tr'):
            cells = row.xpath('./th | ./td')
            assert len(cells) == len(alignments)
            for cell, align in zip(cells, alignments, strict=True):
                assert cell.get('align') == align
                assert f'text-align:{align};' in cell.get('style')


def test_html_radar_email_uses_tables_highlights_changes_and_escapes_upstream(store):
    store.save_settings(config(dashboard_url='https://controller.example.test/gms-update-radar'))
    run_id = store.claim('manual')
    counts = result(changes=3, errors={'exemptions': '404: <script>bad()</script>'})
    counts['categories']['packages'].update(added=1, modified=1, removed=1)
    changes = [
        {'category': 'packages', 'key': 'one', 'type': 'added',
         'before': None, 'after': {'file_name': 'new.apk', 'description': '<script>alert(1)</script>'}},
        {'category': 'packages', 'key': 'two', 'type': 'modified',
         'before': {'file_name': 'old.apk', 'description': 'older'},
         'after': {'file_name': 'old.apk', 'description': 'newer'}},
        {'category': 'packages', 'key': 'three', 'type': 'removed',
         'before': {'file_name': 'removed.apk'}, 'after': None},
    ]
    store.finish(run_id, 'partial', counts, changes)
    mailer = Mock(return_value={'sent': True})
    radar = service(store, mailer=mailer)
    html_body = radar.preview_html(run_id)
    assert '<table' in html_body and html_body.count('<table') >= 3
    assert 'GMS包更新' in html_body and '保留旧数据' in html_body
    assert 'new.apk' in html_body and 'removed.apk' in html_body
    assert '#137a4b' in html_body and '#a26608' in html_body and '#b4233b' in html_body
    assert '<script>alert(1)</script>' not in html_body
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html_body
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html_body
    assert 'values_json' not in html_body and 'headers_json' not in html_body
    assert '?run=' in html_body
    _assert_mail_table_alignment(html_body, [
        ('left', 'left', 'left', 'left', 'right'),
        ('left', 'right', 'right', 'right', 'right', 'right', 'left'),
        ('left', 'left', 'left'),
        ('left', 'left', 'left', 'left'),
    ])
    radar.notify(run_id)
    assert mailer.call_args.kwargs['is_html'] is True
    assert '<table' in mailer.call_args.args[2]


def test_html_radar_email_missing_category_uses_same_numeric_alignment(store):
    run_id = store.claim('manual')
    summary = result()
    summary['categories'].pop('requirements')
    store.finish(run_id, 'completed', summary, [])
    html_body = service(store).preview_html(run_id)
    _assert_mail_table_alignment(html_body, [
        ('left', 'left', 'left', 'left', 'right'),
        ('left', 'right', 'right', 'right', 'right', 'right', 'left'),
    ])
    row = html.fromstring(html_body).xpath('//table[2]/tbody/tr[td="GMS要求"]')[0]
    assert [cell.text_content() for cell in row.xpath('./td')] == [
        'GMS要求', '—', '—', '—', '—', '—', '无可用数据',
    ]


def test_html_radar_email_no_changes_shows_uncertainty_not_false_clean(store):
    run_id = store.claim('manual')
    store.finish(run_id, 'partial', result(errors={'exemptions': '404 Not Found'}), [])
    html_body = service(store).preview_html(run_id)
    assert '0 项变更不代表所有来源均无更新' in html_body
    assert '失败组保留扫描前的数据' in html_body
    assert '变更明细（最多 20 条）' not in html_body


def test_html_radar_email_renders_requirement_table_as_business_fields(store):
    run_id = store.claim('manual')
    store.finish(run_id, 'completed', result(changes=1), [
        {'category': 'requirements', 'key': 'r', 'type': 'added', 'before': None,
         'after': {'row_key': 'opaque', 'source_key': 'requirements',
                   'headers_json': '["Name", "Type", "Package name", "Description"]',
                   'values_json': '["TrichromeLibrary", "APK", "com.google.android.trichromelibrary", "Browser library"]'}}
    ])
    html_body = service(store).preview_html(run_id)
    assert 'TrichromeLibrary' in html_body
    assert 'com.google.android.trichromelibrary' in html_body
    assert 'headers_json' not in html_body
    assert 'values_json' not in html_body


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


def test_status_exposes_read_only_scan_capability_without_admin_settings_probe(client, store, monkeypatch):
    status_url = '/api/gms-update-radar/status'
    forbidden = client.get(status_url, headers={'x-test-role': 'user'})
    assert forbidden.status_code == 200
    assert forbidden.json()['data']['can_manage'] is False
    assert forbidden.headers['cache-control'] == 'no-store'
    assert 'recipients' not in forbidden.json()['data']

    admin = client.get(status_url, headers={'x-test-role': 'admin'})
    assert admin.status_code == 200
    assert admin.json()['data']['can_manage'] is True

    # A successful temporary admin verification also grants admin-only routes.
    from features.auth import access
    monkeypatch.setattr(access, 'is_elevated', lambda _: True)
    monkeypatch.setattr(api, 'is_elevated', lambda _: True)
    elevated = client.get(status_url, headers={'x-test-role': 'user'})
    assert elevated.json()['data']['can_manage'] is True
    assert client.get('/api/gms-update-radar/settings',
                      headers={'x-test-role': 'user'}).status_code == 200

    # Preserve anonymous development mode semantics (no global login).
    monkeypatch.setattr(access, 'authentication_required', lambda: False)
    development = client.get(status_url)
    assert development.status_code == 200
    assert development.json()['data']['can_manage'] is True
    assert client.get(status_url, headers={'x-test-role': 'user'}).json()['data']['can_manage'] is True


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


def test_start_scan_rejected_once_shutdown_begins(store):
    radar = service(store)
    radar.closing.set()
    with pytest.raises(ApiError) as raised:
        radar.start_scan()
    assert '停止' in raised.value.message


def test_shutdown_cancels_running_scan_forwards_event_and_persists_interrupted(store):
    entered, release = threading.Event(), threading.Event()
    radar = service(store)
    seen = {}

    def scan(**kwargs):
        seen.update(kwargs)
        entered.set()
        assert release.wait(5)
        raise InterruptedError('服务停止，本组未完成扫描，保留原数据')

    radar.scanner = scan
    run_id = radar.start_scan()
    try:
        assert entered.wait(5)
        # stop_radar 先置 closing（即 cancel_event），worker 以 interrupted 收尾。
        radar.closing.set()
    finally:
        release.set()
        radar.worker.join(5)
    assert seen['cancel_event'] is radar.closing
    assert seen['cancelled'] == radar.closing.is_set  # bound method: ==，不用 is
    assert store.run(run_id)['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_shutdown_joins_worker_off_event_loop_before_recovering(monkeypatch):
    from features.gms.radar import service as radar_module

    loop_thread = threading.get_ident()
    closing = threading.Event()
    events = []

    def join(timeout):
        assert timeout == 30
        assert closing.is_set()
        assert threading.get_ident() != loop_thread
        events.append('joined')

    worker = SimpleNamespace(is_alive=Mock(side_effect=[True, False]), join=join)
    radar = SimpleNamespace(closing=closing, worker=worker,
                            store=SimpleNamespace(recover=lambda: events.append('recovered')))
    monkeypatch.setattr(radar_module, 'get_service', lambda: radar)
    task = asyncio.create_task(asyncio.sleep(60))
    await radar_module.stop_radar(task)
    assert task.cancelled()
    assert events == ['joined', 'recovered']


def test_change_truncation_and_run_retention(store, monkeypatch):
    from features.gms.radar import repository

    monkeypatch.setattr(repository, 'MAX_CHANGES_PER_RUN', 2)
    monkeypatch.setattr(repository, 'RETAINED_RUNS', 3)
    monkeypatch.setattr(repository, 'RETAINED_DAYS', 0)
    first = store.claim('manual')
    store.finish(first, 'completed', {**result(), 'changes_total': 3},
                 [{'category': 'packages', 'key': str(index), 'type': 'modified',
                   'before': {}, 'after': {}} for index in range(3)])
    # 明细截断到阈值，真实总量与截断数保留在 summary。
    assert store.changes(first)['total'] == 2
    summary = store.run(first)['summary']
    assert summary['changes_total'] == 3 and summary['changes_truncated'] == 1
    latest = []
    for _ in range(5):
        run_id = store.claim('manual')
        store.finish(run_id, 'completed', result(), [])
        latest.append(run_id)
    # 保留策略只留最近 3 个终态运行，孤儿明细一并清理。
    assert store.history()['total'] == 3
    assert sorted(item['id'] for item in store.history()['items']) == latest[-3:]
    for run_id in latest[-3:]:
        assert store.run(run_id)['status'] == 'completed'
    with pytest.raises(ApiError):
        store.run(first)
    with pytest.raises(ApiError):
        store.run(latest[0])


def test_radar_reservation_uses_the_single_manual_process_lock(tmp_path,monkeypatch):
    from foundation.process_sync import release_sync_lock, sync_lock_held, try_acquire_sync_lock
    db=tmp_path/'gms_update.sqlite3'
    monkeypatch.setattr(scanning.service,'DB_PATH',db)
    path=db.with_suffix('.sync.lock')
    manual=try_acquire_sync_lock(path)
    try:
        with pytest.raises(ApiError):
            scanning.reserve_scanners()
    finally:
        release_sync_lock(manual)
    scanning.reserve_scanners()
    try:
        assert sync_lock_held(path)
        with pytest.raises(ApiError):
            scanning.service.reserve()
    finally:
        scanning.release_scanners()
    assert not sync_lock_held(path)


def test_recovery_preserves_other_process_scan_and_thread_start_failure_releases_locks(store, monkeypatch):
    from foundation.process_sync import release_sync_lock, sync_lock_held, try_acquire_sync_lock

    descriptor = try_acquire_sync_lock(store.scan_lock)
    try:
        run_id = store.claim("manual")
        RadarStore(store.path).recover()
        assert store.run(run_id)["status"] == "running"
    finally:
        release_sync_lock(descriptor)
    store.recover()
    radar = service(store)
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("thread limit")))
    with pytest.raises(RuntimeError, match="thread limit"):
        radar.start_scan()
    assert store.history()["items"][0]["status"] == "failed"
    assert not sync_lock_held(store.scan_lock)


def test_calendar_period_rollup_deduplicates_changes_and_reports_missing_days(store):
    cfg = config(enabled=True, tasks=['packages'])
    store.save_settings(cfg, now=instant('2026-10-01T00:00:00+00:00'))
    for day, before, after in ((1, 'r1', 'r2'), (3, 'r2', 'r3')):
        now = instant(f'2026-11-{day:02d}T04:00:00+00:00')
        run_id = store.claim('manual', now=now)
        store.finish(run_id, 'completed', {**result(changes=1), 'tasks': ['packages']}, [
            {'category': 'packages', 'key': 'p', 'type': 'modified',
             'before': {'version': before}, 'after': {'version': after}},
        ], now=now)
    now = instant('2026-12-01T00:00:00+00:00')
    ids = store.prepare_periods(now)
    report = next(store.run(i) for i in ids if store.run(i)['trigger'] == 'monthly')
    assert report['summary']['period']['start'] == '2026-11-01'
    assert report['summary']['period']['end'] == '2026-11-30'
    assert report['summary']['categories']['packages']['days_scanned'] == 2
    assert report['summary']['changes_total'] == 1
    assert report['summary']['events_total'] == 2
    assert report['status'] == 'partial'
    item = store.changes(report['id'])['items'][0]
    assert item['before_json'] == {'version': 'r1'}
    assert item['after_json'] == {'version': 'r3'}
    assert RadarStore(store.path).prepare_periods(now) == []
    assert store.history(kind='monthly')['total'] == 1
    assert store.history(kind='daily')['total'] == 2
    radar = service(store)
    subject, _body = radar.preview(report['id'])
    assert '月报' in subject and '覆盖不完整' in subject
    html_body = radar.preview_html(report['id'])
    assert '2026-11-30' in html_body
    _assert_mail_table_alignment(html_body, [
        ('left', 'right', 'right', 'right', 'right', 'right', 'right', 'right', 'left'),
        ('left', 'left', 'left', 'left'),
    ])
    radar.notify(report['id'])
    assert store.run(report['id'])['email_status'] == 'sent'


def test_period_boundaries_use_local_calendar_and_schedule_time():
    from features.gms.radar.periods import closed_periods

    cfg = config(enabled=True)
    before = dict((kind, (start, end)) for kind, start, end in closed_periods(
        cfg, instant('2026-11-30T19:59:00+00:00')))
    assert 'monthly' not in before
    after = dict((kind, (start, end)) for kind, start, end in closed_periods(
        cfg, instant('2026-11-30T20:00:00+00:00')))
    assert after['monthly'] == (date(2026, 11, 1), date(2026, 12, 1))
