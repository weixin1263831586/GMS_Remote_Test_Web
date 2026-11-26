"""Authorized document adapters for the shared local LLM retrieval engine."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from features.retrieval import IntelligentRAG, IntelligentSource, RetrievalHit, RetrievalResult
from foundation.config import config_manager
from foundation.redaction import redact_sensitive_text

from .external.web_sources import WebKnowledgeProvider, model_catalog_fingerprint
from .personal_semantic import _redacted_body, _revision, retrieve_personal_contexts
from .policy_imports import IMPORT_TAG, _approved_content, content_hash, is_policy_import, policy_import_filter

PAGE_SIZE = 2800


def personal_source(store, owner, *, space_id="", approved_only=False, catalog_hash=""):
    source_id = "authorized_imports" if approved_only else "personal_documents"
    provider = WebKnowledgeProvider()

    def authorized(doc):
        if not doc or (space_id and doc.get("space_id") != space_id):
            return False
        if approved_only:
            grants = provider.approved_imports(owner, model_catalog_hash=catalog_hash)
            digest = grants.get(doc.get("doc_id"))
            return bool(catalog_hash and digest and is_policy_import(doc.get("tags"))
                        and _approved_content(doc, digest))
        return not policy_import_filter(owner)(doc)

    def get(doc_id):
        doc = store.get_doc(owner, doc_id)
        return doc if authorized(doc) else None

    def revision(doc):
        if approved_only:
            return provider.approved_imports(owner, model_catalog_hash=catalog_hash).get(doc["doc_id"], "")
        return _revision(doc)

    def passage(doc, start):
        body = _redacted_body(doc)
        if not 0 <= start < len(body):
            return None
        rev = revision(doc)
        if not rev:
            return None
        title = doc["title"] if not approved_only or content_hash(doc) == rev else body.splitlines()[0]
        return RetrievalHit(
            source_id, f'{doc["doc_id"]}:{start}', rev, body[start:start + PAGE_SIZE],
            "candidate", {"doc_id": doc["doc_id"], "title": redact_sensitive_text(title)[:200],
                          "start": start, "total_chars": len(body), "offset_basis": "redacted_document",
                          "scope": "owner_approved_import" if approved_only else "owner_document"},
        )

    def current(hit):
        doc = get(hit.metadata["doc_id"])
        start = hit.metadata["start"]
        return bool(doc and revision(doc) == hit.revision
                    and _redacted_body(doc)[start:start + len(hit.text)] == hit.text)

    def search(query, limit):
        rows = []
        if approved_only:
            # Search owner-approved documents only; do not send unapproved candidates to the model.
            docs = store.search(owner, query, space_id=space_id, tag=IMPORT_TAG, limit=50)
            terms = [word.casefold() for word in re.findall(r"[\w.-]{2,}", query)[:24]]
            for candidate in docs:
                doc = get(candidate["doc_id"])
                if not doc:
                    continue
                body = _redacted_body(doc).casefold()
                positions = [body.find(term) for term in terms if term in body]
                start = max(0, min(positions) - 350) if positions else 0
                hit = passage(doc, start)
                if hit:
                    rows.append(hit)
                if len(rows) >= limit:
                    break
        else:
            contexts, _ = retrieve_personal_contexts(
                store, owner, query, space_id=space_id, limit=limit,
                exclude_document=policy_import_filter(owner),
            )
            for context in contexts:
                doc = get(context["doc_id"])
                if not doc or context.get("source_revision") != revision(doc):
                    continue
                body = _redacted_body(doc)
                snippet = redact_sensitive_text(str(context.get("snippet") or ""))
                start = body.find(snippet)
                if start < 0 or not snippet:
                    continue
                hit = passage(doc, max(0, start - 350))
                if hit:
                    rows.append(hit)
        return RetrievalResult(rows, "matched" if rows else "no_match")

    def read(hit, page):
        doc = get(hit.metadata["doc_id"])
        if not doc or revision(doc) != hit.revision:
            return RetrievalResult([], "stale")
        # Page zero is the beginning; later pages continue from the original hit.
        start = 0 if page == 0 else max(0, hit.metadata["start"] + page * PAGE_SIZE)
        result = passage(doc, start)
        return RetrievalResult([result] if result else [], "matched" if result else "no_match")

    def inventory():
        rows = []
        for doc_id in store.list_doc_ids(owner, space_id=space_id,
                                         tag=IMPORT_TAG if approved_only else "", limit=12):
            doc = get(doc_id)
            hit = passage(doc, 0) if doc else None
            if hit:
                rows.append(hit)
            if len(rows) >= 8:
                break
        return rows

    return IntelligentSource(source_id, "当前账号已授权导入规范" if approved_only else "当前账号普通知识文档",
                             search, current, read, inventory)


def web_source(*, android_api_level=None, cdd_only=False):
    provider = WebKnowledgeProvider()

    def permitted(row):
        if not row or row["error"] or row["revision"] != row["current_revision"]:
            return False
        url = urlsplit(row["url"])
        if url.scheme != "https" or url.hostname == "docs.partner.android.com":
            return False
        if row["kind"] == "cdd":
            return (url.netloc == "source.android.com"
                    and bool(re.fullmatch(r"/docs/compatibility/(\d+)/android-\1-cdd/?", url.path)))
        return not cdd_only and (row["license"].startswith("Administrator-approved content use")
                                 or (url.netloc == "source.android.com" and url.path.startswith("/docs/")))

    def from_row(row):
        if not permitted(row):
            return None
        text = redact_sensitive_text(row["original"])[:3500]
        return RetrievalHit("web_documents", row["token"], row["revision"], text, "background",
                            {"title": redact_sensitive_text(row["name"])[:160],
                             "section": redact_sensitive_text(row["section"])[:300],
                             "source_url": row["url"], "android_version": row["android_version"],
                             "requirement_id": row["requirement_id"], "requirement_level": row["level"],
                             "condition": redact_sensitive_text(row["condition"])[:1500],
                             "applicability_verified": False})

    def current(hit):
        row = provider.read_requirement(hit.reference)
        fresh = from_row(row)
        return bool(fresh and fresh.revision == hit.revision and fresh.text == hit.text
                    and fresh.metadata == hit.metadata)

    def search(query, limit):
        hits, _ = provider.search_with_status(query, limit=limit, android_api_level=android_api_level,
                                              public_cdd_only=cdd_only)
        rows = []
        for hit in hits:
            row = provider.read_requirement(hit.source_path)
            fresh = from_row(row)
            if fresh and fresh.revision == hit.source_revision:
                rows.append(fresh)
        return RetrievalResult(rows, "matched" if rows else "no_match")

    def read(hit, page):
        if page < 0:
            return RetrievalResult([], "unavailable")
        if not current(hit):
            return RetrievalResult([], "stale")
        data = provider.read_cdd_scope(hit.reference, mode="section", offset=page * 3, limit=3,
                                       android_api_level=android_api_level, include_documents=not cdd_only)
        rows = []
        for item in data["results"]:
            row = from_row(provider.read_requirement(item["reference"]))
            if row and row.revision == hit.revision:
                rows.append(row)
        return RetrievalResult(rows, data["status"])

    return IntelligentSource("web_documents", "公开 CDD 及经管理员批准的本地网页索引" if not cdd_only else "公开 Android CDD",
                             search, current, read)


def local_document_model(analyzer_factory, *, preferred=None, catalog_hash=""):
    """Select the local route once and reject mid-run catalog changes."""
    try:
        config = (config_manager.load_config() or {}).get("retrieval") or {}
        if preferred is None and isinstance(config, dict):
            preferred = config.get("llm_provider")
            if preferred is not None and (not isinstance(preferred, str) or not preferred.strip()):
                raise ValueError("Invalid local LLM provider")
        analyzer = analyzer_factory()
        name = analyzer.rag_local_provider(preferred)
        fingerprint = model_catalog_fingerprint(analyzer.config)
    except Exception:
        analyzer, name, fingerprint = None, None, ""
    if catalog_hash and fingerprint != catalog_hash:
        name = None

    def invoke(prompt, **kwargs):
        if not name or not fingerprint:
            return {"success": False}
        fresh = analyzer_factory()
        if (model_catalog_fingerprint(fresh.config) != fingerprint
                or fresh.rag_local_provider(name) != name):
            raise PermissionError("Model route changed")
        return fresh.generate_local_rag(prompt, provider_name=name, **kwargs)

    return invoke, name, fingerprint


def local_document_rag(question, sources, analyzer_factory, *, limit=8, preferred=None, catalog_hash="", recall_only=False):
    invoke, name, _ = local_document_model(analyzer_factory, preferred=preferred, catalog_hash=catalog_hash)
    result = IntelligentRAG(sources, invoke, limit=limit).run(redact_sensitive_text(question), recall_only=recall_only)
    result["provider"] = name or ""
    return result


def render_rag_answer(result):
    """Render only reviewed claims; exact quote and reference remain inspectable."""
    claims = result["claims"]
    if not claims:
        return {"model_unavailable": "本地 RAG 模型不可用或未返回有效复核结果，以下仅为检索候选。",
                "authorization_changed": "模型路由或文档授权已变化，本次分析已停止。"
                }.get(result["status"], "证据不足，无法给出有原文支持的结论。")
    lines = []
    for claim in claims:
        refs = " ".join(f'[{cite["id"]}]' for cite in claim["citations"])
        lines.append(f'{claim["statement"]} {refs}')
    lines.append("以上为本地模型基于已读取证据的复核结果，并非完整语料覆盖或独立人工验证。")
    return "\n\n".join(lines)
