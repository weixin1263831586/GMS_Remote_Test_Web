"""APK router - APK upload, analysis, decompilation, and source browsing APIs."""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import StreamingResponse

from features.firmware.apk import (
    ANDROID_NS,
    JAVA_IDENTIFIER_RE,
    _build_apk_symbol_index,
    _cleanup_files,
    _create_apk_task,
    _get_apk_task,
    _get_apk_upload_lock,
    _normalize_apk_filename,
    _normalize_apk_task_id,
    _read_manifest_xml,
    _run_jadx_analysis,
    _safe_join,
    _score_apk_symbol_candidate,
)
from foundation.errors import handle_api_errors
from foundation.uploads import merge_files_to_path, save_upload_to_path

from . import runtime
from .responses import ApiResponse


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/apk/upload")
@handle_api_errors
async def upload_apk(
    file: UploadFile | None = File(None),
    chunk_index: int | None = Form(None),
    total_chunks: int | None = Form(None),
    upload_id: str | None = Form(None),
    file_name: str | None = Form(None),
):
    """Upload APK file for analysis."""
    if not file:
        return ApiResponse.error("No file provided", status_code=400)

    try:
        filename = _normalize_apk_filename(file_name or file.filename)
        task_id = _normalize_apk_task_id(upload_id)
        task_dir = _safe_join(runtime.apk_upload_dir, task_id)
        apk_path = _safe_join(task_dir, filename)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    os.makedirs(task_dir, exist_ok=True)

    if (chunk_index is None) != (total_chunks is None):
        return ApiResponse.error("Incomplete chunk parameters", status_code=400)

    if chunk_index is not None and total_chunks is not None:
        if total_chunks <= 0 or chunk_index < 0 or chunk_index >= total_chunks:
            return ApiResponse.error("Invalid chunk parameters", status_code=400)

        chunk_path = _safe_join(task_dir, f"{filename}.part{chunk_index}")
        try:
            await save_upload_to_path(file, chunk_path, runtime.apk_max_file_size)
        except ValueError as e:
            _cleanup_files([chunk_path])
            return ApiResponse.error(str(e), status_code=400)

        chunk_paths = [_safe_join(task_dir, f"{filename}.part{i}") for i in range(total_chunks)]
        upload_lock = _get_apk_upload_lock(task_id)
        async with upload_lock:
            if os.path.exists(apk_path):
                file_size = os.path.getsize(apk_path)
                return ApiResponse.success({"task_id": task_id, "filename": filename, "size": file_size, "uploaded": True})

            all_chunks_ready = all(os.path.exists(path) for path in chunk_paths)
            if not all_chunks_ready:
                return ApiResponse.success({
                    "task_id": task_id, "filename": filename,
                    "chunk_received": chunk_index + 1, "total_chunks": total_chunks, "uploaded": False,
                })

            total_size = sum(os.path.getsize(path) for path in chunk_paths)
            if total_size > runtime.apk_max_file_size:
                _cleanup_files([*chunk_paths, apk_path])
                return ApiResponse.error(f"File too large, max {runtime.apk_max_file_size // (1024*1024)}MB", status_code=400)

            await asyncio.to_thread(merge_files_to_path, chunk_paths, apk_path)
            _cleanup_files(chunk_paths)

            file_size = os.path.getsize(apk_path)
            if file_size > runtime.apk_max_file_size:
                _cleanup_files([apk_path])
                return ApiResponse.error(f"File too large, max {runtime.apk_max_file_size // (1024*1024)}MB", status_code=400)

            _create_apk_task(task_id, apk_path, filename)
            return ApiResponse.success({"task_id": task_id, "filename": filename, "size": file_size, "uploaded": True})
    else:
        try:
            file_size = await save_upload_to_path(file, apk_path, runtime.apk_max_file_size)
        except ValueError as e:
            _cleanup_files([apk_path])
            return ApiResponse.error(str(e), status_code=400)

        _create_apk_task(task_id, apk_path, filename)
        return ApiResponse.success({"task_id": task_id, "filename": filename, "size": file_size})


@router.post("/api/apk/analyze/{task_id}")
@handle_api_errors
async def analyze_apk(task_id: str):
    """Start jadx decompilation analysis."""
    task, err = _get_apk_task(task_id, require_completed=False)
    if err:
        return err

    if task["status"] == "analyzing":
        return ApiResponse.error("Analysis in progress", status_code=400)

    if task["status"] == "completed":
        return ApiResponse.success({"task_id": task_id, "status": "completed", "progress": task.get("progress", 100), "already_completed": True})

    if task["status"] not in ("uploaded", "error"):
        return ApiResponse.error(f"Current status {task['status']} does not allow starting analysis", status_code=400)

    apk_path = task["apk_path"]
    if not os.path.exists(apk_path):
        return ApiResponse.error("APK file not found, please re-upload", status_code=404)

    output_dir = os.path.join(runtime.apk_upload_dir, task_id, "jadx_output")

    with runtime.global_state.apk_analysis_tasks_lock:
        t = runtime.global_state.apk_analysis_tasks[task_id]
        t.update({"status": "analyzing", "progress": 5, "output_dir": output_dir, "error": None})

    task = asyncio.create_task(_run_jadx_analysis(task_id, apk_path, output_dir))
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)
    return ApiResponse.success({"task_id": task_id, "status": "analyzing"})


@router.get("/api/apk/status/{task_id}")
@handle_api_errors
async def get_apk_status(task_id: str):
    """Get APK analysis status."""
    task, err = _get_apk_task(task_id, require_completed=False)
    if err:
        return err

    return ApiResponse.success({
        "task_id": task_id,
        "status": task["status"],
        "progress": task["progress"],
        "filename": task.get("filename", ""),
        "error": task.get("error"),
    })


@router.get("/api/apk/manifest/{task_id}")
@handle_api_errors
async def get_apk_manifest(task_id: str):
    """Get parsed AndroidManifest.xml."""
    task, err = _get_apk_task(task_id)
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
async def get_apk_permissions(task_id: str):
    """Get APK permission list."""
    task, err = _get_apk_task(task_id)
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
async def get_apk_source(task_id: str, path: str = "", view: bool = False):
    """Browse decompiled source tree or view file content."""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    sources_dir = _safe_join(task.get("output_dir", ""), "sources")
    if not os.path.exists(sources_dir):
        return ApiResponse.error("Source directory not found", status_code=404)

    if view:
        try:
            file_path = _safe_join(sources_dir, path)
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
        except Exception as e:
            return ApiResponse.error(f"Failed to read file: {e}", status_code=500)
    else:
        try:
            target_dir = _safe_join(sources_dir, path) if path else sources_dir
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
async def search_apk_source_files(task_id: str, q: str, limit: int = 20):
    """Search decompiled source files by filename without loading the full tree in the browser."""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    query = (q or "").strip().lower()
    if len(query) < 2:
        return ApiResponse.success({"items": [], "total": 0})

    limit = max(1, min(limit, 50))
    sources_dir = _safe_join(task.get("output_dir", ""), "sources")
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
async def find_apk_symbol_definition(task_id: str, symbol: str, path: str = "", line: int = 0):
    """Find a best-effort Java symbol definition in decompiled APK sources."""
    task, err = _get_apk_task(task_id)
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
async def download_apk_source(task_id: str):
    """Download decompiled source ZIP."""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    output_dir = task.get("output_dir", "")
    if not os.path.exists(output_dir):
        return ApiResponse.error("Output directory not found", status_code=404)

    filename = task.get("filename", "app.apk").replace(".apk", "_decompiled")
    zip_path = shutil.make_archive(
        os.path.join(runtime.apk_upload_dir, task_id, filename),
        "zip",
        output_dir,
    )

    def iterfile():
        with open(zip_path, "rb") as f:
            yield from f
        with contextlib.suppress(Exception):
            os.remove(zip_path)

    return StreamingResponse(
        iterfile(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}.zip"'},
    )


@router.get("/api/apk/tasks")
@handle_api_errors
async def list_apk_tasks():
    """List all APK analysis tasks."""
    with runtime.global_state.apk_analysis_tasks_lock:
        tasks = [
            {
                "task_id": tid,
                "filename": task.get("filename", ""),
                "status": task["status"],
                "progress": task["progress"],
                "timestamp": task.get("timestamp", 0),
                "error": task.get("error"),
            }
            for tid, task in runtime.global_state.apk_analysis_tasks.items()
        ]

    tasks.sort(key=lambda t: t["timestamp"], reverse=True)
    return ApiResponse.success({"tasks": tasks, "total": len(tasks)})


@router.delete("/api/apk/task/{task_id}")
@handle_api_errors
async def delete_apk_task(task_id: str):
    """Delete APK analysis task and its files."""
    with runtime.global_state.apk_analysis_tasks_lock:
        if task_id not in runtime.global_state.apk_analysis_tasks:
            return ApiResponse.error("Task not found", status_code=404)
        runtime.global_state.apk_analysis_tasks.pop(task_id)

    task_dir = os.path.join(runtime.apk_upload_dir, task_id)
    await asyncio.to_thread(shutil.rmtree, task_dir, ignore_errors=True)
    with runtime.global_state.apk_upload_locks_lock:
        runtime.global_state.apk_upload_locks.pop(task_id, None)

    return ApiResponse.success(message="Task deleted")
