"""APK router - APK upload, analysis, decompilation, and source browsing APIs."""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from features.apk.service import (
    ANDROID_NS,
    JAVA_IDENTIFIER_RE,
    _build_apk_symbol_index,
    _get_apk_upload_lock,
    _read_manifest_xml,
    _score_apk_symbol_candidate,
    cleanup_files,
    create_apk_task,
    get_apk_task,
    normalize_apk_filename,
    normalize_apk_task_id,
    persist_apk_task_locked,
    run_jadx_analysis,
    safe_join,
)
from features.auth import require_agent_scope, require_authenticated_user
from features.users import owner_id_from_request
from foundation.errors import handle_api_errors
from foundation.responses import ApiResponse
from foundation.uploads import merge_files_to_path, save_upload_to_path

from . import runtime


logger = logging.getLogger(__name__)

router = APIRouter()
MAX_APK_UPLOAD_CHUNKS = 1_000
SOURCE_SEARCH_MAX_QUERY = 256
SOURCE_SEARCH_MAX_FILES = 20000
SOURCE_SEARCH_MAX_SCAN_BYTES = 512 * 1024 * 1024
SOURCE_SEARCH_DEFAULT_LIMIT = 50
SOURCE_SEARCH_MAX_LIMIT = 200
SOURCE_READ_MAX_LINES = 4000


def _owner_id(request: Request) -> str:
    return require_authenticated_user(request).resource_owner_id  # ADR 0010: APK 工件按账号分区

def _require_apk_scope(request: Request) -> None:
    """APK 全链路统一 scope：上传/分析/读取结果
    都要求 ``apk.analyze_own``；人类角色默认已持有，Agent Token 需显式授权。
    """
    require_agent_scope("apk.analyze_own")(request)


def _task_id_in_use(task_id: str) -> bool:
    with runtime.global_state.apk_analysis_tasks_lock:
        return task_id in runtime.global_state.apk_analysis_tasks


@router.post("/api/apk/upload")
@handle_api_errors
async def upload_apk(
    request: Request,
    file: UploadFile | None = File(None),
    chunk_index: int | None = Form(None),
    total_chunks: int | None = Form(None),
    upload_id: str | None = Form(None),
    file_name: str | None = Form(None),
):
    """Upload APK file for analysis."""
    _require_apk_scope(request)
    if not file:
        return ApiResponse.error("No file provided", status_code=400)
    owner_id = _owner_id(request)

    try:
        filename = normalize_apk_filename(file_name or file.filename)
        task_id = normalize_apk_task_id(upload_id)
        task_dir = safe_join(runtime.apk_upload_dir, task_id)
        apk_path = safe_join(task_dir, filename)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    if _task_id_in_use(task_id):
        return ApiResponse.error('Task ID already exists', status_code=409)

    os.makedirs(task_dir, exist_ok=True)

    if (chunk_index is None) != (total_chunks is None):
        return ApiResponse.error("Incomplete chunk parameters", status_code=400)

    if chunk_index is not None and total_chunks is not None:
        if (
            total_chunks <= 0
            or total_chunks > MAX_APK_UPLOAD_CHUNKS
            or chunk_index < 0
            or chunk_index >= total_chunks
        ):
            return ApiResponse.error("Invalid chunk parameters", status_code=400)

        chunk_path = safe_join(task_dir, f"{filename}.part{chunk_index}")
        upload_lock = _get_apk_upload_lock(task_id)
        async with upload_lock:
            if _task_id_in_use(task_id):
                return ApiResponse.error('Task ID already exists', status_code=409)
            metadata_path = safe_join(task_dir, 'upload_metadata.json')
            metadata = {
                'filename': filename,
                'total_chunks': total_chunks,
                'owner_id': owner_id,
            }
            if os.path.exists(metadata_path):
                try:
                    with open(metadata_path, encoding='utf-8') as handle:
                        if json.load(handle) != metadata:
                            return ApiResponse.error(
                                'Chunk metadata does not match upload session',
                                status_code=400,
                            )
                except (OSError, json.JSONDecodeError):
                    return ApiResponse.error(
                        'Upload session metadata is invalid',
                        status_code=400,
                    )
            else:
                if os.path.exists(apk_path):
                    return ApiResponse.error("Upload session ownership could not be verified", status_code=409)
                with open(metadata_path, 'w', encoding='utf-8') as handle:
                    json.dump(metadata, handle)

            chunk_paths = [
                safe_join(task_dir, f"{filename}.part{i}")
                for i in range(total_chunks)
            ]
            file_size = os.path.getsize(apk_path) if os.path.isfile(apk_path) else 0
            if not 0 < file_size <= runtime.apk_max_file_size:
                cleanup_files([apk_path])
                try:
                    await save_upload_to_path(file, chunk_path, runtime.apk_max_file_size)
                except ValueError as exc:
                    cleanup_files([chunk_path])
                    return ApiResponse.error(str(exc), status_code=413)

                present = [path for path in chunk_paths if os.path.isfile(path)]
                file_size = sum(os.path.getsize(path) for path in present)
                if file_size > runtime.apk_max_file_size:
                    cleanup_files([*chunk_paths, metadata_path])
                    return ApiResponse.error(
                        f"File too large, max {runtime.apk_max_file_size // (1024 * 1024)}MB",
                        status_code=413,
                    )
                if len(present) < total_chunks:
                    return ApiResponse.success({
                        "task_id": task_id, "filename": filename,
                        "chunk_received": chunk_index + 1, "total_chunks": total_chunks, "uploaded": False,
                    })
                if file_size == 0:
                    return ApiResponse.error("APK file is empty", status_code=400)
                await asyncio.to_thread(merge_files_to_path, chunk_paths, apk_path)

            try:
                create_apk_task(task_id, apk_path, filename, owner_id)
            except ValueError as exc:
                return ApiResponse.error(str(exc), status_code=429)
            cleanup_files([*chunk_paths, metadata_path])
            return ApiResponse.success({"task_id": task_id, "filename": filename, "size": file_size, "uploaded": True})
    else:
        upload_lock = _get_apk_upload_lock(task_id)
        async with upload_lock:
            if _task_id_in_use(task_id):
                return ApiResponse.error('Task ID already exists', status_code=409)
            # Do not allow a single-file request to take over an incomplete
            # chunk session (including one owned by another authenticated user).
            metadata_path = safe_join(task_dir, 'upload_metadata.json')
            if os.path.exists(metadata_path):
                return ApiResponse.error('Upload session already exists', status_code=409)
            with os.scandir(task_dir) as entries:
                session_has_files = any(entries)
            if session_has_files:
                return ApiResponse.error('Upload session directory is not empty', status_code=409)
            try:
                file_size = await save_upload_to_path(
                    file, apk_path, runtime.apk_max_file_size
                )
            except ValueError as e:
                cleanup_files([apk_path])
                return ApiResponse.error(str(e), status_code=413)
            try:
                create_apk_task(task_id, apk_path, filename, owner_id)
            except ValueError as exc:
                cleanup_files([apk_path])
                return ApiResponse.error(str(exc), status_code=429)
        return ApiResponse.success({"task_id": task_id, "filename": filename, "size": file_size})


@router.post("/api/apk/analyze/{task_id}")
@handle_api_errors
async def analyze_apk(task_id: str, request: Request):
    """Start jadx decompilation analysis."""
    _require_apk_scope(request)
    task, err = get_apk_task(
        task_id, require_completed=False, owner_id=_owner_id(request)
    )
    if err:
        return err
    task_id = normalize_apk_task_id(task_id)

    apk_path = task["apk_path"]
    if not os.path.exists(apk_path):
        return ApiResponse.error("APK file not found, please re-upload", status_code=404)

    output_dir = safe_join(runtime.apk_upload_dir, task_id, "jadx_output")

    with runtime.global_state.apk_analysis_tasks_lock:
        current = runtime.global_state.apk_analysis_tasks.get(task_id)
        if (
            current is None
            or current.get("owner_id") != task.get("owner_id")
            or current.get("apk_path") != apk_path
        ):
            return ApiResponse.error("任务不存在", status_code=404)
        status = current.get("status")
        if status == "analyzing":
            return ApiResponse.error("Analysis in progress", status_code=400)
        if status == "completed":
            return ApiResponse.success({
                "task_id": task_id,
                "status": "completed",
                "progress": current.get("progress", 100),
                "already_completed": True,
            })
        if status not in ("uploaded", "error"):
            return ApiResponse.error(
                f"Current status {status} does not allow starting analysis",
                status_code=400,
            )
        current.update({
            "status": "analyzing",
            "progress": 5,
            "output_dir": output_dir,
            "error": None,
        })
        persist_apk_task_locked(task_id)

    task = asyncio.create_task(run_jadx_analysis(task_id, apk_path, output_dir))
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)
    return ApiResponse.success({"task_id": task_id, "status": "analyzing"})


@router.get("/api/apk/status/{task_id}")
@handle_api_errors
async def get_apk_status(task_id: str, request: Request):
    """Get APK analysis status."""
    _require_apk_scope(request)
    task, err = get_apk_task(
        task_id, require_completed=False, owner_id=_owner_id(request)
    )
    if err:
        return err

    return ApiResponse.success({
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "filename": task.get("filename", ""),
        "error": task.get("error"),
        "partial": bool(task.get("partial", False)),
        "jadx_exit_code": task.get("jadx_exit_code"),
        "jadx_error_count": task.get("jadx_error_count"),
    })


@router.get("/api/apk/manifest/{task_id}")
@handle_api_errors
async def get_apk_manifest(task_id: str, request: Request):
    """Get parsed AndroidManifest.xml."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    raw_xml, err = _read_manifest_xml(task)
    if err:
        return ApiResponse.error(err, status_code=404)

    manifest_info = {}
    try:
        root = ET.fromstring(raw_xml)
        manifest_info["package"] = root.get("package", "")
        manifest_info["versionName"] = root.get(f"{{{ANDROID_NS}}}versionName", "")
        manifest_info["versionCode"] = root.get(f"{{{ANDROID_NS}}}versionCode", "")

        uses_sdk = root.find("uses-sdk")
        if uses_sdk is not None:
            manifest_info["minSdkVersion"] = uses_sdk.get(f"{{{ANDROID_NS}}}minSdkVersion", "")
            manifest_info["targetSdkVersion"] = uses_sdk.get(f"{{{ANDROID_NS}}}targetSdkVersion", "")

        application = root.find("application")
        if application is not None:
            for activity in application.findall("activity"):
                for intent_filter in activity.findall("intent-filter"):
                    for action in intent_filter.findall("action"):
                        if action.get(f"{{{ANDROID_NS}}}name") == "android.intent.action.MAIN":
                            manifest_info["launchActivity"] = activity.get(f"{{{ANDROID_NS}}}name", "")
                            break
    except ET.ParseError as e:
        logger.warning(f"APK manifest XML parse error for task {task_id}: {e}")

    return ApiResponse.success({"manifest": manifest_info, "raw_xml": raw_xml})


@router.get("/api/apk/permissions/{task_id}")
@handle_api_errors
async def get_apk_permissions(task_id: str, request: Request):
    """Get APK permission list."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    raw_xml, err = _read_manifest_xml(task)
    if err:
        return ApiResponse.error(err, status_code=404)

    permissions = []
    try:
        root = ET.fromstring(raw_xml)
        for perm in root.findall("uses-permission"):
            perm_name = perm.get(f"{{{ANDROID_NS}}}name", "")
            if perm_name:
                short_name = perm_name.split(".")[-1] if "." in perm_name else perm_name
                permissions.append({"name": perm_name, "short_name": short_name})
    except ET.ParseError as e:
        logger.warning(f"APK permissions XML parse error for task {task_id}: {e}")

    return ApiResponse.success({"permissions": permissions, "total": len(permissions)})


@router.get("/api/apk/source/{task_id}")
@handle_api_errors
async def get_apk_source(
    task_id: str,
    request: Request,
    path: str = "",
    view: bool = False,
):
    """Browse decompiled source tree or view file content."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    sources_dir = safe_join(task.get("output_dir", ""), "sources")
    if not os.path.exists(sources_dir):
        return ApiResponse.error("Source directory not found", status_code=404)

    if view:
        try:
            file_path = safe_join(sources_dir, path)
        except ValueError:
            return ApiResponse.error("Illegal path", status_code=400)
        if not os.path.isfile(file_path):
            return ApiResponse.error("File not found", status_code=404)
        file_size = os.path.getsize(file_path)
        if file_size > runtime.apk_max_source_file_size:
            return ApiResponse.error(f"File too large ({file_size // 1024}KB), exceeds {runtime.apk_max_source_file_size // (1024*1024)}MB limit", status_code=400)

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                content = f.read()
            return ApiResponse.success({"path": path, "content": content, "size": file_size})
        except Exception:
            logger.warning("Failed to read decompiled source file %s", file_path, exc_info=True)
            return ApiResponse.error("Failed to read file", status_code=500)
    else:
        try:
            target_dir = safe_join(sources_dir, path) if path else sources_dir
        except ValueError:
            return ApiResponse.error("Illegal path", status_code=400)
        if not os.path.isdir(target_dir):
            return ApiResponse.error("Directory not found", status_code=404)

        items = []
        try:
            for entry in sorted(os.scandir(target_dir), key=lambda e: (not e.is_dir(), e.name.lower())):
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "path": os.path.relpath(entry.path, sources_dir),
                    "size": entry.stat().st_size if entry.is_file() else 0,
                })
        except PermissionError:
            return ApiResponse.error("Permission denied", status_code=403)

        return ApiResponse.success({"items": items, "path": path, "total": len(items)})


@router.get("/api/apk/search/{task_id}")
@handle_api_errors
async def search_apk_source_files(
    task_id: str,
    request: Request,
    q: str,
    limit: int = 20,
):
    """Search decompiled source files by filename without loading the full tree in the browser."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    query = (q or "").strip().lower()
    if len(query) < 2:
        return ApiResponse.success({"items": [], "total": 0})

    limit = max(1, min(limit, 50))
    sources_dir = safe_join(task.get("output_dir", ""), "sources")
    if not os.path.exists(sources_dir):
        return ApiResponse.error("Source directory not found", status_code=404)

    matches = []
    for root, _dirnames, filenames in os.walk(sources_dir):
        for name in filenames:
            if query not in name.lower():
                continue
            path = os.path.relpath(os.path.join(root, name), sources_dir)
            matches.append({"path": path, "name": name})
            if len(matches) >= limit:
                break
        if len(matches) >= limit:
            break

    return ApiResponse.success({"items": matches, "total": len(matches), "limited": len(matches) >= limit})


@router.get("/api/apk/definition/{task_id}")
@handle_api_errors
async def find_apk_symbol_definition(
    task_id: str,
    request: Request,
    symbol: str,
    path: str = "",
    line: int = 0,
):
    """Find a best-effort Java symbol definition in decompiled APK sources."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    symbol = (symbol or "").strip()
    if not re.fullmatch(JAVA_IDENTIFIER_RE, symbol):
        return ApiResponse.error("Invalid symbol name", status_code=400)

    symbols = await asyncio.to_thread(_build_apk_symbol_index, task_id, task)
    candidates = symbols.get(symbol, [])
    if not candidates:
        return ApiResponse.error(f"Definition not found: {symbol}", status_code=404)

    best = sorted(
        candidates,
        key=lambda item: _score_apk_symbol_candidate(item, path, line),
        reverse=True,
    )[0]
    return ApiResponse.success({"definition": best, "candidates": candidates[:20]})


@router.get("/api/apk/download/{task_id}")
@handle_api_errors
async def download_apk_source(task_id: str, request: Request):
    """Download decompiled source ZIP."""
    _require_apk_scope(request)
    task, err = get_apk_task(task_id, owner_id=_owner_id(request))
    if err:
        return err

    output_dir = task.get("output_dir", "")
    if not os.path.exists(output_dir):
        return ApiResponse.error("Output directory not found", status_code=404)

    filename = normalize_apk_filename(task.get("filename", "app.apk")).replace(".apk", "_decompiled").replace(".jar", "_decompiled")
    # 压缩可能很大,放线程池避免阻塞事件循环。
    zip_path = await asyncio.to_thread(
        shutil.make_archive, safe_join(runtime.apk_upload_dir, task_id, filename), "zip", output_dir)

    def iterfile():
        # 客户端中断时 StreamingResponse 会关闭生成器（yield 处抛出
        # GeneratorExit），原实现的 os.remove 在 with 之后永远执行不到；
        # try/finally 保证临时 ZIP 在任何退出路径都被清理。
        try:
            with open(zip_path, "rb") as f:
                yield from f
        finally:
            with contextlib.suppress(Exception):
                os.remove(zip_path)

    return StreamingResponse(
        iterfile(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}.zip"'},
    )


@router.get("/api/apk/tasks")
@handle_api_errors
async def list_apk_tasks(request: Request):
    """List all APK analysis tasks."""
    _require_apk_scope(request)
    with runtime.global_state.apk_analysis_tasks_lock:
        tasks = [
            {
                "task_id": tid,
                "filename": task.get("filename", ""),
                "status": task["status"],
                "progress": task["progress"],
                "timestamp": task.get("timestamp", 0),
                "error": task.get("error"),
                "partial": bool(task.get("partial", False)),
            }
            for tid, task in runtime.global_state.apk_analysis_tasks.items()
            if task.get('owner_id') == _owner_id(request)
        ]

    tasks.sort(key=lambda t: t["timestamp"], reverse=True)
    return ApiResponse.success({"tasks": tasks, "total": len(tasks)})


@router.delete("/api/apk/task/{task_id}")
@handle_api_errors
async def delete_apk_task(task_id: str, request: Request):
    """Delete APK analysis task and its files."""
    _require_apk_scope(request)
    try:
        safe_task_id = normalize_apk_task_id(task_id)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    with runtime.global_state.apk_analysis_tasks_lock:
        task = runtime.global_state.apk_analysis_tasks.get(safe_task_id)
        if not task or task.get('owner_id') != _owner_id(request):
            return ApiResponse.error("Task not found", status_code=404)
        if task.get('status') == 'analyzing':
            return ApiResponse.error(
                'Cannot delete a task while analysis is running',
                status_code=409,
            )
        runtime.global_state.apk_analysis_tasks.pop(safe_task_id)
        if runtime.apk_task_store is not None:
            runtime.apk_task_store.delete(safe_task_id)

    task_dir = safe_join(runtime.apk_upload_dir, safe_task_id)
    await asyncio.to_thread(shutil.rmtree, task_dir, ignore_errors=True)
    with runtime.global_state.apk_upload_locks_lock:
        runtime.global_state.apk_upload_locks.pop(safe_task_id, None)

    return ApiResponse.success(message="Task deleted")


def _source_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"success": False, "error": message})


def _apk_task_for_read(task_id: str, owner_id: str) -> dict[str, Any]:
    task, err = get_apk_task(task_id, require_completed=True, owner_id=owner_id)
    if err is not None:
        raise ValueError("APK 任务不存在或未完成")
    return task


@router.get("/api/apk/source-search/{task_id}")
async def apk_source_content_search(
    task_id: str,
    request: Request,
    q: str = Query(..., min_length=1, max_length=SOURCE_SEARCH_MAX_QUERY),
    limit: int = Query(SOURCE_SEARCH_DEFAULT_LIMIT, ge=1, le=SOURCE_SEARCH_MAX_LIMIT),
    path_filter: str = Query("", max_length=256),
):
    owner_id = owner_id_from_request(request)
    _require_apk_scope(request)
    try:
        task = _apk_task_for_read(normalize_apk_task_id(task_id), owner_id)
    except ValueError as exc:
        return _source_error(str(exc), status_code=404)
    sources_dir = safe_join(str(task.get("output_dir") or ""), "sources")
    if not os.path.isdir(sources_dir):
        return _source_error("源码目录不存在", status_code=404)

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


@router.get("/api/apk/source-read/{task_id}")
async def apk_source_read(
    task_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=512),
    offset: int = Query(0, ge=0),
    limit: int = Query(400, ge=1, le=SOURCE_READ_MAX_LINES),
):
    owner_id = owner_id_from_request(request)
    _require_apk_scope(request)
    try:
        task = _apk_task_for_read(normalize_apk_task_id(task_id), owner_id)
    except ValueError as exc:
        return _source_error(str(exc), status_code=404)
    sources_dir = safe_join(str(task.get("output_dir") or ""), "sources")
    if not os.path.isdir(sources_dir):
        return _source_error("源码目录不存在", status_code=404)
    normalized = (path or "").replace("\\", "/").lstrip("/")
    if normalized.startswith("..") or "/../" in normalized or "\x00" in normalized:
        return _source_error("非法路径", status_code=422)
    target = safe_join(sources_dir, normalized)
    if not os.path.isfile(target):
        return _source_error("文件不存在", status_code=404)
    max_file = int(runtime.apk_max_source_file_size or 0)
    if max_file and os.path.getsize(target) > max_file:
        return _source_error(
                f"文件超过读取上限 {max_file // (1024 * 1024)}MB；请用搜索定位后缩小范围",
                status_code=413,
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
