from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .parsers import parse_file
from .external.web_sources import model_catalog_fingerprint
from .intelligent import (local_document_model, local_document_rag, personal_insight_index,
                          personal_source, render_rag_answer, web_source, wiki_source)
from .storage import POLICY_IMPORT_SOURCE, KnowledgeStore


# 解耦 seam（test_feature_dependency_cycles）：ask() 需要 AI 生成答案，但
# knowledge 不得静态 import features.assistant（会与 reports 侧构成
# knowledge→assistant→reports→knowledge 依赖环）。组合根
# （bootstrap/routes.py）通过 set_ask_analyzer_factory 注入分析器；
# 未注入时 ask() 走既有的检索降级路径，不抛出。


def _unwired_analyzer() -> Any:
    raise RuntimeError("AI analyzer 未接入（composition root 未注册）")


_ask_analyzer_factory: Any = _unwired_analyzer


def set_ask_analyzer_factory(factory: Any) -> None:
    """Composition-root seam: register the AI analyzer used by ask()."""
    global _ask_analyzer_factory
    _ask_analyzer_factory = factory


def _title_from_text(text: str, fallback: str = "无标题") -> str:
    for line in (text or "").splitlines():
        value = line.strip().lstrip("#").strip()
        if value:
            return value[:120]
    return fallback


def _summary(text: str, size: int = 220) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value[:size] + ("..." if len(value) > size else "")


def _auto_tags(text: str, limit: int = 6) -> list[str]:
    tags: list[str] = []
    patterns = [
        r"\bRK\d{4,}\b",
        r"\b(?:CTS|GTS|VTS|STS|BTS|Mainline|GMS)\b",
        r"\bAndroid\s?\d{1,2}\b",
        r"[\u4e00-\u9fff]{2,6}",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text or "", flags=re.IGNORECASE):
            tag = re.sub(r"\s+", "", str(match)).strip()
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= limit:
                return tags
    return tags


class KnowledgeService:
    def __init__(self, store: KnowledgeStore | None = None) -> None:
        self.store = store or KnowledgeStore()

    def create_doc_from_text(
        self,
        user_id: str,
        *,
        space_id: str,
        title: str = "",
        content: str,
        parent_id: str = "",
        tags: Any = None,
        links: list[dict[str, Any]] | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"error": "文档内容不能为空"}
        tag_list = tags if tags is not None else _auto_tags(content)
        return self.store.create_doc(
            user_id,
            space_id=space_id,
            parent_id=parent_id,
            title=title or _title_from_text(content),
            content_md=content,
            raw_content=content,
            summary=_summary(content),
            tags=tag_list,
            source=source,
            links=links or [],
        )

    def create_doc_from_file(
        self,
        user_id: str,
        *,
        space_id: str,
        file_path: str,
        filename: str,
        parent_id: str = "",
        tags: Any = None,
        policy_import: bool = False,
    ) -> dict[str, Any]:
        parsed = parse_file(file_path, filename, policy_import=policy_import)
        text = (parsed.get("text") or "").strip()
        if not text:
            return {"error": f"无法从文件 {filename} 提取文本"}
        doc = self.store.create_doc(
            user_id,
            space_id=space_id,
            parent_id=parent_id,
            title=_title_from_text(text, Path(filename).stem or filename),
            content_md=text,
            raw_content=text,
            summary=_summary(text),
            tags=tags if tags is not None else _auto_tags(text),
            source=POLICY_IMPORT_SOURCE if policy_import else parsed.get("source") or "upload",
            source_file=filename,
        )
        self.store.add_attachment(
            user_id,
            doc["doc_id"],
            source_path=file_path,
            original_name=filename,
            extracted_text=text,
        )
        return self.store.get_doc(user_id, doc["doc_id"]) or doc

    def document_understanding(self, user_id, *, approved_only=False, sync=False, batch_size=4):
        """Process only the authenticated owner's ordinary or explicitly approved corpus."""
        catalog = model_catalog_fingerprint()
        index = personal_insight_index(self.store, user_id, approved_only=approved_only)
        if not index.model_key or not catalog:
            return {"status": "disabled", "processed": 0, "total": 0, "coverage_complete": False}
        source = personal_source(self.store, user_id, approved_only=approved_only, catalog_hash=catalog)
        manifest, complete = source.manifest()
        if not sync:
            return index.status(manifest, complete=complete)
        model, name, fingerprint = local_document_model(_ask_analyzer_factory, catalog_hash=catalog)
        if not name or fingerprint != catalog:
            return {"status": "model_unavailable", "coverage_complete": False}

        def current(hit):
            fresh = personal_insight_index(self.store, user_id, approved_only=approved_only)
            return fresh.model_key == index.model_key and source.current(hit)

        return index.sync(manifest, current, model, complete=complete, batch_size=batch_size)

    def ask(self, user_id: str, question: str, *, space_id: str = "", limit: int = 8,
            include_external: bool = False, include_authorized_imports: bool = False) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"answer": "请输入问题。", "contexts": []}
        # The personal route already binds the authenticated owner. Only explicitly
        # approved imports with the current model catalog may join ordinary documents.
        catalog = model_catalog_fingerprint()
        sources = [personal_source(self.store, user_id, space_id=space_id)]
        if include_authorized_imports and catalog:
            sources.append(personal_source(self.store, user_id, space_id=space_id,
                                           approved_only=True, catalog_hash=catalog))
        if include_external:
            sources.extend([web_source(), wiki_source()])
        result = local_document_rag(question, sources, _ask_analyzer_factory,
                                    limit=limit, catalog_hash=catalog)
        contexts = [{"doc_id": item["metadata"].get("doc_id"), "title": item["metadata"].get("title"),
                     "snippet": item["text"], "source": item["source"], "reference": item["reference"],
                     "source_revision": item["revision"], "evidence_id": item["id"],
                     **item["metadata"]} for item in result["evidence"]]
        return {"answer": render_rag_answer(result),
                "mode": "ai" if result["claims"] else "retrieval",
                "provider": result["provider"], "error": "" if result["claims"] else result["status"],
                "contexts": contexts, "retrieval": {"status": result["status"], "coverage_complete": False},
                "rag": result}


def analyze_public_cdd(question, *, limit=8, android_api_level=None, recall_only=False):
    """Public-only Agent entry. No owner or restricted document content is exposed."""
    result = local_document_rag(question, [web_source(android_api_level=android_api_level, cdd_only=True)],
                                _ask_analyzer_factory, limit=limit, recall_only=recall_only)
    rows = []
    for item in result["evidence"]:
        metadata = item["metadata"]
        rows.append({"source": item["source"], "source_path": item["reference"],
                     "source_revision": item["revision"], "snippet": item["text"],
                     "title": metadata.get("title"), "applicable_versions": str(metadata.get("android_version") or ""),
                     "source_anchors": [{"url": metadata["source_url"], "revision": item["revision"],
                                         "evidence_type": "official"}],
                     "extra": {**metadata, "source_kind": "cdd", "status": "indexed",
                               "reference": item["reference"], "applicability_context": metadata.get("condition", "")},
                     "evidence_level": "background"})
    return {"answer": render_rag_answer(result) if not recall_only else "",
            "rag": result, "coverage_complete": False, "results": rows}


def sync_due_cdd_understanding():
    """Use the existing Controller tick; no new service or external model."""
    from .external import cdd_intelligence
    from .external.web_sources import WebKnowledgeProvider

    key = cdd_intelligence.enabled_key()
    if not key:
        return []
    model, name, fingerprint = local_document_model(_ask_analyzer_factory)
    if not name or not fingerprint or fingerprint != model_catalog_fingerprint():
        return [{"operation": "document_understanding", "status": "model_unavailable"}]
    return [cdd_intelligence.sync_batch(WebKnowledgeProvider(), model, key)]


def analyze_external_knowledge(question, *, sources, limit=8, android_api_level=None):
    """Public CDD and separately approved local Wiki; never expose owner imports."""
    selected = set(sources or ["web_documents"])
    if selected == {"web_documents"}:
        return analyze_public_cdd(question, limit=limit, android_api_level=android_api_level)
    adapters = []
    if "web_documents" in selected:
        adapters.append(web_source(android_api_level=android_api_level, cdd_only=True))
    if "android_internals" in selected:
        adapters.append(wiki_source(android_api_level=android_api_level))
    result = local_document_rag(question, adapters, _ask_analyzer_factory, limit=limit)
    return {"answer": render_rag_answer(result), "rag": result, "coverage_complete": False,
            "analysis_scope": "public_cdd_and_approved_local_wiki",
            "results": [{"source": item["source"], "source_path": item["reference"],
                         "source_revision": item["revision"], "snippet": item["text"],
                         "title": item["metadata"].get("title"), "extra": item["metadata"],
                         "evidence_level": "background"} for item in result["evidence"]]}


def run_authorized_rag(question, sources, *, limit=8, recall_only=False):
    """Use the injected model gateway with domain-owned, request-authorized adapters."""
    return local_document_rag(question, sources, _ask_analyzer_factory, limit=limit, recall_only=recall_only)
