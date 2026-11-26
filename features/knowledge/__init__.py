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
from .model_context import public_cdd_context, retrieve_public_cdd_context
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
    "public_cdd_context",
    "retrieve_public_cdd_context",
    "recall_owner_policy_imports",
    "recall_web_references",
    "router",
    "sync_due_web_sources",
    "run_authorized_rag",
]


_LAZY_API_EXPORTS = {
    "ExternalSearchRequest": ".external_api",
    "run_authorized_rag": ".service",
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
    provider = WebKnowledgeProvider()
    outcomes = provider.sync_due()
    outcomes.extend(provider.sync_due_semantic())
    from .service import sync_due_cdd_understanding, wiki_document_understanding

    outcomes.extend(sync_due_cdd_understanding())
    try:
        wiki = wiki_document_understanding(sync=True)
        if wiki.get('status') != 'disabled':
            outcomes.append({'source_id': 'android_internals', 'operation': 'document_understanding', **wiki})
    except Exception:
        outcomes.append({'source_id': 'android_internals', 'operation': 'document_understanding', 'status': 'error'})
    return outcomes
