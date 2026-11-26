"""Section and requirement extraction for external HTML documents."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

from lxml import html


_REQUIREMENT = re.compile(r"\[([A-Z][A-Z0-9]*-(?:\d+|SR)-\d+)\]")
_LEVEL = re.compile(r"\b(MUST NOT|SHALL NOT|SHOULD NOT|STRONGLY RECOMMENDED|NOT RECOMMENDED|MUST|SHALL|REQUIRED|SHOULD|RECOMMENDED|MAY|OPTIONAL)\b")
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _text(node):
    return " ".join(node.text_content().split())


def parse_document(body: bytes, url: str, kind: str, android_version: int | None):
    root = html.fromstring(body, parser=html.HTMLParser(encoding="utf-8", no_network=True))
    candidates = root.xpath("//div[contains(concat(' ',normalize-space(@class),' '),' devsite-article-body ')]")
    if not candidates:
        candidates = root.xpath("//article|//main")
    if not candidates:
        raise ValueError("Document body not found")
    article = candidates[0]
    if kind == "cdd":
        headings = root.xpath("//h1")
        titles = " ".join(_text(n) for n in headings)
        if not re.search(rf"Android\s+{android_version}\s+Compatibility\s+Definition", titles, re.I):
            raise ValueError("CDD title/version mismatch")
    for node in article.xpath(".//script|.//style|.//nav|.//footer|.//aside|.//form|.//del|.//s|.//strike"):
        node.drop_tree()
    section, anchor, condition = "", "", ""
    sections, rows = [], []
    for node in article.iter():
        tag = node.tag
        if tag in _HEADING:
            section, anchor, condition = _text(node), node.get("id", ""), ""
            continue
        if tag not in {"p", "li", "dt", "dd", "pre", "tr"}:
            continue
        if any(parent.tag in {"li", "tr"} for parent in node.iterancestors() if parent is not article):
            continue
        text = _text(node)
        if not text or not section:
            continue
        ids = list(_REQUIREMENT.finditer(text)) if _REQUIREMENT.match(text) else []
        link = url + ("#" + quote(anchor, safe="-_.:") if anchor else "")
        if ids:
            # Inline references may point to other clauses, not define new ones.
            match = ids[0]
            level = _LEVEL.search(text[match.end():])
            rows.append((section, match.group(1), level.group(1) if level else "",
                         condition, text, link))
        else:
            sections.append((section, "", "", condition, text, link))
            if tag == "p":
                condition = text[-4000:]
    if kind == "cdd" and len(rows) < 50:
        raise ValueError("CDD requirement coverage unexpectedly low")
    if not rows and not sections:
        raise ValueError("No indexable content")
    return rows + sections


def document_revision(body: bytes):
    return hashlib.sha256(body).hexdigest()
