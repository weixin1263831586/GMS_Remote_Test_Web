"""Yuque-style personal knowledge base feature + external knowledge federation."""

from __future__ import annotations

from .api import page_router, router
from .external import (
    EVIDENCE_LEVEL_BACKGROUND,
    ExternalKnowledgeError,
    ExternalKnowledgeProvider,
    KnowledgeHit,
    ProviderStatus,
    federated_reindex,
    federated_search,
    federated_status,
)


__all__ = [
    "EVIDENCE_LEVEL_BACKGROUND",
    "ExternalKnowledgeError",
    "ExternalKnowledgeProvider",
    "KnowledgeHit",
    "ProviderStatus",
    "federated_reindex",
    "federated_search",
    "federated_status",
    "page_router",
    "router",
]
