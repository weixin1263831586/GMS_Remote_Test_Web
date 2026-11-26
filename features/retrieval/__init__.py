"""Shared retrieval, local semantic index and evidence budget.

Domain providers own access checks, source revisions and document content.
"""
from .evidence import evidence_followup_required, evidence_plan_context, summarize_evidence
from .semantic import (
    LocalEmbedding,
    VectorIndex,
    embedding_configuration_issue,
    local_embedding,
    local_reranker,
    semantic_coverage,
)
from .service import (
    RankedBatch,
    RankingEvaluationCase,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalService,
    RetrievalSource,
    collect_ranked_sources,
    compact_observation,
    evaluate_ranking,
    evaluate_ranking_comparison,
    evaluate_root_cause_comparison,
    fuse_rankings,
)


__all__ = [
    "LocalEmbedding",
    "RankedBatch",
    "RankingEvaluationCase",
    "RetrievalHit",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalSource",
    "VectorIndex",
    "collect_ranked_sources",
    "compact_observation",
    "embedding_configuration_issue",
    "evaluate_ranking",
    "evaluate_ranking_comparison",
    "evaluate_root_cause_comparison",
    "evidence_followup_required",
    "evidence_plan_context",
    "fuse_rankings",
    "local_embedding",
    "local_reranker",
    "semantic_coverage",
    "summarize_evidence",
]
