from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import shlex
import time
import urllib.parse
import uuid
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse

from features.devices import ssh_connection_failed_response
from foundation.errors import handle_api_errors

from . import runtime
from .api_support import ApiResponse
from .models import SuiteApkAnalyzeRequest, SuiteDiagnosisTargetRequest
from .suite_helpers import (
    _get_available_test_suites,
    _resolve_suite_diagnosis_target,
)
from .suite_modules import search_latest_suite_modules
from .suites import get_default_suites_path, is_config_host_local


logger = logging.getLogger(__name__)
router = APIRouter()

_SUITES_CACHE: dict[str, Any] = {}
_SUITES_CACHE_TS: dict[str, float] = {}
_SUITES_CACHE_TTL_SECONDS = 300


def _get_cached_suites(base_path: str) -> dict[str, Any] | None:
    """Return cached suite discovery result if still fresh."""
    now = time.time()
    ts = _SUITES_CACHE_TS.get(base_path, 0)
    if now - ts < _SUITES_CACHE_TTL_SECONDS:
        return _SUITES_CACHE.get(base_path)
    return None


def _set_cached_suites(base_path: str, payload: dict[str, Any]) -> None:
    """Store suite discovery result with timestamp."""
    _SUITES_CACHE[base_path] = payload
    _SUITES_CACHE_TS[base_path] = time.time()


# ==================== List Suites ====================

@router.get("/api/test/suites")
@handle_api_errors
async def list_suites(base_path: str = None, force_refresh: bool = Query(False)):
    """List all available test suites."""
    config = runtime.config_manager.load_config()
    base_path = base_path or config.get("suites_path") or get_default_suites_path(config)

    cached = None if force_refresh else _get_cached_suites(base_path)
    if cached is not None:
        logger.debug("[TestSuites] Returning cached suite list for %s", base_path)
        return JSONResponse(content={**cached, "cached": True})

    try:
        suites = _get_available_test_suites(config, base_path)
    except RuntimeError as exc:
        if "SSH connection failed" in str(exc):
            logger.warning("[TestSuites] SSH unavailable while listing suites: %s", exc)
            return JSONResponse(content={
                "success": False,
                "suites": [],
                "count": 0,
                "base_path": base_path,
                "source": "ssh",
                "error": "SSH connection failed",
                "warning": "测试套件主机 SSH 连接失败，请检查主机、账号、密码或密钥配置。",
                "cached": False,
            })
        raise
    payload = {
        "success": True,
        "suites": suites,
        "count": len(suites),
        "base_path": base_path,
        "source": "local" if is_config_host_local(config) else "ssh",
    }
    _set_cached_suites(base_path, payload)
    return JSONResponse(content={**payload, "cached": False})


@router.get("/api/test/suites/modules")
@handle_api_errors
async def search_suite_modules(
    query: str = Query(..., description="模块关键词，例如 Camera"),
    suite_types: str = Query("cts,vts,gts,sts", description="逗号分隔套件类型，例如 cts,vts,gts,sts"),
    per_suite_limit: int = Query(30, ge=1, le=200),
):
    """Search latest CTS/VTS/GTS/STS testcases for modules matching a keyword."""
    config = runtime.config_manager.load_config()
    types = [item.strip() for item in suite_types.split(",") if item.strip()]
    payload = await asyncio.to_thread(
        search_latest_suite_modules,
        config,
        query,
        types,
        per_suite_limit,
    )
    return ApiResponse.success(payload)


# ==================== Diagnose Target ====================

@router.post("/api/test/suites/diagnose-target")
@handle_api_errors
async def diagnose_suite_target(req: SuiteDiagnosisTargetRequest):
    """Locate the most likely suite artifact and source path for a report failure."""
    try:
        target = await asyncio.to_thread(
            _resolve_suite_diagnosis_target,
            runtime.config_manager.load_config(),
            test_type=req.test_type, suite_version=req.suite_version,
            module=req.module, test_name=req.test_name,
            class_names=req.class_names, suite_path=req.suite_path,
        )
        return ApiResponse.success(target)
    except Exception as e:
        logger.error(f"[TestSuites] Diagnosis target failed: {e}", exc_info=True)
        return ApiResponse.error(f"Targeting failed: {e}", status_code=500)


# ==================== Suite File Browsing ====================

_SUITE_SCRIPT_PREAMBLE = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
if target != root and not target.startswith(root + os.sep):
    emit({"success": False, "error": "Illegal path"})
    sys.exit(0)
"""

SUITE_FILE_LIST_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
items = []
for name in sorted(os.listdir(target), key=lambda n: n.lower()):
    full_path = os.path.join(target, name)
    try:
        real_path = os.path.realpath(full_path)
        if real_path != root and not real_path.startswith(root + os.sep):
            continue
        st = os.stat(full_path)
        is_dir = os.path.isdir(full_path)
        rel = os.path.relpath(full_path, root)
        items.append({"name": name, "path": "" if rel == "." else rel, "type": "directory" if is_dir else "file", "size": 0 if is_dir else st.st_size, "modified": int(st.st_mtime), "is_apk": (not is_dir) and name.lower().endswith(".apk"), "is_jar": (not is_dir) and name.lower().endswith(".jar")})
    except OSError:
        continue
items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
emit({"success": True, "path": "" if target == root else os.path.relpath(target, root), "root": root, "items": items})
"""

SUITE_FILE_INFO_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isfile(target):
    emit({"success": False, "error": "File not found"})
    sys.exit(0)
st = os.stat(target)
name_lower = target.lower()
emit({"success": True, "real_path": target, "name": os.path.basename(target), "size": st.st_size, "modified": int(st.st_mtime), "is_apk": name_lower.endswith(".apk"), "is_jar": name_lower.endswith(".jar")})
"""

# 把一个目录打包成远程临时 zip，返回 zip 路径与文件夹名。供「下载文件夹」用：
# 浏览器无法在一次响应里下载保持目录结构的多个文件，统一打包成 zip 流式回传，
# 解压后顶层即为被下载的文件夹名。
SUITE_DIR_ZIP_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
import tempfile, zipfile
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="suite_dl_")
os.close(fd)
try:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for current, dirs, files in os.walk(target):
            for name in files:
                full_path = os.path.join(current, name)
                real_full = os.path.realpath(full_path)
                if real_full != root and not real_full.startswith(root + os.sep):
                    continue
                arc = os.path.relpath(full_path, target)
                zipf.write(full_path, arc)
    st = os.stat(zip_path)
    emit({"success": True, "zip_path": zip_path, "name": os.path.basename(target), "size": st.st_size})
except Exception as e:
    try:
        os.remove(zip_path)
    except OSError:
        pass
    emit({"success": False, "error": str(e)})
"""

SUITE_FILE_SEARCH_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
query = sys.argv[3].lower()
limit = int(sys.argv[4])
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
items = []
for current, dirs, files in os.walk(target):
    dirs[:] = [d for d in dirs if not d.startswith('.')]
    for name in sorted(dirs, key=str.lower):
        if query and query not in name.lower():
            continue
        full_path = os.path.join(current, name)
        rel = os.path.relpath(full_path, root)
        items.append({"name": name, "path": "" if rel == "." else rel, "type": "directory", "size": 0, "modified": int(os.path.getmtime(full_path))})
        if len(items) >= limit:
            emit({"success": True, "items": items})
            sys.exit(0)
    for name in sorted(files, key=str.lower):
        if query and query not in name.lower():
            continue
        full_path = os.path.join(current, name)
        try:
            st = os.stat(full_path)
        except OSError:
            continue
        rel = os.path.relpath(full_path, root)
        lower = name.lower()
        items.append({"name": name, "path": rel, "type": "file", "size": st.st_size, "modified": int(st.st_mtime), "is_apk": lower.endswith(".apk"), "is_jar": lower.endswith(".jar")})
        if len(items) >= limit:
            emit({"success": True, "items": items})
            sys.exit(0)
emit({"success": True, "items": items})
"""


def _normalize_suite_relative_path(path: str | None) -> str:
    rel_path = (path or "").replace("\\", "/").strip().strip("/")
    if not rel_path:
        return ""
    parts = [part for part in rel_path.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("Illegal path")
    return "/".join(parts)


def _get_suite_root_from_path(suite_path: str, config: dict[str, Any]) -> str:
    raw_path = (suite_path or "").replace("\\", "/").strip().rstrip("/")
    if not raw_path or not raw_path.startswith("/"):
        raise ValueError("Invalid test suite path")
    suite_root = raw_path[:-len("/tools")] if raw_path.endswith("/tools") else raw_path
    suite_root = suite_root.rstrip("/")
    if not suite_root:
        raise ValueError("Invalid test suite path")
    base_path = (config.get("suites_path") or "").replace("\\", "/").strip().rstrip("/")
    if base_path.startswith("/") and not (suite_root == base_path or suite_root.startswith(base_path + "/")):
        raise ValueError("Test suite not in configured suites directory")
    return suite_root


def _build_suite_remote_path(suite_path: str, path: str | None, config: dict[str, Any]) -> tuple:
    suite_root = _get_suite_root_from_path(suite_path, config)
    rel_path = _normalize_suite_relative_path(path)
    remote_path = suite_root if not rel_path else f"{suite_root}/{rel_path}"
    return suite_root, rel_path, remote_path


def _run_suite_file_script(ssh, script: str, suite_root: str, remote_path: str, timeout: int = 20) -> dict[str, Any]:
    cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(suite_root)} {shlex.quote(remote_path)}"
    output, error, code = runtime.ssh_manager.execute_command(ssh, cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(error.strip() or output.strip() or "Remote file operation failed")
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Remote file response parse failed: {e}"
        ) from e


def _search_suite_files_local(suite_root: str, query: str, limit: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    query_lower = query.lower()
    if not os.path.isdir(suite_root):
        return matches
    for current, dirs, files in os.walk(suite_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(dirs, key=str.lower):
            if query_lower and query_lower not in name.lower():
                continue
            full_path = os.path.join(current, name)
            rel = os.path.relpath(full_path, suite_root)
            matches.append({
                "name": name,
                "path": "" if rel == "." else rel,
                "type": "directory",
                "size": 0,
                "modified": int(os.path.getmtime(full_path)),
            })
            if len(matches) >= limit:
                return matches
        for name in sorted(files, key=str.lower):
            if query_lower and query_lower not in name.lower():
                continue
            full_path = os.path.join(current, name)
            try:
                st = os.stat(full_path)
            except OSError:
                continue
            rel = os.path.relpath(full_path, suite_root)
            lower = name.lower()
            matches.append({
                "name": name,
                "path": rel,
                "type": "file",
                "size": st.st_size,
                "modified": int(st.st_mtime),
                "is_apk": lower.endswith(".apk"),
                "is_jar": lower.endswith(".jar"),
            })
            if len(matches) >= limit:
                return matches
    return matches


@router.get("/api/test/suites/files")
@handle_api_errors
async def list_suite_files(suite_path: str = Query(...), path: str = Query("")):
    """Browse test suite directory files."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()

        payload = _run_suite_file_script(ssh, SUITE_FILE_LIST_SCRIPT, suite_root, remote_path)
        if not payload.get("success"):
            return ApiResponse.error(payload.get("error", "Directory read failed"), status_code=400)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "path": payload.get("path", rel_path), "items": payload.get("items", [])})


@router.get("/api/test/suites/search")
@handle_api_errors
async def search_suite_files(
    suite_path: str = Query(...),
    query: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=200),
):
    """Search files/directories by name inside a test suite."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, _, _ = _build_suite_remote_path(suite_path, "", config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    if os.path.isdir(suite_root):
        items = await asyncio.to_thread(_search_suite_files_local, suite_root, query.strip(), limit)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "query": query, "items": items, "count": len(items)})

    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()

        script = f"{SUITE_FILE_SEARCH_SCRIPT}"
        cmd = (
            f"python3 -c {shlex.quote(script)} {shlex.quote(suite_root)} {shlex.quote(suite_root)} "
            f"{shlex.quote(query.strip().lower())} {shlex.quote(str(limit))}"
        )
        output, error, code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, cmd, timeout=60
        )
        if code != 0:
            raise RuntimeError(error.strip() or output.strip() or "Remote search failed")
        payload = json.loads(output.strip() or "{}")
        if not payload.get("success"):
            return ApiResponse.error(payload.get("error", "Search failed"), status_code=400)
        items = payload.get("items") or []
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "query": query, "items": items, "count": len(items)})


@router.get("/api/test/suites/download")
@handle_api_errors
async def download_suite_file(suite_path: str = Query(...), path: str = Query(...), inline: bool = Query(False)):
    """Download a specified file from test suite directory.

    inline=True 时返回 Content-Disposition: inline，让浏览器内联显示（用于双击
    预览 HTML 报告等），而非强制下载。
    """
    config = runtime.config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get("success"):
            runtime.ssh_manager.return_connection(ssh)
            return ApiResponse.error(info.get("error", "File not found"), status_code=404)

        sftp = ssh.open_sftp()
        remote_file = sftp.open(info["real_path"], "rb")
    except Exception:
        runtime.ssh_manager.return_connection(ssh)
        raise

    filename = info.get("name") or os.path.basename(remote_path) or "download"
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download"
    quoted_filename = urllib.parse.quote(filename)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def iter_remote_file():
        try:
            while True:
                chunk = remote_file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            finally:
                try:
                    sftp.close()
                finally:
                    runtime.ssh_manager.return_connection(ssh)

    if inline:
        disposition = "inline"
    else:
        disposition = f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}'

    return StreamingResponse(
        iter_remote_file(),
        media_type=media_type,
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(info.get("size", 0)),
        },
    )


@router.get("/api/test/suites/download-dir")
@handle_api_errors
async def download_suite_directory(suite_path: str = Query(...), path: str = Query(...)):
    """Download a directory from the test suite as a zip archive (folder tree preserved)."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        info = _run_suite_file_script(ssh, SUITE_DIR_ZIP_SCRIPT, suite_root, remote_path)
        if not info.get("success"):
            runtime.ssh_manager.return_connection(ssh)
            return ApiResponse.error(info.get("error", "Directory not found"), status_code=404)

        sftp = ssh.open_sftp()
        remote_file = sftp.open(info["zip_path"], "rb")
    except Exception:
        runtime.ssh_manager.return_connection(ssh)
        raise

    folder_name = info.get("name") or os.path.basename(remote_path) or "download"
    filename = f"{folder_name}.zip"
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download.zip"
    quoted_filename = urllib.parse.quote(filename)
    zip_path = info["zip_path"]

    def iter_remote_dir():
        try:
            while True:
                chunk = remote_file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            finally:
                # 清理远程临时 zip，再归还连接。
                with contextlib.suppress(Exception):
                    sftp.remove(zip_path)
                try:
                    sftp.close()
                finally:
                    runtime.ssh_manager.return_connection(ssh)

    return StreamingResponse(
        iter_remote_dir(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}',
            "Content-Length": str(info.get("size", 0)),
        },
    )


@router.post("/api/test/suites/apk/analyze")
@handle_api_errors
async def create_suite_apk_analysis_task(req: SuiteApkAnalyzeRequest):
    """Copy an APK from test suite for APK analysis."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(req.suite_path, req.path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    task_id = str(uuid.uuid4())
    sftp = None
    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get("success"):
            return ApiResponse.error(info.get("error", "File not found"), status_code=404)
        if not (info.get("is_apk") or info.get("is_jar")):
            return ApiResponse.error("Only APK/JAR files supported for decompilation", status_code=400)
        if int(info.get("size", 0)) > runtime.apk_max_file_size:
            return ApiResponse.error(f"File too large, max {runtime.apk_max_file_size // (1024*1024)}MB", status_code=400)

        filename = runtime.normalize_apk_filename(info.get("name") or os.path.basename(remote_path))
        task_dir = runtime.safe_join(runtime.apk_upload_dir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        apk_path = runtime.safe_join(task_dir, filename)

        sftp = ssh.open_sftp()
        await asyncio.to_thread(sftp.get, info["real_path"], apk_path)

        if os.path.getsize(apk_path) > runtime.apk_max_file_size:
            runtime.cleanup_files([apk_path])
            return ApiResponse.error(f"File too large, max {runtime.apk_max_file_size // (1024*1024)}MB", status_code=400)

        runtime.create_apk_task(task_id, apk_path, filename)
        return ApiResponse.success({"task_id": task_id, "filename": filename, "size": os.path.getsize(apk_path), "source_path": req.path})
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)
    finally:
        if sftp:
            with contextlib.suppress(Exception):
                sftp.close()
        runtime.ssh_manager.return_connection(ssh)
