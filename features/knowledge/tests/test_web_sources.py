"""Regression coverage for public policy ingestion and local retrieval."""

from __future__ import annotations

import socket
from unittest.mock import Mock

import pytest

from features.knowledge.external import web_sources
from features.knowledge.external.document_index import parse_document
from features.knowledge.external.web_fetch import public_addresses, validate_url
from features.knowledge.external.web_sources import WebKnowledgeProvider


def cdd(version, marker='TTS'):
    filler = ''.join(f'<li>[C-0-{i}] MUST support filler {i}.</li>' for i in range(2, 52))
    return (
        f'<article><h1>Android {version} Compatibility Definition</h1>'
        '<h2 id="tts">3.11. Text-to-Speech</h2>'
        '<p>If the device declares audio output, it:</p><ul>'
        f'<li>[C-1-1] MUST support {marker} framework APIs.</li>'
        '<li>[C-SR-1] is STRONGLY RECOMMENDED to support speech.</li>'
        '<li><del>[C-2-1] MUST support removed behavior.</del></li>'
        '</ul><p>See [C-1-1] for background.</p>'
        '<h2 id="other">4. Other</h2><ul>'
        '<li>[C-1-1] MUST support unrelated behavior.</li>'
        f'{filler}</ul></article>'
    ).encode()


def configure(provider, version):
    return provider.save(
        name=f'Android {version} CDD',
        url=f'https://source.android.com/docs/compatibility/{version}/android-{version}-cdd',
        kind='cdd',
        android_version=version,
        license='CC-BY-4.0',
    )


@pytest.mark.parametrize('url', [
    'http://source.android.com/docs',
    'https://user:secret@example.com/',
    'https://example.com:8080/',
    'https://docs.partner.android.com/gms/policies/domains/reqs?authuser=1',
    'https://accounts.google.com/',
])
def test_url_validation(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_dns_mixed_public_private_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
    ])
    with pytest.raises(ValueError):
        public_addresses('example.com')


def test_cdd_preserves_section_conditions_and_recommendation():
    rows = parse_document(cdd(16), 'https://source.android.com/cdd', 'cdd', 16)
    matches = [r for r in rows if r[1] == 'C-1-1']
    assert len(matches) == 2
    assert matches[0][0].startswith('3.11.')
    assert matches[0][3] == 'If the device declares audio output, it:'
    assert matches[0][5].endswith('#tts')
    assert any(r[1] == 'C-SR-1' and r[2] == 'STRONGLY RECOMMENDED' for r in rows)
    assert not any(r[1] == 'C-2-1' for r in rows)
    with pytest.raises(ValueError, match='version'):
        parse_document(cdd(15), 'https://source.android.com/cdd', 'cdd', 16)


def test_versions_incremental_sync_and_failure_isolation(tmp_path, monkeypatch):
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source16 = configure(provider, 16)
    source17 = configure(provider, 17)
    fetch = Mock(side_effect=[(cdd(16), 'e16', ''), (cdd(17), 'e17', '')])
    monkeypatch.setattr(web_sources, 'fetch_html', fetch)
    original = provider.sync(source16)
    provider.sync(source17)
    hits = provider.search('TTS', android_api_level=36)
    assert hits and all(hit.applicable_versions == '16' for hit in hits)
    assert all(hit.source_revision == original['revision'] for hit in hits)
    token = hits[0].source_path
    fetch.side_effect = None
    fetch.return_value = (None, 'e16', '')
    assert provider.sync(source16)['unchanged']
    assert provider.search('TTS', android_api_level=36)[0].source_path == token
    fetch.side_effect = OSError('network down')
    with pytest.raises(OSError):
        provider.sync(source16)
    assert provider.search('TTS', android_api_level=36)[0].extra['status'] == 'stale'
    assert provider.search('TTS', android_api_level=37)[0].extra['status'] == 'indexed'
    fetch.side_effect = None
    fetch.return_value = (cdd(16, 'NewSpeech'), 'new', '')
    replacement = provider.sync(source16)
    assert replacement['revision'] != original['revision']
    assert provider.search('NewSpeech', android_api_level=36)
    provider.delete(source16)
    assert not provider.search('NewSpeech', android_api_level=36)


def test_disabled_source_does_not_return_cached_content(tmp_path, monkeypatch):
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source_id = configure(provider, 16)
    monkeypatch.setattr(web_sources, 'fetch_html', lambda *args, **kwargs: (cdd(16), '', ''))
    provider.sync(source_id)
    provider.save(
        source_id=source_id, name='CDD', url='https://source.android.com/docs/compatibility/16/android-16-cdd',
        kind='cdd', android_version=16, license='CC-BY-4.0', enabled=False,
    )
    assert not provider.search('TTS', android_api_level=36)
    assert provider.status().status == 'disabled'


def test_distinct_conditions_have_distinct_citations(tmp_path, monkeypatch):
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source = configure(provider, 16)
    body = cdd(16).replace(
        b'<li>[C-SR-1] is STRONGLY RECOMMENDED to support speech.</li>',
        b'</ul><p>If the device has a touchscreen:</p>'
        b'<ul><li>[C-1-1] MUST support TTS framework APIs.</li>',
    )
    monkeypatch.setattr(web_sources, 'fetch_html', lambda *_args, **_kwargs: (body, '', ''))
    provider.sync(source)
    rows = provider.compare_requirements('3.11. Text-to-Speech', 'C-1-1')
    assert len({row['token'] for row in rows}) == len(rows) == 2
    for row in rows:
        detail = web_sources.serialize_clause(provider.read_requirement(row['token']))
        assert detail['condition'] == row['condition']
        assert detail['source_path'] == row['token']
        assert detail['source_anchors'][0]['revision'] == row['revision']
        assert detail['evidence_level'] == 'background'
    assert len(provider.compare_requirements('3.11. Text-to-Speech', 'C-1-1', limit=1)) == 1
    assert not provider.compare_requirements('3.11. Text-to-Speech', 'C-1-1', android_api_level=35)


def test_failed_sources_do_not_starve_remaining_sources(tmp_path, monkeypatch):
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source_ids = {configure(provider, version) for version in (15, 16, 17)}
    monkeypatch.setattr(web_sources, 'fetch_html', Mock(side_effect=OSError))
    first = {row['source_id'] for row in provider.sync_due(max_sources=2)}
    second = {row['source_id'] for row in provider.sync_due(max_sources=2)}
    assert first | second == source_ids


def test_retention_preserves_current_revision_and_one_prior_revision(tmp_path, monkeypatch):
    monkeypatch.setattr(web_sources, 'MAX_REVISIONS_PER_SOURCE', 2)
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source = configure(provider, 16)
    fetch = Mock(side_effect=[(cdd(16, marker), '', '') for marker in ('First', 'Second', 'Third')])
    monkeypatch.setattr(web_sources, 'fetch_html', fetch)
    tokens = []
    section = '3.11. Text-to-Speech'
    for _ in range(3):
        provider.sync(source)
        tokens.append(provider.compare_requirements(section, 'C-1-1')[0]['token'])
    assert provider.read_requirement(tokens[0]) is None
    assert all(provider.read_requirement(token) for token in tokens[1:])


def test_nested_requirements_and_table_context():
    body = cdd(16).replace(
        b'<li>[C-1-1] MUST support TTS framework APIs.</li>',
        b'<li>[C-1-1] MUST support TTS framework APIs.<ul>'
        b'<li>[C-1-9] MUST preserve nested behavior.</li></ul></li>'
        b'<li>For handheld devices:<ul><li>[C-1-8] MUST support speech.</li></ul></li>',
    ).replace(
        b'<h2 id="other">',
        b'<table><thead><tr><th>Device</th><th>Requirement</th></tr></thead>'
        b'<tbody><tr><td>Phone</td><td>Must retain audio</td></tr></tbody></table><h2 id="other">',
    )
    rows = parse_document(body, 'https://source.android.com/cdd', 'cdd', 16)
    parent = next(r for r in rows if r[1] == 'C-1-1' and r[0].startswith('3.11'))
    nested = next(r for r in rows if r[1] == 'C-1-9')
    assert 'nested behavior' not in parent[4]
    assert 'MUST support TTS' in nested[3]
    assert 'For handheld devices' in next(r for r in rows if r[1] == 'C-1-8')[3]
    table = next(r for r in rows if 'Phone | Must retain audio' in r[4])
    assert 'Device | Requirement' in table[3]


def test_sync_coalesces_and_revision_read(tmp_path, monkeypatch):
    from foundation.process_sync import release_sync_lock, try_acquire_sync_lock

    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source16 = configure(provider, 16)
    source17 = configure(provider, 17)
    fetch = Mock(return_value=(cdd(16), '', ''))
    monkeypatch.setattr(web_sources, 'fetch_html', fetch)
    lock = try_acquire_sync_lock(tmp_path / 'sync-locks' / (source16 + '.lock'))
    assert lock is not None
    try:
        assert provider.sync(source16)['status'] == 'in_progress'
        fetch.assert_not_called()
    finally:
        release_sync_lock(lock)
    provider.sync(source16)
    hits = provider.search('TTS', android_api_level=36)
    token = hits[0].source_path
    initial = provider.read_requirement(token)
    assert initial['original'] and initial['revision'] == initial['current_revision']
    fetch.return_value = (cdd(17), '', '')
    provider.sync(source17)
    rows = provider.compare_requirements('3.11. Text-to-Speech', 'C-1-1')
    assert [r['android_version'] for r in rows] == [16, 17]
    fetch.return_value = (cdd(16, 'NewSpeech'), '', '')
    provider.sync(source16)
    old = provider.read_requirement(token)
    assert old['revision'] != old['current_revision']
    assert old['original'] == initial['original']
    assert provider.sync_due() == []
    provider.delete(source16)
    assert provider.read_requirement(token) is None



def test_url_only_create_and_manual_name(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from features.knowledge import external_api

    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    fetch = Mock(side_effect=[(cdd(16), 'e16', ''), (cdd(17), 'e17', ''), (cdd(17, 'NewSpeech'), 'new', '')])
    monkeypatch.setattr(web_sources, 'fetch_html', fetch)
    monkeypatch.setattr(external_api, 'WebKnowledgeProvider', lambda: provider)
    monkeypatch.setattr(external_api, 'require_elevated_admin', lambda _request: None)
    app = FastAPI()
    app.include_router(external_api.router)
    client = TestClient(app)
    url16 = 'https://source.android.com/docs/compatibility/16/android-16-cdd'
    url17 = 'https://source.android.com/docs/compatibility/17/android-17-cdd'
    response = client.post('/external/web-sources', json={'url': url16})
    assert response.status_code == 200
    assert response.json()['data']['sync']['status'] == 'ready'
    auto_id = response.json()['data']['id']
    rows = provider.list_sources()
    assert rows[0]['name'] == 'Android 16 Compatibility Definition'
    assert rows[0]['android_version'] == 16
    assert rows[0]['indexed_count'] > 50
    assert provider.search('TTS', android_api_level=36)
    assert not provider.search('TTS', android_api_level=37)
    response = client.post('/external/web-sources', json={'url': url17, 'name': '手写名称'})
    assert response.status_code == 200
    manual_id = response.json()['data']['id']
    provider.sync(manual_id)
    row = next(r for r in provider.list_sources() if r['id'] == manual_id)
    assert row['name'] == '手写名称'
    assert row['android_version'] == 17
    old_revision = row['revision']
    response = client.put('/external/web-sources/' + manual_id, json={'url': url17, 'name': '新名称'})
    assert response.status_code == 200
    row = next(r for r in provider.list_sources() if r['id'] == manual_id)
    assert row['name'] == '新名称'
    assert row['revision'] == old_revision
    response = client.put('/external/web-sources/' + auto_id, json={'url': url16, 'enabled': False})
    assert response.status_code == 200
    assert not provider.search('TTS', android_api_level=36)
    assert fetch.call_count == 3


def test_unknown_source_requires_approval_and_failed_initial_sync_is_retryable(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from features.knowledge import external_api

    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    fetch = Mock(side_effect=OSError('network down'))
    monkeypatch.setattr(web_sources, 'fetch_html', fetch)
    monkeypatch.setattr(external_api, 'WebKnowledgeProvider', lambda: provider)
    monkeypatch.setattr(external_api, 'require_elevated_admin', lambda _request: None)
    app = FastAPI()
    app.include_router(external_api.router)
    client = TestClient(app)
    url = 'https://example.com/document'
    assert client.post('/external/web-sources', json={'url': url}).status_code == 422
    fetch.assert_not_called()
    response = client.post('/external/web-sources', json={'url': url, 'authorized_for_ai_processing': True})
    assert response.status_code == 200
    assert response.json()['data']['sync']['status'] == 'error'
    source_id = response.json()['data']['id']
    assert provider.list_sources()[0]['error']
    fetch.side_effect = None
    fetch.return_value = (b'<article><h1>Example policy</h1><p>Public content.</p></article>', '', '')
    assert client.post('/external/web-sources/' + source_id + '/sync').status_code == 200
    assert provider.list_sources()[0]['name'] == 'Example policy'
    assert provider.list_sources()[0]['indexed_count'] == 1
    assert not provider.list_sources()[0]['error']


def cdd_with_memory_table(version=16):
    """CDD 片段：2.2.1 同时有 ram.low 标志位与按分辨率的内存门槛表。"""
    filler = ''.join(f'<li>[C-0-{i}] MUST support filler {i}.</li>' for i in range(2, 52))
    return (
        f'<article><h1>Android {version} Compatibility Definition</h1>'
        '<h2 id="hardware">2.2.1. Hardware</h2>'
        '<ul><li>[7.6.1/H-9-1] MUST declare the feature flag android.hardware.ram.low.</li></ul>'
        '<table><tbody>'
        '<tr><td>qHD</td><td>[7.6.1/H-1-1] The memory available to the kernel and userspace '
        'MUST be at least 416MB.</td></tr>'
        '<tr><td>FHD</td><td>[7.6.1/H-3-1] The memory available to the kernel and userspace '
        'MUST be at least 896MB.</td></tr>'
        '</tbody></table>'
        f'<h2 id="other">4. Other</h2><ul>{filler}</ul></article>'
    ).encode()


def test_section_clauses_accompany_the_matching_row(tmp_path, monkeypatch):
    """问到内存门槛时，同节的数值条款必须一起返回，而不是只有命中的标志位。

    旧实现按行返回：只有含 "ram" 的 ram.low 标志位命中，真正给出数值的内存
    门槛表（措辞是 "memory available to the kernel"）因为不共享词元被漏掉，
    Agent 据此得出「CDD 未给出最低 RAM 要求」的错误结论。
    """
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source_id = configure(provider, 16)
    monkeypatch.setattr(web_sources, 'fetch_html', lambda *args, **kwargs: (cdd_with_memory_table(), '', ''))
    provider.sync(source_id)

    hits = provider.search('RAM requirement', limit=5, android_api_level=36)
    snippets = [hit.snippet for hit in hits]
    assert any('android.hardware.ram.low' in text for text in snippets)
    assert any('at least 416MB' in text for text in snippets)
    assert any('at least 896MB' in text for text in snippets)
    assert all(hit.chapter == '2.2.1. Hardware' for hit in hits)


def test_short_version_tokens_do_not_widen_matching():
    """纯数字版本号不参与 FTS 匹配：版本由 android_api_level 精确过滤。"""
    assert web_sources._match_terms(['16', 'ram', 'requirement']) == ['ram', 'requirement']
    assert web_sources._match_terms(['17']) == ['17']


def test_status_reports_indexed_clause_coverage(tmp_path, monkeypatch):
    """状态说明带条款数/章节数，避免「只索引 1 篇」被读成覆盖不全。"""
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    source_id = configure(provider, 16)
    monkeypatch.setattr(web_sources, 'fetch_html', lambda *args, **kwargs: (cdd_with_memory_table(), '', ''))
    provider.sync(source_id)

    detail = provider.status().detail
    assert '1/1 sources indexed' in detail
    assert 'clauses in' in detail
    assert provider.indexed_coverage()['sections'] >= 2
