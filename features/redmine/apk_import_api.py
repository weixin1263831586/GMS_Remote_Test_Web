"""evidence artifact -> APK 分析导入。

- ``POST /api/redmine-agent/artifacts/{artifact_id}/apk-analysis``
  在 owner 内部把已保存的 APK 原件复制进现有 APK 任务目录，再复用
  ``create_apk_task()``。绝不由 Controller 自己调用 HTTP upload，也绝不把
  Redmine 存储路径暴露给 APK API。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.apk import (
    persist_apk_task_locked,
    run_jadx_analysis,
    safe_join,
)
from features.apk import runtime as apk_runtime
from features.auth import require_agent_scope
from features.redmine.evidence.service import EvidenceError
from features.redmine.evidence.store import owner_evidence_store
from features.users import owner_id_from_request


router = APIRouter(prefix="/api/redmine-agent")

APK_MAGIC = b"PK\x03\x04"
APK_MAX_ZIP_ENTRIES = 20000


def _validate_apk_structure(path: str) -> None:
    """伪装 ZIP 拒绝：ZIP 完整性 + AndroidManifest.xml + DEX。

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
    max_size = int(apk_runtime.apk_max_file_size or 0)
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

    from features.apk import create_apk_task, normalize_apk_filename

    task_id = str(uuid.uuid4())
    try:
        display_name = normalize_apk_filename(
            str(artifact.get("original_filename") or f"attachment_{artifact.get('attachment_id')}.apk")
        )
        task_dir = safe_join(str(apk_runtime.apk_upload_dir), task_id)
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
    # source_ref：让分析结论可追溯到原附件。
    with apk_runtime.global_state.apk_analysis_tasks_lock:
        task = apk_runtime.global_state.apk_analysis_tasks.get(task_id)
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

    output_dir = safe_join(str(apk_runtime.apk_upload_dir), task_id, "jadx_output")
    with apk_runtime.global_state.apk_analysis_tasks_lock:
        current = apk_runtime.global_state.apk_analysis_tasks.get(task_id)
        if current is not None:
            current.update({"status": "analyzing", "progress": 5, "error": None})
            persist_apk_task_locked(task_id)
    background = asyncio.create_task(run_jadx_analysis(task_id, target_path, output_dir))
    apk_runtime.global_state.background_tasks.add(background)
    background.add_done_callback(apk_runtime.global_state.background_tasks.discard)

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
