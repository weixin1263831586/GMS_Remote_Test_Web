"""Local LLM document ingestion and revision-bound recall."""
import json

from features.knowledge.external import cdd_intelligence, web_sources
from features.knowledge.external.web_sources import WebKnowledgeProvider


def test_understanding_batch_contributes_recall_but_never_replaces_original(tmp_path, monkeypatch):
    config = {"retrieval": {"auto_understand_public_cdd": True}}
    ai = {"enabled": True, "primary_provider": "local", "providers": {
        "local": {"enabled": True, "model": "fixture", "base_url": "http://127.0.0.1:3000"},
    }}
    monkeypatch.setattr(web_sources.config_manager, "load_config", lambda: config)
    monkeypatch.setattr(web_sources.config_manager, "get_ai_config", lambda: ai)
    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    url = "https://source.android.com/docs/compatibility/16/android-16-cdd"
    source = provider.save(name="CDD 16", url=url, kind="cdd", android_version=16, license="CC-BY-4.0")
    token = "POL-" + "a" * 24
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source,))
        db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r1", "2026-01-01"))
        db.execute("INSERT INTO clauses VALUES(?,?,?,?,?,?,?,?,?)",
                   (source, "r1", "Memory", "", "MUST", "", "RAM MUST be 4 GB.", url, token))

    def model(prompt, **_kwargs):
        assert json.loads(prompt)["passages"][0]["id"] == token
        return {"success": True, "content": json.dumps({"items": [{
            "id": token, "terms": ["capacityalias"],
            "facts": [{"kind": "requirement", "statement": "Memory requirement", "quote": "RAM MUST be 4 GB."},
                      {"kind": "requirement", "statement": "Invalid", "quote": "8 GB"}],
        }]})}

    key = cdd_intelligence.enabled_key()
    assert provider.search("capacityalias") == []
    result = cdd_intelligence.sync_batch(provider, model, key)
    assert result["coverage_complete"] and result["enriched"] == 1
    hits, retrieval = provider.search_with_status("capacityalias", public_cdd_only=True)
    assert hits[0].source_path == token and hits[0].snippet == "RAM MUST be 4 GB."
    assert retrieval["document_understanding_hits"] == 1
    with provider._db() as db:
        stored = json.loads(db.execute("SELECT facts_json FROM cdd_insights").fetchone()[0])
    assert len(stored["facts"]) == 1
    assert cdd_intelligence.sync_batch(provider, model, key)["coverage_complete"]
    ai["providers"]["local"]["model"] = "changed"
    assert provider.search("capacityalias") == []
    ai["providers"]["local"]["model"] = "fixture"
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET revision='r2' WHERE id=?", (source,))
    assert provider.search("capacityalias") == []
