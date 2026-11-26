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
    <th>Not qualified</th></tr><tr><td>Android 16 or higher</td><td>3 GB</td><td>4 GB</td>
    <td>Less than 3 GB</td></tr></table><p>Example footnote.</p>
    <p>End of changed requirements</p><h3>Next section</h3><p>Unrelated text.</p></article>"""
    with closing(connect(tmp_path / 'lookup.sqlite3')) as conn, conn:
        sync_source(conn, 1, fetched(body), force=True, timestamp='2026-01-01')
        url = SOURCE.url + '#low-ram'
        assert policy_lookup_target(url) == url
        assert policy_lookup_target('过GMS认证，Android17设备RAM要求，Go和非Go') == 'section:Low RAM'
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
