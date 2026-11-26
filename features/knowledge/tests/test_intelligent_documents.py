"""Owner-scoped document understanding, model changes and derived recall."""
import json

from features.knowledge import intelligent
from features.knowledge.external import web_sources
from features.knowledge.storage import KnowledgeStore
from features.retrieval import PassageInsightIndex, RetrievalHit, passage_token


def test_personal_document_understanding_is_owner_revision_and_model_bound(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "kb.sqlite3", tmp_path / "files")
    monkeypatch.setattr(web_sources, "index_path", lambda: tmp_path / "external.sqlite3")
    config = {"retrieval": {"allow_local_personal_llm_understanding": True}}
    ai = {"enabled": True, "primary_provider": "local", "providers": {
        "local": {"enabled": True, "model": "fixture", "base_url": "http://127.0.0.1:3000"},
    }}
    monkeypatch.setattr(intelligent.config_manager, "load_config", lambda: config)
    monkeypatch.setattr(web_sources.config_manager, "get_ai_config", lambda: ai)
    doc = store.create_doc("owner-a", space_id=store.default_space_id("owner-a"),
                           title="Policy", content_md="Handheld devices MUST support feature X.")
    store.create_doc("owner-b", space_id=store.default_space_id("owner-b"),
                     title="Other", content_md="Handheld devices MUST support feature Y.")
    source = intelligent.personal_source(store, "owner-a")
    manifest, complete = source.manifest()
    assert complete and len(manifest) == 1
    index = intelligent.personal_insight_index(store, "owner-a")

    def model(prompt, **kwargs):
        rows = json.loads(prompt)["passages"]
        assert len(rows) == 1 and "feature Y" not in prompt
        return {"success": True, "content": json.dumps({"items": [{
            "id": rows[0]["id"], "terms": ["uniquefeaturealias"],
            "facts": [{"kind": "requirement", "statement": "Feature X", "quote": "MUST support feature X."}],
        }]})}

    result = index.sync(manifest, source.current, model, complete=complete)
    assert result["coverage_complete"] and result["enriched"] == 1
    hits = index.search(["uniquefeaturealias"], source.resolve)
    assert len(hits) == 1 and hits[0].metadata["doc_id"] == doc["doc_id"]
    other = intelligent.personal_source(store, "owner-b")
    assert not intelligent.personal_insight_index(store, "owner-b").search(["uniquefeaturealias"], other.resolve)
    ai["providers"]["local"]["model"] = "new"
    assert not intelligent.personal_insight_index(store, "owner-a").search(["uniquefeaturealias"], source.resolve)
    ai["providers"]["local"]["model"] = "fixture"
    store.update_doc("owner-a", doc["doc_id"], {"raw_content": "updated", "content_md": "updated"})
    assert not index.search(["uniquefeaturealias"], source.resolve)


def test_missing_llm_output_is_retryable_not_complete(tmp_path):
    hit = RetrievalHit("test", "doc:0", "r1", "Original text")
    token = passage_token(hit)
    index = PassageInsightIndex(tmp_path / "insights.sqlite3", "model-a")
    result = index.sync({token: hit}, lambda _: True,
                        lambda *_args, **_kwargs: {"success": True, "content": '{"items":[]}'},
                        complete=True)
    assert result["errors"] == 1
    assert result["processed"] == 0
    assert not result["coverage_complete"]
    assert not index.search(["Original"], lambda *_: hit)


def test_revoked_passage_is_not_persisted(tmp_path):
    hit = RetrievalHit("test", "doc:0", "r1", "Original text")
    token = passage_token(hit)
    index = PassageInsightIndex(tmp_path / "insights.sqlite3", "model-a")
    authorized = True

    def model(prompt, **kwargs):
        nonlocal authorized
        authorized = False
        return {"success": True, "content": json.dumps({"items": [{
            "id": token, "terms": ["alias"],
            "facts": [{"kind": "definition", "statement": "Original", "quote": "Original text"}],
        }]})}

    result = index.sync({token: hit}, lambda _: authorized, model, complete=True)
    assert result["processed"] == 0
    assert not result["coverage_complete"]
    assert not index.search(["alias"], lambda *_: hit)
