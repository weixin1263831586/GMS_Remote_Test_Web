"""Read GMS policy sections and their related rows from the retained scan."""

from __future__ import annotations

import json
import re
from urllib.parse import quote, unquote, urlsplit

from .mainline_records import escape_like
from .sources import SOURCES


SOURCE = "gms_requirements"
SOURCE_URL = next(source.url for source in SOURCES if source.key == SOURCE)
MAX_SECTIONS = 5
MAX_ROWS = 200
MAX_TEXT = 32000
CLAUSE_ID = re.compile(r"(?<![A-Za-z0-9])(?:GMS(?:X|-VSR)?|VSR)-[0-9][A-Z0-9_.-]*", re.I)


def policy_lookup_target(message: str) -> str:
    """Recognize explicit policy URLs/IDs and certification memory questions."""
    for url in re.findall(r"https?://[^\s<>，。；]+", message):
        try:
            parsed = urlsplit(url.rstrip(").,;"))
        except ValueError:
            continue
        if (parsed.scheme == "https" and parsed.netloc == "docs.partner.android.com"
                and parsed.path.rstrip("/") == urlsplit(SOURCE_URL).path):
            target = parsed._replace(query="").geturl()
            return target if len(target) <= 256 else ""
    match = CLAUSE_ID.search(message)
    if match:
        return "section:" + match.group(0).rstrip(".-").upper()
    if re.search(r"(?<![A-Za-z0-9])GMS(?![A-Za-z0-9])", message, re.I) and re.search(
        r"(?<![A-Za-z0-9])RAM(?![A-Za-z0-9])|Low\s*RAM|内存|运存|isLowRamDevice", message, re.I
    ):
        # Query the policy topic, not a hardcoded answer or a CDD substitute.
        return "section:Low RAM"
    return ""


def _like(text):
    return "%" + escape_like(text) + "%"


def lookup_requirement_sections(conn, query):
    """Return bounded, revision-bound human-readable evidence, never model input."""
    row = conn.execute(
        "SELECT * FROM gms_update_requirement_coverage WHERE source_key=?", (SOURCE,)
    ).fetchone()
    if row is None:
        latest = conn.execute(
            "SELECT run_id,state,discovered,fetched,parsed FROM gms_update_source_results "
            "WHERE source_key=? ORDER BY run_id DESC LIMIT 1", (SOURCE,),
        ).fetchone()
        return {"status": "scan_failed" if latest and latest["state"] == "failed" else "not_indexed",
                "sections": [], "coverage_complete": False,
                "latest_scan": dict(latest) if latest else None,
                "latest_scan_confirmed": False}
    coverage = json.loads(row["coverage_json"])
    revision = coverage.get("source_revision") or ""
    if not revision:
        return {"status": "revision_missing", "sections": [], "coverage_complete": False}
    latest = conn.execute(
        "SELECT run_id,state FROM gms_update_source_results WHERE source_key=? ORDER BY run_id DESC LIMIT 1",
        (SOURCE,),
    ).fetchone()
    params = [SOURCE]
    keys = []
    try:
        parsed = urlsplit(query) if query.startswith(("https://", "http://")) else None
    except ValueError:
        return {"status": "invalid_url", "sections": [], "coverage_complete": False}
    order = "level DESC, section_key"
    order_params = []
    if parsed:
        if (parsed.scheme != "https" or parsed.netloc != "docs.partner.android.com"
                or parsed.path.rstrip("/") != urlsplit(SOURCE_URL).path or not parsed.fragment):
            return {"status": "anchor_required", "sections": [], "coverage_complete": False}
        anchor = unquote(parsed.fragment)
        keys = [key for key, value in (coverage.get("section_anchors") or {}).items()
                if value == anchor][:MAX_ROWS]
        # Previously indexed revisions may still have exact clause URLs.
        for hit in conn.execute(
            "SELECT DISTINCT section_key FROM gms_update_requirement_clauses "
            "WHERE source_key=? AND source_revision=? AND source_url=?",
            (SOURCE, revision, SOURCE_URL + "#" + parsed.fragment),
        ):
            if hit[0] not in keys:
                keys.append(hit[0])
        if not keys:
            return {"status": "anchor_not_indexed", "sections": [], "coverage_complete": False}
        predicate = "section_key IN (" + ",".join("?" for _ in keys) + ")"
        params.extend(keys)
    elif CLAUSE_ID.fullmatch(query.removeprefix("section:")):
        predicate = ("section_key IN (SELECT section_key FROM gms_update_requirement_clauses "
                     "WHERE source_key=? AND source_revision=? AND requirement_id=? COLLATE NOCASE)")
        params.extend([SOURCE, revision, query.removeprefix("section:")])
    else:
        term = query.removeprefix("section:").strip()
        if not term:
            return {"status": "query_required", "sections": [], "coverage_complete": False}
        predicate = ('(title LIKE ? ESCAPE "\\" OR text_excerpt LIKE ? ESCAPE "\\")')
        params.extend([_like(term), _like(term)])
        order = 'CASE WHEN title LIKE ? ESCAPE "\\" THEN 0 ELSE 1 END, level DESC, section_key'
        order_params.append(_like(term))
    sections = conn.execute(
        "SELECT * FROM gms_update_requirement_sections WHERE source_key=? AND " + predicate
        + " ORDER BY " + order + " LIMIT ?", (*params, *order_params, MAX_SECTIONS + 1)
    ).fetchall()
    result = []
    for section in sections[:MAX_SECTIONS]:
        key = section["section_key"]
        clauses = conn.execute(
            "SELECT * FROM gms_update_requirement_clauses WHERE source_key=? AND section_key=? "
            "AND source_revision=? ORDER BY requirement_id,clause_key LIMIT ?",
            (SOURCE, key, revision, MAX_ROWS + 1),
        ).fetchall()
        rows = conn.execute(
            "SELECT * FROM gms_update_requirement_table_rows WHERE source_key=? AND section_key=? "
            "ORDER BY table_index,row_index LIMIT ?", (SOURCE, key, MAX_ROWS + 1)
        ).fetchall()
        text = section["text_excerpt"]
        anchor = (coverage.get("section_anchors") or {}).get(key)
        url = SOURCE_URL + "#" + quote(anchor, safe="-_.:") if anchor else ""
        if not url:
            urls = {item["source_url"] for item in clauses if item["anchor_kind"] == "section"}
            if len(urls) == 1:
                url = urls.pop()
        result.append({
            "section_key": key, "title": section["path"], "source_url": url,
            "source_revision": revision, "text": text[:MAX_TEXT],
            "clauses": [{k: item[k] for k in ("requirement_id", "change_version", "change_kind",
                         "source_url", "content_hash")} for item in clauses[:MAX_ROWS]],
            "table_rows": [{"headers": json.loads(item["headers_json"]),
                            "values": json.loads(item["values_json"]),
                            "table_index": item["table_index"], "row_index": item["row_index"],
                            "content_hash": item["content_hash"]} for item in rows[:MAX_ROWS]],
            "media": [item for item in coverage.get("media", []) if item.get("section_key") == key][:MAX_ROWS],
            "truncated": (len(text) > MAX_TEXT or len(rows) > MAX_ROWS or len(clauses) > MAX_ROWS),
        })
    return {"status": "matched" if result else "no_match", "sections": result,
            "source_revision": revision, "indexed_at": row["indexed_at"],
            "latest_scan_confirmed": bool(latest and latest["run_id"] == row["run_id"]
                                          and latest["state"] == "ready"),
            "truncated": len(sections) > MAX_SECTIONS or any(item["truncated"] for item in result),
            "coverage_complete": False, "applicability_verified": False,
            "original_document_verified": False, "scope": "retained_partner_html_human_only"}
