"""外部知识源联邦层：Provider 协议与统一结果形状。

ADR 0014：外部知识是**只读、联邦、background-only** 的 provider 层：

- 每个 provider 自带存储/生命周期/配置，provider 之间零横向依赖；
- 跨 feature 只能经 ``features.knowledge`` 公共面使用本子包；
- 所有命中必须携带 provenance（source / source_revision /
  applicable_versions / confidence / last_verified / license），
  ``evidence_level`` 恒为 "background"——外部知识解释机制，
  绝不作为 verified root cause 的证据；
- provider 未配置 / 配置无效时必须 fail-closed（status=disabled，
  search 返回空），调用方永远收不到异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


EVIDENCE_LEVEL_BACKGROUND = "background"

#: 上游 knowledge-pack/policy.yaml 的 license.expression（ADR 0014）：
#: 不要硬编码简化后的 "CC BY-NC-SA 4.0"——上游声明的是双许可表达式，且
#: Knowledge Pack 重新分发授权（SmartPerfetto 专属）不适用于 GMS。运行期以
#: reindex 时从 policy 读到的值为准，此常量仅是 policy 缺失时的回退展示值。
EXTERNAL_KNOWLEDGE_LICENSE = "CC-BY-NC-SA-4.0 OR LicenseRef-AIW-Commercial"

#: 联邦结果允许的 source 标识（新增 provider 时在此登记）。
KNOWN_SOURCES: tuple[str, ...] = ("android_internals",)


@dataclass(frozen=True)
class SourceAnchor:
    """Wiki 命中指向的上游源码/文档锚点（ADR 0014：Wiki→codesearch 闭环）。

    Agent 拿到 anchor 后可直接调用 codesearch/SDK 检索验证，而不是把
    Wiki 正文当结论。``evidence_type`` 沿用上游 sources[].type（aosp /
    kernel / official / vendor ...），语义即证据等级链路里的层级。
    """

    repo: str = ""
    revision: str = ""
    path: str = ""
    url: str = ""
    evidence_type: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "repo": self.repo,
            "revision": self.revision,
            "path": self.path,
            "url": self.url,
            "evidence_type": self.evidence_type,
        }


@dataclass(frozen=True)
class ProviderStatus:
    """单个外部知识源的运行状态（/sources 端点与 CLI 共用）。"""

    source: str
    enabled: bool
    status: str  # "ready" | "disabled" | "error" | "empty" | "not_configured"
    detail: str = ""
    source_revision: str = ""
    doc_count: int = 0
    last_sync_at: str = ""
    license: str = EXTERNAL_KNOWLEDGE_LICENSE
    #: revision 三态（ADR 0014：外部知识更新链路可复现）：
    #: available = 本地 clone HEAD（pull 后领先于索引）；
    #: approved  = 管理员批准并已索引的 revision；
    #: source_revision = 本次索引内容对应的 revision（= approved 时链路闭环）。
    available_revision: str = ""
    approved_revision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "enabled": self.enabled,
            "status": self.status,
            "detail": self.detail,
            "source_revision": self.source_revision,
            "doc_count": self.doc_count,
            "last_sync_at": self.last_sync_at,
            "license": self.license,
            "available_revision": self.available_revision,
            "approved_revision": self.approved_revision,
        }


@dataclass
class KnowledgeHit:
    """联邦检索统一结果形状；provenance 字段缺失视为缺陷。"""

    source: str
    title: str
    snippet: str
    source_path: str = ""
    chapter: str = ""
    source_revision: str = ""
    applicable_versions: str = ""
    confidence: str = ""
    last_verified: str = ""
    last_verified_against: str = ""
    license: str = EXTERNAL_KNOWLEDGE_LICENSE
    evidence_level: str = EVIDENCE_LEVEL_BACKGROUND
    score: float = 0.0
    #: 上游 sources[] 投影出的源码锚点：codesearch 验证入口（可为空）。
    source_anchors: list[SourceAnchor] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "snippet": self.snippet,
            "source_path": self.source_path,
            "chapter": self.chapter,
            "source_revision": self.source_revision,
            "applicable_versions": self.applicable_versions,
            "confidence": self.confidence,
            "last_verified": self.last_verified,
            "last_verified_against": self.last_verified_against,
            "license": self.license,
            "evidence_level": self.evidence_level,
            "score": round(self.score, 4),
            "source_anchors": [anchor.to_dict() for anchor in self.source_anchors],
        }


@runtime_checkable
class ExternalKnowledgeProvider(Protocol):
    """外部知识源最小协议：状态 + 检索；无写路径。"""

    source_id: str

    def status(self) -> ProviderStatus: ...

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        android_api_level: int | None = None,
    ) -> list[KnowledgeHit]: ...


class ExternalKnowledgeError(Exception):
    """Provider 内部错误；联邦层捕获后转成 per-source 失败状态。"""


# ---------------------------------------------------------------------------
# 子包自有 tokenizer：与 features/knowledge/storage.py 的 FTS5 分词逻辑保持
# 等价行为（英文词 + CJK 2-gram），但刻意不 import storage 私有函数——
# external 子包与个人知识库存储层零耦合。
# ---------------------------------------------------------------------------

_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_.:+-]*")

_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]+|[a-z][a-z0-9]+")

# 测试脚手架/断言模板词：诊断现场把类名、模块名、断言文案整段送来检索，
# camel 拆词后会产生大量泛化英文词，OR 匹配会拉来完全无关的机制文章
# （如 colorModeEvents 失败召回 FrameTimeline）。宁可少召回，不给噪声。
_GENERIC_TERMS = frozenset({
    "android", "cts", "gts", "vts", "sts", "test", "tests", "testing",
    "case", "cases", "module", "host", "suite", "expected", "actual",
    "least", "assert", "assertion", "fail", "failed", "failure",
    "failures", "error", "errors", "exception", "exceptions",
    "com", "org", "java", "kotlin", "www", "http", "https",
})


def search_terms(query: str, *, limit: int = 12) -> list[str]:
    """把自然语言查询清洗成 FTS5 安全的词元列表（引号包裹由调用方负责）。

    - camelCase 标识符拆子词（CtsStatsdAtomHostTestCases → cts/statsd/atom）；
    - 泛化测试脚手架词丢弃（``_GENERIC_TERMS``）；
    - CJK 无分词：按 2-gram 拆，保证中文查询可召回。
    """
    raw = (query or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = term.strip().strip('"').lower()
        if len(term) >= 2 and term not in seen and term not in _GENERIC_TERMS:
            seen.add(term)
            out.append(term)

    # 配额优先级：整词 → camel 子词 → 连字符/带点补充 → CJK 2-gram。
    # 每次入列后检查配额，避免单个长类名的子词挤占后续词元。
    for token in raw.split():
        add(token)
        if len(out) >= limit:
            return out
        for word in _CAMEL_WORD_RE.findall(token):
            add(word)
            if len(out) >= limit:
                return out
    for term in _TERM_RE.findall(raw.lower()):
        add(term)
        if len(out) >= limit:
            return out
    # CJK 无分词：按 2-gram 拆，保证中文查询可召回。
    for block in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        for i in range(len(block) - 1):
            gram = block[i : i + 2]
            if gram not in seen:
                seen.add(gram)
                out.append(gram)
                if len(out) >= limit:
                    return out
    return out


def fts_safe_query(query: str, *, limit: int = 12) -> str:
    """构造 FTS5 MATCH 查询串；词元引号包裹，杜绝 FTS 语法注入。"""
    parts = [f'"{term}"' for term in search_terms(query, limit=limit) if term]
    return " OR ".join(parts)
