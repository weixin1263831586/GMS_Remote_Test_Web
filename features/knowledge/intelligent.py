"""Authorized document adapters for the shared local LLM retrieval engine."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from features.kkagent import model_catalog_fingerprint
from features.retrieval import (
    IntelligentRAG,
    IntelligentSource,
    PassageInsightIndex,
    RetrievalHit,
    RetrievalResult,
    fuse_rankings,
    passage_token,
)
from foundation.config import config_manager
from foundation.redaction import redact_sensitive_text

from .external.web_sources import WebKnowledgeProvider
from .personal_semantic import _redacted_body, _revision, retrieve_personal_contexts
from .policy_imports import IMPORT_TAG, _approved_content, content_hash, is_policy_import, policy_import_filter


PAGE_SIZE = 2800


def understanding_model_key(flag):
    config = (config_manager.load_config() or {}).get("retrieval") or {}
    if not isinstance(config, dict) or config.get(flag) is not True:
        return ""
    catalog = model_catalog_fingerprint()
    preferred = config.get("llm_provider", "")
    if not catalog or not isinstance(preferred, str):
        return ""
    return hashlib.sha256(json.dumps(["passage-understanding-v1", catalog, preferred]).encode()).hexdigest()


def personal_insight_index(store, owner, *, approved_only=False):
    identity = hashlib.sha256(owner.encode()).hexdigest()
    kind = "approved" if approved_only else "ordinary"
    key = understanding_model_key("allow_local_personal_llm_understanding")
    return PassageInsightIndex(store.db_path.parent / "understanding" / identity / (kind + ".sqlite3"), key)


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
        index = personal_insight_index(store, owner, approved_only=approved_only)
        hints = index.search(re.findall(r"[\w.-]{2,}", query), resolve, limit=limit)
        pool = {item.identity: item for item in [*hints, *rows]}
        ranked = fuse_rankings([[item.identity for item in rows], [item.identity for item in hints]], limit=limit)
        return RetrievalResult([pool[key] for key in ranked], "matched" if ranked else "no_match")

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

    def resolve(reference, rev):
        try:
            doc_id, offset = reference.rsplit(":", 1)
            doc = get(doc_id)
            hit = passage(doc, int(offset)) if doc else None
            return hit if hit and hit.revision == rev else None
        except (ValueError, KeyError):
            return None

    def manifest():
        ids = store.list_doc_ids(owner, space_id=space_id, tag=IMPORT_TAG if approved_only else "", limit=32)
        complete = len(ids) < 32
        rows = {}
        for doc_id in ids:
            doc = get(doc_id)
            if not doc:
                continue
            body = _redacted_body(doc)
            complete &= len(body) <= 64000
            for start in range(0, min(len(body), 64000), 2400):
                if len(rows) >= 512:
                    return rows, False
                # Ingestion uses 2400-character windows; query reads use the same
                # resolver and may include up to PAGE_SIZE characters.
                hit = passage(doc, start)
                if hit:
                    rows[passage_token(hit)] = hit
        return rows, complete

    return IntelligentSource(source_id, "当前账号已授权导入规范" if approved_only else "当前账号普通知识文档",
                             search, current, read, inventory, manifest, resolve)


def web_source(*, android_api_level=None, cdd_only=False, public_only=False):
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
        return not cdd_only and (
            (url.netloc == "source.android.com" and url.path.startswith("/docs/"))
            or (not public_only and row["license"].startswith("Administrator-approved content use")))

    def from_row(row):
        if not permitted(row):
            return None
        text = redact_sensitive_text(row["original"])[:3500]
        return RetrievalHit("web_documents", row["token"], row["revision"], text, "background",
                            {"title": redact_sensitive_text(row["name"])[:160],
                             "section": redact_sensitive_text(row["section"])[:300],
                             "source_url": row["url"], "android_version": row["android_version"],
                             "source_kind": row["kind"],
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
                                              public_cdd_only=cdd_only, public_only=public_only)
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

    label = "公开 Android CDD" if cdd_only else "公开 AOSP 文档与 Android CDD"
    if not cdd_only and not public_only:
        label += "及经管理员批准的网页"
    return IntelligentSource("web_documents", label, search, current, read)


def wiki_source(*, android_api_level=None):
    """Read only approved indexed Wiki content with explicit local-model use approval."""
    from .external.android_internals import AndroidInternalsProvider
    from .external.frontmatter import _scalarize

    def state():
        config = config_manager.load_config() or {}
        retrieval = config.get("retrieval") or {}
        if not isinstance(retrieval, dict) or retrieval.get("allow_local_wiki_llm") is not True:
            return None, ""
        provider = AndroidInternalsProvider.from_config(config)
        if provider is None:
            return None, ""
        status = provider.status()
        policy = provider.load_policy()
        rev = status.source_revision
        if (status.status != "ready" or not rev or rev != status.approved_revision
                or rev != status.available_revision or policy.degraded or not policy.license_expression):
            return None, ""
        return provider, rev

    def valid_policy(db, provider, rev):
        meta = dict(db.execute("SELECT key,value FROM wiki_meta").fetchall())
        policy = provider.load_policy()
        if (policy.degraded or meta.get("source_revision") != rev
                or meta.get("approved_revision") != rev
                or json.loads(meta.get("policy_state") or "{}").get("content_hash") != policy.content_hash):
            return None
        return policy

    def page_hits(provider, rev, policy, row, starts):
        path = row["path"]
        fm = provider._load_frontmatter(row["frontmatter"])
        if not policy.is_eligible(path, fm.get("tags") or []):
            return
        body = redact_sensitive_text(row["content"])
        for start in starts:
            if not 0 <= start < len(body):
                continue
            yield RetrievalHit("android_internals", f"{path}:{start}", rev, body[start:start + PAGE_SIZE],
                               "background", {"path": path, "start": start, "total_chars": len(body),
                               "title": redact_sensitive_text(row["title"])[:200],
                               "applicable_versions": _scalarize(fm.get("applicable_versions")),
                               "license": policy.license_expression,
                               "applicability_verified": False, "offset_basis": "redacted_indexed_document"})

    def passage(provider, rev, path, start):
        with provider._connect() as db:
            db.execute("BEGIN")
            policy = valid_policy(db, provider, rev)
            if not policy:
                return None
            row = db.execute("SELECT path,title,content,frontmatter FROM wiki_pages WHERE path=?", (path,)).fetchone()
            return next(page_hits(provider, rev, policy, row, [start]), None) if row else None

    def current(hit):
        provider, rev = state()
        if not provider or rev != hit.revision:
            return False
        fresh = passage(provider, rev, hit.metadata["path"], hit.metadata["start"])
        return fresh == hit

    def search(query, limit):
        provider, rev = state()
        if not provider:
            return RetrievalResult([], "unavailable")
        rows = []
        for hit in provider.search(query, limit=limit, android_api_level=android_api_level):
            if hit.source_revision != rev:
                continue
            # FTS snippets can contain highlighting; locate by indexed section line or page start.
            with provider._connect() as db:
                row = db.execute("SELECT content FROM wiki_pages WHERE path=?", (hit.source_path,)).fetchone()
            if row is None:
                continue
            start_line = (hit.extra.get("section") or {}).get("start_line")
            body = row["content"]
            # The indexed page does not include frontmatter. Use heading matching
            # rather than claiming an exact original source line for a redacted passage.
            heading = (hit.extra.get("section") or {}).get("heading")
            text = redact_sensitive_text(body)
            pos = text.find(str(heading)) if heading and start_line else 0
            item = passage(provider, rev, hit.source_path, max(0, pos - 200))
            if item:
                rows.append(item)
        hints = wiki_insight_index().search(re.findall(r"[\w.-]{2,}", query), resolve, limit=limit)
        pool = {hit.identity: hit for hit in [*hints, *rows]}
        ranked = fuse_rankings([[hit.identity for hit in rows], [hit.identity for hit in hints]], limit=limit)
        return RetrievalResult([pool[key] for key in ranked], "matched" if ranked else "no_match")

    def read(hit, page):
        provider, rev = state()
        if not provider or rev != hit.revision:
            return RetrievalResult([], "stale")
        start = 0 if page == 0 else max(0, hit.metadata["start"] + page * PAGE_SIZE)
        fresh = passage(provider, rev, hit.metadata["path"], start)
        return RetrievalResult([fresh] if fresh else [], "matched" if fresh else "no_match")

    def resolve(reference, revision):
        try:
            path, offset = reference.rsplit(":", 1)
            provider, rev = state()
            return passage(provider, rev, path, int(offset)) if provider and rev == revision else None
        except (ValueError, KeyError):
            return None

    def manifest():
        provider, rev = state()
        if not provider:
            return {}, False
        rows = {}
        complete = True
        with provider._connect() as db:
            db.execute("BEGIN")
            policy = valid_policy(db, provider, rev)
            if not policy:
                return {}, False
            cursor = db.execute("SELECT path,title,content,frontmatter FROM wiki_pages ORDER BY path")
            for row in cursor:
                # A bounded manifest is not the complete upstream corpus.
                complete &= len(row["content"]) <= 128000
                for hit in page_hits(provider, rev, policy, row, range(0, min(len(row["content"]), 128000), 2400)):
                    if len(rows) >= 4096:
                        return rows, False
                    rows[passage_token(hit)] = hit
        return rows, complete

    return IntelligentSource("android_internals", "已批准版本且获本地模型处理授权的 Android 机制 Wiki（背景证据）",
                             search, current, read, None, manifest, resolve)


def wiki_insight_index():
    from .external.android_internals import index_db_path

    key = understanding_model_key("auto_understand_approved_wiki")
    config = (config_manager.load_config() or {}).get("retrieval") or {}
    if not isinstance(config, dict) or config.get("allow_local_wiki_llm") is not True:
        key = ""
    return PassageInsightIndex(index_db_path().parent / "wiki-understanding.sqlite3", key)



def local_document_model(analyzer_factory, *, catalog_hash=""):
    """Select the local route once and reject mid-run catalog changes."""
    try:
        config = (config_manager.load_config() or {}).get("retrieval", {})
        analyzer = analyzer_factory()
        name = analyzer.configured_rag_provider(config)
        fingerprint = model_catalog_fingerprint(analyzer.config)
    except Exception:
        analyzer, name, fingerprint = None, None, ""
    if catalog_hash and fingerprint != catalog_hash:
        name = None

    def invoke(prompt, **kwargs):
        if not name or not fingerprint:
            return {"success": False}
        fresh = analyzer_factory()
        current_config = (config_manager.load_config() or {}).get("retrieval", {})
        if (model_catalog_fingerprint(fresh.config) != fingerprint
                or fresh.configured_rag_provider(current_config) != name):
            raise PermissionError("Model route changed")
        return fresh.generate_local_rag(prompt, provider_name=name, **kwargs)

    return invoke, name, fingerprint


def local_document_rag(question, sources, analyzer_factory, *, limit=8, catalog_hash="", recall_only=False):
    invoke, name, _ = local_document_model(analyzer_factory, catalog_hash=catalog_hash)
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
    unresolved = result.get("unresolved") or []
    if unresolved:
        lines.append("尚未确认：" + "；".join(str(item) for item in unresolved))
    lines.append("以上为本地模型基于已读取证据的复核结果，并非完整语料覆盖或独立人工验证。")
    return "\n\n".join(lines)
