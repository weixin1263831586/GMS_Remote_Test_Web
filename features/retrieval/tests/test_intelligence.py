"""Model-led retrieval tests with deterministic local model responses."""
import json

from features.retrieval import IntelligentRAG, IntelligentSource, RetrievalHit, RetrievalResult


def response(**data):
    return {"success": True, "content": json.dumps(data)}


def test_rewrites_missed_query_reads_authorized_document_and_reviews_claim():
    initial = RetrievalHit("docs", "doc:0", "r1", "Definitions", metadata={"title": "Policy"})
    later = RetrievalHit("docs", "doc:2800", "r1", "For handheld devices, RAM MUST be at least 4 GB.")
    queries = []
    reads = []
    stages = []

    def search(query, _limit):
        queries.append(query)
        return RetrievalResult([], "no_match")

    def read(hit, page):
        reads.append((hit.reference, page))
        return RetrievalResult([later], "matched")

    def model(prompt, **_kwargs):
        payload = json.loads(prompt)
        stage = payload["stage"]
        stages.append(stage)
        if stage == "plan":
            assert payload["documents"] == [{"id": "D1", "source": "docs", "title": "Policy"}]
            return response(searches=[{"source": "docs", "query": "handheld RAM"},
                                      {"source": "unauthorized", "query": "private"}], open=["D1", "D999"])
        if stage == "inspect":
            if len(payload["evidence"]) == 1:
                return response(reads=[{"id": "E1", "page": 1}, {"id": "E999", "page": 0}])
            return response(searches=[], reads=[])
        if stage == "draft":
            return response(claims=[
                {"statement": "手持设备至少 4 GB RAM", "citations": [{"id": "E2", "quote": "RAM MUST be at least 4 GB."}]},
                {"statement": "所有设备至少 8 GB", "citations": [{"id": "E2", "quote": "8 GB"}]},
            ])
        assert stage == "review"
        assert len(payload["claims"]) == 1
        return response(verdicts=[{"id": "C1", "supported": True}])

    source = IntelligentSource("docs", "Authorized docs", search, lambda _hit: True, read, lambda: [initial])
    result = IntelligentRAG([source], model).run("非 Go 内存要求")
    assert queries == ["非 Go 内存要求", "handheld RAM"]
    assert reads == [("doc:0", 1)]
    assert stages == ["plan", "inspect", "inspect", "draft", "review"]
    assert result["status"] == "model_reviewed"
    assert result["claims"][0]["statement"] == "手持设备至少 4 GB RAM"
    assert not result["coverage_complete"] and not result["independent_verification"]


def test_revoked_document_is_not_sent_to_subsequent_model_or_returned():
    hit = RetrievalHit("docs", "doc", "r1", "Protected text")
    allowed = True
    stages = []

    def current(_hit):
        return allowed

    def model(prompt, **_kwargs):
        nonlocal allowed
        payload = json.loads(prompt)
        stages.append(payload["stage"])
        if payload["stage"] == "plan":
            return response(searches=[])
        allowed = False
        return response(reads=[])

    source = IntelligentSource("docs", "Owner docs", lambda *_: RetrievalResult([hit], "matched"), current)
    result = IntelligentRAG([source], model).run("policy")
    assert stages == ["plan", "inspect"]
    assert result["status"] == "authorization_changed"
    assert result["evidence"] == [] and result["claims"] == []


def test_local_model_failure_retains_search_candidates_without_generated_claims():
    hit = RetrievalHit("docs", "doc", "r1", "Original passage")
    source = IntelligentSource("docs", "Owner docs", lambda *_: RetrievalResult([hit], "matched"), lambda _: True)
    result = IntelligentRAG([source], lambda *_args, **_kw: {"success": False}).run("question")
    assert result["status"] == "model_unavailable"
    assert result["model_calls"] == 1
    assert result["claims"] == [] and result["evidence"][0]["text"] == "Original passage"


def test_review_disagreement_drops_claim_and_no_fabricated_citation():
    hit = RetrievalHit("docs", "doc", "r1", "Only handheld devices require 4 GB RAM.")
    replies = iter([
        response(searches=[]),
        response(reads=[]),
        response(claims=[
            {"statement": "所有设备都要求 4 GB", "citations": [{"id": "E1", "quote": "4 GB RAM"}]},
            {"statement": "编造引用", "citations": [{"id": "E999", "quote": "original"}]},
        ]),
        response(verdicts=[{"id": "C1", "supported": False}, {"id": "C999", "supported": True}]),
    ])
    source = IntelligentSource("docs", "Docs", lambda *_: RetrievalResult([hit], "matched"), lambda _: True)
    result = IntelligentRAG([source], lambda *_args, **_kw: next(replies)).run("RAM")
    assert result["status"] == "insufficient_evidence"
    assert result["claims"] == []
