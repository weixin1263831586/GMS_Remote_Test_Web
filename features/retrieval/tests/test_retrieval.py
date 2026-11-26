import json

import pytest

from features.retrieval import (
    RankedBatch,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalService,
    VectorIndex,
    collect_ranked_sources,
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


def test_shared_fanout_keeps_source_failure_and_total_result_bound():
    def fail():
        raise OSError("do not expose provider details")

    hits, statuses = collect_ranked_sources([
        ("first", lambda: RankedBatch(["a", "b"], {"status": "ready", "coverage_complete": True})),
        ("broken", fail),
        ("second", lambda: RankedBatch(["c", "a"], {"status": "ready", "coverage_complete": True})),
    ], limit=2, identity=lambda hit: hit)
    assert hits == ["a", "c"]
    assert statuses[1]["detail"] == "OSError"
    assert all(not status["coverage_complete"] for status in statuses)


def test_pending_tokens_and_coverage_resume_authorized_manifest(tmp_path):
    index = VectorIndex(tmp_path / "vectors.sqlite3")
    index.upsert("owner", "v1", {"a": "memory", "obsolete": "audio"}, Encoder())
    assert index.pending_tokens("owner", "v1", ["c", "a", "b", "c"]) == ["c", "b"]
    assert index.coverage("owner", "v1", ["a", "b"]) == {
        "status": "partial", "indexed": 1, "total": 2, "coverage_complete": False,
    }
    assert not index.coverage("owner", "v1", ["a"], corpus_complete=False)["coverage_complete"]
    assert index.coverage("owner", "v1", ["a"])["coverage_complete"]
    assert index.pending_tokens("other", "v1", ["a"]) == ["a"]


def test_semantic_model_configuration_requires_explicit_opt_in(monkeypatch):
    from features.retrieval import semantic

    config = {"retrieval": {"enabled": "false", "enabled_sources": ["redmine_cases"],
                            "model_path": "/local/model", "model_revision": "v1",
                            "reranker_path": "/local/ranker", "reranker_revision": "v1"}}
    monkeypatch.setattr(semantic.config_manager, "load_config", lambda: config)
    assert semantic.embedding_configuration_issue("redmine_cases") == "invalid_config"
    assert semantic.local_embedding("redmine_cases") is None
    assert semantic.local_reranker("redmine_cases") is None
    config["retrieval"] = ["invalid"]
    assert semantic.local_embedding() is None
    assert semantic.local_reranker() is None


def test_semantic_model_revision_must_be_nonempty_string(monkeypatch):
    from features.retrieval import semantic

    config = {"retrieval": {"enabled": True, "model_path": "/local/model",
                            "model_revision": None, "reranker_path": "/local/ranker",
                            "reranker_revision": 1}}
    monkeypatch.setattr(semantic.config_manager, "load_config", lambda: config)
    with pytest.raises(ValueError, match="embedding path and revision"):
        semantic.local_embedding()
    with pytest.raises(ValueError, match="reranker path and revision"):
        semantic.local_reranker()


def test_paired_ranking_evaluation_excludes_unlabelled_cases():
    from features.retrieval import RankingEvaluationCase, evaluate_ranking_comparison

    cases = [
        RankingEvaluationCase("one", ["a"], ["b", "a"], ["a", "b"]),
        RankingEvaluationCase("two", ["c"], [], ["c"]),
        RankingEvaluationCase("unlabelled", [], ["x"], ["x"]),
    ]
    result = evaluate_ranking_comparison(cases, k=1)
    assert result["labelled_cases"] == 2 and result["unlabelled_cases"] == 1
    assert result["metrics"]["recall_at_k"] == {"baseline": 0.0, "candidate": 1.0, "delta": 1.0}
    assert evaluate_ranking_comparison(cases[2:])["metrics"]["recall_at_k"]["delta"] is None
    with pytest.raises(ValueError, match="unique"):
        evaluate_ranking_comparison([cases[0], cases[0]])


def test_paired_root_cause_review_keeps_abstentions_and_missing_reviews_visible():
    from features.retrieval import evaluate_root_cause_comparison

    result = evaluate_root_cause_comparison([
        ("one", "incorrect", "correct"),
        ("two", "correct", "abstained"),
        ("three", "unreviewed", "correct"),
    ])
    assert result["paired_cases"] == 2 and result["unreviewed_cases"] == 1
    assert result["baseline"]["accuracy"] == result["candidate"]["accuracy"] == 0.5
    assert result["candidate"]["answered_accuracy"] == 1.0
    assert result["candidate"]["abstained"] == 1
    assert result["accuracy_delta"] == 0.0
    assert evaluate_root_cause_comparison([])["accuracy_delta"] is None
