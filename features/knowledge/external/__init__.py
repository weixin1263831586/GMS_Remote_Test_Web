"""外部知识源联邦层（ADR 0014）。

跨 feature 只能经 ``features.knowledge`` 公共面使用本子包；
provider 之间零横向依赖，与个人知识库存储层零耦合。
"""

from __future__ import annotations

from .android_internals import AndroidInternalsProvider, index_db_path
from .base import (
    EVIDENCE_LEVEL_BACKGROUND,
    EXTERNAL_KNOWLEDGE_LICENSE,
    KNOWN_SOURCES,
    ExternalKnowledgeError,
    ExternalKnowledgeProvider,
    KnowledgeHit,
    ProviderStatus,
    SourceAnchor,
    fts_safe_query,
    search_terms,
)
from .content_policy import AIWContentPolicy, load_policy
from .federation import (
    FederatedKnowledgeService,
    federated_reindex,
    federated_search,
    federated_service,
    federated_status,
    reset_singleton_for_tests,
)
from .frontmatter import parse_frontmatter
from .ranking import extract_source_anchors, parse_aosp_url, rerank_hits


__all__ = [
    "EVIDENCE_LEVEL_BACKGROUND",
    "EXTERNAL_KNOWLEDGE_LICENSE",
    "KNOWN_SOURCES",
    "AIWContentPolicy",
    "AndroidInternalsProvider",
    "ExternalKnowledgeError",
    "ExternalKnowledgeProvider",
    "FederatedKnowledgeService",
    "KnowledgeHit",
    "ProviderStatus",
    "SourceAnchor",
    "extract_source_anchors",
    "federated_reindex",
    "federated_search",
    "federated_service",
    "federated_status",
    "fts_safe_query",
    "index_db_path",
    "load_policy",
    "parse_aosp_url",
    "parse_frontmatter",
    "rerank_hits",
    "reset_singleton_for_tests",
    "search_terms",
]
