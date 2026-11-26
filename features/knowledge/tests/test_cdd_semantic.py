"""Public CDD semantic retrieval without external model dependencies."""

from features.knowledge.external import cdd_semantic
from features.knowledge.external.web_sources import WebKnowledgeProvider


class Encoder:
    identity = "fixture-model-v1"

    def encode(self, texts):
        return [[1.0, 0.0] if "memory" in text or "运行容量" in text else [0.0, 1.0] for text in texts]


def test_public_semantic_recall_without_lexical_hit(tmp_path, monkeypatch):
    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    monkeypatch.setattr(cdd_semantic, "local_embedding", lambda: Encoder())
    source = provider.save(name="CDD 16", url="https://source.android.com/docs/compatibility/16/android-16-cdd",
                           kind="cdd", android_version=16, license="CC-BY-4.0")
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source,))
        db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r1", "2026-01-01"))
        db.execute("INSERT INTO clauses VALUES(?,?,?,?,?,?,?,?,?)",
                   (source, "r1", "Memory", "C-1-1", "MUST", "For devices", "memory capacity",
                    "https://source.android.com/docs/compatibility/16/android-16-cdd#memory", "POL-abc"))
    assert provider.sync_semantic(source)["coverage_complete"]
    assert not provider._lexical_search("运行容量", android_api_level=36)
    hits, status = provider.search_with_status("运行容量", android_api_level=36)
    assert status["coverage_complete"] and hits[0].source_path == "POL-abc"
    assert hits[0].source_revision == "r1" and hits[0].evidence_level == "background"
    assert not provider.search("运行容量", android_api_level=37)
    provider.save(source_id=source, name="CDD 16",
                  url="https://source.android.com/docs/compatibility/16/android-16-cdd",
                  kind="cdd", android_version=16, license="CC-BY-4.0", enabled=False)
    assert not provider.search("运行容量", android_api_level=36)


def test_unconfigured_embedding_keeps_lexical_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(cdd_semantic, "local_embedding", lambda: None)
    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    assert provider.search_with_status("memory") == ([], {
        "status": "disabled", "coverage_complete": False, "indexed": 0, "total": 0
    })
