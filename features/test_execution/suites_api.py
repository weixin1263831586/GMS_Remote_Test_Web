from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import shlex
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
from .suites import get_default_suites_path, is_config_host_local


logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== List Suites ====================

@router.get("/api/test/suites")
@handle_api_errors
async def list_suites(base_path: str = None):
    """List all available test suites."""
    config = runtime.config_manager.load_config()
    base_path = base_path or config.get("suites_path") or get_default_suites_path(config)
    suites = _get_available_test_suites(config, base_path)
    return JSONResponse(content={
        "success": True, "suites": suites, "count": len(suites),
        "base_path": base_path, "source": "local" if is_config_host_local(config) else "ssh",
    })


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
    print(json.dumps(payload, ensure_ascii=False))
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


@router.get("/api/test/suites/files")
@handle_api_errors
async def list_suite_files(suite_path: str = Query(...), path: str = Query("")):
    """Browse test suite directory files."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    with runtime.ssh_manager.optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()

        payload = _run_suite_file_script(ssh, SUITE_FILE_LIST_SCRIPT, suite_root, remote_path)
        if not payload.get("success"):
            return ApiResponse.error(payload.get("error", "Directory read failed"), status_code=400)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "path": payload.get("path", rel_path), "items": payload.get("items", [])})


@router.get("/api/test/suites/download")
@handle_api_errors
async def download_suite_file(suite_path: str = Query(...), path: str = Query(...)):
    """Download a specified file from test suite directory."""
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

    return StreamingResponse(
        iter_remote_file(),
        media_type=media_type,
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
