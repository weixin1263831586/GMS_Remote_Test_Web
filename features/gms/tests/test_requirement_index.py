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
