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
MAX_RELATED_SECTIONS = 40
MAX_RELATED_CLAUSES = 20
MAX_RELATED_FRAGMENTS = 5
MAX_RELATED_TABLE_ROWS = 20
RAM_TERM = re.compile(r'\bRAM\b|\b(?:advertised|physical|available|system|device|total|usable)\s+memory\b', re.I)
# Subsystem-local memory is not evidence of installed device RAM capacity.
_SUBSYSTEM_MEMORY = re.compile(
    r'\b(?:Android Packet Filtering|APF|Context Hub Runtime Environment|'
    r'Android Virtualization Framework|IOMMU)\b', re.I
)
_GO_DEFINITION = re.compile(
    r'\b(?:A Go device is|Android Go (?:device|edition)|Go devices? (?:are|must|is))\b', re.I
)
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
        # A qualified subtopic must not be rewritten to the declaration rule.
        if re.search(r"preload|预装|模块|module|APEX", message, re.I):
            return "section:Module preload requirements for low RAM devices"
        versions = set(re.findall(r"Android\s*(\d{2})(?!\d)", message, re.I))
        suffix = ";android=" + next(iter(versions)) if len(versions) == 1 else ""
        return "section:Low RAM" + suffix
    return ""


def _like(text):
    return "%" + escape_like(text) + "%"


def _ram_table(headers):
    """Recognize the declaration table by all four column meanings."""
    fields = {" ".join(str(value).casefold().split()) for value in headers}
    return {"os version", "must be low ram", "optionally low ram", "not qualified for gms"} <= fields


def _matches_android(label, version):
    match = re.fullmatch(r"Android\s+(\d{2})(?:\s+(or higher|and higher|or later|and later))?",
                         str(label).strip(), re.I)
    if not match:
        return False
    minimum = int(match.group(1))
    return version >= minimum if match.group(2) else version == minimum


_EXPLICIT_GO_DEFINITION = re.compile(
    r'\bA Go device is\b.{1,700}?(?=\s+(?:See Android Go|A new S/W|Note:|\[GMS)|$)',
    re.I,
)


def _ram_candidate_version(original, context, version):
    """Classify simple explicit device OS scopes; ambiguous scopes stay unverified."""
    scopes = []
    for text in (context, original):
        match = re.search(
            r'\bDEVICEs?\s+(?:that\s+)?(?:running|run|launching with|launching or upgrading to|'
            r'launching with or upgrading to|upgrading to)\s+Android\s+(\d{2})'
            r'(?:\s+(or higher|or later|and higher|to\s+Android\s+\d{2}))?'
            r'(?=\s*[:.,;]|\s+(?:with|and|that|including|MUST|MAY|are)\b|$)',
            text, re.I,
        )
        if not match:
            continue
        low = int(match.group(1))
        qualifier = (match.group(2) or '').lower()
        high = int(qualifier[-2:]) if qualifier.startswith('to android ') else (
            99 if qualifier else low)
        scopes.append(low <= version <= high)
    if scopes and all(scopes):
        return "version_match"
    if scopes and not any(scopes):
        return "other_version"
    return "unverified"


def _related_ram_evidence(conn, revision, coverage, excluded_keys, android_version):
    """Search the retained revision across sections, not just the declaration rule.

    A RAM mention is a candidate, not an applicable certification threshold.
    Keep the indexed conditions and nearby text for human review.
    """
    sections = {}
    excluded_subsystem_sections = 0
    scanned_sections = 0
    definitions = []
    seen_clauses = set()
    other_version_clauses = 0
    anchors = coverage.get("section_anchors") or {}
    for row in conn.execute(
        "SELECT section_key,path,text_excerpt FROM gms_update_requirement_sections "
        "WHERE source_key=? ORDER BY rowid", (SOURCE,),
    ):
        key = row["section_key"]
        if key in excluded_keys:
            continue
        scanned_sections += 1
        if _SUBSYSTEM_MEMORY.search(row["path"]):
            excluded_subsystem_sections += 1
            continue
        anchor = anchors.get(key)
        sections[key] = {
            "title": row["path"], "section_key": key, "source_revision": revision,
            "source_url": SOURCE_URL + "#" + quote(anchor, safe="-_.:") if anchor else "",
            "clauses": [], "table_rows": [], "fragments": [],
            "go_definition": bool(_GO_DEFINITION.search(row["text_excerpt"])),
            "clause_matches": 0, "table_row_matches": 0, "fragment_matches": 0,
        }
        text = row["text_excerpt"]
        for match in _EXPLICIT_GO_DEFINITION.finditer(text):
            definition = match.group(0).strip()
            if definition and all(item["text"] != definition for item in definitions):
                definitions.append({"text": definition, "title": row["path"],
                                    "source_url": sections[key]["source_url"]})
        # Merge nearby matches so a long condition/list is not represented as
        # several near-identical snippets. These are explicitly excerpts.
        ranges = []
        for match in sorted([*RAM_TERM.finditer(text), *_GO_DEFINITION.finditer(text)], key=lambda m: m.start()):
            start, end = max(0, match.start() - 260), min(len(text), match.end() + 360)
            if ranges and start <= ranges[-1][1] and end - ranges[-1][0] <= 1400:
                ranges[-1] = (ranges[-1][0], end)
            else:
                ranges.append((start, end))
        item = sections[key]
        item["fragment_matches"] = len(ranges)
        item["fragments"] = [
            {"text": text[start:end], "start": start, "end": end,
             "excerpt": start != 0 or end != len(text)}
            for start, end in ranges[:MAX_RELATED_FRAGMENTS]
        ]
    for row in conn.execute(
        "SELECT * FROM gms_update_requirement_clauses WHERE source_key=? AND source_revision=? "
        "ORDER BY rowid", (SOURCE, revision),
    ):
        item = sections.get(row["section_key"])
        if item is None or not RAM_TERM.search(row["original_text"] + "\n" + row["context_text"]):
            continue
        # A firmware buffer measured in bytes is not installed device RAM.
        if not re.search(r'\bRAM\b', row["original_text"] + "\n" + row["context_text"], re.I) and re.search(
            r'\b(?:\d+\s+bytes|APF|Packet Filter)\b', row["original_text"], re.I
        ):
            continue
        identity = (row["requirement_id"], row["original_text"], row["context_text"], row["scope_json"])
        if identity in seen_clauses:
            continue
        seen_clauses.add(identity)
        applicability = _ram_candidate_version(row["original_text"], row["context_text"], android_version)
        if applicability == "other_version":
            other_version_clauses += 1
        item["clause_matches"] += 1
        if len(item["clauses"]) < MAX_RELATED_CLAUSES:
            item["clauses"].append({**{k: row[k] for k in (
                "requirement_id", "original_text", "context_text", "source_url", "content_hash",
                "change_version", "change_kind", "scope_json",
            )}, "version_applicability": applicability})
    for row in conn.execute(
        "SELECT * FROM gms_update_requirement_table_rows WHERE source_key=? "
        "ORDER BY table_index,row_index", (SOURCE,),
    ):
        item = sections.get(row["section_key"])
        if item is None:
            continue
        headers, values = json.loads(row["headers_json"]), json.loads(row["values_json"])
        if not RAM_TERM.search(" ".join(map(str, headers + values))):
            continue
        item["table_row_matches"] += 1
        if len(item["table_rows"]) < MAX_RELATED_TABLE_ROWS:
            item["table_rows"].append({"headers": headers, "values": values,
                                       "content_hash": row["content_hash"]})
    matches = [item for item in sections.values()
               if item["clause_matches"] or item["table_row_matches"] or item["fragment_matches"]]
    # Prefer direct clause evidence, but retain section-only notes and tables.
    matches.sort(key=lambda item: (not item["go_definition"], not item["clause_matches"],
                                   not item["table_row_matches"], item["section_key"]))
    selected = matches[:MAX_RELATED_SECTIONS]
    return {
        "sections": selected, "section_matches": len(matches),
        "clause_matches": sum(item["clause_matches"] for item in matches),
        "table_row_matches": sum(item["table_row_matches"] for item in matches),
        "scanned_sections": scanned_sections,
        "excluded_subsystem_sections": excluded_subsystem_sections,
        "go_product_definitions": definitions[:5],
        "other_version_clauses": other_version_clauses,
        "truncated": len(matches) > len(selected) or any(
            item["clause_matches"] > len(item["clauses"])
            or item["table_row_matches"] > len(item["table_rows"])
            or item["fragment_matches"] > len(item["fragments"]) for item in selected
        ),
        "coverage_complete": False, "applicability_verified": False,
        "scope": "retained_revision_ram_mentions",
    }


def requirement_index_consistent(conn, coverage):
    """Check the retained revision against all indexed record families."""
    if not coverage or not coverage.get("source_revision"):
        return False
    counts = conn.execute(
        "SELECT count(*) AS total, count(DISTINCT source_revision) AS revisions, "
        "min(source_revision) AS revision FROM gms_update_requirement_clauses WHERE source_key=?",
        (SOURCE,),
    ).fetchone()
    if (not counts["total"] or counts["total"] != coverage.get("clause_candidates")
            or counts["revisions"] != 1 or counts["revision"] != coverage["source_revision"]):
        return False
    sections = conn.execute(
        "SELECT count(*) FROM gms_update_requirement_sections WHERE source_key=?", (SOURCE,),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT count(*) FROM gms_update_requirement_table_rows WHERE source_key=?", (SOURCE,),
    ).fetchone()[0]
    return sections == coverage.get("sections") and rows == coverage.get("table_rows")


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
    index_consistent = requirement_index_consistent(conn, coverage)
    params = [SOURCE]
    keys = []
    version_match = re.fullmatch(r"section:low ram;android=(\d{2})", query.strip(), re.I)
    android_version = int(version_match.group(1)) if version_match else None
    if version_match:
        query = "section:Low RAM"
    ram_scope = query.strip().casefold() in {"section:low ram", "section:gms-10.0-001"}
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
        ram_scope = anchor in {"low-ram", "GMS-10.0-001"}
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
    elif query.strip().casefold() == "section:low ram":
        # The certification memory intent refers to the declaration rule, not
        # every section that mentions low RAM (preload, performance, etc.).
        # Match the indexed clause or its actual section anchor; never invent text.
        keys = [key for key, anchor in (coverage.get("section_anchors") or {}).items()
                if anchor == "low-ram"]
        for hit in conn.execute(
            "SELECT DISTINCT section_key FROM gms_update_requirement_clauses "
            "WHERE source_key=? AND source_revision=? AND requirement_id=?",
            (SOURCE, revision, "GMS-10.0-001"),
        ):
            if hit[0] not in keys:
                keys.append(hit[0])
        predicate = "section_key IN (" + ",".join("?" for _ in keys) + ")" if keys else "0=1"
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
        bounded = len(clauses) > MAX_ROWS or len(rows) > MAX_ROWS
        context_limited = False
        if ram_scope:
            # Older indexes may attach neighboring storage policy to this heading.
            # Only display the exact clause and a table with the declaration schema.
            filtered_clauses = [item for item in clauses if item["requirement_id"] == "GMS-10.0-001"]
            filtered_rows = [item for item in rows if _ram_table(json.loads(item["headers_json"]))]
            context_limited = (len(filtered_clauses) != len(clauses) or len(filtered_rows) != len(rows)
                               or bounded)
            clauses, rows = filtered_clauses, filtered_rows
            if context_limited:
                # The flattened legacy excerpt cannot reliably separate notes and
                # adjacent policy; do not present it as an exact original section.
                text = ""
        anchor = (coverage.get("section_anchors") or {}).get(key)
        url = SOURCE_URL + "#" + quote(anchor, safe="-_.:") if anchor else ""
        if not url:
            urls = {item["source_url"] for item in clauses if item["anchor_kind"] == "section"}
            if len(urls) == 1:
                url = urls.pop()
        if ram_scope and not clauses:
            continue
        table_rows = [{"headers": json.loads(item["headers_json"]),
                       "values": json.loads(item["values_json"]),
                       "table_index": item["table_index"], "row_index": item["row_index"],
                       "content_hash": item["content_hash"]} for item in rows[:MAX_ROWS]]
        version_rows = []
        if android_version is not None:
            for item in table_rows:
                headers = [" ".join(str(value).casefold().split()) for value in item["headers"]]
                index = headers.index("os version")
                if index < len(item["values"]) and _matches_android(item["values"][index], android_version):
                    version_rows.append(item)
        result.append({
            "context_limited": context_limited, "version_rows": version_rows,
            "section_key": key, "title": section["path"], "source_url": url,
            "source_revision": revision, "text": text[:MAX_TEXT],
            "clauses": [{k: item[k] for k in ("requirement_id", "change_version", "change_kind",
                         "source_url", "content_hash", "original_text", "context_text",
                         "scope_json", "version_mentions_json", "clause_key")}
                        for item in clauses[:MAX_ROWS]],
            "table_rows": table_rows,
            "media": [] if context_limited else [item for item in coverage.get("media", [])
                                                 if item.get("section_key") == key][:MAX_ROWS],
            "truncated": (len(text) > MAX_TEXT or bounded),
        })
    related_ram = (_related_ram_evidence(conn, revision, coverage, set(keys), android_version)
                   if version_match else None)
    return {"status": "matched" if result or (related_ram and related_ram["sections"]) else "no_match",
            "sections": result, "related_ram": related_ram,
            "requested_android_version": android_version,
            "lookup_scope": "low_ram_declaration" if ram_scope else "section",
            "source_revision": revision, "indexed_at": row["indexed_at"],
            "revision_text_scope": coverage.get("revision_text_scope", "unknown"),
            "historical_applicability_verified": False,
            "index_consistent": index_consistent,
            "latest_scan_confirmed": bool(index_consistent and latest and latest["run_id"] == row["run_id"]
                                          and latest["state"] == "ready"),
            "truncated": len(sections) > MAX_SECTIONS or any(item["truncated"] for item in result),
            "coverage_complete": False, "applicability_verified": False,
            "original_document_verified": False, "scope": "retained_partner_html_human_only"}
