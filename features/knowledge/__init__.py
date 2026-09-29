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


_LAZY_API_EXPORTS = {
    "AndroidInternalsProvider": ".external.android_internals",
    "ExternalSearchRequest": ".external_api",
    "index_db_path": ".external.android_internals",
}


def __getattr__(name: str):
    """Lazy public-surface exports (heavy/route modules stay unimported)."""
    module_path = _LAZY_API_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path, __name__), name)
    globals()[name] = value
    return value
