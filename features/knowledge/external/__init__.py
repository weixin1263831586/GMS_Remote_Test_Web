"""外部知识源联邦层（ADR 0014）。

跨 feature 只能经 ``features.knowledge`` 公共面使用本子包；
provider 之间零横向依赖，与个人知识库存储层零耦合。
"""

from __future__ import annotations

from .android_internals import AndroidInternalsProvider, index_db_path, parse_frontmatter
from .base import (
    EVIDENCE_LEVEL_BACKGROUND,
    EXTERNAL_KNOWLEDGE_LICENSE,
    KNOWN_SOURCES,
    ExternalKnowledgeError,
    ExternalKnowledgeProvider,
    KnowledgeHit,
    ProviderStatus,
    fts_safe_query,
    search_terms,
)
from .federation import (
    FederatedKnowledgeService,
    federated_reindex,
    federated_search,
    federated_status,
    reset_singleton_for_tests,
)


__all__ = [
    "EVIDENCE_LEVEL_BACKGROUND",
    "EXTERNAL_KNOWLEDGE_LICENSE",
    "KNOWN_SOURCES",
    "AndroidInternalsProvider",
    "ExternalKnowledgeError",
    "ExternalKnowledgeProvider",
    "FederatedKnowledgeService",
    "KnowledgeHit",
    "ProviderStatus",
    "federated_reindex",
    "federated_search",
    "federated_status",
    "fts_safe_query",
    "index_db_path",
    "parse_frontmatter",
    "reset_singleton_for_tests",
    "search_terms",
]
