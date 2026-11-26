"""Typed retrieval orchestration and bounded structured evidence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from collections.abc import Callable, Hashable, Sequence
from typing import Any, Generic, Protocol, TypeVar


@dataclass(frozen=True)
class RetrievalQuery:
    text: str
    limit: int = 10
    android_api_level: int | None = None
    owner_id: str = ""
    revision: str = ""

    def __post_init__(self):
        if not self.text.strip() or len(self.text) > 8192 or not 1 <= self.limit <= 100:
            raise ValueError("Invalid retrieval query or limit")


@dataclass(frozen=True)
class RetrievalHit:
    source: str
    reference: str
    revision: str
    text: str
    evidence_level: str = "candidate"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def identity(self):
        return self.source, self.reference, self.revision


@dataclass(frozen=True)
class RetrievalResult:
    hits: list[RetrievalHit]
    status: str
    coverage_complete: bool = False
    detail: str = ""


class RetrievalSource(Protocol):
    """A request-scoped provider; caller binds authorization before registration."""

    source_id: str

    def search(self, query: RetrievalQuery) -> RetrievalResult: ...


def fuse_rankings(rankings, *, limit, exact_keys=(), k=60, tie_key=None):
    """RRF over ordered identity lists; never compare raw provider scores."""
    if k < 1 or limit < 1:
        raise ValueError("Invalid ranking budget")
    scores = {}
    first = {}
    for ranking in rankings:
        for rank, key in enumerate(dict.fromkeys(ranking), 1):
            first.setdefault(key, len(first))
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    exact = set(exact_keys)
    return sorted(scores, key=lambda key: (key not in exact, -scores[key],
                                          tie_key(key) if tie_key else first[key]))[:limit]


_Hit = TypeVar("_Hit")


@dataclass(frozen=True)
class RankedBatch(Generic[_Hit]):
    """An authorized source's ordered candidates and independently reported health."""

    hits: Sequence[_Hit]
    status: dict[str, Any]


def collect_ranked_sources(
    sources: Sequence[tuple[str, Callable[[], RankedBatch[_Hit]]]],
    *,
    limit: int,
    identity: Callable[[_Hit], Hashable],
    tie_key: Callable[[_Hit], Any] | None = None,
) -> tuple[list[_Hit], list[dict[str, Any]]]:
    """Shared fan-out, failure isolation and rank fusion; never load a business provider."""
    if not 1 <= limit <= 100 or len(sources) > 16:
        raise ValueError("Invalid retrieval budget")
    if len({source for source, _ in sources}) != len(sources):
        raise ValueError("Duplicate retrieval source")
    representatives = {}
    representative_ranks = {}
    rankings = []
    statuses = []
    for source, fetch in sources:
        try:
            batch = fetch()
            if not isinstance(batch, RankedBatch):
                raise TypeError("Invalid ranked batch")
            # Validate the whole bounded source result before merging any of it.
            candidates = [(identity(hit), hit) for hit in batch.hits[:limit]]
            ranking = list(dict.fromkeys(key for key, _ in candidates))
            first_hits = {}
            for key, hit in candidates:
                first_hits.setdefault(key, hit)
            status = {**batch.status, "source": source}
            if len(batch.hits) > limit:
                status["coverage_complete"] = False
                status["limited"] = True
            for rank, key in enumerate(ranking):
                if key not in representative_ranks or rank < representative_ranks[key]:
                    representatives[key] = first_hits[key]
                    representative_ranks[key] = rank
            rankings.append(ranking)
            statuses.append(status)
        except Exception as exc:
            statuses.append({"source": source, "status": "error", "coverage_complete": False,
                             "detail": type(exc).__name__})
    keys = fuse_rankings(rankings, limit=limit,
                         tie_key=(lambda key: tie_key(representatives[key])) if tie_key else None)
    if len(representatives) > limit:
        for status in statuses:
            status["coverage_complete"] = False
            status["result_limit_reached"] = True
    return [representatives[key] for key in keys], statuses


class RetrievalService:
    """Fan out bounded requests to injected providers; isolate failures."""

    def __init__(self, sources: list[RetrievalSource]):
        if len(sources) > 16 or len({item.source_id for item in sources}) != len(sources):
            raise ValueError("Invalid retrieval source set")
        self.sources = tuple(sources)

    def search(self, query: RetrievalQuery):
        def search_provider(provider):
            result = provider.search(query)
            if not isinstance(result, RetrievalResult):
                raise TypeError("Invalid provider result")
            hits = []
            rejected = 0
            for hit in result.hits[:query.limit]:
                if (not isinstance(hit, RetrievalHit) or hit.source != provider.source_id
                        or not hit.reference or not hit.revision
                        or (query.revision and hit.revision != query.revision)):
                    rejected += 1
                    continue
                hits.append(hit)
            return RankedBatch(hits, {
                "status": result.status,
                "coverage_complete": (result.coverage_complete and not rejected
                                      and result.status == "ready" and len(result.hits) <= query.limit),
                "detail": result.detail, "rejected_hits": rejected,
            })

        results, statuses = collect_ranked_sources(
            [(provider.source_id, lambda provider=provider: search_provider(provider))
             for provider in self.sources],
            limit=query.limit, identity=lambda hit: hit.identity,
        )
        return {"results": results, "sources_status": statuses,
                "coverage_complete": bool(statuses) and all(row["coverage_complete"] for row in statuses)}


def compact_observation(payload: dict[str, Any], *, max_bytes: int = 8000) -> str:
    """Keep valid JSON and explicit omissions, including source health and citations."""
    if max_bytes < 256:
        raise ValueError("Observation budget too small")
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def size(value):
        return len(encode(value).encode("utf-8"))

    if size(payload) <= max_bytes:
        return encode(payload)

    priority = ("success", "tool_name", "error", "status", "status_code", "code",
                "coverage_complete", "source_coverage_complete", "index_complete",
                "latest_confirmed", "next_query", "next_offset", "total", "offset", "page_complete",
                "sources_status", "retrieval", "source", "reference",
                "source_revision", "source_path", "source_anchors", "requirement_id",
                "original", "condition", "scope", "run_id", "items", "results", "data", "extra")

    def shorten(value, width, count, depth=0):
        if isinstance(value, str):
            return value if len(value) <= width else value[:width] + "…"
        if isinstance(value, list):
            kept = [shorten(item, width, count, depth + 1) for item in value[:count]]
            if len(value) > count:
                kept.append({"omitted_items": len(value) - count})
            return kept
        if isinstance(value, dict):
            if depth >= 10:
                return {"omitted_fields": len(value)}
            order = [key for key in priority if key in value]
            order.extend(key for key in value if key not in order)
            return {key: shorten(value[key], width, count, depth + 1) for key in order[:32]}
        return value

    for width, count in ((2400, 12), (1200, 8), (600, 5), (240, 3), (100, 1)):
        result = shorten(payload, width, count)
        result["observation_truncated"] = True
        if size(result) <= max_bytes:
            return encode(result)
    result = {key: shorten(payload[key], 100, 1) for key in priority[:6] if key in payload}
    result["observation_truncated"] = True
    result["detail"] = "Evidence omitted; request a narrower query or exact reference."
    return encode(result) if size(result) <= max_bytes else encode({"observation_truncated": True})


def evaluate_ranking(ranked, relevant, *, k=10):
    """Compute binary Recall@K, MRR@K and nDCG@K from stable evidence identities."""
    if k < 1:
        raise ValueError("Invalid evaluation cutoff")
    expected = set(relevant)
    if not expected:
        return {"recall_at_k": None, "mrr_at_k": None, "ndcg_at_k": None}
    selected = list(dict.fromkeys(ranked))[:k]
    ranks = [rank for rank, key in enumerate(selected, 1) if key in expected]
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(expected)) + 1))
    return {"recall_at_k": len(ranks) / len(expected),
            "mrr_at_k": 1 / ranks[0] if ranks else 0.0, "ndcg_at_k": dcg / ideal}


@dataclass(frozen=True)
class RankingEvaluationCase:
    """One blinded query with human-labelled evidence and paired retrieval results."""

    case_id: str
    relevant: Sequence[Hashable]
    baseline: Sequence[Hashable]
    candidate: Sequence[Hashable]


def evaluate_ranking_comparison(cases: Sequence[RankingEvaluationCase], *, k: int = 10) -> dict:
    """Compare paired queries; never count missing ground truth as a success."""
    if not 1 <= k <= 100:
        raise ValueError("Invalid evaluation cutoff")
    if any(not case.case_id.strip() for case in cases) or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Evaluation case identities must be unique and nonempty")
    rows = []
    for case in cases:
        baseline = evaluate_ranking(case.baseline, case.relevant, k=k)
        candidate = evaluate_ranking(case.candidate, case.relevant, k=k)
        rows.append({"case_id": case.case_id, "baseline": baseline, "candidate": candidate,
                     "labelled": bool(case.relevant)})
    labelled = [row for row in rows if row["labelled"]]
    metrics = {}
    for metric in ("recall_at_k", "mrr_at_k", "ndcg_at_k"):
        old = sum(row["baseline"][metric] for row in labelled) / len(labelled) if labelled else None
        new = sum(row["candidate"][metric] for row in labelled) / len(labelled) if labelled else None
        metrics[metric] = {"baseline": old, "candidate": new,
                           "delta": new - old if labelled else None}
    return {"k": k, "total_cases": len(rows), "labelled_cases": len(labelled),
            "unlabelled_cases": len(rows) - len(labelled), "metrics": metrics, "cases": rows}


def evaluate_root_cause_comparison(reviews: Sequence[tuple[str, str, str]]) -> dict:
    """Aggregate paired human review verdicts; no inference from text similarity."""
    allowed = {"correct", "incorrect", "abstained", "unreviewed"}
    if any(not case_id.strip() or old not in allowed or new not in allowed
           for case_id, old, new in reviews) or len({row[0] for row in reviews}) != len(reviews):
        raise ValueError("Invalid or duplicate root cause review")
    paired = [row for row in reviews if "unreviewed" not in row[1:]]
    result = {"total_cases": len(reviews), "paired_cases": len(paired),
              "unreviewed_cases": len(reviews) - len(paired)}
    for name, index in (("baseline", 1), ("candidate", 2)):
        counts = {verdict: sum(row[index] == verdict for row in paired)
                  for verdict in ("correct", "incorrect", "abstained")}
        answered = counts["correct"] + counts["incorrect"]
        result[name] = {**counts, "accuracy": counts["correct"] / len(paired) if paired else None,
                        "answered_accuracy": counts["correct"] / answered if answered else None}
    result["accuracy_delta"] = (result["candidate"]["accuracy"] - result["baseline"]["accuracy"]
                                if paired else None)
    return result
