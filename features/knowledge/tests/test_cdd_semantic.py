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


def test_public_scheduler_resumes_batches_and_rotates_after_failure(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    import features.retrieval as retrieval
    from features.knowledge.external import web_sources

    config = {"retrieval": {"auto_index_public_cdd": True}}
    monkeypatch.setattr(web_sources.config_manager, "load_config", lambda: config)
    monkeypatch.setattr(retrieval, "embedding_configuration_issue", lambda: None)
    monkeypatch.setattr(cdd_semantic, "local_embedding", lambda: Encoder())
    path = tmp_path / "web.sqlite3"
    provider = WebKnowledgeProvider(path)
    ids = []
    for version in (16, 17):
        url = f"https://source.android.com/docs/compatibility/{version}/android-{version}-cdd"
        source = provider.save(name=f"CDD {version}", url=url, kind="cdd",
                               android_version=version, license="CC-BY-4.0")
        ids.append(source)
        with provider._db(write=True) as db:
            db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source,))
            db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r1", "2026-01-01"))
            for i in range(2):
                db.execute("INSERT INTO clauses VALUES(?,?,?,?,?,?,?,?,?)",
                           (source, "r1", "Memory", "", "MUST", "", "memory",
                            url, f"{source}-{i}"))
    first, second = sorted(ids)

    def fail_once(source_id, **kwargs):
        assert source_id == first
        raise RuntimeError("private error")

    monkeypatch.setattr(provider, "sync_semantic", fail_once)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = provider.sync_due_semantic(now=now, batch_size=1)
    assert result == [{"source_id": first, "status": "error", "coverage_complete": False,
                       "detail": "RuntimeError", "operation": "semantic_index"}]
    # New instance simulates a controller restart; the attempt order is durable.
    restarted = WebKnowledgeProvider(path)
    for offset, expected in enumerate((second, first, second, first), 1):
        result = restarted.sync_due_semantic(now=now + timedelta(minutes=offset), batch_size=1)
        assert result[0]["source_id"] == expected
        assert result[0]["indexed"] == (1 if offset <= 2 else 2)
    assert restarted.sync_due_semantic() == []
    config["retrieval"]["auto_index_public_cdd"] = "true"
    assert restarted.sync_due_semantic() == []


def test_dense_recall_rejects_manifest_change_during_query(tmp_path):
    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    url = "https://source.android.com/docs/compatibility/16/android-16-cdd"
    source = provider.save(name="CDD 16", url=url, kind="cdd",
                           android_version=16, license="CC-BY-4.0")
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source,))
        db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r1", "2026-01-01"))
        db.execute("INSERT INTO clauses VALUES(?,?,?,?,?,?,?,?,?)",
                   (source, "r1", "Memory", "", "MUST", "", "memory", url, "old-token"))
    assert cdd_semantic.CddSemantic(provider, embedder=Encoder()).sync(source)["coverage_complete"]

    class ChangingEncoder(Encoder):
        def encode(self, texts):
            with provider._db(write=True) as db:
                db.execute("UPDATE sources SET revision='r2' WHERE id=?", (source,))
                db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r2", "2026-01-02"))
            return super().encode(texts)

    hits, status = cdd_semantic.CddSemantic(provider, embedder=ChangingEncoder()).search("memory")
    assert hits == []
    assert status["status"] == "stale" and not status["coverage_complete"]


def test_model_context_only_recalls_fresh_public_cdd(tmp_path, monkeypatch):
    from features.knowledge import retrieve_public_cdd_context
    from features.knowledge.external import web_sources

    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    monkeypatch.setattr(web_sources, "index_path", lambda: provider._path())
    monkeypatch.setattr(cdd_semantic, "local_embedding", lambda: Encoder())
    urls = (
        ("https://source.android.com/docs/compatibility/16/android-16-cdd", "cdd", 16),
        ("https://source.android.com/docs/compatibility/17/android-17-cdd", "cdd", 17),
        ("https://example.com/document", "document", None),
    )
    ids = []
    for i, (url, kind, version) in enumerate(urls):
        source = provider.save(name=f"Source {i}", url=url, kind=kind,
                               android_version=version, license="approved")
        ids.append(source)
        with provider._db(write=True) as db:
            db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source,))
            db.execute("INSERT INTO revisions VALUES(?,?,?)", (source, "r1", "2026-01-01"))
            db.execute("INSERT INTO clauses VALUES(?,?,?,?,?,?,?,?,?)",
                       (source, "r1", "Memory", "", "MUST", "", "memory", url,
                        "POL-" + str(i) * 24))
    for source in ids[:2]:
        assert provider.sync_semantic(source)["coverage_complete"]
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET error='OSError' WHERE id=?", (ids[1],))
    hits, status = provider.search_with_status("memory", public_cdd_only=True)
    assert [hit.source_path for hit in hits] == ["POL-" + "0" * 24]
    assert status["total"] == status["indexed"] == 1
    context = retrieve_public_cdd_context("memory")
    assert [item["reference"] for item in context] == ["POL-" + "0" * 24]
    assert not context[0]["applicability_verified"]


def test_wiki_scheduler_shares_budget_and_requires_current_approval(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock

    import features.retrieval as retrieval
    from features.knowledge.external import federation, web_sources

    provider = WebKnowledgeProvider(tmp_path / "web.sqlite3")
    config = {"retrieval": {"auto_index_public_cdd": True, "auto_index_approved_wiki": True}}
    monkeypatch.setattr(web_sources.config_manager, "load_config", lambda: config)
    monkeypatch.setattr(retrieval, "embedding_configuration_issue", lambda *args: None)
    source_id = provider.save(name="CDD", url="https://source.android.com/docs/compatibility/16/android-16-cdd",
                              kind="cdd", android_version=16, license="CC-BY-4.0")
    with provider._db(write=True) as db:
        db.execute("UPDATE sources SET revision='r1' WHERE id=?", (source_id,))
    monkeypatch.setattr(provider, "semantic_status", lambda _id: {"status": "partial", "coverage_complete": False})
    cdd_sync = Mock(return_value={"status": "partial", "coverage_complete": False})
    monkeypatch.setattr(provider, "sync_semantic", cdd_sync)
    wiki = Mock()
    wiki.status.return_value = SimpleNamespace(status="ready", source_revision="w1",
                                               approved_revision="w1", available_revision="w2")
    wiki.semantic_status.return_value = {"status": "partial", "coverage_complete": False}
    wiki.sync_semantic.return_value = {"status": "ready", "coverage_complete": True}
    monkeypatch.setattr(federation, "federated_service", lambda: SimpleNamespace(provider=lambda _id: wiki))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert provider.sync_due_semantic(now=now)[0]["source_id"] == source_id
    assert provider.sync_due_semantic(now=now + timedelta(minutes=1))[0]["status"] == "stale"
    wiki.sync_semantic.assert_not_called()
    assert provider.semantic_schedule_status("android_internals", wiki=True)["last_auto_index_attempt"]
    wiki.status.return_value.available_revision = "w1"
    assert provider.sync_due_semantic(now=now + timedelta(minutes=2))[0]["source_id"] == source_id
    assert provider.sync_due_semantic(now=now + timedelta(minutes=3))[0]["source_id"] == "android_internals"
    wiki.sync_semantic.assert_called_once_with(batch_size=128)
    config["retrieval"]["auto_index_approved_wiki"] = False
    assert not provider.semantic_schedule_status("android_internals", wiki=True)["auto_index_enabled"]
