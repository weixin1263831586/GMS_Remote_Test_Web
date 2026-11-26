"""Bounded evidence metadata for model-led retrieval and follow-up queries."""

from __future__ import annotations

import re
from typing import Any


_RESULT_KEYS = ("results", "items", "contexts", "matches")
_SOURCE_FAILURES = frozenset(("error", "unavailable", "not_configured", "empty", "partial",
                              "failed", "stale", "interrupted", "timeout", "pending", "busy"))


def summarize_evidence(tool: str, payload: Any) -> dict | None:
    """Keep coverage and provenance separate from content or model judgments."""
    if not isinstance(payload, dict):
        return None
    data = payload
    tool_failed = False
    observation_truncated = False
    for _ in range(8):
        tool_failed |= data.get("success") is False
        observation_truncated |= data.get("observation_truncated") is True
        nested = data.get("data")
        if not isinstance(nested, dict):
            break
        data = nested
    else:
        observation_truncated = True
    source_status = data.get("sources_status")
    source_groups = data.get("sources")
    grouped_records = []
    group_limited = False
    has_groups = isinstance(source_groups, dict) and any(
        isinstance(row, dict) and isinstance(row.get("items"), list)
        for row in source_groups.values()
    )
    if has_groups:
        if source_status is None:
            source_status = []
        if isinstance(source_status, list):
            source_status = list(source_status)
            group_limited = len(source_groups) > 16
            for name, row in list(source_groups.items())[:16]:
                if not isinstance(row, dict) or not isinstance(row.get("items"), list):
                    group_limited = True
                    continue
                source_status.append({**row, "source": str(name),
                                      "coverage_complete": row.get("coverage_complete") is True
                                      and not row.get("possibly_truncated")})
                group_limited |= bool(row.get("possibly_truncated")) or len(row["items"]) > 30
                grouped_records.extend({**item, "source": str(name)} for item in row["items"][:30]
                                       if isinstance(item, dict))
    records = next((data[key] for key in _RESULT_KEYS if isinstance(data.get(key), list)), None)
    if records is None and has_groups and isinstance(source_status, list):
        records = grouped_records
    batches = data.get("queries")
    batch_rows = [row for row in batches[:16] if isinstance(row, dict)] if isinstance(batches, list) else []
    if records is None and batch_rows:
        records = [item for row in batch_rows if isinstance(row.get("matches"), list)
                   for item in row["matches"][:30] if isinstance(item, dict)]
    sdk = bool(data.get("source_id")) and isinstance(data.get("matches"), list) and (
        "reproducible" in data or "commit" in data)
    gerrit = "gerrit" in tool and records is not None
    is_retrieval = (isinstance(source_status, list) or "coverage_complete" in data
                    or "retrieval" in data or sdk or gerrit or (records is not None
                        and any(part in tool for part in ("search", "recall", "knowledge", "evidence"))))
    if not is_retrieval:
        return None
    gaps = []
    sources = []
    references = []
    revisions = []
    if data.get("coverage_complete") is False:
        gaps.append("incomplete_coverage")
    if group_limited:
        gaps.append("bounded_or_unconfirmed")
    if data.get("source_coverage_complete") is False:
        gaps.append("incomplete_source_scope")
    if data.get("index_complete") is False or data.get("index_warnings"):
        gaps.append("incomplete_evidence_index")
    retrieval_status = data.get("retrieval")
    if isinstance(retrieval_status, dict):
        if retrieval_status.get("status") in _SOURCE_FAILURES:
            gaps.append("semantic:" + tool + ":" + str(retrieval_status["status"]))
        elif retrieval_status.get("coverage_complete") is False and retrieval_status.get("status") != "disabled":
            gaps.append("incomplete_evidence_index")
    if data.get("remote_search_error"):
        gaps.append("remote_search_failed")
    if sdk and data.get("global_coverage_complete") is False:
        gaps.append("selected_sdk_scope_only")
    if sdk and data.get("reproducible") is False:
        gaps.append("sdk_revision_unpinned")
    if gerrit:
        if data.get("configured") is False:
            gaps.append("source:gerrit:not_configured")
        if data.get("error"):
            gaps.append("source:gerrit:error")
        stats = data.get("stats")
        if isinstance(stats, dict) and stats.get("moreChanges"):
            gaps.append("more_result_pages")
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("total"), int) and records is not None:
        offset = meta.get("offset", 0)
        if isinstance(offset, int) and offset + len(records) < meta["total"]:
            gaps.append("more_result_pages")
    if (data.get("limited") is True or data.get("next_offset") is not None
            or data.get("page_complete") is False or data.get("next_query")
            or any(row.get("truncated") or row.get("next_offset") is not None
                   or row.get("page_complete") is False or row.get("next_query") for row in batch_rows)):
        gaps.append("more_result_pages")
    if any(row.get("coverage_complete") is False or row.get("source_coverage_complete") is False
           for row in batch_rows):
        gaps.append("incomplete_source_scope")
    if any(row.get("index_complete") is False for row in batch_rows):
        gaps.append("incomplete_evidence_index")
    if (isinstance(batches, list) and (len(batches) > 16 or len(batch_rows) < len(batches)
            or any(not isinstance(row.get("matches"), list) or len(row["matches"]) > 30
                   for row in batch_rows))):
        gaps.append("bounded_or_unconfirmed")
    if records is not None and len(records) > 30:
        gaps.append("bounded_or_unconfirmed")
    if isinstance(source_status, list) and len(source_status) > 16:
        gaps.append("bounded_or_unconfirmed")
    if any(row.get("total") == 0 for row in batch_rows):
        gaps.append("batch_query_without_match")
    if data.get("possibly_truncated") is True or data.get("latest_confirmed") is False:
        gaps.append("bounded_or_unconfirmed")
    if tool_failed:
        gaps.append("tool_failed")
    if observation_truncated:
        gaps.append("bounded_or_unconfirmed")
    if records == []:
        gaps.append("no_match_in_returned_scope")
    if isinstance(source_status, list):
        for row in source_status[:16]:
            if not isinstance(row, dict) or not row.get("source"):
                gaps.append("bounded_or_unconfirmed")
                continue
            state = str(row.get("status") or "unknown")
            source = {"source": str(row["source"])[:80], "status": state[:40]}
            for key in ("source_revision", "approved_revision", "available_revision"):
                if row.get(key):
                    source[key] = str(row[key])[:128]
            if row.get("coverage_complete") is False and state != "not_applicable":
                gaps.append("source_coverage:" + source["source"])
            if state in _SOURCE_FAILURES or state == "unknown":
                gaps.append("source:" + source["source"] + ":" + state)
            if row.get("available_revision") and row.get("source_revision") != row["available_revision"]:
                gaps.append("source_revision_not_current:" + source["source"])
            retrieval = row.get("retrieval")
            if isinstance(retrieval, dict):
                source["semantic_status"] = str(retrieval.get("status") or "unknown")[:40]
                if retrieval.get("status") in _SOURCE_FAILURES:
                    gaps.append("semantic:" + source["source"] + ":" + source["semantic_status"])
                elif retrieval.get("coverage_complete") is False and retrieval.get("status") != "disabled":
                    gaps.append("incomplete_evidence_index")
            sources.append(source)
    for item in (records or [])[:30]:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
        reference = item.get("reference") or extra.get("reference") or item.get("source_path")
        revision = item.get("source_revision") or item.get("revision")
        origin = item.get("source") or tool
        journal = item.get("journal_reference")
        if isinstance(journal, dict) and journal.get("reference") and journal.get("source_revision"):
            reference, revision, origin = journal["reference"], journal["source_revision"], "redmine_journals"
        if sdk:
            # Request labels (HEAD, Android release, OpenGrok index) are not commits.
            commit = str(item.get("commit") or data.get("commit") or "")
            path = str(item.get("path") or "")
            reference, revision = "", ""
            if path and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
                repo = str(item.get("repo") or data.get("repo_scope") or "")
                reference = f"{repo + '/' if repo else ''}{path}:{item.get('line') or ''}"
                revision, origin = commit, data["source_id"]
                if repo and data.get("reproducible") is True:
                    revisions.append({"kind": "sdk", "project": repo, "revision": commit.lower(),
                                      "reference": reference, "source": str(origin)[:80]})
            else:
                gaps.append("sdk_revision_unpinned")
        if gerrit and not data.get("error") and data.get("configured") is not False:
            patch = item.get("currentPatchSet") or item.get("current_patch_set") or {}
            patch_revision = patch.get("revision") if isinstance(patch, dict) else None
            rest_revision = item.get("current_revision")
            sha = str(patch_revision or rest_revision or "").lower()
            project = str(item.get("project") or "")
            change = str(item.get("number") or item.get("_number") or "")
            if (project and change.isdigit() and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha)
                    and (not patch_revision or not rest_revision or str(patch_revision).lower() == str(rest_revision).lower())):
                reference, revision, origin = f"gerrit:{project}:{change}", sha, "gerrit"
                revisions.append({"kind": "gerrit", "project": project, "revision": sha,
                                  "reference": reference, "source": "gerrit"})
        if reference and revision:
            ref = {"reference": str(reference)[:300], "revision": str(revision)[:128],
                   "source": str(origin)[:80]}
            if ref not in references:
                references.append(ref)
        if extra.get("status") == "stale":
            gaps.append("stale_candidate")
    if len(references) > 16:
        gaps.append("bounded_or_unconfirmed")
    return {"tool": tool, "returned": len(records) if records is not None else None,
            "coverage_complete": (data.get("coverage_complete") is True
                                  and data.get("source_coverage_complete") is not False
                                  and data.get("index_complete") is not False
                                  and not gaps),
            "sources": sources, "references": references[:16],
            **({"revision_candidates": revisions[:16]} if revisions else {}),
            "gaps": list(dict.fromkeys(gaps))[:24]}


def evidence_plan_context(observations: list, *, limit=8) -> dict:
    """Summarize prior observations; never invent a source or issue a tool call."""
    evidence = [item["evidence"] for item in observations if isinstance(item, dict)
                and isinstance(item.get("evidence"), dict)]
    selected = evidence[-max(1, min(limit, 8)):]
    revision_rows = [row for item in selected for row in item.get("revision_candidates", [])
                     if isinstance(row, dict)]
    links = []
    for sdk in revision_rows:
        if sdk.get("kind") != "sdk":
            continue
        for change in revision_rows:
            if (change.get("kind") == "gerrit" and sdk.get("project") == change.get("project")
                    and sdk.get("revision") == change.get("revision")):
                link = {"project": sdk["project"], "revision": sdk["revision"],
                        "sdk_reference": sdk["reference"], "gerrit_reference": change["reference"],
                        "relation": "same_project_and_commit", "applicability_verified": False}
                if link not in links and len(links) < 16:
                    links.append(link)
    return {"retrievals": selected, "omitted_retrievals": max(0, len(evidence) - len(selected)),
            "sdk_gerrit_links": links,
            "has_unresolved_scope": any(not item.get("coverage_complete") or item.get("gaps")
                                        for item in evidence)}


def evidence_followup_required(observations: list) -> bool:
    """Request at most one extra planning round for an explicit unresolved retrieval gap."""
    if any(isinstance(item, dict) and item.get("evidence_followup") for item in observations):
        return False
    evidence = [item["evidence"] for item in observations
                if isinstance(item, dict) and isinstance(item.get("evidence"), dict)]
    actionable = {"incomplete_coverage", "incomplete_source_scope", "incomplete_evidence_index",
                  "remote_search_failed", "more_result_pages", "batch_query_without_match",
                  "no_match_in_returned_scope", "bounded_or_unconfirmed",
                  "selected_sdk_scope_only", "sdk_revision_unpinned", "tool_failed", "stale_candidate"}
    return any(gap in actionable or gap.startswith(("source:", "semantic:", "source_revision_not_current:",
                                                    "source_coverage:"))
               for item in evidence for gap in item.get("gaps", []) if isinstance(gap, str))
