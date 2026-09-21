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

EXTERNAL_KNOWLEDGE_LICENSE = "CC BY-NC-SA 4.0"

#: 联邦结果允许的 source 标识（新增 provider 时在此登记）。
KNOWN_SOURCES: tuple[str, ...] = ("android_internals",)


@dataclass(frozen=True)
class ProviderStatus:
    """单个外部知识源的运行状态（/sources 端点与 CLI 共用）。"""

    source: str
    enabled: bool
    status: str  # "ready" | "disabled" | "error" | "empty"
    detail: str = ""
    source_revision: str = ""
    doc_count: int = 0
    last_sync_at: str = ""
    license: str = EXTERNAL_KNOWLEDGE_LICENSE

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
        }


@runtime_checkable
class ExternalKnowledgeProvider(Protocol):
    """外部知识源最小协议：状态 + 检索；无写路径。"""

    source_id: str

    def status(self) -> ProviderStatus: ...

    def search(self, query: str, *, limit: int = 5) -> list[KnowledgeHit]: ...


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
