"""Unified source scanning, idempotence, partial coverage and lock tests."""
import sys
from collections import Counter
from contextlib import closing
from dataclasses import replace

import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import html

from features.gms import cli, scanner, service
from features.gms.api import router
from features.gms.fetching import canonical_url
from features.gms.mainline_records import upsert_issues
from features.gms.parsers.mainline import KnownIssue, ReleasePage
from features.gms.repository import connect
from features.gms.sources import SOURCES, sources_for_tasks
from foundation.error_model import register_api_error_handler
from foundation.process_sync import release_sync_lock, try_acquire_sync_lock


CTS=next(s.url for s in SOURCES if s.key=='cts_downloads')
INDEX=next(s.url for s in SOURCES if s.key=='mainline_preload')
PATH='/mainline/release/release-notes/2026/nov/notes-PRELOAD-2026-11-01'
PAGE='https://docs.partner.android.com'+PATH
OTHER=PAGE+'-other'
VTS=next(s.url for s in SOURCES if s.key=='vts_downloads')


def doc(content):
    return '<html><article class="devsite-article">'+content+'</article></html>'


def cts(name='android-cts-17_r1-linux_x86-arm.zip'):
    return doc('<h2>Android 17</h2><p><a href="https://example.test/'+name+'">CTS 17 R1 ARM</a></p>')


def index(extra=False):
    return doc('<nav class="devsite-book-nav"><li><span class="devsite-nav-title-no-path">2026 Build Release Notes</span>'
               '<ul class="devsite-nav-section"><li><a href="'+PATH+'">November</a></li>'+
               ('<li><a href="'+PATH+'-other">Other</a></li>' if extra else '')+'</ul></li></nav>')


def release():
    return doc('''<table><tr><td>Preload partner zip</td><td>123456</td></tr></table>
        <h2>Android</h2><ul><li><strong>CTS known issues:</strong><ul>
        <li>[17][test] <code>Module</code> internal bug ref. 123456
        <ul><li><code>pkg.Test#testFoo</code></li></ul></li></ul></li></ul>
        <h2>Security patches in this Mainline release</h2><table>
        <tr><th>CVE</th><th>Module</th><th>Version</th></tr>
        <tr><td>CVE-2026-1234, CVE-2026-5678</td><td>Media</td><td>17</td></tr></table>''')


class Session:
    def __init__(self, pages):
        self.pages={canonical_url(k):v for k,v in pages.items()}
        self.calls=Counter()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        pass

    def get(self,url,**kwargs):
        url=canonical_url(url)
        self.calls[url]+=1
        status,body=self.pages.get(url,(404,''))
        response=requests.Response()
        response.status_code=status
        response.url=url
        response.encoding='utf-8'
        response._content=body.encode('utf-8')
        return response


def scan(db,pages,keys,mode='full'):
    session=Session(pages)
    result=scanner.run_scan(db,mode,keys,session_factory=lambda _:session)
    return result,session


def test_full_scan_uses_one_database_and_fetches_mainline_once(tmp_path):
    path=tmp_path/'gms_update.sqlite3'
    result,session=scan(path,{CTS:(200,cts()),INDEX:(200,index()),PAGE:(200,release())},['cts_downloads','mainline_preload'])
    assert result['state']=='completed',result
    assert session.calls[canonical_url(PAGE)]==1
    assert not (tmp_path/'mainline_known_issues.sqlite3').exists()
    assert not (tmp_path/'gms_update_monitor.sqlite3').exists()
    with closing(connect(path)) as conn:
        for table,count in [('gms_update_artifacts',1),('gms_update_mainline_packages',1),
                            ('mainline_known_issues',1),('mainline_security_patches',2)]:
            assert conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==count
        assert {r[0] for r in conn.execute('SELECT origin FROM gms_update_change_events')}=={'baseline'}
    second,_=scan(path,{CTS:(200,cts()),INDEX:(200,index()),PAGE:(200,release())},['cts_downloads','mainline_preload'])
    assert second['state']=='completed' and second['changes']==0


def test_partial_source_preserves_verified_records_and_other_sources_update(tmp_path):
    path=tmp_path/'gms_update.sqlite3'
    pages={CTS:(200,cts()),INDEX:(200,index()),PAGE:(200,release())}
    first,_=scan(path,pages,['cts_downloads','mainline_preload'])
    assert first['state']=='completed',first
    pages={CTS:(200,cts('cts-new.zip')),INDEX:(200,index(extra=True)),PAGE:(200,release()),OTHER:(404,'')}
    result,_=scan(path,pages,['cts_downloads','mainline_preload','vts_downloads'])
    assert result['state']=='partial' and set(result['errors'])=={'mainline_preload','vts_downloads'}
    with closing(connect(path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM mainline_known_issues').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM mainline_security_patches').fetchone()[0]==2
        assert conn.execute('SELECT file_name FROM gms_update_artifacts').fetchone()[0]=='cts-new.zip'
        assert conn.execute("SELECT state FROM gms_update_source_results WHERE run_id=? AND source_key='mainline_preload'",(result['run_id'],)).fetchone()[0]=='partial'
        assert conn.execute('SELECT COUNT(*) FROM gms_update_documents WHERE run_id=? AND status_code=404',(result['run_id'],)).fetchone()[0]==2


def test_parse_failed_preload_does_not_publish_exemptions_or_cves(tmp_path):
    database = tmp_path / 'gms_update.sqlite3'
    second_path = PATH.replace('2026-11-01', '2026-11-02')
    second_url = PAGE.replace(PATH, second_path)
    index_html = index(extra=True).replace(PATH + '-other', second_path)
    first_html = release()
    verified_pages = {INDEX: (200, index_html), PAGE: (200, first_html), second_url: (200, first_html)}
    baseline, _ = scan(database, verified_pages, ['mainline_preload'])
    assert baseline['state'] == 'completed'

    incomplete = first_html.replace('Preload partner zip', 'Unverified build')
    incomplete = incomplete.replace('pkg.Test#testFoo', 'pkg.Test#unverified')
    incomplete = incomplete.replace('CVE-2026-1234', 'CVE-2026-9999')
    verified_pages[second_url] = (200, incomplete)
    partial, _ = scan(database, verified_pages, ['mainline_preload'])
    assert partial['state'] == 'partial'

    with closing(connect(database)) as conn:
        evidence = conn.execute(
            'SELECT state FROM gms_update_documents WHERE run_id=? AND url=?',
            (partial['run_id'], canonical_url(second_url)),
        ).fetchone()
        assert evidence['state'] == 'parse_failed'
        issues = {row['test_case'] for row in conn.execute('SELECT test_case FROM mainline_known_issues')}
        cves = {row['cve_id'] for row in conn.execute('SELECT cve_id FROM mainline_security_patches')}
        assert issues == {'pkg.Test#testFoo'}
        assert cves == {'CVE-2026-1234', 'CVE-2026-5678'}
        assert partial['changes'] == 0


def test_scan_api_returns_shared_error_envelopes(tmp_path, monkeypatch):
    monkeypatch.setattr(service, 'DB_PATH', tmp_path / 'gms_update.sqlite3')
    app = FastAPI()
    register_api_error_handler(app)
    app.include_router(router)

    with TestClient(app) as client:
        bad_source = client.post('/api/gms-update-center/scan', params={'source': 'unknown'})
        missing_run = client.get('/api/gms-update-center/scan/runs/999999')
    assert bad_source.status_code == 400
    assert bad_source.json() == {'success': False, 'error': 'unknown scan source', 'code': 'MALFORMED_REQUEST'}
    assert missing_run.status_code == 404
    assert missing_run.json() == {'success': False, 'error': 'scan run not found', 'code': 'NOT_FOUND'}


def test_incremental_checks_hash_without_generating_changes(tmp_path):
    path=tmp_path/'gms_update.sqlite3'
    pages={CTS:(200,cts())}
    first,_=scan(path,pages,['cts_downloads'])
    assert first['state']=='completed',first
    result,_=scan(path,pages,['cts_downloads'],'incremental')
    assert result['state']=='completed' and result['skipped']==1 and result['changes']==0


def test_direct_scan_contends_with_web_and_inherited_lock_stays_held(tmp_path,monkeypatch):
    path=tmp_path/'test.db'
    lock=path.with_suffix('.sync.lock')
    descriptor=try_acquire_sync_lock(lock)
    try:
        calls=[]
        def scan(*args,**kwargs):
            calls.append(args[0])
            return {'state':'completed'}
        monkeypatch.setattr(cli,'run_scan',scan)
        args=['scan','--db',str(path)]
        monkeypatch.setattr(sys,'argv',args)
        assert cli.main()==2 and not calls
        monkeypatch.setattr(sys,'argv',[*args,'--sync-lock-fd',str(descriptor)])
        assert cli.main()==0 and calls==[path]
        assert try_acquire_sync_lock(lock) is None
    finally:
        release_sync_lock(descriptor)


def test_read_only_audit_does_not_create_target_database(tmp_path,monkeypatch):
    path=tmp_path/'never.sqlite3'
    called=[]
    monkeypatch.setattr(cli,'run_scan',lambda db,*args,**kwargs:called.append(db) or {'state':'completed'})
    monkeypatch.setattr(sys,'argv',['scan','--db',str(path),'--audit'])
    assert cli.main()==0 and not path.exists()
    assert called[0]!=path and not called[0].exists()


def test_restart_reports_interrupted_state(tmp_path,monkeypatch):
    path=tmp_path/'gms_update.sqlite3'
    monkeypatch.setattr(service,'DB_PATH',path)
    with closing(connect(path)) as conn:
        run_id=scanner.begin_run(conn,'full',[SOURCES[0]],'manual')
    state=service.status()
    assert state['state']=='interrupted' and state['run_id']==run_id
    assert state['mode']=='full' and state['started_at'] and not state['running']
    assert state['returncode']==1 and state['error']


def test_mainline_three_tasks_share_one_source():
    assert sources_for_tasks(['mainline','exemptions','security_patches'])==['mainline_preload']


def test_exemption_identity_retains_distinct_android_versions(tmp_path):
    issue=KnownIssue('https://example.test/notes','Notes',2026,'Nov','Android','CTS','16','cat','M','C','E','text')
    with closing(connect(tmp_path/'gms_update.sqlite3')) as conn,conn:
        upsert_issues(conn,[issue,replace(issue,android_versions='17')],'now')
        assert [r[0] for r in conn.execute('SELECT android_versions FROM mainline_known_issues ORDER BY android_versions')]==['16','17']


def test_security_patch_row_preserves_all_cves_and_ignores_note_references():
    from features.gms.parsers.mainline import extract_security_patches

    page = ReleasePage(2026, 'Nov', 'https://example.test/notes')
    doc = html.fromstring('''<article class="devsite-article">
        <h2>Security patches in this Mainline release</h2>
        <table><tr><th>CVE</th><th>Module</th><th>Version</th></tr>
        <tr><td>CVE-2026-1234, CVE-2026-5678, CVE-2026-1234</td>
        <td>Media</td><td>16; reference CVE-2026-9999</td></tr>
        <tr><td>CVE-2026-5678</td><td>Network</td><td>17</td></tr></table>
        </article>''')
    rows = extract_security_patches(page, doc)
    assert [(r.cve_id, r.mainline_module) for r in rows] == [
        ('CVE-2026-1234','Media'), ('CVE-2026-5678','Media'), ('CVE-2026-5678','Network')]
    assert rows[0].version_note == '16; reference CVE-2026-9999'


def test_mainline_fingerprint_keeps_previous_encoding():
    import hashlib

    from features.gms.parsing import mainline_doc_hash, stable_doc_hash

    document = html.fromstring('<article class="devsite-article"><h2>Notes</h2><script>ignored</script><a href="/a">Link</a><table><tr><td rowspan="2">Patch</td></tr></table></article>')
    previous_payload = repr(('Notes Link Patch', ['/a'], [('h2', None, None), ('td', '2', None)]))
    assert mainline_doc_hash(document) == hashlib.sha256(previous_payload.encode('utf-8')).hexdigest()
    assert stable_doc_hash(document) != mainline_doc_hash(document)
