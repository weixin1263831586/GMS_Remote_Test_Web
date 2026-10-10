from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import threading
import time
import urllib.parse
import uuid
from pathlib import PurePosixPath
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from features.auth import (
    CurrentUser,
    principal_owner_id,
    require_authenticated_user_when_auth_required,
)
from features.devices import ssh_connection_failed_response
from foundation.error_model import ApiError
from foundation.errors import handle_api_errors
from foundation.responses import ApiResponse

from . import runtime
from .models import SuiteApkAnalyzeRequest, SuiteDiagnosisTargetRequest
from .suite_helpers import (
    get_available_test_suites,
    resolve_suite_diagnosis_target,
)
from .suite_local_files import (
    list_suite_files_local,
    local_suite_directory_response,
    local_suite_file_info,
    local_suite_file_response,
    run_folder_suffix,
    search_suite_files_local,
)
from .suite_modules import search_latest_suite_modules
from .suite_remote_scripts import (
    SUITE_DIR_CLEANUP_SCRIPT,
    SUITE_DIR_ZIP_SCRIPT,
    SUITE_FILE_INFO_SCRIPT,
    SUITE_FILE_LIST_SCRIPT,
    SUITE_FILE_SEARCH_SCRIPT,
)
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
        # SSH find / 本地深度遍历是同步阻塞（timeout=30），与
        # diagnose_suite_target 一致移出事件循环。
        suites = await asyncio.to_thread(get_available_test_suites, config, base_path)
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
async def diagnose_suite_target(
    req: SuiteDiagnosisTargetRequest,
    _user: CurrentUser | None = Depends(
        require_authenticated_user_when_auth_required
    ),
):
    """Locate the most likely suite artifact and source path for a report failure."""
    try:
        target = await asyncio.to_thread(
            resolve_suite_diagnosis_target,
            runtime.config_manager.load_config(),
            test_type=req.test_type, suite_version=req.suite_version,
            module=req.module, test_name=req.test_name,
            class_names=req.class_names, suite_path=req.suite_path,
        )
        return ApiResponse.success(target)
    except Exception as e:
        logger.error("[TestSuites] Diagnosis target failed: %s", e, exc_info=True)
        # 不回显 str(e):异常文本可能含路径/命令,详见 foundation/errors.py。
        return ApiResponse.error("Internal server error", status_code=500)


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


def _run_suite_file_script(
    ssh, script: str, suite_root: str, remote_path: str, timeout: int = 20,
    *, extra_args: tuple[str, ...] = (), ssh_manager=None,
) -> dict[str, Any]:
    cmd = shlex.join(["python3", "-c", script, suite_root, remote_path, *extra_args])
    manager = runtime.ssh_manager if ssh_manager is None else ssh_manager
    result = manager.execute_command(ssh, cmd, timeout=timeout)
    if not result.ok:
        message = (
            result.stderr.strip() or result.stdout.strip()
            or "Remote file operation failed"
        )
        if result.timed_out:
            raise TimeoutError(message)
        raise RuntimeError(message)
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Remote file response parse failed: {e}"
        ) from e


def suite_dir_zip_timeout() -> int:
    """远端结果目录打包超时（秒）。

    Tradefed 结果目录可达数 GB，远端 zipfile 压缩耗时远超普通 stat/list
    脚本；默认 600s，可用 GMS_SUITE_ZIP_TIMEOUT_SECONDS 按主机磁盘速度调整。
    """
    try:
        return max(60, int(os.getenv("GMS_SUITE_ZIP_TIMEOUT_SECONDS", "600")))
    except ValueError:
        return 600


def _zip_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


class _SuiteStreamingResponse(StreamingResponse):
    """Close remote resources even before iteration or after disconnect."""

    def __init__(self, *args, cleanup, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.cleanup)




# ==================== Suite File Browsing ====================

@router.get("/api/test/suites/files")
@handle_api_errors
async def list_suite_files(suite_path: str = Query(...), path: str = Query("")):
    """Browse test suite directory files."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    if is_config_host_local(config):
        payload = await asyncio.to_thread(
            list_suite_files_local, suite_root, remote_path
        )
        if not payload.get("success"):
            return ApiResponse.error(payload.get("error", "Directory read failed"), status_code=400)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "path": payload.get("path", rel_path), "items": payload.get("items", [])})

    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()

        # 同步 SSH 脚本（默认 20s 超时）必须在 worker 线程执行，
        # 否则慢主机会阻塞事件循环、拖垮同进程其他请求。
        payload = await run_in_threadpool(
            _run_suite_file_script, ssh, SUITE_FILE_LIST_SCRIPT, suite_root, remote_path
        )
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
        items = await asyncio.to_thread(search_suite_files_local, suite_root, query.strip(), limit)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "query": query, "items": items, "count": len(items)})

    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()

        script = f"{SUITE_FILE_SEARCH_SCRIPT}"
        cmd = (
            f"python3 -c {shlex.quote(script)} {shlex.quote(suite_root)} {shlex.quote(suite_root)} "
            f"{shlex.quote(query.strip().lower())} {shlex.quote(str(limit))}"
        )
        search_result = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, cmd, timeout=60
        )
        if not search_result.ok:
            raise RuntimeError(
                search_result.stderr.strip() or search_result.stdout.strip()
                or "Remote search failed"
            )
        payload = json.loads(search_result.stdout.strip() or "{}")
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

    if is_config_host_local(config):
        try:
            return await asyncio.to_thread(
                local_suite_file_response, suite_root, remote_path, inline
            )
        except (ValueError, FileNotFoundError) as exc:
            status = 400 if isinstance(exc, ValueError) else 404
            return ApiResponse.error(str(exc), status_code=status)

    # 远程分支：连接获取、info 脚本（同步 SSH，默认 20s 超时）和 SFTP 打开
    # 全部放入 worker 线程执行——慢主机会阻塞事件循环，拖垮同进程其他请求。
    # 下载流改用 _SuiteStreamingResponse：客户端中途断开/迭代异常时由
    # shield 清理路径归还连接，与目录下载（download_suite_directory）同一套
    # 资源保护机制。
    manager = runtime.ssh_manager
    ssh = None
    sftp = remote_file = None
    info: dict[str, Any] = {}
    closed = False
    preparing = False
    cleanup_lock = threading.Lock()

    def cleanup():
        nonlocal closed
        with cleanup_lock:
            if closed:
                return
            closed = True
            if preparing:
                return
        if remote_file is not None:
            with contextlib.suppress(Exception):
                remote_file.close()
        if sftp is not None:
            with contextlib.suppress(Exception):
                sftp.close()
        if ssh is not None:
            manager.return_connection(ssh)

    def prepare():
        nonlocal ssh, sftp, remote_file, info, preparing
        acquired_ssh = manager.get_connection(config)
        if not acquired_ssh:
            raise RuntimeError("SSH connection failed")
        with cleanup_lock:
            if closed:
                manager.return_connection(acquired_ssh)
                raise TimeoutError("File download cancelled")
            ssh = acquired_ssh
            preparing = True
        try:
            info = _run_suite_file_script(
                ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path,
                ssh_manager=manager,
            )
        except BaseException:
            with cleanup_lock:
                preparing = False
                cancelled = closed
            if cancelled:
                manager.return_connection(ssh)
            raise
        if not info.get("success"):
            with cleanup_lock:
                preparing = False
            raise FileNotFoundError(info.get("error", "File not found"))
        # Cancellation may return the SSH connection while the synchronous
        # info command is still unwinding. Publish SFTP resources under the
        # same lock as cleanup so they can never be opened after that return.
        with cleanup_lock:
            preparing = False
            if closed:
                cancelled = True
            else:
                cancelled = False
                sftp = ssh.open_sftp()
                remote_file = sftp.open(info["real_path"], "rb")
        if cancelled:
            manager.return_connection(ssh)
            raise TimeoutError("File download cancelled")

    try:
        await run_in_threadpool(prepare)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(cleanup)
        raise
    except TimeoutError as exc:
        await run_in_threadpool(cleanup)
        raise ApiError.dependency_timeout(
            "远程文件信息获取超时，请重试",
            next_actions=[{"action": "Retry the download"}],
        ) from exc
    except FileNotFoundError as exc:
        await run_in_threadpool(cleanup)
        raise ApiError.not_found(str(exc)) from exc
    except Exception as exc:
        await run_in_threadpool(cleanup)
        if "SSH connection failed" in str(exc):
            raise ApiError.upstream_failure(
                "SSH connection failed",
                service="ssh",
                next_actions=[
                    {"action": "verify host sshd", "command": "ping <host>"},
                    {"action": "retry after fixing SSH access"},
                ],
            ) from exc
        raise ApiError.upstream_failure(
            "远程文件下载准备失败", service="ssh",
            next_actions=[{"action": "Check Worker SSH/SFTP and retry the download"}],
        ) from exc

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
            cleanup()

    if inline:
        disposition = "inline"
    else:
        disposition = f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}'

    try:
        return _SuiteStreamingResponse(
            iter_remote_file(),
            cleanup=cleanup,
            media_type=media_type,
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(info.get("size", 0)),
            },
        )
    except BaseException:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(cleanup)
        raise


@router.get("/api/test/suites/download-dir")
@handle_api_errors
async def download_suite_directory(suite_path: str = Query(...), path: str = Query(...)):
    """Download a directory from the test suite as a zip archive (folder tree preserved)."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    if is_config_host_local(config):
        try:
            return await asyncio.to_thread(
                local_suite_directory_response, suite_root, remote_path, rel_path
            )
        except (ValueError, FileNotFoundError) as exc:
            status = 400 if isinstance(exc, ValueError) else 404
            return ApiResponse.error(str(exc), status_code=status)

    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    manager = runtime.ssh_manager
    operation = uuid.uuid4().hex
    sftp = remote_file = None
    info = {}
    started = closed = False
    cleanup_lock = threading.Lock()

    def cleanup():
        nonlocal closed
        with cleanup_lock:
            if closed:
                return
            closed = True
        if remote_file is not None:
            with contextlib.suppress(Exception):
                remote_file.close()
        if sftp is not None:
            if info.get("zip_path"):
                with contextlib.suppress(Exception):
                    sftp.remove(info["zip_path"])
            with contextlib.suppress(Exception):
                sftp.close()
        if started:
            try:
                result = _run_suite_file_script(
                    ssh, SUITE_DIR_CLEANUP_SCRIPT, suite_root, remote_path,
                    timeout=10, extra_args=(operation,), ssh_manager=manager,
                )
                if not result.get("success"):
                    logger.warning("Remote archive cancellation is pending its timeout/stale cleanup")
            except Exception:
                logger.warning("Remote suite archive cleanup could not complete", exc_info=True)
        manager.return_connection(ssh)

    def prepare():
        nonlocal info, sftp, remote_file, started
        timeout = suite_dir_zip_timeout()
        with cleanup_lock:
            if closed:
                raise TimeoutError("Archive download cancelled")
            started = True
        info = _run_suite_file_script(
            ssh, SUITE_DIR_ZIP_SCRIPT, suite_root, remote_path,
            timeout=timeout, ssh_manager=manager,
            extra_args=(operation,
                        str(_zip_setting("GMS_SUITE_ZIP_MAX_BYTES", 8 * 1024 ** 3)),
                        str(_zip_setting("GMS_SUITE_ZIP_MAX_FILES", 100000)),
                        str(_zip_setting("GMS_SUITE_ZIP_MIN_FREE_BYTES", 256 * 1024 ** 2)),
                        str(_zip_setting("GMS_SUITE_ZIP_STALE_SECONDS", 86400)),
                        str(max(1, timeout - 5))),
        )
        if not info.get("success"):
            started = False  # The script cleans failed builds before exiting.
            errors = {
                "DEPENDENCY_TIMEOUT": ApiError.dependency_timeout,
                "DEPENDENCY_UNAVAILABLE": ApiError.dependency_unavailable,
                "INVALID_SEMANTICS": ApiError.invalid_semantics,
                "UPSTREAM_FAILURE": ApiError.upstream_failure,
            }
            raise errors.get(info.get("code"), ApiError.not_found)(
                info.get("error", "Directory not found"),
                next_actions=[{"action": "Retry with a smaller directory or check Worker temporary disk space"}],
            )

        with cleanup_lock:
            if closed:
                raise TimeoutError("Archive download cancelled")
            archive_path = info.get("zip_path")
            if not isinstance(archive_path, str):
                raise RuntimeError("Invalid remote archive path")
            parts = PurePosixPath(archive_path).parts
            if (len(parts) < 5 or parts[0] != "/" or ".." in parts
                    or parts[-1] != "archive.zip" or parts[-2] != operation
                    or not re.fullmatch(r"gms-suite-downloads-[0-9]+", parts[-3])):
                raise RuntimeError("Remote archive does not belong to this download")
            sftp = ssh.open_sftp()
            sftp.get_channel().settimeout(30)
            remote_file = sftp.open(info["zip_path"], "rb")

    try:
        await run_in_threadpool(prepare)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(cleanup)
        raise
    except TimeoutError as exc:
        await run_in_threadpool(cleanup)
        raise ApiError.dependency_timeout(
            "远端目录打包超时或失败，请重试或分批下载较小目录",
            next_actions=[{"action": "Retry the download"}],
        ) from exc
    except ApiError:
        await run_in_threadpool(cleanup)
        raise
    except Exception as exc:
        await run_in_threadpool(cleanup)
        raise ApiError.upstream_failure(
            "远端目录打包或文件传输失败", service="ssh",
            next_actions=[{"action": "Check Worker SSH/SFTP and retry the download"}],
        ) from exc

    def iter_remote_dir():
        try:
            while True:
                chunk = remote_file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            cleanup()

    try:
        folder_name = info.get("name") or os.path.basename(remote_path) or "download"
        filename = f"{folder_name}{run_folder_suffix(rel_path)}.zip"
        ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download.zip"
        quoted_filename = urllib.parse.quote(filename)
        return _SuiteStreamingResponse(
            iter_remote_dir(),
            cleanup=cleanup,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}',
                "Content-Length": str(info.get("size", 0)),
            },
        )
    except BaseException:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(cleanup)
        raise


@router.post("/api/test/suites/apk/analyze")
@handle_api_errors
async def create_suite_apk_analysis_task(req: SuiteApkAnalyzeRequest, request: Request):
    """Copy an APK from test suite for APK analysis."""
    config = runtime.config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(req.suite_path, req.path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    if is_config_host_local(config):
        try:
            info = await asyncio.to_thread(
                local_suite_file_info, suite_root, remote_path
            )
        except (ValueError, FileNotFoundError) as exc:
            status = 400 if isinstance(exc, ValueError) else 404
            return ApiResponse.error(str(exc), status_code=status)
        if not (info["is_apk"] or info["is_jar"]):
            return ApiResponse.error(
                "Only APK/JAR files supported for decompilation", status_code=400
            )
        if int(info["size"]) > runtime.apk_max_file_size:
            return ApiResponse.error(
                f"File too large, max {runtime.apk_max_file_size // (1024*1024)}MB",
                status_code=400,
            )

        task_id = str(uuid.uuid4())
        filename = runtime.normalize_apk_filename(info["name"])
        task_dir = runtime.safe_join(runtime.apk_upload_dir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        apk_path = runtime.safe_join(task_dir, filename)
        await asyncio.to_thread(shutil.copyfile, info["real_path"], apk_path)
        runtime.create_apk_task(
            task_id,
            apk_path,
            filename,
            principal_owner_id(request),
        )
        return ApiResponse.success({
            "task_id": task_id,
            "filename": filename,
            "size": os.path.getsize(apk_path),
            "source_path": req.path,
        })

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

        runtime.create_apk_task(
            task_id,
            apk_path,
            filename,
            principal_owner_id(request),
        )
        return ApiResponse.success({"task_id": task_id, "filename": filename, "size": os.path.getsize(apk_path), "source_path": req.path})
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)
    finally:
        if sftp:
            with contextlib.suppress(Exception):
                sftp.close()
        runtime.ssh_manager.return_connection(ssh)
