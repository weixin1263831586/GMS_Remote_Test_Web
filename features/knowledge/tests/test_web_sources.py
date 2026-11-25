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
