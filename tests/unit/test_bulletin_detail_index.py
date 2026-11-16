"""Critical Fix detail content stays attributable, searchable and recoverable."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from lxml import html

from features.system import recall_mainline_references
from features.system.mainline_issues import api as mainline_api
from features.system.mainline_issues.repository import init_db as init_mainline_db
from features.system.update_monitor import api as monitor_api
from features.system.update_monitor.models import SOURCES, FetchedDocument
from features.system.update_monitor.parsers import parse_gms_bulletins
from features.system.update_monitor.parsing import matched_excerpt
from features.system.update_monitor.repository import (
    bulletin_detail_coverage,
    init_db,
    query_bulletin_patches,
    sync_source,
)
from features.system.update_radar.snapshots import compare, snapshot


SOURCE = next(source for source in SOURCES if source.key == 'gms_bulletins')
OVERVIEW = '''<article class="devsite-article"><h2>2026 bulletins</h2><table>
<tr><td>A-568181732 Bluetooth fix</td><td><a href="/partners/releases/cf/nov-2026#568181732">November</a></td><td>2027-03-26</td></tr>
<tr><td>A-5681817329 Different fix</td><td><a href="/partners/releases/cf/nov-2026#5681817329">November</a></td><td>2027-03-26</td></tr>
</table></article>'''


def detail(cve='CVE-2026-58835'):
    return f'''<html><body><article class="devsite-article">
<h2>Mandatory functional patches</h2>
<h3>A-5681817329 Different fix</h3><p>Unrelated CVE-2026-99999 for Android 16.</p>
<h3>A-568181732 Bluetooth fix</h3><p>{'padding ' * 1800}</p>
<p>处理{cve}的修复 ag/40830952 for Android 17.</p>
<p><a href="https://example.com/change/40830952">Source patch</a></p>
<h2>Other references</h2><p>CVE-2026-11111 unrelated footer</p>
</article></body></html>'''


class Session:
    def __init__(self, text=None, status=200):
        self.text = detail() if text is None else text
        self.status = status
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        return SimpleNamespace(status_code=self.status, url=url, text=self.text)


def fetched(text=OVERVIEW):
    return FetchedDocument(SOURCE, html.fromstring(text), 'Critical Fix', 'unchanged-overview', 200, SOURCE.url)


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setattr('features.system.update_monitor.parsers.time.sleep', lambda *_: None)
    path = tmp_path / 'gms_update_monitor.sqlite3'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    conn.commit()
    yield path, conn
    conn.close()


def scan(conn, session):
    result = sync_source(conn, 1, fetched(), force=False, timestamp='2026-11-16T03:00:00+00:00', session=session)
    conn.commit()
    return result


def test_details_are_scoped_to_the_exact_patch_and_month_fetch_is_deduplicated(index):
    _path, _conn = index
    session = Session()
    rows = parse_gms_bulletins(fetched(), session=session).bulletin_patches
    patch, neighbour = rows
    assert patch.patch_id == 'A-568181732'
    assert 'CVE-2026-58835' in patch.detail_text and 'Android 17' in patch.android_versions
    assert 'https://example.com/change/40830952' in patch.detail_text
    assert patch.detail_status == 'ready' and patch.detail_fetched_at
    assert 'CVE-2026-99999' not in patch.detail_text
    assert 'CVE-2026-11111' not in patch.detail_text
    assert 'CVE-2026-58835' not in neighbour.detail_text
    assert len(session.calls) == 1


def test_agent_recalls_cve_only_in_detail_tail_with_a_matching_excerpt(index):
    path, conn = index
    scan(conn, Session())
    result = recall_mainline_references(['CVE-2026-58835'], data_root=path.parent)['sources']['critical_patches']
    assert result['status'] == 'matched' and result['coverage_complete']
    assert len(result['items']) == 1
    row = result['items'][0]
    assert row['patch_id'] == 'A-568181732'
    assert 'CVE-2026-58835' in row['detail_excerpt'] and len(row['detail_excerpt']) <= 4000
    assert row['detail_content_sha256']
    by_bug = recall_mainline_references(['ag/40830952'], data_root=path.parent)['sources']['critical_patches']
    assert by_bug['items'][0]['reference'] == row['reference']
    prefix = recall_mainline_references(['CVE-2026-5883'], data_root=path.parent)['sources']['critical_patches']
    assert prefix['status'] == 'no_match'


def test_excerpt_keeps_the_cve_when_many_other_identifiers_precede_it():
    terms = [f'CtsExample{index}TestCases' for index in range(20)]
    text = '\n'.join(term + ' padding' * 200 for term in terms) + '\nCVE-2026-58835'
    excerpt = matched_excerpt(text, [*terms, 'CVE-2026-58835'])
    assert len(excerpt) <= 4000 and 'CVE-2026-58835' in excerpt


def test_incremental_scan_rechecks_detail_when_overview_hash_does_not_change(index):
    _path, conn = index
    scan(conn, Session())
    old_hash = conn.execute("SELECT content_hash FROM gms_update_bulletin_patches WHERE patch_id='A-568181732'").fetchone()[0]
    scanned, changes, _parsed = scan(conn, Session(detail('CVE-2026-77777')))
    assert scanned and changes > 0
    assert query_bulletin_patches(conn, q='CVE-2026-58835')['total'] == 0
    assert query_bulletin_patches(conn, q='CVE-2026-77777')['total'] == 1
    new_hash = conn.execute("SELECT content_hash FROM gms_update_bulletin_patches WHERE patch_id='A-568181732'").fetchone()[0]
    assert new_hash != old_hash
    _scanned, unchanged, _parsed = scan(conn, Session(detail('CVE-2026-77777')))
    assert unchanged == 0  # Fetch timestamps are not content changes.


def test_radar_detects_detail_changes_without_reporting_fetch_timestamps_as_changes(index):
    path, conn = index
    scan(conn, Session())
    before = snapshot(path)
    scan(conn, Session())
    assert compare(before, snapshot(path))[1] == []
    scan(conn, Session(detail('CVE-2026-77777')))
    counts, _changes = compare(before, snapshot(path))
    assert counts['bulletins']['modified'] == 1


@pytest.mark.parametrize('session', [Session(status=403), Session('<html>temporary empty shell</html>')])
def test_failed_detail_refresh_keeps_previous_text_and_reports_incomplete_coverage(index, session):
    path, conn = index
    scan(conn, Session())
    before = dict(conn.execute("SELECT * FROM gms_update_bulletin_patches WHERE patch_id='A-568181732'").fetchone())
    scan(conn, session)
    after = dict(conn.execute("SELECT * FROM gms_update_bulletin_patches WHERE patch_id='A-568181732'").fetchone())
    assert after['detail_text'] == before['detail_text']
    assert after['android_versions'] == before['android_versions']
    assert after['detail_fetched_at'] == before['detail_fetched_at']
    assert after['detail_status'] == 'stale'
    result = recall_mainline_references(['CVE-2026-58835'], data_root=path.parent)['sources']['critical_patches']
    assert result['status'] == 'matched' and result['coverage_complete'] is False
    missing = recall_mainline_references(['CVE-2026-77777'], data_root=path.parent)['sources']['critical_patches']
    assert missing['status'] == 'no_match' and missing['coverage_complete'] is False


def test_empty_overview_does_not_delete_existing_records(index):
    _path, conn = index
    scan(conn, Session())
    with pytest.raises(ValueError, match='existing records were retained'):
        sync_source(conn, 2, fetched('<article>Temporary shell</article>'), force=True,
                    timestamp='2026-11-16', session=Session())
    assert conn.execute('SELECT COUNT(*) FROM gms_update_bulletin_patches').fetchone()[0] == 2


async def test_both_page_apis_search_detail_content(index, tmp_path, monkeypatch):
    _path, conn = index
    scan(conn, Session())
    mainline = sqlite3.connect(':memory:')
    mainline.row_factory = sqlite3.Row
    init_mainline_db(mainline)
    mainline.commit()
    monkeypatch.setattr(mainline_api, 'settings', SimpleNamespace(data_root=tmp_path))
    monkeypatch.setattr(mainline_api, '_connect_db', lambda: mainline)
    monkeypatch.setattr(monitor_api, '_get_db', lambda: conn)
    result = await mainline_api.list_mainline_critical_patches(q='CVE-2026-58835', limit=100, offset=0)
    assert result['total'] == 1 and 'CVE-2026-58835' in result['items'][0]['detail_excerpt']
    result = await monitor_api.list_gms_update_monitor_bulletins(q='CVE-2026-58835', year='', kind='',
                                                               android_version='', limit=100, offset=0)
    assert result['meta']['total'] == 1 and result['data']['coverage_complete']
    assert query_bulletin_patches(conn, q='%')['total'] == 0
    mainline.close()


def test_schema_migration_is_process_safe_and_preserves_legacy_records(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE gms_update_bulletin_patches ('
                     'source_key TEXT,item_key TEXT,year TEXT,patch_id TEXT,description TEXT,'
                     'bulletin_month TEXT,deadline TEXT,content_hash TEXT,first_seen_at TEXT,last_seen_at TEXT,'
                     'PRIMARY KEY(source_key,item_key))')
        conn.execute("INSERT INTO gms_update_bulletin_patches VALUES ('gms_bulletins','old','2026','A-123456789',"
                     "'Legacy fix','November','2027-03-26','hash','2026-11-14','2026-11-14')")
        conn.execute('PRAGMA user_version=1')

    def migrate(_):
        with sqlite3.connect(path, timeout=10) as conn:
            init_db(conn)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(migrate, range(4)))
    with sqlite3.connect(path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        assert conn.execute('SELECT patch_id,detail_status FROM gms_update_bulletin_patches').fetchone() == (
            'A-123456789', 'not_scanned',
        )
        assert bulletin_detail_coverage(conn)['coverage_complete'] is False
