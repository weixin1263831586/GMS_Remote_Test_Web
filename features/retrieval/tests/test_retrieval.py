import json

import pytest

from features.retrieval import (
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalService,
    VectorIndex,
    compact_observation,
    evaluate_ranking,
    fuse_rankings,
)


class Encoder:
    def encode(self, texts):
        return [[1.0, 0.0] if "memory" in text else [0.0, 1.0] for text in texts]


def test_vector_manifest_revision_and_model_isolation(tmp_path):
    index = VectorIndex(tmp_path / "vectors.sqlite3")
    encoder = Encoder()
    assert index.upsert("public", "v1", {"old": "memory", "new": "audio"}, encoder) == 2
    assert index.indexed("public", "v1", ["new"]) == {"new"}
    assert index.search("public", "v1", ["new"], [1, 0]) == [("new", 0)]
    assert index.search("other", "v1", ["old"], [1, 0]) == []
    assert index.search("public", "v2", ["old"], [1, 0]) == []
    index.prune("public", "v1", ["new"])
    assert index.indexed("public", "v1", ["old", "new"]) == {"new"}


def test_invalid_vector_does_not_publish_partial_batch(tmp_path):
    class Invalid:
        def encode(self, texts):
            return [[1, 0], [float("nan"), 0]]
    index = VectorIndex(tmp_path / "vectors.sqlite3")
    with pytest.raises(ValueError):
        index.upsert("public", "v1", {"a": "a", "b": "b"}, Invalid())
    assert not index.indexed("public", "v1", ["a", "b"])


def test_rrf_exact_match_and_evaluation():
    assert fuse_rankings([["a", "b"], ["b", "c"]], limit=3) == ["b", "a", "c"]
    assert fuse_rankings([["a", "b"], ["b", "c"]], limit=3, exact_keys=["c"])[0] == "c"
    metrics = evaluate_ranking(["a", "b", "c"], ["b", "c"], k=2)
    assert metrics["recall_at_k"] == 0.5 and metrics["mrr_at_k"] == 0.5
    assert evaluate_ranking([], [], k=1)["ndcg_at_k"] is None


def test_provider_failure_and_revision_filter():
    class Good:
        source_id = "public"
        def search(self, query):
            return RetrievalResult([RetrievalHit("public", "a", "v1", "text"),
                                    RetrievalHit("other", "b", "v1", "text")], "ready", True)
    class Bad:
        source_id = "unavailable"
        def search(self, query):
            raise OSError("private error detail")
    result = RetrievalService([Good(), Bad()]).search(RetrievalQuery("query", revision="v1"))
    assert len(result["results"]) == 1
    assert not result["coverage_complete"]
    assert result["sources_status"][1]["detail"] == "OSError"


def test_observations_remain_valid_json_and_preserve_status():
    payload = {"success": True, "data": {"coverage_complete": False,
               "sources_status": [{"source": "cdd", "status": "error"}],
               "results": [{"source_revision": "rev", "reference": "ref", "snippet": "字" * 8000}]}}
    value = compact_observation(payload, max_bytes=2000)
    assert len(value.encode()) <= 2000
    result = json.loads(value)
    assert result["observation_truncated"] and result["success"]
    assert result["data"]["sources_status"][0]["status"] == "error"


def test_embedding_configuration_diagnostics(tmp_path, monkeypatch):
    from features.retrieval import embedding_configuration_issue
    from features.retrieval import semantic

    config = {}
    monkeypatch.setattr(semantic.config_manager, "load_config", lambda: config)
    assert embedding_configuration_issue() == "disabled"
    config["retrieval"] = {"enabled": True}
    assert embedding_configuration_issue("personal_knowledge") == "source_disabled"
    assert embedding_configuration_issue() == "model_unconfigured"
    config["retrieval"].update(model_path=str(tmp_path / "missing"), model_revision="v1")
    assert embedding_configuration_issue() == "model_missing"
    config["retrieval"]["model_path"] = str(tmp_path)
    monkeypatch.setattr(semantic.importlib.util, "find_spec", lambda name: None)
    assert embedding_configuration_issue() == "dependency_missing"
    monkeypatch.setattr(semantic.importlib.util, "find_spec", lambda name: object())
    assert embedding_configuration_issue() is None
    config["retrieval"]["enabled_sources"] = "cdd"
    assert embedding_configuration_issue() == "invalid_config"


def test_rejected_hits_prevent_complete_coverage():
    class Provider:
        source_id = "test"

        def search(self, query):
            return RetrievalResult([RetrievalHit("test", "ok", "v1", "text"),
                                    RetrievalHit("other", "invalid", "v1", "text")], "ready", True)

    result = RetrievalService([Provider()]).search(RetrievalQuery("q"))
    assert not result["coverage_complete"]
    assert result["sources_status"][0]["rejected_hits"] == 1


def test_vector_dimensions_are_stable_across_batches(tmp_path):
    index = VectorIndex(tmp_path / "index.sqlite3")
    index.upsert("source", "model", {"old": "memory"}, Encoder())

    class WrongDimension:
        def encode(self, texts):
            return [[1, 0, 0] for _ in texts]

    with pytest.raises(ValueError, match="model_revision"):
        index.upsert("source", "model", {"new": "test"}, WrongDimension())
    assert index.indexed("source", "model", ["old", "new"]) == {"old"}
    assert index.upsert("source", "model-v2", {"new": "test"}, WrongDimension()) == 1


def test_vector_sync_lock_is_namespace_scoped_and_released(tmp_path):
    first = VectorIndex(tmp_path / "vectors.sqlite3")
    second = VectorIndex(first.path)
    with first.sync_lock("owner-a") as acquired:
        assert acquired
        with second.sync_lock("owner-a") as duplicate:
            assert not duplicate
        with second.sync_lock("owner-b") as independent:
            assert independent
    with second.sync_lock("owner-a") as acquired:
        assert acquired
