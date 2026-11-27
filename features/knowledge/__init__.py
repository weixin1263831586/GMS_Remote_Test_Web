"""Yuque-style personal knowledge base feature + external knowledge federation."""

from __future__ import annotations

import logging

from features.kkagent import model_catalog_fingerprint
from foundation.config import config_manager

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
    android_property_names,
    recall_web_references,
)
from .model_context import public_cdd_context, retrieve_public_cdd_context
from .policy_imports import owner_policy_imports_current, recall_owner_policy_imports


logger = logging.getLogger(__name__)


__all__ = [
    "EVIDENCE_LEVEL_BACKGROUND",
    "ExternalKnowledgeError",
    "ExternalKnowledgeProvider",
    "ExternalSearchRequest",
    "KnowledgeHit",
    "ProviderStatus",
    "android_property_names",
    "federated_reindex",
    "federated_search",
    "federated_status",
    "model_catalog_fingerprint",
    "owner_policy_imports_current",
    "page_router",
    "public_cdd_context",
    "recall_owner_policy_imports",
    "recall_web_references",
    "retrieve_public_cdd_context",
    "router",
    "run_authorized_rag",
    "sync_due_web_sources",
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


def sync_due_web_sources(*, owners=(), owner_index_jobs=None):
    from .indexing import personal_index_jobs, sync_approved_wiki
    from .service import sync_due_cdd_understanding, wiki_document_understanding

    provider = WebKnowledgeProvider()
    config = (config_manager.load_config() or {}).get("retrieval")
    config = config if isinstance(config, dict) else {}
    if config.get("auto_register_public_web_sources", True) is True:
        provider.register_defaults()
    outcomes = provider.sync_due()
    try:
        outcomes.extend(sync_approved_wiki(config))
    except Exception:
        logger.warning("Approved Wiki indexing failed", exc_info=True)
    owners = set(owners)
    jobs = {}
    for discover in (personal_index_jobs, owner_index_jobs):
        if discover is not None:
            try:
                jobs.update(discover(owners, config))
            except Exception:
                logger.warning("Owner knowledge index discovery failed", exc_info=True)
    outcomes.extend(provider.sync_due_semantic(max_sources=2, additional_jobs=jobs))

    outcomes.extend(sync_due_cdd_understanding())
    try:
        wiki = wiki_document_understanding(sync=True)
        if wiki.get('status') != 'disabled':
            outcomes.append({'source_id': 'android_internals', 'operation': 'document_understanding', **wiki})
    except Exception:
        outcomes.append({'source_id': 'android_internals', 'operation': 'document_understanding', 'status': 'error'})
    return outcomes
