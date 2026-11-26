"""Typed retrieval orchestration and bounded structured evidence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Protocol


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


def fuse_rankings(rankings, *, limit, exact_keys=(), k=60):
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
    return sorted(scores, key=lambda key: (key not in exact, -scores[key], first[key]))[:limit]


class RetrievalService:
    """Fan out bounded requests to injected providers; isolate failures."""

    def __init__(self, sources: list[RetrievalSource]):
        if len(sources) > 16 or len({item.source_id for item in sources}) != len(sources):
            raise ValueError("Invalid retrieval source set")
        self.sources = tuple(sources)

    def search(self, query: RetrievalQuery):
        representatives = {}
        rankings = []
        statuses = []
        for provider in self.sources:
            try:
                result = provider.search(query)
                if not isinstance(result, RetrievalResult):
                    raise TypeError("Invalid provider result")
                hits = []
                for hit in result.hits[:query.limit]:
                    if (hit.source != provider.source_id or not hit.reference or not hit.revision
                            or (query.revision and hit.revision != query.revision)):
                        continue
                    representatives.setdefault(hit.identity, hit)
                    hits.append(hit.identity)
                rankings.append(hits)
                statuses.append({"source": provider.source_id, "status": result.status,
                                 "coverage_complete": result.coverage_complete, "detail": result.detail})
            except Exception as exc:
                statuses.append({"source": provider.source_id, "status": "error",
                                 "coverage_complete": False, "detail": type(exc).__name__})
        keys = fuse_rankings(rankings, limit=query.limit)
        return {"results": [representatives[key] for key in keys], "sources_status": statuses,
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
                "coverage_complete", "sources_status", "retrieval", "source", "reference",
                "source_revision", "source_path", "source_anchors", "requirement_id",
                "run_id", "items", "results", "data", "extra")

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
