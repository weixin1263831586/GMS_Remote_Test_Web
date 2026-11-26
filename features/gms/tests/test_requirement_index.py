"""Synthetic fixtures for requirement parsing and transactional publication."""

import json
from contextlib import closing

from lxml import html

from features.gms.models import FetchedDocument
from features.gms.parsers.requirements import parse_gms_requirements
from features.gms.repository import connect
from features.gms.sources import SOURCES
from features.gms.update_records import sync_source


SOURCE = next(item for item in SOURCES if item.key == 'gms_requirements')
BODY = """<article class="devsite-article">
<h2 id="overview">1. Overview</h2>
<div><p>See [GMS-1-099] for background.</p>
<h3 id="nested">1.1. Nested</h3><div role="tabpanel" id="chipset-16">
<p>Start of requirements added in Android 17</p>
<p>For devices with a supported feature:</p>
<ul><li><strong id="GMS-1.1-001">[GMS-1.1-001]</strong> MUST enable the feature on Android 16.
<ul><li>[GMSX-1.1-002] MUST preserve the nested setting.</li></ul></li></ul>
<p>End of added requirements</p>
<p>[VSR-1.1-003] MUST retain a setting.</p>
<table><thead><tr><th rowspan="2">Device</th><th colspan="2">Limits</th></tr>
<tr><th>Minimum</th><th>Maximum</th></tr></thead><tbody>
<tr><td>Example</td><td>1</td><td>2</td></tr></tbody></table>
<img src="/static/example.png" alt="Example figure">
</div><div role="tabpanel" id="chipset-17"><p>[VSR-1.1-003] MUST retain another setting.</p></div></div>
<h2 id="last">2. Last</h2><p>[GMS-VSR-2-001] MUST support the example.</p>
</article>"""


def fetched(body=BODY):
    return FetchedDocument(SOURCE, html.fromstring(body), 'Requirements', 'source-hash', 200, SOURCE.url)


def test_nested_sections_markers_tables_and_scope():
    parsed = parse_gms_requirements(fetched())
    by_id = {item.requirement_id: item for item in parsed.requirement_clauses}
    assert len(parsed.requirement_clauses) == 5
    clause = by_id['GMS-1.1-001']
    assert clause.change_version == 'Android 17' and clause.change_kind == 'added'
    assert json.loads(clause.version_mentions_json) == ['Android 16']
    assert clause.anchor_kind == 'clause' and clause.source_url.endswith('#GMS-1.1-001')
    assert clause.context_text == 'For devices with a supported feature:'
    assert 'nested setting' not in clause.original_text
    assert by_id['GMSX-1.1-002'].change_version == 'Android 17'
    repeated = [item for item in parsed.requirement_clauses if item.requirement_id == 'VSR-1.1-003']
    assert len({item.clause_key for item in repeated}) == 2
    assert {tuple(json.loads(item.scope_json)) for item in repeated} == {('chipset-16',), ('chipset-17',)}
    assert all(not item.change_kind for item in repeated)
    assert parsed.requirement_coverage['reference_only_ids'] == ['GMS-1-099']
    assert not parsed.requirement_coverage['missing_observed_ids']
    assert not parsed.requirement_coverage['coverage_complete']
    assert parsed.requirement_coverage['unclosed_markers'] == 0
    assert parsed.requirement_coverage['media'][0]['alt'] == 'Example figure'
    assert len(parsed.requirement_table_rows) == 1
    assert json.loads(parsed.requirement_table_rows[0].headers_json) == ['Device', 'Limits / Minimum', 'Limits / Maximum']
    assert 'GMS-VSR-2-001' not in next(item.text_excerpt for item in parsed.requirement_sections if item.number == '1.1')


def test_publish_derived_index_with_scan_records(tmp_path):
    with closing(connect(tmp_path / 'gms.sqlite3')) as conn, conn:
        doc = fetched()
        sync_source(conn, 1, doc, force=True, timestamp='2026-01-01')
        assert conn.execute('SELECT count(*) FROM gms_update_requirement_clauses').fetchone()[0] == 5
        row = conn.execute('SELECT coverage_json FROM gms_update_requirement_coverage').fetchone()
        assert json.loads(row[0])['clause_candidates'] == 5
        _, changes, _ = sync_source(conn, 2, doc, force=True, timestamp='2026-01-02')
        assert changes == 0
        assert conn.execute('SELECT run_id FROM gms_update_requirement_coverage').fetchone()[0] == 2


def test_table_ids_media_and_duplicate_anchors_are_not_verified_clauses():
    parsed = parse_gms_requirements(fetched("""<article class="devsite-article">
        <h2 id="chapter">1. Chapter</h2>
        <div id="GMS-1-001">Unrelated anchor</div>
        <p id="GMS-1-001">[GMS-1-001] MUST preserve feature.</p>
        <table><tr><th>Rule</th><th>Context</th></tr>
        <tr><td>[GMS-1-002]</td><td>See this figure <img src="/example.png"></td></tr></table>
        </article>"""))
    coverage = parsed.requirement_coverage
    assert len(parsed.requirement_clauses) == 1
    assert parsed.requirement_clauses[0].anchor_kind == 'section'
    assert coverage['duplicate_anchor_count'] == 1
    assert coverage['table_only_definition_candidates'] == ['GMS-1-002']
    assert 'GMS-1-002' not in coverage['missing_observed_ids']
    assert coverage['media_inventory_count'] == 1
    assert not coverage['applicability_verified']
    assert not coverage['original_document_verified']
    assert not coverage['coverage_complete']


def test_low_ram_section_lookup_preserves_table_note_and_real_anchor(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections, policy_lookup_target

    body = """<article class="devsite-article"><h2>10. Performance</h2>
    <h3 id="low-ram">Declare Low RAM devices</h3>
    <p>Start of requirements changed in Android 17</p>
    <p>[GMS-10.0-001] Example low memory declaration.</p>
    <p>Note: Example upgrade restriction.</p>
    <table><tr><th>OS Version</th><th>Must be Low RAM</th><th>Optionally Low RAM</th>
    <th>Not qualified for GMS</th></tr><tr><td>Android 16 or higher</td><td>3 GB</td><td>4 GB</td>
    <td>Less than 3 GB</td></tr></table><p>Example footnote.</p>
    <p>End of changed requirements</p><h3>Next section</h3><p>Unrelated text.</p></article>"""
    with closing(connect(tmp_path / 'lookup.sqlite3')) as conn, conn:
        sync_source(conn, 1, fetched(body), force=True, timestamp='2026-01-01')
        url = SOURCE.url + '#low-ram'
        assert policy_lookup_target(url) == url
        assert policy_lookup_target('过GMS认证，Android17设备RAM要求，Go和非Go') == 'section:Low RAM;android=17'
        assert policy_lookup_target('Android CDD RAM') == ''
        for query in (url, 'section:GMS-10.0-001', 'section:Low RAM'):
            data = lookup_requirement_sections(conn, query)
            assert data['status'] == 'matched'
            assert not data['coverage_complete']
            section = data['sections'][0]
            assert section['source_url'] == url
            assert 'Example upgrade restriction' in section['text']
            assert 'Example footnote' in section['text']
            assert 'Unrelated text' not in section['text']
            assert section['table_rows'][0]['values'] == ['Android 16 or higher', '3 GB', '4 GB', 'Less than 3 GB']
        assert lookup_requirement_sections(conn, SOURCE.url + '#missing')['status'] == 'anchor_not_indexed'
        assert lookup_requirement_sections(conn, 'https://example.com/#low-ram')['status'] == 'anchor_required'


def test_protected_section_lookup_requires_human_and_elevated_session(monkeypatch):
    from unittest.mock import Mock

    import pytest
    from fastapi import HTTPException

    from features.gms import queries

    connect_db = Mock()
    monkeypatch.setattr(queries, '_get_db', connect_db)
    elevated = Mock(side_effect=HTTPException(403))
    human = Mock()
    monkeypatch.setattr(queries, 'require_elevated_admin', elevated)
    monkeypatch.setattr(queries, 'require_human_principal', human)
    with pytest.raises(HTTPException) as exc:
        queries.list_gms_requirement_clauses(None, q='section:Low RAM')
    assert exc.value.status_code == 403
    human.assert_not_called()
    connect_db.assert_not_called()
    elevated.side_effect = None
    human.side_effect = HTTPException(403)
    with pytest.raises(HTTPException) as exc:
        queries.list_gms_requirement_clauses(None, q='section:Low RAM')
    assert exc.value.status_code == 403
    connect_db.assert_not_called()


def test_empty_policy_index_distinguishes_failed_scan(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections

    with closing(connect(tmp_path / 'empty.sqlite3')) as conn, conn:
        assert lookup_requirement_sections(conn, 'section:Low RAM')['status'] == 'not_indexed'
        conn.execute(
            """INSERT INTO gms_update_source_results
            (run_id,source_key,state,discovered,fetched,parsed,error)
            VALUES (2,'gms_requirements','failed',1,1,0,'internal failure detail')"""
        )
        result = lookup_requirement_sections(conn, 'section:Low RAM')
        assert result['status'] == 'scan_failed'
        assert result['latest_scan']['fetched'] == 1
        assert result['latest_scan']['parsed'] == 0
        assert 'error' not in result['latest_scan']
        assert not result['latest_scan_confirmed']


def test_case_distinct_table_rows_and_tab_ids_publish_without_key_collisions(tmp_path):
    doc = fetched("""<article class="devsite-article">
        <h2 id="low-ram">Example requirements</h2>
        <div role="tabpanel" id="Device-A"><p>[GMS-1-001] Example A.</p></div>
        <div role="tabpanel" id="device-a"><p>[GMS-1-001] Example B.</p></div>
        <table><tr><th>Feature</th></tr><tr><td>Enabled</td></tr>
        <tr><td>enabled</td></tr></table>
        <img src="https://[invalid" alt="Unresolved link">
        </article>""")
    parsed = parse_gms_requirements(doc)
    assert len({item.clause_key for item in parsed.requirement_clauses}) == 2
    assert len({item.row_key for item in parsed.requirement_table_rows}) == 2
    assert parsed.requirement_coverage['unresolved_media'] == 1
    with closing(connect(tmp_path / 'collision.sqlite3')) as conn, conn:
        sync_source(conn, 1, doc, force=True, timestamp='2026-01-01')
        assert conn.execute('SELECT count(*) FROM gms_update_requirement_table_rows').fetchone()[0] == 2
        assert conn.execute('SELECT count(*) FROM gms_update_requirement_clauses').fetchone()[0] == 2


def test_low_ram_declaration_excludes_preload_and_other_memory_sections(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections, policy_lookup_target

    body = """<article class="devsite-article">
    <h2>4. Preload</h2><h3 id="module-preload-requirements-for-low-ram-devices">
    Module preload requirements for low RAM devices</h3>
    <p>[GMS-4.2.2-001] Example preload rule.</p>
    <table><tr><th>Module</th></tr><tr><td>Example module</td></tr></table>
    <h2>10. Performance</h2><h3 id="low-ram">Declare Low RAM devices</h3>
    <p>[GMS-10.0-001] Example declaration rule.</p>
    <table><tr><th>OS Version</th><th>Must be Low RAM</th><th>Optionally Low RAM</th>
    <th>Not qualified for GMS</th></tr><tr><td>Android 16 or higher</td>
    <td>3 GB</td><td>4 GB</td><td>Less than 3 GB</td></tr></table>
    <h3>RAM usage</h3><p>Other Low RAM guidance.</p></article>"""
    with closing(connect(tmp_path / 'precise.sqlite3')) as conn, conn:
        sync_source(conn, 1, fetched(body), force=True, timestamp='2026-01-01')
        result = lookup_requirement_sections(conn, 'section:Low RAM')
        assert len(result['sections']) == 1
        section = result['sections'][0]
        assert section['source_url'].endswith('#low-ram')
        assert section['clauses'][0]['requirement_id'] == 'GMS-10.0-001'
        assert section['table_rows'][0]['values'] == ['Android 16 or higher', '3 GB', '4 GB', 'Less than 3 GB']
        assert 'Example preload rule' not in section['text']
        query = policy_lookup_target('GMS Low RAM 模块预装要求')
        assert query == 'section:Module preload requirements for low RAM devices'
        preload = lookup_requirement_sections(conn, query)
        assert preload['sections'][0]['source_url'].endswith('#module-preload-requirements-for-low-ram-devices')
        conn.execute("DELETE FROM gms_update_requirement_clauses WHERE requirement_id='GMS-10.0-001'")
        coverage = json.loads(conn.execute('SELECT coverage_json FROM gms_update_requirement_coverage').fetchone()[0])
        coverage['section_anchors'] = {k: v for k, v in coverage['section_anchors'].items() if v != 'low-ram'}
        conn.execute('UPDATE gms_update_requirement_coverage SET coverage_json=?', (json.dumps(coverage),))
        assert lookup_requirement_sections(conn, 'section:Low RAM')['status'] == 'no_match'


def test_ram_lookup_isolates_legacy_mixed_section_and_selects_version(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections

    doc = fetched("""<article class="devsite-article">
        <h3 id="low-ram">Declare Low RAM devices</h3>
        <p>[GMS-10.0-001] Example declaration.</p>
        <table><tr><th>OS Version</th><th>Must be Low RAM</th><th>Optionally Low RAM</th>
        <th>Not qualified for GMS</th></tr>
        <tr><td>Android 16 or higher</td><td>Example A</td><td>Example B</td><td>Example C</td></tr>
        <tr><td>Android 15</td><td>Old A</td><td>Old B</td><td>Old C</td></tr></table>
        <p>Note: Example note.</p>
        <p>[GMS-10.4-001] Example storage.</p>
        <table><tr><th>OS Version</th><th>Flash storage size</th></tr>
        <tr><td>Android 15 or higher</td><td>Example storage value</td></tr></table></article>""")
    with closing(connect(tmp_path / 'mixed.sqlite3')) as conn, conn:
        sync_source(conn, 1, doc, force=True, timestamp='2026-01-01')
        for query in ('section:Low RAM;android=17', 'section:GMS-10.0-001', SOURCE.url + '#low-ram'):
            result = lookup_requirement_sections(conn, query)
            section = result['sections'][0]
            assert section['context_limited']
            assert [clause['requirement_id'] for clause in section['clauses']] == ['GMS-10.0-001']
            assert len(section['table_rows']) == 2
            assert 'Example storage' not in str(section)
            assert not section['text']
            if result['requested_android_version'] == 17:
                assert len(section['version_rows']) == 1
                assert section['version_rows'][0]['values'][1] == 'Example A'


def test_semantic_heading_creates_section_and_table_text_is_not_duplicated():
    parsed = parse_gms_requirements(fetched("""<article class="devsite-article">
        <h3 id="low-ram">Declare Low RAM devices</h3>
        <p>[GMS-10.0-001] Example declaration.</p>
        <table><tr><th>Limit</th></tr><tr><td>Unique cell value</td></tr></table>
        <div role="heading" aria-level="3" id="storage">10.4. Storage</div>
        <p>[GMS-10.4-001] Example storage.</p></article>"""))
    first, second = parsed.requirement_clauses
    assert first.section_key != second.section_key
    assert parsed.requirement_table_rows[0].section_key == first.section_key
    assert 'Unique cell value' not in next(s.text_excerpt for s in parsed.requirement_sections
                                           if s.section_key == first.section_key)



def test_versioned_ram_lookup_retrieves_other_sections_without_merging_thresholds(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections, policy_lookup_target

    body = """<article class="devsite-article">
    <h3 id="low-ram">Declare Low RAM devices</h3>
    <p>[GMS-10.0-001] Example RAM declaration.</p>
    <table><tr><th>OS Version</th><th>Must be Low RAM</th><th>Optionally Low RAM</th>
    <th>Not qualified for GMS</th></tr>
    <tr><td>Android 16 or higher</td><td>A</td><td>B</td><td>C</td></tr></table>
    <h3 id="page">Page size</h3><p>Devices with 8 GB of RAM:</p>
    <p>[GMS-VSR-3.14-003] STRONGLY RECOMMENDED to enable an example option.</p>
    <p>Note: an additional RAM condition applies.</p>
    <h3 id="feature">Feature preload</h3>
    <p>[GMS-7.21-002] MUST preload an example APK if they have 4 GB of RAM.</p>
    <h3 id="notes">Other notes</h3><p>Note: RAM has a separate condition.</p>
    <h3 id="unrelated">Unrelated</h3><p>[GMS-2-001] Example program.</p>
    </article>"""
    with closing(connect(tmp_path / 'ram-overview.sqlite3')) as conn, conn:
        sync_source(conn, 1, fetched(body), force=True, timestamp='2026-01-01')
        result = lookup_requirement_sections(
            conn, policy_lookup_target('GMS Android16设备RAM要求，Go版和非Go版'))
        assert [s['source_url'].split('#')[-1] for s in result['sections']] == ['low-ram']
        related = result['related_ram']
        assert related['section_matches'] == 3
        assert related['clause_matches'] == 2
        assert not related['applicability_verified'] and not related['coverage_complete']
        by_anchor = {s['source_url'].split('#')[-1]: s for s in related['sections']}
        assert set(by_anchor) == {'page', 'feature', 'notes'}
        assert '8 GB of RAM' in by_anchor['page']['clauses'][0]['context_text']
        assert '4 GB of RAM' in by_anchor['feature']['clauses'][0]['original_text']
        assert by_anchor['notes']['fragments']
        assert lookup_requirement_sections(conn, 'section:GMS-10.0-001')['related_ram'] is None


def test_deleted_revision_text_never_enters_active_clause_or_table():
    doc = fetched("""<article class="devsite-article">
        <h3 id="example">Example requirements</h3>
        <p><span>[GMS-1-001] MUST support Android <del>12</del><ins>17</ins>
        and <span style="text-decoration: line-through">old</span> new mode.</span></p>
        <table><tr><th>Version</th><th>Limit</th></tr>
        <tr><td>Android <span><s>12</s>17</span></td><td><del>1</del>2 GB</td></tr></table>
        <p>Note: Retain this note.</p></article>""")
    parsed = parse_gms_requirements(doc)
    clause = parsed.requirement_clauses[0]
    assert 'Android 17' in clause.original_text
    assert '12' not in clause.original_text and 'old' not in clause.original_text
    assert json.loads(parsed.requirement_table_rows[0].values_json) == ['Android 17', '2 GB']
    assert 'Retain this note' in parsed.requirement_sections[0].text_excerpt
    assert '12' in html.tostring(doc.doc, encoding='unicode')
    assert parsed.requirement_coverage['revision_text_scope'] == 'active_markup_only'
    assert parsed.requirement_coverage['removed_revision_nodes'] == 4


def test_device_ram_candidates_prioritize_go_definition_and_exclude_subsystem_memory(tmp_path):
    from features.gms.requirement_lookup import lookup_requirement_sections

    doc = fetched("""<article class="devsite-article">
        <h3 id="low-ram">Declare Low RAM devices</h3>
        <p>[GMS-10.0-001] Example declaration.</p>
        <h3 id="apf">Android Packet Filtering</h3>
        <p>[GMS-2-001] MUST indicate APF RAM.</p>
        <h3 id="feature">Feature requirements</h3>
        <p>[GMS-3-001] Example feature with 8 GB RAM.</p>
        <h3 id="definitions">Definitions</h3>
        <p>A Go device is a class of low-memory devices.</p></article>""")
    with closing(connect(tmp_path / 'scope.sqlite3')) as conn, conn:
        sync_source(conn, 1, doc, force=True, timestamp='2026-01-01')
        result = lookup_requirement_sections(conn, 'section:Low RAM;android=16')
        related = result['related_ram']
        assert related['excluded_subsystem_sections'] == 1
        assert related['sections'][0]['source_url'].endswith('#definitions')
        assert related['sections'][0]['go_definition']
        assert all(not item['source_url'].endswith('#apf') for item in related['sections'])
        assert not related['applicability_verified']



def test_removed_marker_preserves_historical_status():
    parsed = parse_gms_requirements(fetched("""<article class="devsite-article">
        <h3>Example</h3><p>Start of requirements removed in Android 17</p>
        <p>[GMS-1-001] Historical example.</p><p>End of removed requirements</p>
        <p>[GMS-1-002] Another example.</p></article>"""))
    first, second = parsed.requirement_clauses
    assert (first.change_kind, first.change_version) == ('removed', 'Android 17')
    assert not second.change_kind
    assert not parsed.requirement_coverage['historical_applicability_verified']
    assert not parsed.requirement_coverage['unclosed_markers']
