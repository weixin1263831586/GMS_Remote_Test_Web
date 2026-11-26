"""Model-safe context from public, revision-bound Android CDD candidates."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

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
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "source.android.com"
                or not re.fullmatch(r"/docs/compatibility/(\d+)/android-\1-cdd/?", parsed.path)):
            continue
        anchors = hit.get("source_anchors") or []
        if not any(isinstance(anchor, dict) and anchor.get("url") == url
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
