"""Redmine 证据快照只读 API。

所有端点：
- 从认证身份推导 owner（禁止任何 owner_id 参数）；
- 显式检查 ``redmine.read`` / ``artifacts.read_own`` / ``apk.analyze_own``；
- 不返回服务端内部路径、异常堆栈或凭据；
- 大内容一律分页/分段。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from features.auth import require_agent_scope
from features.redmine.evidence import (
    DOWNLOAD_POLICIES,
    EvidenceError,
    EvidenceFetcher,
    parse_issue_ref,
    snapshot_completeness,
    start_evidence_fetch,
    wait_for_snapshot,
)
from features.redmine.evidence_store import owner_evidence_store
from features.users import owner_id_from_request
from foundation.errors import handle_api_errors


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/redmine-agent")

_IMAGE_MIME_WHITELIST = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}
MCP_IMAGE_MAX_ENCODED_BYTES = 768 * 1024
EVIDENCE_CACHE_TTL_SECONDS = 300
SEARCH_MAX_QUERY_CHARS = 256
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
SNIPPET_CONTEXT_CHARS = 200


def _snapshot_fresh(snapshot: dict[str, Any], ttl_seconds: int) -> bool:
    """快照 fetched_at 距今是否仍在 TTL 内（无法解析时视为不新鲜）。"""
    from datetime import datetime, timedelta

    fetched_at = str(snapshot.get("fetched_at") or "")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return datetime.now() - fetched < timedelta(seconds=ttl_seconds)


def _owner(request: Request) -> str:
    return owner_id_from_request(request)


def _get_snapshot(owner_id: str, snapshot_id: str) -> dict[str, Any]:
    store = owner_evidence_store(owner_id)
    snapshot = store.get_snapshot(str(snapshot_id or ""))
    if snapshot is None:
        raise EvidenceError("snapshot 不存在", status_code=404)
    return snapshot


def _get_artifact(owner_id: str, artifact_id: str) -> dict[str, Any]:
    store = owner_evidence_store(owner_id)
    artifact = store.get_artifact(str(artifact_id or ""))
    if artifact is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    snapshot = store.get_snapshot(str(artifact.get("snapshot_id") or ""))
    if snapshot is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    return artifact


def _error_response(exc: EvidenceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": str(exc)},
    )


# ------------------------------------------------------------------ snapshot

@router.post("/issues/{issue_id}/evidence")
@handle_api_errors
async def create_evidence_snapshot(
    issue_id: str,
    request: Request,
):
    """创建（或刷新）一个 issue 的证据快照。"""
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    body: dict[str, Any] = {}
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            body = payload
    except Exception:
        body = {}

    refresh = bool(body.get("refresh", True))
    download = str(body.get("download") or "none")
    if download not in DOWNLOAD_POLICIES:
        return _error_response(
            EvidenceError(f"download 必须是 {DOWNLOAD_POLICIES} 之一", status_code=422)
        )

    from features.redmine.evidence import owner_base_url, preflight_owner_fetch

    try:
        base_url = owner_base_url(owner_id)
        numeric_id = parse_issue_ref(issue_id, base_url)
    except EvidenceError as exc:
        return _error_response(exc)

    store = owner_evidence_store(owner_id)
    if not refresh:
        # refresh=false 只允许复用短 TTL 内的 ready 快照；
        # partial/failed 一律重新抓取。
        existing = store.latest_snapshot_for_issue(numeric_id)
        if (
            existing
            and str(existing.get("status")) == "ready"
            and _snapshot_fresh(existing, EVIDENCE_CACHE_TTL_SECONDS)
        ):
            return {
                "success": True,
                "data": _snapshot_payload(existing, cache_hit=True),
            }

    # 快照 pre-flight（2026-09-11 反馈）：凭据/地址缺失时在建快照之前
    # 快速失败，不再留 failed 垃圾快照（evidence_fetch.run 内仍保留同一
    # 校验作为后台任务兜底）。
    try:
        preflight_owner_fetch(owner_id)
    except EvidenceError as exc:
        return _error_response(exc)

    fetcher = EvidenceFetcher(owner_id, store=store)
    snapshot = fetcher.create_snapshot(numeric_id, download=download)
    start_evidence_fetch(owner_id, snapshot)
    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "data": {
                "snapshot_id": snapshot["snapshot_id"],
                "issue_id": numeric_id,
                "status": snapshot.get("status"),
                "status_url": f"/api/redmine-agent/evidence/{snapshot['snapshot_id']}",
            },
        },
    )


def _snapshot_payload(snapshot: dict[str, Any], *, cache_hit: bool = False) -> dict[str, Any]:
    artifacts = None
    data = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "issue_id": snapshot.get("issue_id"),
        "status": snapshot.get("status"),
        "cache_hit": cache_hit,
        "content_sha256": snapshot.get("content_sha256") or "",
        "journal_count": int(snapshot.get("journal_count") or 0),
        "attachment_count": int(snapshot.get("attachment_count") or 0),
        "downloaded_count": int(snapshot.get("downloaded_count") or 0),
        "download_policy": snapshot.get("download_policy") or "none",
        "fetched_at": snapshot.get("fetched_at") or "",
        "source_updated_on": snapshot.get("source_updated_on") or "",
        "errors": snapshot.get("errors") or [],
        "completeness": snapshot.get("completeness") or snapshot_completeness(snapshot, artifacts),
        "complete": False,
    }
    data["complete"] = (
        str(data["status"]) == "ready"
        and all(data["completeness"].values())
        and not data["errors"]
    )
    return data


@router.get("/evidence/{snapshot_id}")
@handle_api_errors
async def get_evidence_status(snapshot_id: str, request: Request):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    return {"success": True, "data": _snapshot_payload(snapshot)}


@router.get("/issues/{issue_id}/evidence/latest")
@handle_api_errors
async def get_latest_evidence_snapshot(issue_id: str, request: Request):
    """该 owner 某 issue 的最新快照（2026-09-11 反馈：issue-show 参数语义）。

    issue-show 收到 issue_id 时用它解析 snapshot_id；优先返回最近 ready
    快照，若只有 running/partial/failed 则返回最近一条并附带其 status，
    便于 CLI 区分「无快照」与「快照未就绪」。
    """
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    from features.redmine.evidence import owner_base_url

    try:
        base_url = owner_base_url(owner_id)
        numeric_id = parse_issue_ref(issue_id, base_url)
    except EvidenceError as exc:
        return _error_response(exc)
    store = owner_evidence_store(owner_id)
    candidates = store.list_snapshots_for_issue(numeric_id, limit=20)
    if not candidates:
        return _error_response(
            EvidenceError(
                f"issue {numeric_id} 还没有证据快照；先运行 "
                f"gms-rt-redmine-issue-fetch {numeric_id}",
                status_code=404,
            )
        )
    ready = [s for s in candidates if str(s.get("status")) == "ready"]
    snapshot = (ready or candidates)[0]
    return {
        "success": True,
        "data": {
            "issue_id": numeric_id,
            "snapshot_id": snapshot.get("snapshot_id"),
            "status": snapshot.get("status"),
            "ready": str(snapshot.get("status")) == "ready",
        },
    }


@router.get("/evidence/{snapshot_id}/wait")
@handle_api_errors
async def wait_evidence_snapshot(
    snapshot_id: str,
    request: Request,
    timeout: int = Query(120, ge=1, le=600),
):
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    try:
        _get_snapshot(owner_id, snapshot_id)
        snapshot = await __import__("asyncio").to_thread(
            wait_for_snapshot, owner_id, snapshot_id, timeout_seconds=timeout
        )
    except EvidenceError as exc:
        return _error_response(exc)
    return {"success": True, "data": _snapshot_payload(snapshot)}


@router.get("/evidence/{snapshot_id}/issue")
@handle_api_errors
async def get_evidence_issue(
    snapshot_id: str,
    request: Request,
    description_offset: int = Query(0, ge=0),
    description_limit: int = Query(65536, ge=1, le=262144),
):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    manifest = snapshot.get("manifest") or {}
    description = str(manifest.get("description") or "")
    window = description[description_offset:description_offset + description_limit]
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "issue_id": snapshot.get("issue_id"),
            "subject": manifest.get("subject") or "",
            "status": manifest.get("status") or "",
            "project": manifest.get("project") or "",
            "tracker": manifest.get("tracker") or "",
            "priority": manifest.get("priority") or "",
            "assigned_to": manifest.get("assigned_to") or "",
            "created_on": manifest.get("created_on") or "",
            "updated_on": manifest.get("updated_on") or "",
            "fields": manifest.get("fields") or {},
            "description": {
                "total_chars": len(description),
                "offset": description_offset,
                "returned_chars": len(window),
                "text": window,
                "truncated": description_offset + description_limit < len(description),
            },
        },
    }


@router.get("/evidence/{snapshot_id}/journals")
@handle_api_errors
async def get_evidence_journals(
    snapshot_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    cursor: str = Query(""),
):
    require = require_agent_scope("redmine.read")
    require(request)
    try:
        snapshot = _get_snapshot(_owner(request), snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    journals = (snapshot.get("manifest") or {}).get("journals") or []
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            return _error_response(EvidenceError("cursor 非法", status_code=422))
    window = journals[start:start + limit]
    next_cursor = None
    if start + limit < len(journals):
        next_cursor = str(start + limit)
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "issue_id": snapshot.get("issue_id"),
            "total": len(journals),
            "returned": len(window),
            "next_cursor": next_cursor,
            "truncated": False,
            "journals": window,
        },
    }


@router.get("/evidence/{snapshot_id}/artifacts")
@handle_api_errors
async def get_evidence_artifacts(snapshot_id: str, request: Request):
    require = require_agent_scope("redmine.read")
    require(request)
    owner_id = _owner(request)
    try:
        snapshot = _get_snapshot(owner_id, snapshot_id)
    except EvidenceError as exc:
        return _error_response(exc)
    store = owner_evidence_store(owner_id)
    artifacts = store.list_artifacts(snapshot["snapshot_id"])
    items = []
    for artifact in artifacts:
        items.append({
            "artifact_id": artifact.get("artifact_id"),
            "attachment_id": artifact.get("attachment_id"),
            "filename": artifact.get("filename"),
            "original_filename": artifact.get("original_filename"),
            "content_type": artifact.get("content_type"),
            "detected_content_type": artifact.get("detected_content_type") or "",
            "kind": artifact.get("kind"),
            "status": artifact.get("status"),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "declared_size": int(artifact.get("declared_size") or 0),
            "sha256": artifact.get("sha256") or "",
            "error": artifact.get("error") or "",
        })
    return {
        "success": True,
        "data": {
            "snapshot_id": snapshot.get("snapshot_id"),
            "total": len(items),
            "artifacts": items,
        },
    }


# ------------------------------------------------------------------ artifacts

def _artifact_file(owner_id: str, artifact_id: str, *, derived: bool = False) -> tuple[dict[str, Any], Path]:
    artifact = _get_artifact(owner_id, artifact_id)
    store = owner_evidence_store(owner_id)
    rel = str(artifact.get("derived_text_path") or "") if derived else str(artifact.get("stored_path") or "")
    # stored_path 已在 _artifact_from_row 中剥离，需要直接查询数据库。
    with store._connect() as conn:
        row = conn.execute(
            "SELECT stored_path, derived_text_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
            (str(artifact_id),),
        ).fetchone()
    if row is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    rel = str(row["derived_text_path"] or "") if derived else str(row["stored_path"] or "")
    if not rel:
        raise EvidenceError(
            "该 artifact 没有可用文本" if derived else "该 artifact 未下载",
            status_code=404,
        )
    path = store.resolve_internal(rel)
    if not path.is_file():
        raise EvidenceError("artifact 文件缺失", status_code=404)
    return artifact, path


@router.get("/artifacts/{artifact_id}/download")
@handle_api_errors
async def download_evidence_artifact(artifact_id: str, request: Request):
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id)
    except EvidenceError as exc:
        return _error_response(exc)

    from features.redmine.utils import attachment_content_disposition

    def iterfile():
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type=str(artifact.get("content_type") or "application/octet-stream"),
        headers={
            "Content-Disposition": attachment_content_disposition(artifact.get("original_filename")),
            "X-Evidence-SHA256": str(artifact.get("sha256") or ""),
        },
    )


@router.get("/artifacts/{artifact_id}/text")
@handle_api_errors
async def read_evidence_artifact_text(
    artifact_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(65536, ge=1, le=262144),
):
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id, derived=True)
    except EvidenceError as exc:
        # 原件存在但无派生文本时，尝试直接按 UTF-8 读取原文件。
        try:
            artifact, path = _artifact_file(_owner(request), artifact_id)
            if str(artifact.get("kind") or "") not in {"text", "log"}:
                raise
        except EvidenceError:
            return _error_response(exc)
    data = path.read_bytes()
    size = len(data)
    # 以字符方式切片：先解码为文本再切片，保证多字节字符不被截半。
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gb18030", errors="replace")
    window = text[offset:offset + limit]
    return {
        "success": True,
        "data": {
            "artifact_id": artifact.get("artifact_id"),
            "total_chars": len(text),
            "total_bytes": size,
            "offset": offset,
            "returned_chars": len(window),
            "text": window,
            "truncated": offset + limit < len(text),
        },
    }


@router.get("/artifacts/{artifact_id}/image")
@handle_api_errors
async def read_evidence_artifact_image(artifact_id: str, request: Request):
    """返回 MCP image content（base64 原图），超限时明确报错而不是悄悄截断。"""
    require = require_agent_scope("artifacts.read_own")
    require(request)
    try:
        artifact, path = _artifact_file(_owner(request), artifact_id)
    except EvidenceError as exc:
        return _error_response(exc)
    kind = str(artifact.get("kind") or "")
    detected = str(artifact.get("detected_content_type") or "")
    declared = str(artifact.get("content_type") or "").split(";")[0].strip().lower()
    if kind != "image":
        return _error_response(EvidenceError("artifact 不是图片", status_code=422))
    mime = detected if detected in _IMAGE_MIME_WHITELIST else (
        declared if declared in _IMAGE_MIME_WHITELIST else ""
    )
    if not mime:
        suffix = path.suffix.lower()
        mime = next(
            (m for m, ext in _IMAGE_MIME_WHITELIST.items() if ext == suffix), ""
        )
    if not mime:
        return _error_response(
            EvidenceError("图片 MIME 类型无法判定；请下载原件人工查看", status_code=422)
        )
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    if len(encoded) > MCP_IMAGE_MAX_ENCODED_BYTES:
        return _error_response(
            EvidenceError(
                f"图片编码后 {len(encoded)} 字节，超过 MCP 上限 {MCP_IMAGE_MAX_ENCODED_BYTES}；"
                "请下载原件查看",
                status_code=413,
            ),
        )
    return {
        "success": True,
        "data": {
            "artifact_id": artifact.get("artifact_id"),
            "mime_type": mime,
            "base64": encoded,
            "encoding": "base64",
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "sha256": artifact.get("sha256") or "",
            "scaled": False,
            "derived_sha256": artifact.get("sha256") or "",
        },
    }

