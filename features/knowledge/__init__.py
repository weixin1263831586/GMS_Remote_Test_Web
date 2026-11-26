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
from .external.web_sources import (
    WebKnowledgeProvider,
    model_catalog_fingerprint,
    recall_web_references,
)
from .policy_imports import owner_policy_imports_current, recall_owner_policy_imports


__all__ = [
    "EVIDENCE_LEVEL_BACKGROUND",
    "ExternalKnowledgeError",
    "ExternalKnowledgeProvider",
    "ExternalSearchRequest",
    "KnowledgeHit",
    "ProviderStatus",
    "federated_reindex",
    "federated_search",
    "federated_status",
    "model_catalog_fingerprint",
    "owner_policy_imports_current",
    "page_router",
    "recall_owner_policy_imports",
    "recall_web_references",
    "router",
    "sync_due_web_sources",
]


_LAZY_API_EXPORTS = {
    "ExternalSearchRequest": ".external_api",
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


def sync_due_web_sources():
    return WebKnowledgeProvider().sync_due()
