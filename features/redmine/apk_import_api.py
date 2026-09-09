"""evidence artifact -> APK 分析导入及源码正文检索（2026-09-08 计划 §11）。

- ``POST /api/redmine-agent/artifacts/{artifact_id}/apk-analysis``
  在 owner 内部把已保存的 APK 原件复制进现有 APK 任务目录，再复用
  ``create_apk_task()``。绝不由 Controller 自己调用 HTTP upload，也绝不把
  Redmine 存储路径暴露给 APK API。
- ``GET  /api/apk/source-search/{task_id}`` 反编译源码正文搜索（固定字符串）。
- ``GET  /api/apk/source-read/{task_id}`` 反编译源码分段读取（行范围 + 游标）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_agent_scope
from features.firmware import (
    normalize_apk_task_id,
    persist_apk_task_locked,
    run_jadx_analysis,
    safe_join,
)
from features.firmware import runtime as firmware_runtime
from features.redmine.evidence import EvidenceError
from features.redmine.evidence_store import owner_evidence_store
from features.users import owner_id_from_request


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/redmine-agent")
apk_router = APIRouter(prefix="/api/apk")

APK_MAGIC = b"PK\x03\x04"
APK_MAX_ZIP_ENTRIES = 20000
SOURCE_SEARCH_MAX_QUERY = 256
SOURCE_SEARCH_MAX_FILES = 20000
SOURCE_SEARCH_MAX_SCAN_BYTES = 512 * 1024 * 1024
SOURCE_SEARCH_DEFAULT_LIMIT = 50
SOURCE_SEARCH_MAX_LIMIT = 200
SNIPPET_CONTEXT_LINES = 3
SOURCE_READ_MAX_LINES = 4000


def _validate_apk_structure(path: str) -> None:
    """伪装 ZIP 拒绝（计划 §11/§16）：ZIP 完整性 + AndroidManifest.xml + DEX。

    仅改名为 .apk 的普通 ZIP、损坏 ZIP、缺 manifest/DEX 的包都必须在
    进入 JADX 前被 422 拒绝。
    """
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad is not None:
                raise EvidenceError(
                    f"ZIP 完整性校验失败（损坏条目: {bad[:100]}）", status_code=422
                )
            names = archive.namelist()
            if len(names) > APK_MAX_ZIP_ENTRIES:
                raise EvidenceError(
                    f"ZIP 条目数 {len(names)} 超过上限 {APK_MAX_ZIP_ENTRIES}",
                    status_code=422,
                )
            name_set = set(names)
            has_manifest = (
                "AndroidManifest.xml" in name_set
                or any(n.lower() == "androidmanifest.xml" for n in names)
            )
            has_dex = any(n.lower().endswith(".dex") for n in names)
            if not has_manifest:
                raise EvidenceError(
                    "ZIP 内缺少 AndroidManifest.xml，不是合法 APK", status_code=422
                )
            if not has_dex:
                raise EvidenceError(
                    "ZIP 内缺少 DEX 文件（classes.dex），不是合法 APK", status_code=422
                )
    except zipfile.BadZipFile as exc:
        raise EvidenceError("ZIP 结构损坏，无法解析", status_code=422) from exc
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError(f"读取 APK 文件失败: {type(exc).__name__}", status_code=500) from exc


def _owner(request: Request) -> str:
    return owner_id_from_request(request)


def _error(exc: EvidenceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content={"success": False, "error": str(exc)}
    )


def _get_ready_apk_artifact(owner_id: str, artifact_id: str) -> tuple[dict[str, Any], str]:
    """返回 (artifact, stored_path)，校验 owner、ready、APK 结构。"""
    store = owner_evidence_store(owner_id)
    artifact = store.get_artifact(str(artifact_id or ""))
    if artifact is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    snapshot = store.get_snapshot(str(artifact.get("snapshot_id") or ""))
    if snapshot is None:
        raise EvidenceError("artifact 不存在", status_code=404)
    with store._connect() as conn:
        row = conn.execute(
            "SELECT stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
            (str(artifact_id),),
        ).fetchone()
    stored_rel = str(row["stored_path"]) if row else ""
    if not stored_rel:
        raise EvidenceError("artifact 未下载，无法导入 APK 分析", status_code=409)
    path = store.resolve_internal(stored_rel)
    if not path.is_file():
        raise EvidenceError("artifact 文件缺失", status_code=404)
    if str(artifact.get("status")) != "ready":
        raise EvidenceError("artifact 状态不是 ready", status_code=409)
    name = str(artifact.get("original_filename") or artifact.get("filename") or "")
    if not name.lower().endswith(".apk"):
        raise EvidenceError("仅支持 .apk 附件", status_code=422)
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic != APK_MAGIC:
        raise EvidenceError("附件不是合法的 ZIP/APK 结构", status_code=422)
    _validate_apk_structure(path)
    max_size = int(firmware_runtime.apk_max_file_size or 0)
    if max_size and path.stat().st_size > max_size:
        raise EvidenceError(
            f"APK 超过大小上限 {max_size // (1024 * 1024)}MB", status_code=413
        )
    return artifact, str(path)


@router.post("/artifacts/{artifact_id}/apk-analysis")
async def create_apk_analysis_from_artifact(artifact_id: str, request: Request):
    require = require_agent_scope("apk.analyze_own")
    require(request)
    owner_id = _owner(request)
    try:
        artifact, source_path = _get_ready_apk_artifact(owner_id, artifact_id)
    except EvidenceError as exc:
        return _error(exc)

    from features.firmware import create_apk_task, normalize_apk_filename

    task_id = str(uuid.uuid4())
    try:
        display_name = normalize_apk_filename(
            str(artifact.get("original_filename") or f"attachment_{artifact.get('attachment_id')}.apk")
        )
        task_dir = safe_join(str(firmware_runtime.apk_upload_dir), task_id)
        target_path = safe_join(task_dir, display_name)
    except ValueError as exc:
        return _error(EvidenceError(str(exc), status_code=422))
    os.makedirs(task_dir, exist_ok=True)
    shutil.copyfile(source_path, target_path)
    snapshot_id = str(artifact.get("snapshot_id") or "")
    try:
        create_apk_task(task_id, target_path, display_name, owner_id)
    except ValueError as exc:
        shutil.rmtree(task_dir, ignore_errors=True)
        return _error(EvidenceError(str(exc), status_code=429))
    # source_ref：让分析结论可追溯到原附件（§11.1）。
    with firmware_runtime.global_state.apk_analysis_tasks_lock:
        task = firmware_runtime.global_state.apk_analysis_tasks.get(task_id)
        if task is not None:
            task["source_ref"] = {
                "kind": "redmine_evidence",
                "issue_id": _issue_id_for_snapshot(owner_id, snapshot_id),
                "snapshot_id": snapshot_id,
                "artifact_id": str(artifact.get("artifact_id")),
                "attachment_id": str(artifact.get("attachment_id")),
                "sha256": str(artifact.get("sha256") or ""),
            }
            persist_apk_task_locked(task_id)

    output_dir = safe_join(str(firmware_runtime.apk_upload_dir), task_id, "jadx_output")
    with firmware_runtime.global_state.apk_analysis_tasks_lock:
        current = firmware_runtime.global_state.apk_analysis_tasks.get(task_id)
        if current is not None:
            current.update({"status": "analyzing", "progress": 5, "error": None})
            persist_apk_task_locked(task_id)
    background = asyncio.create_task(run_jadx_analysis(task_id, target_path, output_dir))
    firmware_runtime.global_state.background_tasks.add(background)
    background.add_done_callback(firmware_runtime.global_state.background_tasks.discard)

    return JSONResponse(
        status_code=202,
        content={
            "success": True,
            "data": {
                "task_id": task_id,
                "status": "analyzing",
                "source_ref": {
                    "issue_id": _issue_id_for_snapshot(owner_id, snapshot_id),
                    "snapshot_id": snapshot_id,
                    "artifact_id": str(artifact.get("artifact_id")),
                    "sha256": str(artifact.get("sha256") or ""),
                },
            },
        },
    )


def _issue_id_for_snapshot(owner_id: str, snapshot_id: str) -> int:
    store = owner_evidence_store(owner_id)
    snapshot = store.get_snapshot(snapshot_id)
    return int(snapshot.get("issue_id") or 0) if snapshot else 0


# ------------------------------------------------------- 源码正文搜索/读取

def _apk_task_for_read(task_id: str, owner_id: str) -> dict[str, Any]:
    from features.firmware import get_apk_task

    task, err = get_apk_task(task_id, require_completed=True, owner_id=owner_id)
    if err is not None:
        raise EvidenceError("APK 任务不存在或未完成", status_code=404)
    return task


@apk_router.get("/source-search/{task_id}")
async def apk_source_content_search(
    task_id: str,
    request: Request,
    q: str = Query(..., min_length=1, max_length=SOURCE_SEARCH_MAX_QUERY),
    limit: int = Query(SOURCE_SEARCH_DEFAULT_LIMIT, ge=1, le=SOURCE_SEARCH_MAX_LIMIT),
    path_filter: str = Query("", max_length=256),
):
    owner_id = _owner(request)
    require = require_agent_scope("apk.analyze_own")
    require(request)
    try:
        task = _apk_task_for_read(normalize_apk_task_id(task_id), owner_id)
    except (EvidenceError, ValueError) as exc:
        return _error(EvidenceError(str(exc), status_code=404))
    sources_dir = safe_join(str(task.get("output_dir") or ""), "sources")
    if not os.path.isdir(sources_dir):
        return _error(EvidenceError("源码目录不存在", status_code=404))

    needle = q
    lowered = needle.lower()
    filter_value = (path_filter or "").strip().lower()
    matches: list[dict[str, Any]] = []
    scanned_files = 0
    scanned_bytes = 0
    limited = False

    def scan() -> None:
        nonlocal scanned_files, scanned_bytes, limited
        for root, _dirs, files in os.walk(sources_dir):
            for name in files:
                if scanned_files >= SOURCE_SEARCH_MAX_FILES or scanned_bytes >= SOURCE_SEARCH_MAX_SCAN_BYTES:
                    limited = True
                    return
                if not name.endswith((".java", ".xml", ".json", ".smali")):
                    continue
                rel = os.path.relpath(os.path.join(root, name), sources_dir)
                if filter_value and filter_value not in rel.lower():
                    continue
                full = os.path.join(root, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                scanned_files += 1
                scanned_bytes += size
                try:
                    with open(full, encoding="utf-8", errors="replace") as handle:
                        for line_no, line in enumerate(handle, start=1):
                            if needle in line or lowered in line.lower():
                                matches.append({
                                    "path": rel,
                                    "line": line_no,
                                    "column": _find_column(line, needle, lowered),
                                    "snippet": line.rstrip("\n")[:400],
                                })
                                if len(matches) >= limit:
                                    return
                except OSError:
                    continue

    await asyncio.to_thread(scan)
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "query": needle,
            "total": len(matches),
            "limited": limited or len(matches) >= limit,
            "scanned_files": scanned_files,
            "matches": matches,
        },
    }


def _find_column(line: str, needle: str, lowered: str) -> int:
    index = line.find(needle)
    if index < 0:
        index = line.lower().find(lowered)
    return index + 1 if index >= 0 else 1


@apk_router.get("/source-read/{task_id}")
async def apk_source_read(
    task_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=512),
    offset: int = Query(0, ge=0),
    limit: int = Query(400, ge=1, le=SOURCE_READ_MAX_LINES),
):
    owner_id = _owner(request)
    require = require_agent_scope("apk.analyze_own")
    require(request)
    try:
        task = _apk_task_for_read(normalize_apk_task_id(task_id), owner_id)
    except (EvidenceError, ValueError) as exc:
        return _error(EvidenceError(str(exc), status_code=404))
    sources_dir = safe_join(str(task.get("output_dir") or ""), "sources")
    if not os.path.isdir(sources_dir):
        return _error(EvidenceError("源码目录不存在", status_code=404))
    normalized = (path or "").replace("\\", "/").lstrip("/")
    if normalized.startswith("..") or "/../" in normalized or "\x00" in normalized:
        return _error(EvidenceError("非法路径", status_code=422))
    target = safe_join(sources_dir, normalized)
    if not os.path.isfile(target):
        return _error(EvidenceError("文件不存在", status_code=404))
    max_file = int(firmware_runtime.apk_max_source_file_size or 0)
    if max_file and os.path.getsize(target) > max_file:
        return _error(
            EvidenceError(
                f"文件超过读取上限 {max_file // (1024 * 1024)}MB；请用搜索定位后缩小范围",
                status_code=413,
            )
        )

    def read_window() -> dict[str, Any]:
        with open(target, encoding="utf-8", errors="replace") as handle:
            all_lines = handle.readlines()
        window = all_lines[offset:offset + limit]
        return {
            "path": normalized,
            "total_lines": len(all_lines),
            "offset": offset,
            "returned_lines": len(window),
            "next_offset": offset + limit if offset + limit < len(all_lines) else None,
            "truncated": offset + limit < len(all_lines),
            "lines": [
                {"line": offset + index + 1, "text": line.rstrip("\n")}
                for index, line in enumerate(window)
            ],
        }

    data = await asyncio.to_thread(read_window)
    return {"success": True, "data": {"task_id": task_id, **data}}
