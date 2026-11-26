"""Opt-in verification against the actual Android CDD HTML documents."""

from __future__ import annotations

import os

import pytest

from features.knowledge.external.document_index import coverage_report
from features.knowledge.external.web_fetch import fetch_html
from features.knowledge.external.web_sources import WebKnowledgeProvider


@pytest.mark.skipif(os.environ.get('GMS_EXTERNAL_POLICY_LIVE') != '1', reason='requires live public network')
@pytest.mark.parametrize('version,api_level', [(15, 35), (16, 36), (17, 37)])
def test_live_cdd_index(tmp_path, version, api_level):
    provider = WebKnowledgeProvider(tmp_path / 'live.sqlite3')
    url = f'https://source.android.com/docs/compatibility/{version}/android-{version}-cdd'
    source = provider.save(
        name=f'Android {version} CDD', url=url, kind='cdd',
        android_version=version, license='CC-BY-4.0',
    )
    body, _, _ = fetch_html(url)
    coverage = coverage_report(body, url, 'cdd', version)
    assert coverage['indexed_definitions'] >= 50
    assert not coverage['missing'] and not coverage['unexpected'], coverage
    print(f'CDD {version} coverage: {coverage}')
    result = provider.sync(source)
    assert len(result['revision']) == 64
    hits = provider.search('Text-to-Speech framework APIs', android_api_level=api_level, limit=20)
    assert hits
    assert all(hit.applicable_versions == str(version) for hit in hits)
    assert any('Text-to-Speech' in hit.chapter and hit.extra['requirement_id'] for hit in hits)
    assert all(hit.source_anchors[0].url.startswith(url) for hit in hits)
    assert all(hit.source_revision == result['revision'] for hit in hits)
    print(f'CDD {version}: revision={result["revision"]} hits={len(hits)}')
