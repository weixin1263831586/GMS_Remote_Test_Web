"""Private document chunk retrieval and fail-closed policy isolation."""

from features.knowledge import personal_semantic
from features.knowledge.personal_semantic import PersonalSemantic, retrieve_personal_contexts
from features.knowledge.storage import POLICY_IMPORT_SOURCE, KnowledgeStore


class Encoder:
    identity = "local-fixture"

    def encode(self, texts):
        return [[1.0, 0.0] if "thermal" in text or "散热" in text else [0.0, 1.0] for text in texts]


def test_chunk_search_owner_and_revision_isolation(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "kb.sqlite3", tmp_path / "attachments")
    monkeypatch.setattr(personal_semantic, "local_embedding", lambda source: Encoder())
    monkeypatch.setattr(personal_semantic, "policy_import_filter",
                        lambda owner: lambda doc: doc.get("source") == POLICY_IMPORT_SOURCE)
    space = store.default_space_id("u1")
    other = store.default_space_id("u2")
    doc = store.create_doc("u1", space_id=space, title="Long", content_md="x" * 2000 + " thermal mitigation")
    store.create_doc("u1", space_id=space, title="Restricted", content_md="thermal",
                     source=POLICY_IMPORT_SOURCE)
    store.create_doc("u2", space_id=other, title="Other", content_md="thermal")
    provider = PersonalSemantic(store, "u1")
    status = provider.sync(batch_size=128)
    assert status["coverage_complete"] and status["total"] == 3
    contexts, status = retrieve_personal_contexts(store, "u1", "散热", limit=5,
                                                   exclude_document=personal_semantic.policy_import_filter("u1"))
    assert status["coverage_complete"]
    assert [item["doc_id"] for item in contexts] == [doc["doc_id"]]
    assert "thermal mitigation" in contexts[0]["snippet"] and contexts[0]["start_char"] >= 1000
    assert PersonalSemantic(store, "u2").search("散热")[0] == []
    store.update_doc("u1", doc["doc_id"], {"raw_content": "new", "content_md": "new"})
    assert provider.search("散热")[0] == []


def test_registered_policy_exclusion_rechecked(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "kb.sqlite3", tmp_path / "attachments")
    monkeypatch.setattr(personal_semantic, "local_embedding", lambda source: Encoder())
    excluded = set()
    monkeypatch.setattr(personal_semantic, "policy_import_filter",
                        lambda owner: lambda doc: doc["doc_id"] in excluded)
    doc = store.create_doc("u1", space_id=store.default_space_id("u1"),
                           title="Normal", content_md="thermal")
    provider = PersonalSemantic(store, "u1")
    assert provider.sync()["indexed"] == 1
    excluded.add(doc["doc_id"])
    assert provider.search("散热")[0] == []
    assert provider.sync()["indexed"] == 0


def test_personal_embedding_redacts_before_splitting(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path / "kb.sqlite3", tmp_path / "attachments")
    monkeypatch.setattr(personal_semantic, "policy_import_filter", lambda owner: lambda doc: False)
    secret = "SECRETEXAMPLE" + "x" * 350
    body = "a " * 495 + "password=" + secret + "\nthermal"
    doc = store.create_doc("u1", space_id=store.default_space_id("u1"),
                           title="Example", content_md=body)

    class RecordingEncoder(Encoder):
        def encode(self, texts):
            assert all("SECRETEXAMPLE" not in text and "x" * 50 not in text for text in texts)
            return super().encode(texts)

    index = PersonalSemantic(store, "u1", embedder=RecordingEncoder())
    assert index.sync()["coverage_complete"]
    hits, _ = index.search("散热")
    assert hits and hits[0]["doc_id"] == doc["doc_id"]
    assert hits[0]["offset_basis"] == "redacted_document"
    assert secret not in hits[0]["snippet"]
