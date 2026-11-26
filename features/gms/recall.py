"""Read-only, identifier-based recall of official Mainline and GMS references."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from foundation.config import settings

from .mainline_records import escape_like, query_security_patches_by_cve
from .parsing import matched_excerpt, reference_term_matches
from .update_records import bulletin_detail_coverage


IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])CVE-\d{4}-\d{4,7}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])(?:A-|P-|ag/|b/)\d{5,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9_])(?:Cts|Gts|Mts|Sts|Vts)[A-Za-z0-9_]+(?![A-Za-z0-9_])|"
    r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){2,}#[\w$]+(?:\[[^\]\r\n]{0,80}\])?",
    re.IGNORECASE,
)


def extract_reference_identifiers(text):
    return list(dict.fromkeys(
        match.group().upper() if match.group().upper().startswith("CVE-") else match.group()
        for match in IDENTIFIER_PATTERN.finditer(text)
    ))


def _lookup(path, table, columns, terms, *, kind, limit):
    if not path.is_file():
        return {"status": "unavailable", "items": [], "reason": "reference database is unavailable"}
    if not terms:
        return {"status": "not_applicable", "items": []}
    try:
        coverage = {}
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            critical = kind == "critical_patch"
            if critical:
                available = {row[1] for row in conn.execute('PRAGMA table_info(gms_update_bulletin_patches)')}
                columns = tuple(column for column in columns if column != 'detail_text' or column in available)
                coverage = bulletin_detail_coverage(conn)
                conn.create_function('reference_term_matches', 2, reference_term_matches)
            security = kind == "security_patch"
            cves = [term for term in terms if re.fullmatch(r"CVE-\d{4}-\d{4,7}", term, re.I)] if security else []
            search_terms = [term for term in terms if term not in cves]
            rows = []
            if search_terms:
                # 列名/表名均来自本文件固定调用方，无注入面。
                clauses = ["(" + " OR ".join(
                    f'reference_term_matches({column}, ?)' if critical else f'{column} LIKE ? ESCAPE "\\"'
                    for column in columns) + ")"
                           for _ in search_terms]
                params = [term if critical else f"%{escape_like(term)}%" for term in search_terms for _ in columns]
                selected = "DISTINCT cve_id" if security else "*"
                rows = [dict(row) for row in conn.execute(
                    f"SELECT {selected} FROM {table} WHERE {' OR '.join(clauses)} LIMIT ?",
                    [*params, 128 if security else limit],
                )]
            if security:
                # CVE 精确匹配；LIKE 命中只作候选，排序统一按发布日期。
                rows = query_security_patches_by_cve(conn, [*cves, *(row["cve_id"] for row in rows)], limit=limit)
    except sqlite3.Error:
        return {"status": "unavailable", "items": [], "reason": "reference database query failed"}
    items = []
    for row in rows:
        # 白名单仅官方内容字段，不外泄内部路径/同步状态。
        allowed = ("id", "cve_id", "mainline_module", "version_note", "release_year", "release_label",
                   "source_url", "source_title", "first_seen_at", "last_seen_at", "issue_type",
                   "test_module", "test_case", "exemption_id", "android_versions", "issue_text",
                   "patch_id", "patch_kind", "description", "year", "bulletin_month", "deadline", "bulletin_url",
                   "detail_status", "detail_fetched_at")
        item = {key: row[key][:4000] if isinstance(row[key], str) else row[key] for key in allowed if key in row}
        if row.get('detail_text'):
            item['detail_excerpt'] = matched_excerpt(row['detail_text'], terms)
            item['detail_content_sha256'] = hashlib.sha256(row['detail_text'].encode()).hexdigest()
        digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        identity = row.get("id") or row.get("item_key") or digest[:16]
        item["reference"] = f"ref:mainline:{kind}:{identity}"
        item["content_sha256"] = digest
        items.append(item)
    return {"status": "matched" if items else "no_match", "items": items, **coverage,
            "possibly_truncated": len(items) == limit}


def recall_mainline_references(identifiers, *, data_root=None, limit=20):
    """Query three official sources independently; unavailable never means no match."""
    root = Path(data_root) if data_root is not None else settings.data_root
    terms = list(dict.fromkeys(str(value)[:256] for value in identifiers))[:128]
    cves = [term for term in terms if re.fullmatch(r"CVE-\d{4}-\d{4,7}", term, re.I)]
    bug_numbers = [term.split("/", 1)[-1].split("-", 1)[-1] for term in terms
                   if re.fullmatch(r"(?:A-|P-|b/)\d{5,}", term, re.I)]
    mainline = root / "gms_update.sqlite3"
    exemptions = _lookup(mainline, "mainline_known_issues",
                         ("test_module", "test_case", "exemption_id", "issue_text"),
                         list(dict.fromkeys([*terms, *bug_numbers]))[:128], kind="exemption", limit=limit)
    critical_terms = list(terms)
    for item in exemptions["items"]:
        number = str(item.get("exemption_id") or "")
        if number.isdigit():
            critical_terms.extend(["A-" + number, "P-" + number])
    security_terms = [*cves, *(term for term in terms if re.fullmatch(r"(?:A-|P-|ag/|b/)\d{5,}", term, re.I))]
    return {
        "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "identifiers": terms,
        "sources": {
            "security_patches": _lookup(mainline, "mainline_security_patches", ("cve_id", "version_note"), security_terms,
                                         kind="security_patch", limit=limit),
            "exemptions": exemptions,
            "critical_patches": _lookup(root / "gms_update.sqlite3", "gms_update_bulletin_patches",
                                        ("patch_id", "description", "detail_text"), list(dict.fromkeys(critical_terms))[:128],
                                        kind="critical_patch", limit=limit),
        },
    }


def recall_gms_requirements(terms, *, data_root=None, limit=5):
    """Read already-authorized scanner records; callers must enforce recipient authorization."""
    root = Path(data_root) if data_root is not None else settings.data_root
    path = root / 'gms_update.sqlite3'
    terms = list(dict.fromkeys(str(term).strip()[:100] for term in terms if len(str(term).strip()) >= 3))[:12]
    unavailable = {'status': 'unavailable', 'coverage_complete': False, 'items': []}
    if not path.is_file():
        return {**unavailable, 'reason': 'GMS requirements index unavailable'}
    if not terms:
        return {'status': 'not_applicable', 'coverage_complete': False, 'items': []}
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            source = conn.execute(
                "SELECT * FROM gms_update_sources WHERE source_key='gms_requirements' AND status_code=200"
            ).fetchone()
            if source is None or not source['content_hash']:
                return {**unavailable, 'reason': 'No verified GMS requirements scan'}
            latest = conn.execute(
                "SELECT state FROM gms_update_source_results WHERE source_key='gms_requirements' ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            # Only the scanner's configured GMS policy document is eligible.
            if not source['url'].startswith('https://docs.partner.android.com/gms/policies/domains/reqs'):
                return {**unavailable, 'reason': 'Unexpected GMS requirements source'}
            candidates = []
            for table, key, section, text_column in (
                ('gms_update_requirement_sections', 'section_key', 'path', 'text_excerpt'),
                ('gms_update_requirement_table_rows', 'row_key', 'section_title', 'row_text'),
                ('gms_update_requirement_version_tags', 'tag_key', 'section_title', 'text_excerpt'),
            ):
                columns = (section, text_column)
                predicates = ' OR '.join(
                    f'{column} LIKE ? ESCAPE "\\"' for _term in terms for column in columns
                )
                params = [f'%{escape_like(term)}%' for term in terms for _ in columns]
                sql = f"SELECT * FROM {table} WHERE source_key='gms_requirements' AND ({predicates}) LIMIT 200"
                for row in conn.execute(sql, params):
                    combined = f'{row[section]} {row[text_column]}'.casefold()
                    score = sum(term.casefold() in combined for term in terms)
                    item = {
                        'reference': 'ref:gms-policy:' + row[key] + ':' + row['content_hash'][:16],
                        'source_url': source['url'],
                        'source_revision': source['content_hash'],
                        'section': row[section],
                        'original_text': matched_excerpt(row[text_column], terms),
                        'content_sha256': row['content_hash'],
                        'fetched_at': source['last_scanned_at'],
                        'source_kind': table.removeprefix('gms_update_requirement_'),
                        'status': 'candidate',
                    }
                    if table.endswith('_version_tags'):
                        item.update(android_version=row['android_version'], change_kind=row['change_kind'],
                                    requirement_ids=row['requirement_ids'])
                    if table.endswith('_table_rows'):
                        item['table_headers'] = row['headers_json'][:2000]
                    candidates.append((score, item))
    except sqlite3.Error:
        return {**unavailable, 'reason': 'GMS requirements query failed'}
    candidates.sort(key=lambda pair: (-pair[0], pair[1]['reference']))
    items = [item for _, item in candidates[:limit]]
    return {'status': 'matched' if items else 'no_match', 'coverage_complete': False, 'items': items,
            'source_scan_state': latest['state'] if latest else 'unknown',
            'reason': 'Authorized retained scanner records; current applicability requires verification'}
