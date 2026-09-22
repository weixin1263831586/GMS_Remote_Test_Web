"""Wiki 命中的 source anchor 提取与 version-aware 重排（ADR 0014）。

- ``parse_aosp_url`` / ``extract_source_anchors``：把上游 ``sources[]`` 的
  googlesource URL 结构化为 :class:`SourceAnchor`，Agent 拿到后可直接调用
  codesearch/SDK 检索验证——Wiki 不再只是"给 AI 多一段文章"。
- ``rerank_hits``：BM25 相对分之上叠加 Android 版本兼容、confidence、
  last_verified 新鲜度与 anchor 匹配加权，避免旧版本机制污染新版本分析。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .base import KnowledgeHit, SourceAnchor


# ---------------------------------------------------------------------------
# BM25 归一化
# ---------------------------------------------------------------------------


def _bm25_score(rank: float, best: float) -> float:
    """bm25 越负越相关；以最佳命中归一到 (0, 1]。

    ``best >= 0``（没有任何负分，退化场景）时统一给 1.0：无区分信号时
    不能把最佳命中误算成 0 分（历史 ``or 1.0`` 写法的边界缺陷）。
    注意这是单 source 内的相对分，跨 source 不可直接比较。
    """
    if best >= 0:
        return 1.0
    return min(1.0, abs(rank) / abs(best))


# ---------------------------------------------------------------------------
# Wiki → codesearch 源码锚点
# ---------------------------------------------------------------------------

_AOSP_URL_RE = re.compile(
    r"https?://android\.googlesource\.com/platform/(?P<repo>[^+/]+)"
    r"/\+/refs/tags/(?P<revision>[^/]+)/(?P<path>.+)"
)
_KERNEL_URL_RE = re.compile(
    r"https?://android\.googlesource\.com/kernel/(?P<repo>[^+/]+)"
    r"/\+/refs/tags/(?P<revision>[^/]+)/(?P<path>.+)"
)
_GOOGLESOURCE_URL_RE = re.compile(
    r"https?://android\.googlesource\.com/(?P<repo>[^+]+)"
    r"(?:/\+/refs/tags/(?P<revision>[^/]+))?/(?P<path>.+)"
)

#: 结果集携带的 anchor 数量上限（URL 很长，避免响应膨胀）。
MAX_ANCHORS_PER_HIT = 8


def parse_aosp_url(url: str) -> SourceAnchor | None:
    """把上游 sources[].path 的 googlesource URL 解析为 SourceAnchor。

    无法识别的 URL 仍保留为 url-only anchor（path 为空），供 Agent 直接
    fetch；仅 http(s) googlesource 形态值得结构化。
    """
    text = str(url or "").strip()
    if not text.startswith("http"):
        return None
    match = _AOSP_URL_RE.match(text)
    if match:
        return SourceAnchor(
            repo=f"platform/{match.group('repo')}",
            revision=match.group("revision"),
            path=match.group("path"),
            url=text,
            evidence_type="aosp",
        )
    match = _KERNEL_URL_RE.match(text)
    if match:
        return SourceAnchor(
            repo=f"kernel/{match.group('repo')}",
            revision=match.group("revision"),
            path=match.group("path"),
            url=text,
            evidence_type="kernel",
        )
    match = _GOOGLESOURCE_URL_RE.match(text)
    if match:
        return SourceAnchor(
            repo=match.group("repo").strip("/"),
            revision=match.group("revision") or "",
            path=match.group("path").strip("/"),
            url=text,
            evidence_type="aosp",
        )
    return SourceAnchor(url=text)


def extract_source_anchors(sources: Any, revision: str = "") -> list[SourceAnchor]:
    """frontmatter ``sources`` 列表 → SourceAnchor 列表（最多 MAX_ANCHORS_PER_HIT）。"""
    if not isinstance(sources, list):
        return []
    anchors: list[SourceAnchor] = []
    for item in sources[:MAX_ANCHORS_PER_HIT]:
        if isinstance(item, dict):
            url = str(item.get("path") or item.get("url") or "").strip()
            evidence_type = str(item.get("type") or "").strip()
        else:
            url = str(item or "").strip()
            evidence_type = ""
        if not url:
            continue
        anchor = parse_aosp_url(url)
        if anchor is None:
            continue
        if not anchor.evidence_type or (not anchor.revision and revision):
            anchor = SourceAnchor(
                repo=anchor.repo,
                revision=anchor.revision or revision,
                path=anchor.path,
                url=anchor.url,
                evidence_type=evidence_type or anchor.evidence_type,
            )
        anchors.append(anchor)
    return anchors


# ---------------------------------------------------------------------------
# version-aware rerank
# ---------------------------------------------------------------------------

_API_RANGE_RE = re.compile(
    r"Android\s*(\d{1,2}).*?API\s*(\d{1,3}).*?Android\s*(\d{1,2}).*?API\s*(\d{1,3})",
    re.IGNORECASE,
)
_SINGLE_API_RE = re.compile(r"Android\s*(\d{1,2}).*?API\s*(\d{1,3})", re.IGNORECASE)


def extract_version_range(text: str) -> tuple[int, int]:
    """从 applicable_versions / last_verified_against 提取 (min_api, max_api)。

    无法解析时返回 (0, 0) 表示未知——未知不加分也不重罚（老页面的
    applicable_versions 可能缺失，与明确过时是两种信号）。
    """
    if not text:
        return (0, 0)
    match = _API_RANGE_RE.search(text)
    if match:
        return (int(match.group(2)), int(match.group(4)))
    single = _SINGLE_API_RE.search(text)
    if single:
        api = int(single.group(2))
        return (api, api)
    return (0, 0)


def _version_boost(applicable: str, verified_against: str, api_level: int) -> float:
    """Android 版本兼容性加权：覆盖目标 API 明显加分，明显过时降权。"""
    low, high = extract_version_range(applicable)
    if high <= 0:
        low, high = extract_version_range(verified_against)
    if high <= 0:
        return 0.0
    if low <= api_level <= high:
        return 0.35
    if api_level > high:
        # 覆盖上限落后于目标版本：机制可能仍适用，但需标记降权。
        gap = api_level - high
        return -0.15 if gap <= 3 else -0.3
    # 覆盖下限已高于目标：明显是新版本专属机制。
    return -0.2


_CONFIDENCE_ORDER = {"low": -0.15, "medium": 0.0, "medium-high": 0.1, "high": 0.2}


def _confidence_boost(confidence: str) -> float:
    return _CONFIDENCE_ORDER.get(str(confidence).strip().lower(), 0.0)


def _freshness_boost(last_verified: str, *, now: datetime | None = None) -> float:
    """last_verified 新鲜度：≤180 天 +0.2，≤540 天 0，更旧 -0.2。"""
    if not last_verified:
        return 0.0
    try:
        verified = datetime.fromisoformat(str(last_verified)[:10])
    except ValueError:
        return 0.0
    now = now or datetime.now(timezone.utc)
    if verified.tzinfo is None:
        verified = verified.replace(tzinfo=timezone.utc)
    age_days = max(0, (now - verified).days)
    if age_days <= 180:
        return 0.2
    if age_days <= 540:
        return 0.0
    return -0.2


def _anchor_boost(anchors: list[SourceAnchor], query_terms: list[str]) -> float:
    """anchor 与查询词元的路径级匹配：机制词直接命中源码路径加分。"""
    if not anchors or not query_terms:
        return 0.0
    joined = " ".join(
        f"{anchor.repo} {anchor.path} {anchor.url}".lower() for anchor in anchors
    )
    for term in query_terms:
        if len(term) >= 4 and term.lower() in joined:
            return 0.15
    return 0.0


def rerank_hits(
    hits: list[KnowledgeHit],
    query: str,
    *,
    android_api_level: int | None = None,
    now: datetime | None = None,
) -> list[KnowledgeHit]:
    """BM25 相对分 + 版本/置信度/新鲜度/anchor 加权后重排（就地返回）。

    加权只在同 source 内比较（BM25 归一化同样是单 source 相对分）；
    总分截断到 [0, 1.5] 防止极端叠加。``android_api_level`` 为 None 时
    跳过版本维度（调用方从设备/报告上下文提取）。
    """
    if not hits:
        return hits
    terms = [term for term in (query or "").split() if term]
    for hit in hits:
        adjusted = hit.score
        if android_api_level is not None:
            adjusted += _version_boost(
                hit.applicable_versions, hit.last_verified_against, int(android_api_level)
            )
        adjusted += _confidence_boost(hit.confidence)
        adjusted += _freshness_boost(hit.last_verified, now=now)
        adjusted += _anchor_boost(hit.source_anchors, terms)
        hit.score = max(0.0, min(1.5, adjusted))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
