from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
import uuid

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from features.auth import (
    require_authenticated_user_when_auth_required,
    require_elevated_admin_when_auth_required,
)
from features.devices import ssh_connection_failed_response
from foundation.archives import (
    derive_suite_dir_name_from_archive,
    is_complete_archive_file,
    sanitize_suite_dir_name,
    sanitize_suite_filename_from_url,
)
from foundation.errors import handle_api_errors
from foundation.responses import error_response

from . import runtime
from .models import (
    TestSuiteAddLocalRequest,
    TestSuiteDownloadRequest,
    TestSuiteExtractRequest,
    TradefedListResultsRequest,
)
from .suite_archives import (
    extract_archive_local_with_progress as _extract_archive_local_with_progress,
)
from .suite_download_security import (
    curl_resolve_arguments as _curl_resolve_arguments,
)
from .suite_download_security import (
    resolve_suite_download_target as _resolve_suite_download_target,
)
from .suite_download_security import (
    validate_suite_download_url as _validate_suite_download_url,
)
from .suite_helpers import resolve_suite_reference
from .suite_transfer_tasks import (
    MAX_SUITE_ARCHIVE_BYTES,
)
from .suite_transfer_tasks import (
    cleanup_old_suite_tasks as _cleanup_old_suite_tasks,
)
from .suite_transfer_tasks import (
    run_suite_download_task as _run_suite_download_task,
)
from .suite_transfer_tasks import (
    run_suite_extract_task as _run_suite_extract_task,
)
from .suite_transfer_tasks import (
    schedule_suite_task as _schedule_suite_task,
)
from .suites import (
    get_default_suites_path,
    is_config_host_local,
)
from .tradefed_results import collect_tradefed_results


logger = logging.getLogger(__name__)
router = APIRouter()

# 下载/解压是日常操作，只需登录；添加本地路径修改主机配置，需要管理员提权。
_WRITE_AUTH = [Depends(require_authenticated_user_when_auth_required)]
_ADD_LOCAL_ELEVATION = [Depends(require_elevated_admin_when_auth_required)]


def _path_within_suite_root(path: str, suite_root: str, label: str) -> str:
    root = os.path.realpath(os.path.expanduser(str(suite_root or '')))
    target = os.path.realpath(os.path.expanduser(str(path or '')))
    if not root or not target or os.path.commonpath([root, target]) != root:
        raise ValueError(f'{label} must stay inside suites_path')
    return target

@router.post("/api/test/suites/add-local", dependencies=_ADD_LOCAL_ELEVATION)
@handle_api_errors
async def add_local_test_suite(req: TestSuiteAddLocalRequest):
    """Add a local test suite path to config."""
    config = runtime.config_manager.load_config()
    if not req.path:
        return error_response("Path cannot be empty", 400)

    if is_config_host_local(config):
        if not os.path.exists(req.path):
            return error_response(f"Path not found: {req.path}", 404)
        if not os.path.isdir(req.path):
            return error_response(f"Not a directory: {req.path}", 400)
    else:
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            check_cmd = (
                f"[ -d {shlex.quote(req.path)} ] "
                "&& echo exists || echo not_exists"
            )
            check_result = await asyncio.to_thread(
                runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=10,
            )
            if check_result.stdout.strip() != "exists":
                return error_response(f"Path not found: {req.path}", 404)

    return JSONResponse(content={"success": True, "message": f"Added local path: {os.path.basename(req.path.rstrip('/'))}", "path": req.path})


@router.post(
    "/api/test/suites/result",
    dependencies=_ADD_LOCAL_ELEVATION,
)
async def list_tradefed_results(
    h: str | None = Query(None),
    help: bool = Query(False),
    req: TradefedListResultsRequest = Body(None),
    force_refresh: bool = Query(False),
):
    """Execute tradefed list results and return test results."""
    resp = runtime.generate_help_or_continue(help, "POST", "/api/test/suites/result")
    if resp:
        return resp

    if req is None:
        return error_response("Missing request body", 400)

    try:
        config = runtime.config_manager.load_config()
        suite_path = req.suite_path
        # Accept short suite names (e.g. android-cts-17_r1) by resolving them
        # against the suite inventory before path validation runs.
        try:
            suite, message = resolve_suite_reference(config, suite_path)
        except RuntimeError:
            suite, message = None, ""
        if suite:
            suite_path = str(suite.get("tools_path") or suite_path)
        elif message:
            return error_response(message, 404)
        tradefed_bin = req.tradefed_bin
        logger.info(f"Querying test suite results for {suite_path}")
        result = await collect_tradefed_results(
            config, suite_path, tradefed_bin, force_refresh=force_refresh,
        )
        if not result.get("success"):
            return error_response(
                result.get("error", "Failed to list Tradefed results"),
                status_code=int(result.get("status_code") or 500),
                raw_output=result.get("raw_output", ""),
            )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error listing tradefed results: {e}")
        return error_response(str(e), 500)


@router.post("/api/test/suites/download-url", dependencies=_WRITE_AUTH)
@handle_api_errors
async def download_test_suite_from_url(
    request: Request,
    req: TestSuiteDownloadRequest,
):
    """Download test suite from URL."""
    config = runtime.config_manager.load_config()
    if not req.url:
        return error_response("Download URL cannot be empty", 400)

    try:
        download_url = _validate_suite_download_url(req.url)
        resolved_target = await asyncio.to_thread(
            _resolve_suite_download_target,
            download_url,
        )
        download_url = resolved_target.url
    except ValueError as exc:
        return error_response(str(exc), 400)

    suites_root = get_default_suites_path(config)
    try:
        save_dir = _path_within_suite_root(
            req.save_dir or suites_root,
            suites_root,
            'save_dir',
        )
    except ValueError as exc:
        return error_response(str(exc), 400)
    os.makedirs(save_dir, exist_ok=True)

    filename = sanitize_suite_filename_from_url(download_url)
    archive_path = os.path.join(save_dir, filename)
    owner_id = runtime.get_client_id_from_request(request)

    if is_config_host_local(config):
        existing_task = runtime.suite_task_store.find_active_download(archive_path)
        if existing_task:
            if existing_task.get("owner_id") != owner_id:
                return error_response(
                    "Another administrator is already downloading this archive",
                    409,
                )
            return JSONResponse(content={"success": True, "message": f"Download task exists: {filename}", "task_id": existing_task.get("task_id"), "archive_path": archive_path, "file_size": 0, "download_method": "local_async_existing"})

        if is_complete_archive_file(archive_path):
            file_size = os.path.getsize(archive_path)
            return JSONResponse(content={"success": True, "message": f"File already exists: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "local_existing"})
        if os.path.exists(archive_path):
            return error_response(f"Incomplete or corrupt archive exists: {archive_path}", 409)

        task_id = str(uuid.uuid4())
        runtime.suite_task_store.create({
            "task_id": task_id, "owner_id": owner_id, "kind": "download",
            "status": "queued", "progress": 0,
            "url": download_url, "filename": filename, "archive_path": archive_path,
            "downloaded_size": 0, "total_size": 0, "message": f"Preparing download: {filename}",
            "created_at": time.time(), "updated_at": time.time(),
        })
        _schedule_suite_task(
            _run_suite_download_task(task_id, download_url, archive_path)
        )
        return JSONResponse(content={"success": True, "message": f"Download started: {filename}", "task_id": task_id, "archive_path": archive_path, "file_size": 0, "download_method": "local_async"})
    else:
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            command_parts = [
                "curl",
                "--proto",
                "=http,https",
                "--max-redirs",
                "0",
                *_curl_resolve_arguments(resolved_target),
                "--max-filesize",
                str(MAX_SUITE_ARCHIVE_BYTES),
                "-o",
                archive_path,
                download_url,
            ]
            cmd = f"{shlex.join(command_parts)} 2>&1"
            # 统一 CommandResult 后属性访问消除位置错用——
            # 历史解包成 (output, exit_code, _) 曾把 stderr 当退出码，
            # curl 成功也被判 Download failed。
            # 同步 execute_command 最长阻塞 600s，放入线程池，
            # 否则整个 FastAPI 事件循环被冻结（loop_watchdog 只能检测、
            # 不能消除卡顿）。
            download_result = await asyncio.to_thread(
                runtime.ssh_manager.execute_command, ssh, cmd, timeout=600,
            )
            if not download_result.ok:
                detail = (
                    download_result.stdout or download_result.stderr or ""
                ).strip()
                return error_response(f"Download failed: {detail}", 500)
            size_cmd = f"stat -c%s '{archive_path}' 2>/dev/null || stat -f%z '{archive_path}' 2>/dev/null || echo 0"
            size_result = await asyncio.to_thread(
                runtime.ssh_manager.execute_command, ssh, size_cmd, timeout=10,
            )
            file_size = int(size_result.stdout.strip())
            return JSONResponse(content={"success": True, "message": f"Download complete: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "ssh"})


@router.get("/api/test/suites/download-status/{task_id}")
async def get_test_suite_download_status(request: Request, task_id: str):
    _cleanup_old_suite_tasks()
    task = runtime.suite_task_store.get(
        task_id,
        runtime.get_client_id_from_request(request),
    )
    if not task:
        return error_response("Download task not found", 404)
    return JSONResponse(content={"success": True, "task": task})


@router.get("/api/test/suites/archives")
async def list_test_suite_archives():
    config = runtime.config_manager.load_config()
    base_path = get_default_suites_path(config)
    archive_exts = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar")

    if is_config_host_local(config):
        archives = []
        if os.path.isdir(base_path):
            for name in sorted(os.listdir(base_path), reverse=True):
                path = os.path.join(base_path, name)
                if os.path.isfile(path) and name.endswith(archive_exts):
                    stat = os.stat(path)
                    archives.append({"name": name, "path": path, "size": stat.st_size, "mtime": stat.st_mtime, "default_dir_name": derive_suite_dir_name_from_archive(path)})
        return JSONResponse(content={"success": True, "archives": archives, "base_path": base_path})

    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()
        find_cmd = f"find {shlex.quote(base_path)} -maxdepth 1 -type f \\( -name '*.zip' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.tar.bz2' -o -name '*.tar' \\) -printf '%T@\\t%s\\t%f\\t%p\\n' 2>/dev/null | sort -nr"
        find_result = await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, find_cmd, timeout=20)
        archives = []
        for line in find_result.stdout.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                mtime, size, name, path = parts
                archives.append({"name": name, "path": path, "size": int(float(size)) if size else 0, "mtime": float(mtime) if mtime else 0, "default_dir_name": derive_suite_dir_name_from_archive(path)})
        return JSONResponse(content={"success": True, "archives": archives, "base_path": base_path})


@router.post("/api/test/suites/extract-start", dependencies=_WRITE_AUTH)
@handle_api_errors
async def start_test_suite_extract(
    request: Request,
    req: TestSuiteExtractRequest,
):
    config = runtime.config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path))

    if not is_config_host_local(config):
        return error_response("Background extraction only supports local host mode", 400)

    suites_root = get_default_suites_path(config)
    try:
        archive_path = _path_within_suite_root(
            req.archive_path, suites_root, 'archive_path'
        )
        extract_dir = _path_within_suite_root(
            extract_dir, suites_root, 'extract_dir'
        )
    except ValueError as exc:
        return error_response(str(exc), 400)

    if not os.path.exists(archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)
    if not is_complete_archive_file(archive_path):
        return error_response(f"Incomplete or unsupported archive: {req.archive_path}", 400)

    os.makedirs(extract_dir, exist_ok=True)
    task_id = str(uuid.uuid4())
    runtime.suite_task_store.create({
        "task_id": task_id,
        "owner_id": runtime.get_client_id_from_request(request),
        "kind": "extract", "status": "queued", "progress": 0,
        "archive_path": archive_path, "extract_dir": extract_dir,
        "target_dir_name": target_dir_name, "extracted_count": 0, "total_count": 0,
        "message": f"Preparing extraction: {os.path.basename(req.archive_path)}",
        "created_at": time.time(), "updated_at": time.time(),
    })

    _schedule_suite_task(
        _run_suite_extract_task(
            task_id,
            archive_path,
            extract_dir,
            target_dir_name,
        )
    )
    return JSONResponse(content={"success": True, "task_id": task_id, "message": "Extraction started", "archive_path": req.archive_path, "target_dir_name": target_dir_name})


@router.get("/api/test/suites/extract-status/{task_id}")
async def get_test_suite_extract_status(request: Request, task_id: str):
    _cleanup_old_suite_tasks()
    task = runtime.suite_task_store.get(
        task_id,
        runtime.get_client_id_from_request(request),
    )
    if not task:
        return error_response("Extract task not found", 404)
    return JSONResponse(content={"success": True, "task": task})


def recover_suite_tasks() -> list[asyncio.Task]:
    """Resume safe downloads and mark interrupted extraction for explicit retry."""
    recovered: list[asyncio.Task] = []
    if runtime.suite_task_store is None:
        return recovered
    config = runtime.config_manager.load_config()
    suites_root = get_default_suites_path(config)
    for task in runtime.suite_task_store.list_active():
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        if task.get("kind") == "extract":
            runtime.suite_task_store.update(
                task_id,
                status="interrupted",
                error=(
                    "Controller restarted during extraction. The partial target "
                    "was preserved; inspect it and start extraction again."
                ),
                retryable=True,
            )
            continue
        try:
            download_url = _validate_suite_download_url(str(task.get("url") or ""))
            archive_path = _path_within_suite_root(
                str(task.get("archive_path") or ""),
                suites_root,
                "archive_path",
            )
        except ValueError as exc:
            runtime.suite_task_store.update(
                task_id,
                status="error",
                error=f"Download recovery rejected: {exc}",
                retryable=False,
            )
            continue
        if is_complete_archive_file(archive_path):
            runtime.suite_task_store.update(
                task_id,
                status="completed",
                progress=100,
                downloaded_size=os.path.getsize(archive_path),
                message=f"Download complete: {os.path.basename(archive_path)}",
            )
            continue
        runtime.suite_task_store.update(
            task_id,
            status="recovering",
            message=f"Resuming download: {os.path.basename(archive_path)}",
        )
        recovered.append(
            _schedule_suite_task(
                _run_suite_download_task(task_id, download_url, archive_path)
            )
        )
    return recovered


@router.post("/api/test/suites/extract", dependencies=_WRITE_AUTH)
@handle_api_errors
async def extract_test_suite_archive(req: TestSuiteExtractRequest):
    """Extract test suite archive."""
    config = runtime.config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path)) if req.target_dir_name else ""

    suites_root = get_default_suites_path(config)
    try:
        archive_path = _path_within_suite_root(
            req.archive_path, suites_root, 'archive_path'
        )
        extract_dir = _path_within_suite_root(
            extract_dir, suites_root, 'extract_dir'
        )
    except ValueError as exc:
        return error_response(str(exc), 400)

    if is_config_host_local(config) and not os.path.exists(archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)

    if is_config_host_local(config):
        try:
            os.makedirs(extract_dir, exist_ok=True)
            result = await asyncio.to_thread(_extract_archive_local_with_progress, archive_path, extract_dir, target_dir_name)
            return JSONResponse(content={"success": True, **result})
        except Exception as e:
            return error_response(f"Extraction failed: {e!s}", 500)
    else:
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            remote_extract_dir = os.path.join(extract_dir, target_dir_name) if target_dir_name else extract_dir
            mkdir_cmd = f"mkdir -p {shlex.quote(remote_extract_dir)}"
            await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, mkdir_cmd, timeout=20)
            cmd = f"tar -xf {shlex.quote(archive_path)} -C {shlex.quote(remote_extract_dir)} 2>&1"
            extract_result = await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, cmd, timeout=300)

            if not extract_result.ok:
                return error_response(f"Extraction failed: {extract_result.stdout}", 500)

            extracted_name = target_dir_name or derive_suite_dir_name_from_archive(req.archive_path)
            extracted_path = os.path.join(extract_dir, extracted_name)
            return JSONResponse(content={"success": True, "message": f"Extraction complete: {extracted_name}", "extracted_path": extracted_path, "extract_method": "ssh"})
