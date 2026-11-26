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
    result = parse_file(str(path), policy_import=True)
    assert 'Bluetooth MUST support' in result['text']
    assert 'hidden script' not in result['text']


def test_general_ask_skips_restricted_imports(tmp_path, monkeypatch):
    from features.knowledge.service import KnowledgeService
    from features.knowledge.storage import KnowledgeStore

    monkeypatch.setattr(web_sources, 'index_path', lambda: tmp_path / 'external.sqlite3')
    store = KnowledgeStore(db_path=tmp_path / 'knowledge.sqlite3', attachment_dir=tmp_path / 'files')
    for title, source in [('Ordinary document', 'manual'), ('Restricted document', 'authorized_policy_import')]:
        store.create_doc('owner-a', space_id='gms', title=title, content_md='Bluetooth', source=source)
    import json

    config = {"enabled": True, "primary_provider": "local", "providers": {
        "local": {"enabled": True, "model": "fixture", "base_url": "http://127.0.0.1:3000"},
    }}
    monkeypatch.setattr(web_sources.config_manager, "get_ai_config", lambda: config)
    analyzer = Mock(config=config)
    analyzer.rag_local_provider.return_value = "local"

    def infer(prompt, **_kwargs):
        payload = json.loads(prompt)
        stage = payload["stage"]
        assert "Restricted document" not in prompt
        if stage == "plan":
            assert any(row["title"] == "Ordinary document" for row in payload["documents"])
            return {"success": True, "content": '{"searches":[]}'}
        if stage == "inspect":
            assert "Ordinary document" in prompt
            return {"success": True, "content": '{"reads":[]}'}
        if stage == "draft":
            return {"success": True, "content": json.dumps({"claims": [
                {"statement": "Bluetooth", "citations": [{"id": "E1", "quote": "Bluetooth"}]},
            ]})}
        return {"success": True, "content": '{"verdicts":[{"id":"C1","supported":true}]}'}

    analyzer.generate_local_rag.side_effect = infer
    monkeypatch.setattr('features.knowledge.service._ask_analyzer_factory', lambda: analyzer)
    result = KnowledgeService(store).ask('owner-a', 'Bluetooth')
    assert result['mode'] == 'ai'
    assert result["rag"]["status"] == "model_reviewed"
    analyzer.generate.assert_not_called()
    assert all(row["title"] == "Ordinary document" for row in result["contexts"])


def test_partner_grant_requires_upstream_account_verification(monkeypatch):
    from types import SimpleNamespace

    from features.knowledge import external_api

    monkeypatch.setattr(external_api, 'require_elevated_admin', lambda _request: SimpleNamespace(resource_owner_id='owner-a'))
    payload = external_api.PartnerGrantRequest(enabled=True, authorized_for_ai_processing=True)
    assert external_api.update_partner_policy_grant(None, payload).status_code == 409


def test_model_catalog_fingerprint_binds_primary_and_fallback_order():
    from copy import deepcopy

    config = {"enabled": True, "primary_provider": "a", "providers": {
        name: {"enabled": True, "model": name, "base_url": f"https://{name}.example.test",
               "api_format": "openai", "api_key": "secret"}
        for name in ("a", "b", "c")
    }}
    fingerprint = web_sources.model_catalog_fingerprint(config)
    assert fingerprint
    changed = deepcopy(config)
    changed["primary_provider"] = "b"
    assert web_sources.model_catalog_fingerprint(changed) != fingerprint
    changed = deepcopy(config)
    changed["providers"] = {name: changed["providers"][name] for name in ("a", "c", "b")}
    assert web_sources.model_catalog_fingerprint(changed) != fingerprint
    changed = deepcopy(config)
    changed["providers"]["a"]["api_key"] = "rotated"
    assert web_sources.model_catalog_fingerprint(changed) == fingerprint
    changed["providers"]["b"]["model"] = ""
    assert web_sources.model_catalog_fingerprint(changed) == ""
    changed = deepcopy(config)
    changed["providers"]["a"]["enabled"] = "false"
    assert web_sources.model_catalog_fingerprint(changed) == ""
    changed = deepcopy(config)
    changed["providers"]["a"]["base_url"] += "?token=secret"
    assert web_sources.model_catalog_fingerprint(changed) == ""


def test_route_change_invalidates_existing_owner_import_grant(tmp_path, monkeypatch):
    config = {"enabled": True, "primary_provider": "a", "providers": {
        name: {"enabled": True, "model": name, "base_url": f"https://{name}.example.test"}
        for name in ("a", "b")
    }}
    monkeypatch.setattr(web_sources.config_manager, "get_ai_config", lambda: config)
    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    provider.approve_import("owner", "doc", "digest")
    assert provider.approved_imports("owner") == {"doc": "digest"}
    config["primary_provider"] = "b"
    assert provider.approved_imports("owner") == {}
