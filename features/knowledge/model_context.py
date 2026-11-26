"""Model-safe context from public, revision-bound Android CDD candidates."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from foundation.config import config_manager
from foundation.redaction import redact_sensitive_text


def public_cdd_context(hits, *, limit=4):
    """Only public CDD evidence may extend the existing report model payload.

    Internal cases, Partner policy, source trees and wiki license-specific
    material require separate model authorization and are not selected here.
    """
    result = []
    seen = set()
    for hit in hits:
        if not isinstance(hit, dict) or hit.get("source") != "web_documents":
            continue
        extra = hit.get("extra")
        if not isinstance(extra, dict) or extra.get("source_kind") != "cdd" or extra.get("status") != "indexed":
            continue
        reference = str(extra.get("reference") or hit.get("source_path") or "")
        revision = str(hit.get("source_revision") or "")
        if not re.fullmatch(r"POL-[a-f0-9]{24}", reference) or not revision or reference in seen:
            continue
        url = str(extra.get("source_url") or "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (parsed.scheme != "https" or parsed.netloc != "source.android.com"
                or not re.fullmatch(r"/docs/compatibility/(\d+)/android-\1-cdd/?", parsed.path)):
            continue
        anchors = hit.get("source_anchors") or []
        if not isinstance(anchors, list) or not any(isinstance(anchor, dict) and anchor.get("url") == url
                   and anchor.get("revision") == revision and anchor.get("evidence_type") == "official"
                   for anchor in anchors):
            continue
        text = redact_sensitive_text(str(hit.get("snippet") or ""))[:1500]
        if not text:
            continue
        seen.add(reference)
        result.append({"reference": reference, "source_revision": revision, "source_url": url,
                       "android_version": str(hit.get("applicable_versions") or "")[:40],
                       "section": str(extra.get("section") or "")[:200],
                       "requirement_id": str(extra.get("requirement_id") or "")[:100],
                       "requirement_level": str(extra.get("requirement_level") or "")[:80],
                       "applicability_context": redact_sensitive_text(str(extra.get("applicability_context") or ""))[:1000],
                       "snippet": text, "evidence_level": "background",
                       "applicability_verified": False})
        if len(result) >= max(1, min(limit, 4)):
            break
    return result


def model_planned_cdd_hits(query, *, android_api_level=None, limit=4):
    """Opt-in local LLM recall for diagnostic workflows; never use a remote fallback."""
    try:
        config = (config_manager.load_config() or {}).get("retrieval") or {}
        if not isinstance(config, dict) or config.get("llm_background_recall") is not True:
            return []
        from .service import analyze_public_cdd

        result = analyze_public_cdd(query, limit=limit, android_api_level=android_api_level, recall_only=True)
        if result["rag"]["status"] == "model_planned_recall":
            return result["results"]
    except Exception:
        pass
    return []


def retrieve_public_cdd_context(query, *, limit=4, android_api_level=None, existing_hits=None):
    """Public background only; share model planning with report and image paths."""
    from .external.web_sources import WebKnowledgeProvider

    query = redact_sensitive_text(str(query or "")).strip()[:512]
    if not query:
        return public_cdd_context(existing_hits or [], limit=limit)
    planned = model_planned_cdd_hits(query, android_api_level=android_api_level, limit=limit)
    if planned:
        return public_cdd_context([*planned, *(existing_hits or [])], limit=limit)
    if existing_hits is not None:
        return public_cdd_context(existing_hits, limit=limit)
    hits, _ = WebKnowledgeProvider().search_with_status(
        query, limit=12, public_cdd_only=True, android_api_level=android_api_level,
    )
    return public_cdd_context([hit.to_dict() for hit in hits], limit=limit)
