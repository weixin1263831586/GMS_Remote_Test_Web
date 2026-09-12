"""Redmine 证据检索 API（从 evidence_api.py 拆出，检索拆分）。

`GET /evidence/{snapshot_id}/search`：manifest（description/journals）+
artifact 派生文本全文匹配。zip 附件的文本成员在 fetch 阶段已派生成带
成员标记的文本，这里按成员给出 ``attachment:<file>.zip!/<member>`` 行级
引用（否则 logcat 已下载却搜不到）。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request

from features.auth import require_agent_scope
from features.redmine.evidence import split_zip_derived_text
from features.redmine.evidence_api import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    SEARCH_MAX_QUERY_CHARS,
    SNIPPET_CONTEXT_CHARS,
    _error_response,
    _get_snapshot,
    _owner,
)
from features.redmine.evidence_store import owner_evidence_store
from foundation.errors import handle_api_errors


router = APIRouter(prefix="/api/redmine-agent")


@router.get("/evidence/{snapshot_id}/search")
@handle_api_errors
async def search_evidence(
    snapshot_id: str,
    request: Request,
    q: str = Query(..., min_length=1, max_length=SEARCH_MAX_QUERY_CHARS),
    limit: int = Query(SEARCH_DEFAULT_LIMIT, ge=1, le=SEARCH_MAX_LIMIT),
):
    from features.redmine.evidence import EvidenceError

    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    try:
        snapshot = _get_snapshot(owner_id, snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    needle = q
    matches: list[dict[str, Any]] = []

    manifest = snapshot.get("manifest") or {}
    description = str(manifest.get("description") or "")
    _collect_text_match(matches, "description", "", description, needle, None)
    for journal in manifest.get("journals") or []:
        notes = str(journal.get("notes") or "")
        hit = _collect_text_match(matches, "journal", "", notes, needle, journal)
        if not hit:
            for detail in journal.get("details") or []:
                blob = json.dumps(detail, ensure_ascii=False)
                _collect_text_match(matches, "journal_detail", "", blob, needle, journal)

    store = owner_evidence_store(owner_id)
    artifacts = store.list_artifacts(snapshot["snapshot_id"])
    index_warnings = [
        {
            "artifact_id": artifact.get("artifact_id"),
            "filename": artifact.get("filename"),
            "warning": artifact.get("error"),
        }
        for artifact in artifacts
        if str(artifact.get("kind") or "") == "archive"
        and str(artifact.get("status") or "") == "partial"
        and artifact.get("error")
    ]
    for artifact in artifacts:
        with store._connect() as conn:
            row = conn.execute(
                "SELECT derived_text_path, stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(artifact.get("artifact_id")),),
            ).fetchone()
        candidates = [str(row["derived_text_path"] or ""), str(row["stored_path"] or "")] if row else []
        for rel in candidates:
            # 空候选（如无派生文本）只跳过本项；用 break 会让后面的
            # stored_path 永远不被扫描（真机 #648526 验收发现的缺陷）。
            if not rel:
                continue
            if len(matches) >= limit:
                break
            try:
                path = store.resolve_internal(rel)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # zip 派生文本带成员标记：按成员给出
            # attachment:<file>.zip!/<member>:L<line> 引用，而不是把整个
            # zip 派生文本当作一段无位置信息的大文本。标记只对 archive
            # 派生文本生效，防止普通附件内容伪造成员引用。
            is_derived = bool(row) and rel == str(row["derived_text_path"] or "")
            members = (
                split_zip_derived_text(text)
                if is_derived and str(artifact.get("kind") or "") == "archive"
                else []
            )
            if members:
                _collect_zip_member_matches(matches, members, needle, artifact, limit)
            else:
                _collect_text_match(matches, "artifact", "", text, needle, artifact)
            break

    matches = matches[:limit]
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "query": q,
            "total": len(matches),
            "limited": len(matches) >= limit,
            "index_complete": not index_warnings,
            "index_warnings": index_warnings,
            "matches": matches,
        },
    }


def _collect_text_match(
    matches: list[dict[str, Any]],
    kind: str,
    path: str,
    text: str,
    needle: str,
    meta: dict[str, Any] | None,
) -> bool:
    index = text.find(needle)
    if index < 0:
        return False
    start = max(0, index - SNIPPET_CONTEXT_CHARS)
    end = min(len(text), index + len(needle) + SNIPPET_CONTEXT_CHARS)
    entry: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "char_index": index,
        "snippet": text[start:end],
    }
    if meta is not None:
        if kind in {"journal", "journal_detail"}:
            entry["journal_id"] = meta.get("id")
        else:
            entry["artifact_id"] = meta.get("artifact_id")
            entry["filename"] = meta.get("filename")
    matches.append(entry)
    return True


def _collect_zip_member_matches(
    matches: list[dict[str, Any]],
    members: list[tuple[str, str]],
    needle: str,
    artifact: dict[str, Any],
    limit: int,
) -> None:
    """zip 派生文本按成员匹配，引用为
    ``attachment:<file>.zip!/<member>`` 并带 1-based 行号。"""

    filename = str(artifact.get("filename") or "")
    for member, member_text in members:
        if len(matches) >= limit:
            return
        index = member_text.find(needle)
        if index < 0:
            continue
        start = max(0, index - SNIPPET_CONTEXT_CHARS)
        end = min(len(member_text), index + len(needle) + SNIPPET_CONTEXT_CHARS)
        matches.append(
            {
                "kind": "artifact",
                "path": f"attachment:{filename}!{member}",
                "line": member_text.count("\n", 0, index) + 1,
                "char_index": index,
                "snippet": member_text[start:end],
                "artifact_id": artifact.get("artifact_id"),
                "filename": filename,
                "zip_member": member,
            }
        )
