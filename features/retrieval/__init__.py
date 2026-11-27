"""Shared retrieval, local semantic index and evidence budget.

Domain providers own access checks, source revisions and document content.
"""
from .evidence import evidence_followup_required, evidence_plan_context, summarize_evidence
from .intelligence import IntelligentRAG, IntelligentSource, understand_passages
from .semantic import (
    LocalEmbedding,
    VectorIndex,
    embedding_configuration_issue,
    local_embedding,
    local_reranker,
    owner_index_job,
    run_scheduled_index_batches,
    semantic_coverage,
)
from .service import (
    RankedBatch,
    RankingEvaluationCase,
    RetrievalHit,
    RetrievalResult,
    collect_ranked_sources,
    compact_observation,
    evaluate_ranking,
    evaluate_ranking_comparison,
    evaluate_root_cause_comparison,
    fuse_rankings,
)
from .understanding import PassageInsightIndex, passage_token


__all__ = [
    "IntelligentRAG",
    "IntelligentSource",
    "LocalEmbedding",
    "PassageInsightIndex",
    "RankedBatch",
    "RankingEvaluationCase",
    "RetrievalHit",
    "RetrievalResult",
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
    "owner_index_job",
    "passage_token",
    "run_scheduled_index_batches",
    "semantic_coverage",
    "summarize_evidence",
    "understand_passages",
]
