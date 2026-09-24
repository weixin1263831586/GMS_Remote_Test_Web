"""FederatedKnowledgeService：外部知识源联邦检索。

ADR 0014：按 source 扇出到各 provider，失败隔离（单 provider 错误只影响
自己的结果集与 sources_status，绝不抛出到调用方）；provider 构造来自
管理员配置（``external_knowledge.providers.*``），未配置即不存在。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .base import (
    KNOWN_SOURCES,
    ExternalKnowledgeError,
    ExternalKnowledgeProvider,
    KnowledgeHit,
    ProviderStatus,
)


logger = logging.getLogger(__name__)

#: RRF 平滑常数（文献标准值）：rank 越靠前贡献 1/(k+rank) 越大，跨 source
#: 的命中在各自通道内的名次可直接比较，无需校准原始分数。
RRF_K = 60


def _rrf_document_key(hit: KnowledgeHit) -> tuple[str, str, str]:
    """Stable logical-document key used to fuse and deduplicate providers."""
    path = str(hit.source_path or "").strip().replace("\\", "/").casefold()
    title = " ".join(str(hit.title or "").split()).casefold()
    if path:
        return ("path", path, title)
    snippet = " ".join(str(hit.snippet or "").split()).casefold()
    return ("text", title, snippet[:240])


class FederatedKnowledgeService:
    """聚合多个 ExternalKnowledgeProvider；生命周期与配置由调用方注入。"""

    def __init__(self, providers: list[ExternalKnowledgeProvider] | None = None) -> None:
        self._providers: dict[str, ExternalKnowledgeProvider] = {}
        self._lock = threading.RLock()
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ExternalKnowledgeProvider) -> None:
        with self._lock:
            self._providers[provider.source_id] = provider

    def sources(self) -> list[str]:
        with self._lock:
            return list(self._providers)

    def provider(self, source_id: str) -> ExternalKnowledgeProvider | None:
        """按 source_id 取 provider；未注册返回 None（fail-closed）。"""
        with self._lock:
            return self._providers.get(source_id)

    def status(self) -> list[dict[str, Any]]:
        """全部 source 的状态（含 disabled 原因）；单 source 异常不冒泡。

        已登记（KNOWN_SOURCES）但未注册 provider 的源也必须
        出现一行 ``not_configured``，而不是从 /sources 里"消失"——
        fail-closed 的含义是显式禁用，不是静默缺位。
        """
        out: list[dict[str, Any]] = []
        with self._lock:
            providers = list(self._providers.values())
            registered = set(self._providers)
        for source_id in KNOWN_SOURCES:
            if source_id not in registered:
                out.append(ProviderStatus(
                    source=source_id,
                    enabled=False,
                    status="not_configured",
                    detail="未配置（fail-closed 禁用），请在 configs 中设置 "
                           "external_knowledge.providers 后重启",
                ).to_dict())
        for provider in providers:
            try:
                out.append(provider.status().to_dict())
            except Exception as exc:
                logger.warning("external knowledge status failed for %s: %s", provider.source_id, exc)
                out.append(ProviderStatus(
                    source=provider.source_id, enabled=True, status="error", detail=str(exc)
                ).to_dict())
        return out

    def search(
        self,
        query: str,
        *,
        sources: list[str] | None = None,
        limit: int = 5,
        android_api_level: int | None = None,
    ) -> dict[str, Any]:
        """联邦检索：返回 ``{"results", "sources_status"}``，永不抛异常。

        - ``sources`` 为空 = 全部已注册 source；
        - 单 provider 失败：记 warning，结果集跳过该 source；
        - 命中一律携带 evidence_level="background"（provider 负责盖章，
          此处兜底校验，防止未来 provider 漏标）。
        """
        query = (query or "").strip()
        raw_sources = list(sources or [])
        wanted = [s for s in raw_sources if s]
        if raw_sources and not wanted:
            # 只含空白条目（如 [""]）＝显式过滤到空：fail-closed，不放大为
            # 全源检索。注意空列表 [] 是"不过滤"，必须走全源（Web API 的
            # sources 字段默认就是 []，误判会让检索端点静默失效）。
            return {"results": [], "sources_status": self.status()}
        wanted = wanted or None
        with self._lock:
            providers = [
                p for sid, p in sorted(self._providers.items())
                if wanted is None or sid in wanted
            ]
        results: list[KnowledgeHit] = []
        status_rows: list[dict[str, Any]] = []
        if not query:
            return {"results": [], "sources_status": self.status()}
        with self._lock:
            registered = set(self._providers)
        # 检索结果同样要给未注册的已知名源一行 not_configured（fail-closed
        # 显式化）；请求了未知 source 时由 API 层 422，这里只处理已知源。
        for source_id in KNOWN_SOURCES:
            if wanted is not None and source_id not in wanted:
                continue
            if source_id not in registered:
                status_rows.append(ProviderStatus(
                    source=source_id,
                    enabled=False,
                    status="not_configured",
                    detail="未配置（fail-closed 禁用）",
                ).to_dict())
        for provider in providers:
            try:
                search_options: dict[str, Any] = {"limit": limit}
                if android_api_level is not None:
                    search_options["android_api_level"] = android_api_level
                hits = provider.search(query, **search_options)
            except Exception as exc:
                logger.warning("external knowledge search failed for %s: %s", provider.source_id, exc)
                status_rows.append(ProviderStatus(
                    source=provider.source_id, enabled=True, status="error", detail=str(exc)
                ).to_dict())
                continue
            try:
                status_rows.append(provider.status().to_dict())
            except Exception as exc:
                # 状态读取失败也必须留行：结果存在但状态行缺失更难排查。
                logger.warning("external knowledge status failed for %s: %s", provider.source_id, exc)
                status_rows.append(ProviderStatus(
                    source=provider.source_id, enabled=True, status="error", detail=str(exc)
                ).to_dict())
            for hit in hits:
                if hit.evidence_level != "background":
                    hit.evidence_level = "background"
                results.append(hit)
        # RRF（Reciprocal Rank Fusion）合并（round-robin 公平但不做
        # relevance calibration；ADR 0014 联邦排序收口）。score = Σ 1/(k + rank_i)：在多个
        # source 都命中的条目获得叠加加分，单命中的条目按各 source 内部
        # 排序保持相对次序（单 source 时退化为该 source 的原始排序，
        # 与旧 round-robin 行为一致）。k=60 是文献标准值，抑制单一
        # source 高 rank 的支配效应；同分按 (source, source_path) 稳定
        # 排序，保证跨调用结果可复现。
        by_source: dict[str, list[KnowledgeHit]] = {}
        for hit in results:
            by_source.setdefault(hit.source, []).append(hit)
        ranked_per_source = [
            by_source[source] for source in sorted(by_source)
        ]
        rrf_scores: dict[tuple[str, str, str], float] = {}
        representatives: dict[tuple[str, str, str], KnowledgeHit] = {}
        for hits in ranked_per_source:
            seen_in_source: set[tuple[str, str, str]] = set()
            unique_rank = 0
            for hit in sorted(hits, key=lambda h: h.score, reverse=True):
                key = _rrf_document_key(hit)
                if key in seen_in_source:
                    continue
                seen_in_source.add(key)
                unique_rank += 1
                rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (
                    RRF_K + unique_rank
                )
                current = representatives.get(key)
                if current is None or (
                    hit.score,
                    hit.source,
                    hit.source_path,
                ) > (
                    current.score,
                    current.source,
                    current.source_path,
                ):
                    representatives[key] = hit
        merged_keys = sorted(
            representatives,
            key=lambda key: (
                -rrf_scores[key],
                representatives[key].source,
                representatives[key].source_path,
                representatives[key].title,
            )
        )
        merged = [representatives[key] for key in merged_keys[: max(1, limit)]]
        return {
            "results": [hit.to_dict() for hit in merged],
            "sources_status": status_rows,
        }


def federated_search(
    query: str,
    *,
    sources: list[str] | None = None,
    limit: int = 5,
    android_api_level: int | None = None,
) -> dict[str, Any]:
    """进程级便捷入口：懒加载单例（配置驱动），供 reports/assistant 复用。"""
    return _singleton().search(
        query,
        sources=sources,
        limit=limit,
        android_api_level=android_api_level,
    )


def federated_status() -> list[dict[str, Any]]:
    return _singleton().status()


def federated_service() -> FederatedKnowledgeService:
    """进程级单例本体（approve/reindex 等对象级操作需要 provider 实例）。"""
    return _singleton()


def federated_reindex(source: str) -> dict[str, Any]:
    """显式重建指定 source 的索引；未配置或并发冲突抛 ExternalKnowledgeError。"""
    provider = _singleton().provider(source)
    if provider is None:
        raise ExternalKnowledgeError(f"外部知识源未配置: {source}")
    reindex = getattr(provider, "reindex", None)
    if reindex is None:
        raise ExternalKnowledgeError(f"外部知识源不支持重建索引: {source}")
    return reindex()


def reset_singleton_for_tests() -> None:
    """测试钩子：清空进程级单例（生产代码不得调用）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None


_SINGLETON: FederatedKnowledgeService | None = None
_SINGLETON_LOCK = threading.Lock()


def _singleton() -> FederatedKnowledgeService:
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = _build_from_config()
        return _SINGLETON


def _build_from_config() -> FederatedKnowledgeService:
    from foundation.config import config_manager

    from .android_internals import AndroidInternalsProvider

    service = FederatedKnowledgeService()
    try:
        config = config_manager.load_config()
    except Exception as exc:
        logger.warning("external knowledge config load failed: %s", exc)
        return service
    provider = AndroidInternalsProvider.from_config(config or {})
    if provider is not None:
        service.register(provider)
    else:
        logger.info("external knowledge source '%s' 未配置（fail-closed 禁用）", KNOWN_SOURCES[0])
    return service
