"""Shared retrieval, local semantic index and evidence budget.

Domain providers own access checks, source revisions and document content.
"""
from .evidence import evidence_followup_required, evidence_plan_context, summarize_evidence
from .semantic import LocalEmbedding, VectorIndex, embedding_configuration_issue, local_embedding, local_reranker
from .service import (
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalService,
    RetrievalSource,
    compact_observation,
    evaluate_ranking,
    fuse_rankings,
)


__all__ = [
    "LocalEmbedding",
    "RetrievalHit",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalSource",
    "VectorIndex",
    "compact_observation",
    "embedding_configuration_issue",
    "evaluate_ranking",
    "evidence_followup_required",
    "evidence_plan_context",
    "fuse_rankings",
    "local_embedding",
    "local_reranker",
    "summarize_evidence",
]
