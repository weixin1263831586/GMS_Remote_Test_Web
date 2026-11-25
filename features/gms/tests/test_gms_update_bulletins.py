"""Critical Fix detail content stays attributable, searchable and recoverable."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from lxml import html

from features.gms import mainline_queries as mainline_api
from features.gms import queries as monitor_api
from features.gms import recall_mainline_references
from features.gms.models import FetchedDocument
from features.gms.parsers.critical import parse_gms_bulletins
from features.gms.parsing import matched_excerpt
from features.gms.radar.scanning import batch_changes, inventory_counts
from features.gms.sources import SOURCES
from features.gms.update_records import (
    bulletin_detail_coverage,
    init_db,
    query_bulletin_patches,
    sync_source,
)


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
    monkeypatch.setattr('features.gms.parsers.critical.time.sleep', lambda *_: None)
    path = tmp_path / 'gms_update.sqlite3'
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    from features.gms.repository import init_db as init_unified_db
    init_unified_db(conn)
    conn.commit()
    yield path, conn
    conn.close()


def scan(conn, session, run_id=1):
    result = sync_source(conn, run_id, fetched(), force=False, timestamp='2026-11-16T03:00:00+00:00', session=session)
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
    before = inventory_counts(path)
    scan(conn, Session(), run_id=2)
    assert batch_changes(path, 2, before, inventory_counts(path), ['bulletins'])[1] == []
    scan(conn, Session(detail('CVE-2026-77777')), run_id=3)
    counts, _changes = batch_changes(path, 3, before, inventory_counts(path), ['bulletins'])
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
    monkeypatch.setattr(mainline_api, 'DB_PATH', tmp_path / 'gms_update.sqlite3')
    monkeypatch.setattr(monitor_api, 'DB_PATH', tmp_path / 'gms_update.sqlite3')
    result = await mainline_api.list_mainline_critical_patches(q='CVE-2026-58835', limit=100, offset=0)
    assert result['total'] == 1 and 'CVE-2026-58835' in result['items'][0]['detail_excerpt']
    result = await monitor_api.list_gms_update_bulletins(q='CVE-2026-58835', year='', kind='',
                                                               android_version='', limit=100, offset=0)
    assert result['meta']['total'] == 1 and result['data']['coverage_complete']
    assert query_bulletin_patches(conn, q='%')['total'] == 0


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


def test_month_navigation_discovers_non_overview_patches_and_preserves_deadlines(index):
    _path, conn = index
    overview = '<html><body>' + OVERVIEW + (
        '<nav><a href="/partners/releases/cf/aug-2026">August Update Bulletin</a>'
        '<a href="/partners/releases/cf/nov-2026">November Update Bulletin</a></nav></body></html>'
    )
    august = '''<article class="devsite-article"><h2>Strongly recommended functional patches</h2>
    <h3 id="507808346">A-507808346 Grant ACCESS_LOCAL_NETWORK permission</h3>
    <p>Android 17</p><h2>Mandatory functional patches</h2>
    <h3 id="543243062">A-543243062 Widget repair</h3><p>Android 16</p></article>'''

    class MonthlySession(Session):
        def get(self, url, **kwargs):
            self.calls.append(url)
            if '/aug-2026' in url:
                return SimpleNamespace(status_code=self.status, url=url, text=august)
            return SimpleNamespace(status_code=200, url=url, text=detail())

    session = MonthlySession()
    _scanned, _changes, parsed = sync_source(
        conn, 1, fetched(overview), force=False, timestamp='2026-11-24', session=session,
    )
    conn.commit()
    rows = {row.patch_id: row for row in parsed.bulletin_patches}
    assert len(session.calls) == 2
    assert len(rows) == 4
    assert rows['A-507808346'].patch_kind == 'strongly_recommended'
    assert rows['A-507808346'].deadline == ''
    assert rows['A-507808346'].bulletin_month == 'August'
    assert rows['A-543243062'].patch_kind == 'mandatory'
    assert rows['A-568181732'].deadline == '2027-03-26'
    assert bulletin_detail_coverage(conn)['coverage_complete'] is True

    sync_source(conn, 2, fetched(overview), force=False, timestamp='2026-11-25',
                session=MonthlySession(status=403))
    conn.commit()
    result = query_bulletin_patches(conn, q='A-507808346')
    assert result['total'] == 1
    assert result['items'][0]['detail_status'] == 'stale'
    assert result['coverage_complete'] is False


def test_monthly_dom_rows_preserve_version_and_exact_commit_link(index):
    _path, _conn = index
    one, two = 'a' * 40, 'b' * 40
    body = f'''<article class="devsite-article">
    <h2>Mandatory functional patches</h2><h3>A-568181732 Bluetooth fix</h3>
    <table><tr><th>Android version</th><th>Subject</th><th>Project</th><th>Download</th></tr>
    <tr><td>16</td><td><a href="https://android.googlesource.com/platform/frameworks/av/+/{one}">Fix callback</a></td>
    <td>platform/frameworks/av</td><td>Download</td></tr>
    <tr><td>17</td><td><a href="https://android.googlesource.com/platform/frameworks/av/+/{two}">Fix callback</a></td>
    <td>platform/frameworks/av</td><td>Download</td></tr></table>
    <h3>A-5681817329 Different fix</h3><p>Android 17</p></article>'''
    patch = parse_gms_bulletins(fetched(), session=Session(body)).bulletin_patches[0]
    rows = json.loads(patch.patch_rows_json)
    assert [row['version'] for row in rows] == ['16', '17']
    assert one in rows[0]['urls'][0] and two in rows[1]['urls'][0]


def test_november_all_five_entries_even_when_overview_has_only_two(index):
    _path, conn = index
    overview = '''<article class="devsite-article"><h2>2026 bulletins</h2><table>
    <tr><td>A-568181732 Audio fix</td><td><a href="/partners/releases/cf/nov-2026#568181732">November</a></td><td>March 26, 2027</td></tr>
    <tr><td>A-543052093 Service fix</td><td><a href="/partners/releases/cf/nov-2026#543052093">November</a></td><td>March 26, 2027</td></tr>
    <tr><td>A-543243062 Widget fix</td><td><a href="/partners/releases/cf/sep-2026#543243062">September</a></td><td>March 26, 2027</td></tr>
    </table></article>'''
    november = '''<html><body><article class="devsite-article">
    <h2>November Update Bulletin</h2><h3>Mandatory functional patches</h3>
    <div><h4 id="568181732">A-568181732 Audio fix</h4><p>Android 16 and 17</p></div>
    <div><h4 id="543052093">A-543052093 Service fix</h4><p>Android 17</p></div>
    <h4 id="543243062">R-543243062 Widget fix</h4><p>Android 16</p>
    <h3>Strongly recommended functional patches</h3>
    <h4 id="445016017">A-445016017 GBL metrics tracking</h4><p>Android 17</p>
    <h4 id="376407624">A-376407624 Fastboot client bug fixes</h4><p>Android 17</p>
    </article><nav>
    <a href="#568181732">A-568181732 Audio fix</a>
    <a href="#543052093">A-543052093 Service fix</a>
    <a href="#543243062">R-543243062 Widget fix</a>
    <a href="#445016017">A-445016017 GBL metrics tracking</a>
    <a href="#376407624">A-376407624 Fastboot client bug fixes</a>
    </nav></body></html>'''
    september = '''<article class="devsite-article"><h2>Mandatory functional patches</h2>
    <h3>A-543243062 Widget fix</h3><p>Android 16</p></article>'''

    class MonthlySession(Session):
        def get(self, url, **kwargs):
            self.calls.append(url)
            return SimpleNamespace(status_code=200, url=url,
                                   text=september if '/sep-2026' in url else self.text)

    session = MonthlySession(november)
    sync_source(conn, 1, fetched(overview), force=True, timestamp='2026-11-24', session=session)
    conn.commit()
    rows = query_bulletin_patches(conn, q='November')['items']
    assert {row['patch_id'] for row in rows} == {
        'A-568181732', 'A-543052093', 'R-543243062', 'A-445016017', 'A-376407624',
    }
    assert sum(row['patch_kind'] == 'mandatory' for row in rows) == 3
    assert sum(row['patch_kind'] == 'strongly_recommended' for row in rows) == 2
    assert bulletin_detail_coverage(conn)['coverage_complete']
    assert query_bulletin_patches(conn, q='September')['total'] == 1

    # An incomplete article may still return HTTP 200 and include the overview
    # mandatory rows. The page TOC must expose the missing recommendation.
    partial = november.replace('<h4 id="376407624">A-376407624 Fastboot client bug fixes</h4><p>Android 17</p>', '')
    sync_source(conn, 2, fetched(overview), force=True, timestamp='2026-11-25',
                session=MonthlySession(partial))
    conn.commit()
    assert query_bulletin_patches(conn, q='November')['total'] == 5
    assert not bulletin_detail_coverage(conn)['coverage_complete']
    row = query_bulletin_patches(conn, q='A-376407624')['items'][0]
    assert row['detail_status'] == 'stale'


def test_month_alias_does_not_create_permanent_incomplete_coverage(index):
    _path, conn = index
    scan(conn, Session())
    renamed = OVERVIEW.replace('/nov-2026', '/november-2026').replace('>November<', '>Nov<')
    sync_source(conn, 2, fetched(renamed), force=True, timestamp='2026-11-25', session=Session())
    conn.commit()
    coverage = bulletin_detail_coverage(conn)
    assert coverage['coverage_complete'] is True
    assert coverage['months_discovered'] == 1
    assert coverage['failed_months'] == []
    assert query_bulletin_patches(conn, q='November')['total'] == 2


def test_month_failure_records_specific_reason_and_preserves_rows(index):
    _path, conn = index
    scan(conn, Session())
    scan(conn, Session(status=403))
    coverage = bulletin_detail_coverage(conn)
    assert not coverage['coverage_complete']
    assert coverage['months_discovered'] == 1
    assert coverage['failed_months'][0]['url'].endswith('/nov-2026')
    assert 'HTTP 403' in coverage['failed_months'][0]['reason']
    assert query_bulletin_patches(conn)['total'] == 2


def test_legacy_category_and_heading_variants(index):
    body = '''<article class="devsite-article">
    <h2>Required fixes</h2><p><strong>Bug ID: A-568181732 Legacy fix</strong></p>
    <p>Android 10</p><h2>Strongly recommended</h2>
    <h3>Another fix (A-123456789)</h3><p>Android 10</p>
    <h2>Mandatory patches</h2><h3>A-5681817329 Different fix</h3><p>Android 10</p>
    </article>'''
    result = parse_gms_bulletins(fetched(), session=Session(body))
    rows = {row.patch_id: row for row in result.bulletin_patches}
    assert rows['A-568181732'].patch_kind == 'mandatory'
    assert rows['A-123456789'].patch_kind == 'strongly_recommended'
    assert list(result.bulletin_month_status.values()) == ['ready']


def test_overview_proves_legacy_mandatory_classification(index):
    body = '''<article class="devsite-article"><h2>Functional fixes</h2>
    <h3>A-568181732 First fix</h3><p>Android 10</p>
    <h3>A-5681817329 Second fix</h3><p>Android 10</p></article>'''
    result = parse_gms_bulletins(fetched(), session=Session(body))
    assert all(row.patch_kind == 'mandatory' for row in result.bulletin_patches)
    assert list(result.bulletin_month_status.values()) == ['ready']


@pytest.mark.parametrize('notice,expected', [
    ('There are no critical fixes for this month.', 'ready'),
    ('There are no mandatory patches for this month.', 'unavailable'),
    ('Release notes temporarily unavailable.', 'unavailable'),
])
def test_empty_month_requires_explicit_no_fix_statement(index, notice, expected):
    overview = OVERVIEW + '<nav><a href="/partners/releases/cf/dec-2019">December</a></nav>'

    class MonthlySession(Session):
        def get(self, url, **kwargs):
            if '/dec-2019' in url:
                return SimpleNamespace(status_code=200, url=url,
                                       text=f'<article class="devsite-article"><p>{notice}</p></article>')
            return super().get(url, **kwargs)

    result = parse_gms_bulletins(fetched(overview), session=MonthlySession())
    status = next(value for url, value in result.bulletin_month_status.items() if '/dec-2019' in url)
    assert status == expected


@pytest.mark.parametrize('label,kind', [
    ('Critical functional fixes (mandatory)', 'mandatory'),
    ('Mandatory bug fixes for Android 12', 'mandatory'),
    ('Functional patches – strongly recommended', 'strongly_recommended'),
    ('Strongly recommended critical bug fixes', 'strongly_recommended'),
    ('Recommended fixes', 'recommended'),
    ('Recommended patches for all devices', 'recommended'),
    ('Mandatory functional patches Android 13', 'mandatory'),
    ('Strongly recommended functional patches Android 12 and later', 'strongly_recommended'),
    ('Mandatory patches not required', ''),
    ('Mandatory patches or recommended fixes', ''),
    ('Non-mandatory fixes', ''),
])
def test_explicit_legacy_category_variants(label, kind):
    from features.gms.parsers.critical import _cf_category

    assert _cf_category(label) == kind


def test_two_explicit_empty_categories_are_required():
    from features.gms.parsers.critical import _cf_explicitly_empty

    first = '<p>No mandatory functional patches.</p>'
    second = '<p>No strongly recommended functional patches.</p>'
    assert _cf_explicitly_empty(html.fromstring('<article>' + first + second + '</article>'))
    assert not _cf_explicitly_empty(html.fromstring('<article>' + first + '</article>'))
    assert not _cf_explicitly_empty(html.fromstring('<article><p>No security patches.</p></article>'))


def test_toc_can_supply_missing_heading_id_but_not_missing_body(index):
    body = '''<html><body><article class="devsite-article">
    <h2>Mandatory fixes</h2><h3 id="first">First fix</h3><p>Android 10</p>
    <h3>A-5681817329 Second fix</h3><p>Android 10</p>
    </article><nav><a href="#first">A-568181732 First fix</a></nav></body></html>'''
    result = parse_gms_bulletins(fetched(), session=Session(body))
    assert list(result.bulletin_month_status.values()) == ['ready']
    assert {row.patch_id for row in result.bulletin_patches} == {'A-568181732', 'A-5681817329'}
    missing = body.replace('<h3 id="first">First fix</h3>', '')
    result = parse_gms_bulletins(fetched(), session=Session(missing))
    assert list(result.bulletin_month_status.values()) == ['unavailable']


def test_historical_recommended_is_not_upgraded_to_strongly_recommended(index):
    body = '''<article class="devsite-article">
    <h2>Mandatory functional patches Android 13</h2>
    <h3>A-568181732 First fix</h3><p>Android 13</p>
    <h3>A-5681817329 Second fix</h3><p>Android 13</p>
    <h2>Recommended patches for all devices</h2>
    <h3>A-123456789 Historical fix</h3><p>Android 13</p>
    <h2>Strongly recommended functional patches Android 12 and later</h2>
    <h3>A-987654321 Another fix</h3><p>Android 12</p>
    </article>'''
    result = parse_gms_bulletins(fetched(), session=Session(body))
    rows = {row.patch_id: row for row in result.bulletin_patches}
    assert rows['A-568181732'].patch_kind == 'mandatory'
    assert rows['A-123456789'].patch_kind == 'recommended'
    assert rows['A-987654321'].patch_kind == 'strongly_recommended'
    assert list(result.bulletin_month_status.values()) == ['ready']


def test_legacy_spaced_and_typographic_patch_ids():
    from features.gms.parsers.critical import _cf_heading_match

    for title in ('A 123456789 Fix', 'A – 123456789 Fix', 'A‑123456789 Fix'):
        assert _cf_heading_match(title).group('id') == 'A-123456789'


def test_legacy_none_requires_all_category_blocks_empty():
    from features.gms.parsers.critical import _cf_explicitly_empty

    body = ('<article><h2>Mandatory functional patches</h2><p>None.</p>'
            '<h2>Recommended patches for all devices</h2><p>None</p>'
            '<h2>Recommended patches for other devices</h2><p>None</p></article>')
    assert _cf_explicitly_empty(html.fromstring(body))
    assert not _cf_explicitly_empty(html.fromstring(body.replace('<p>None</p>', '<p>Pending</p>', 1)))


def test_unqualified_functional_category_preserves_source_label(index):
    body = '''<article class="devsite-article"><h2>Additional functional patches</h2>
    <h3>A-568181732 First fix</h3><p>Android 13</p>
    <h3>A-5681817329 Second fix</h3><p>Android 13</p>
    <h3>A-123456789 Third fix</h3><p>Android 13</p></article>'''
    result = parse_gms_bulletins(fetched(), session=Session(body))
    third = next(row for row in result.bulletin_patches if row.patch_id == 'A-123456789')
    assert third.patch_kind == 'functional_unspecified'
    assert 'Additional functional patches' in third.detail_text
    assert list(result.bulletin_month_status.values()) == ['ready']


def test_legacy_named_sections_have_no_invented_id(index):
    from features.gms.parsers.critical import _cf_legacy_sections

    root = html.fromstring('''<article><h2>Mandatory functional patches</h2>
    <h3 id="one">Named first fix</h3><p>Android 10</p><h4>Patch details</h4><p>First evidence</p>
    <h3 id="two">Named second fix</h3><p>Second evidence</p>
    <h2>Recommended patches for all devices</h2><p>None</p></article>''')
    records, complete = _cf_legacy_sections(root)
    assert complete
    assert len(records) == 2
    assert all(match.group('id') == '' for _, match, _, _ in records)
    assert 'Second evidence' not in ' '.join(node.text_content() for node in records[0][3])
    assert _cf_legacy_sections(html.fromstring('<article><h2>Mandatory patches</h2><p>Unavailable</p></article>')) == ([], False)


def test_legacy_category_preamble_is_retained_not_dropped():
    from features.gms.parsers.critical import _cf_legacy_sections

    root = html.fromstring('''<article><div class="devsite-article-body">
    <h2 id="cat">Mandatory functional patches</h2><p>Important context</p>
    <h3>Group</h3><h3>Named fix</h3><p>Fix evidence</p>
    <h2>Recommended patches</h2><p>None</p>
    </div><footer>Page feedback</footer></article>''')
    rows, complete = _cf_legacy_sections(root)
    assert complete and len(rows) == 1
    content = ' '.join(node.text_content() for node in rows[0][3])
    assert 'Important context' in content and 'Fix evidence' in content
    assert 'Page feedback' not in content


def test_legacy_none_ignores_site_footer_outside_document_body():
    from features.gms.parsers.critical import _cf_explicitly_empty

    root = html.fromstring('''<article><div class="devsite-article-body">
    <h2>Mandatory functional patches</h2><p>None</p>
    <h2>Recommended patches</h2><p>None</p>
    </div><footer>Site feedback</footer></article>''')
    assert _cf_explicitly_empty(root)
