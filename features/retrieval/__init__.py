"""Shared retrieval, local semantic index and evidence budget.

Domain providers own access checks, source revisions and document content.
"""
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
from .semantic import LocalEmbedding, VectorIndex, local_embedding

__all__ = [
    "LocalEmbedding",
    "RetrievalHit",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalService",
    "RetrievalSource",
    "VectorIndex",
    "compact_observation",
    "evaluate_ranking",
    "fuse_rankings",
    "local_embedding",
]
