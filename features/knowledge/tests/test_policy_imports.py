"""Owner and revision boundaries for authorized document imports."""

from __future__ import annotations

from unittest.mock import Mock

from features.knowledge import policy_imports
from features.knowledge.external import web_sources
from features.knowledge.external.web_sources import WebKnowledgeProvider
from features.knowledge.parsers import parse_file


def test_import_requires_exact_owner_content_and_model_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(web_sources, 'model_catalog_fingerprint', lambda: 'route-a')
    provider = WebKnowledgeProvider(tmp_path / 'web.sqlite3')
    doc = {
        'doc_id': 'doc_abc', 'source_file': 'policy.html', 'title': 'Policy',
        'raw_content': 'Bluetooth devices MUST support policy behavior.',
    }
    store = Mock()
    store.search.return_value = [doc]
    policy_imports.approve_policy_import('owner-a', doc, provider=provider)
    assert policy_imports.recall_owner_policy_imports('owner-b', 'Bluetooth', provider=provider, store=store) is None
    store.search.assert_not_called()
    result = policy_imports.recall_owner_policy_imports('owner-a', 'Bluetooth', provider=provider, store=store)
    assert result['items'][0]['reference'].startswith('ref:owner-policy:doc_abc:')
    store.search.return_value = [{**doc, 'raw_content': 'Bluetooth altered policy.'}]
    assert not policy_imports.recall_owner_policy_imports('owner-a', 'Bluetooth', provider=provider, store=store)['items']
    monkeypatch.setattr(web_sources, 'model_catalog_fingerprint', lambda: 'route-b')
    assert policy_imports.recall_owner_policy_imports('owner-a', 'Bluetooth', provider=provider, store=store) is None


def test_html_import_strips_active_content(tmp_path):
    path = tmp_path / 'policy.html'
    path.write_text('<html><body><script>hidden script</script><h2>Policy</h2>'
                    '<p>Bluetooth MUST support the requirement.</p></body></html>', encoding='utf-8')
    result = parse_file(str(path))
    assert 'Bluetooth MUST support' in result['text']
    assert 'hidden script' not in result['text']
