from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid
import zipfile
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from features.devices import ssh_connection_failed_response
from foundation.archives import (
    derive_suite_dir_name_from_archive,
    is_complete_archive_file,
    safe_extract_member_path,
    sanitize_suite_dir_name,
    sanitize_suite_filename_from_url,
    strip_common_archive_root,
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
from .suites import (
    ensure_tradefed_executable,
    get_default_suites_path,
    is_config_host_local,
)


logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Suite Download Tasks ====================

def _update_suite_task(tasks_dict: dict, lock, task_id: str, **updates):
    with lock:
        task = tasks_dict.get(task_id)
        if task:
            task.update(updates)
            task["updated_at"] = time.time()


def _update_suite_download_task(task_id: str, **updates):
    _update_suite_task(runtime.global_state.suite_download_tasks, runtime.global_state.suite_download_tasks_lock, task_id, **updates)


def _update_suite_extract_task(task_id: str, **updates):
    _update_suite_task(runtime.global_state.suite_extract_tasks, runtime.global_state.suite_extract_tasks_lock, task_id, **updates)


_SUITE_TASK_TTL = 3600
_last_suite_cleanup = 0.0


def _cleanup_old_suite_tasks():
    global _last_suite_cleanup
    now = time.time()
    if now - _last_suite_cleanup < 60:
        return
    _last_suite_cleanup = now
    cutoff = now - _SUITE_TASK_TTL
    for tasks, lock in (
        (runtime.global_state.suite_download_tasks, runtime.global_state.suite_download_tasks_lock),
        (runtime.global_state.suite_extract_tasks, runtime.global_state.suite_extract_tasks_lock),
    ):
        with lock:
            stale = [tid for tid, t in tasks.items() if t.get("status") in ("completed", "error") and t.get("updated_at", 0) < cutoff]
            for tid in stale:
                del tasks[tid]


def _parse_curl_size(s: str) -> float:
    s = s.rstrip(",").lower()
    if s.endswith("g"):
        return float(s[:-1]) * 1024 ** 3
    if s.endswith("m"):
        return float(s[:-1]) * 1024 ** 2
    if s.endswith("k"):
        return float(s[:-1]) * 1024
    return float(s)


async def _run_suite_download_task(task_id: str, url: str, archive_path: str):
    part_path = archive_path + ".part"
    filename = os.path.basename(archive_path)
    cmd = ["curl", "-L", "-C", "-", "--connect-timeout", "30", "--max-time", "7200", "--retry", "3", "--retry-delay", "5", "-o", part_path, url]
    _update_suite_download_task(task_id, status="downloading", progress=0, message=f"Downloading: {filename}")

    process = None
    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buffer = ""
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="ignore")
            while "\r" in buffer:
                line, buffer = buffer.split("\r", 1)
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        pct = float(parts[0])
                        total = _parse_curl_size(parts[1])
                        speed = _parse_curl_size(parts[-1])
                        downloaded = total * pct / 100
                        progress = pct if pct > 0 else 0
                        _update_suite_download_task(task_id, progress=min(progress, 99.0), downloaded_size=int(downloaded), total_size=int(total), speed_bps=int(speed))
                    except (ValueError, ZeroDivisionError):
                        pass

        await process.wait()
        if process.returncode != 0:
            if os.path.exists(part_path):
                with contextlib.suppress(OSError):
                    os.remove(part_path)
            _update_suite_download_task(task_id, status="error", progress=0, error=f"Download failed (exit code {process.returncode})")
            return

        os.replace(part_path, archive_path)
        file_size = os.path.getsize(archive_path)
        _update_suite_download_task(task_id, status="completed", progress=100, downloaded_size=file_size, archive_path=archive_path, speed_bps=0, message=f"Download complete: {filename}")
    except Exception as e:
        if process and process.returncode is None:
            process.kill()
        _update_suite_download_task(task_id, status="error", error=f"Download error: {e!s}")


def _extract_archive_local_with_progress(archive_path: str, extract_dir: str, target_dir_name: str, task_id: str | None = None) -> dict[str, Any]:
    target_extract_dir = os.path.join(extract_dir, target_dir_name) if target_dir_name else extract_dir
    if target_dir_name:
        os.makedirs(target_extract_dir, exist_ok=True)

    files_count = 0
    _last_pct = -1

    def progress(done: int, total: int):
        nonlocal _last_pct
        if not task_id:
            return
        pct = int(done / total * 100) if total else 0
        if pct == _last_pct:
            return
        _last_pct = pct
        _update_suite_extract_task(task_id, status="extracting", progress=min(float(pct), 99.0), extracted_count=done, total_count=total)

    def _chmod_tradefed(path: str, name: str):
        if name.endswith("-tradefed"):
            ensure_tradefed_executable(path)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            names = zip_ref.namelist()
            if target_dir_name:
                _, mapped_names = strip_common_archive_root(names)
                total = len([item for item in mapped_names if item[1] and not item[0].endswith("/")])
                for source_name, relative_name in mapped_names:
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(target_extract_dir, relative_name)
                    if source_name.endswith("/"):
                        os.makedirs(target_path, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zip_ref.open(source_name) as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    _chmod_tradefed(target_path, os.path.basename(target_path))
                    files_count += 1
                    progress(files_count, total)
            else:
                total = len(names)
                for member_name in names:
                    zip_ref.extract(member_name, extract_dir)
                    files_count += 0 if member_name.endswith("/") else 1
                    progress(files_count, total)
    elif archive_path.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2")):
        mode = "r:gz" if archive_path.endswith((".tar.gz", ".tgz")) else ("r:bz2" if archive_path.endswith(".tar.bz2") else "r")
        with tarfile.open(archive_path, mode) as tar_ref:
            members = tar_ref.getmembers()
            if target_dir_name:
                names = [m.name for m in members]
                _, mapped_names = strip_common_archive_root(names)
                name_map = dict(mapped_names)
                total = len([m for m in members if m.isfile()])
                for member in members:
                    relative_name = name_map.get(member.name, member.name)
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(target_extract_dir, relative_name)
                    if member.isdir():
                        os.makedirs(target_path, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        src = tar_ref.extractfile(member)
                        if src:
                            with src, open(target_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            _chmod_tradefed(target_path, os.path.basename(target_path))
                            files_count += 1
                            progress(files_count, total)
            else:
                total = len([m for m in members if m.isfile()])
                for member in members:
                    safe_extract_member_path(extract_dir, member.name)
                    tar_ref.extract(member, extract_dir)
                    if member.isfile():
                        extracted = os.path.join(extract_dir, member.name)
                        _chmod_tradefed(extracted, os.path.basename(extracted))
                        files_count += 1
                        progress(files_count, total)
    else:
        cmd = ["tar", "-xf", archive_path, "-C", target_extract_dir if target_dir_name else extract_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "tar extraction failed")

    extracted_name = target_dir_name or derive_suite_dir_name_from_archive(archive_path)
    return {"message": f"Extraction complete: {extracted_name}", "extracted_path": os.path.join(extract_dir, extracted_name), "files_count": files_count, "extract_method": "local"}


async def _run_suite_extract_task(task_id: str, archive_path: str, extract_dir: str, target_dir_name: str):
    try:
        _update_suite_extract_task(task_id, status="extracting", progress=0, message="Extracting...")
        result = await asyncio.to_thread(_extract_archive_local_with_progress, archive_path, extract_dir, target_dir_name, task_id)
        _update_suite_extract_task(task_id, status="completed", progress=100, **result)
    except Exception as e:
        logger.error(f"[Suite Extract] Failed: {e}")
        _update_suite_extract_task(task_id, status="error", error=f"Extraction failed: {e!s}")


@router.post("/api/test/suites/add-local")
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
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            check_cmd = f"[ -d '{req.path}' ] && echo 'exists' || echo 'not_exists'"
            output, _, _ = runtime.ssh_manager.execute_command(ssh, check_cmd, timeout=10)
            if output.strip() != "exists":
                return error_response(f"Path not found: {req.path}", 404)

    return JSONResponse(content={"success": True, "message": f"Added local path: {os.path.basename(req.path.rstrip('/'))}", "path": req.path})


@router.post("/api/test/suites/result")
async def list_tradefed_results(
    h: str | None = Query(None),
    help: bool = Query(False),
    req: TradefedListResultsRequest = Body(None),
    force_refresh: bool = Query(False),
):
    """Execute tradefed list results and return test results."""
    from features.test_execution.tradefed import (
        execute_tradefed_command,
        find_tradefed_binary,
        parse_tradefed_list_results,
    )

    resp = runtime.generate_help_or_continue(help, "POST", "/api/test/suites/result")
    if resp:
        return resp

    if req is None:
        return error_response("Missing request body", 400)

    try:
        config = runtime.config_manager.load_config()
        suite_path = req.suite_path
        tradefed_bin = req.tradefed_bin
        logger.info(f"Querying test suite results for {suite_path}")

        ssh = runtime.ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            if not tradefed_bin:
                tradefed_bin = find_tradefed_binary(ssh, suite_path)
                if not tradefed_bin:
                    runtime.ssh_manager.return_connection(ssh)
                    return error_response(f"No tradefed binary found in {suite_path}", 404)

            output, error, code = execute_tradefed_command(ssh, suite_path, tradefed_bin)
            runtime.ssh_manager.return_connection(ssh)

            if code != 0:
                return error_response(error or f"Command failed with exit code: {code}", status_code=500, raw_output=output)

            results = parse_tradefed_list_results(output)
            return JSONResponse(content={"success": True, "results": results, "count": len(results), "raw_output": output, "cached": False})
        except Exception:
            runtime.ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing tradefed results: {e}")
        return error_response(str(e), 500)


@router.post("/api/test/suites/download-url")
@handle_api_errors
async def download_test_suite_from_url(req: TestSuiteDownloadRequest):
    """Download test suite from URL."""
    config = runtime.config_manager.load_config()
    if not req.url:
        return error_response("Download URL cannot be empty", 400)

    save_dir = req.save_dir or get_default_suites_path(config)
    os.makedirs(save_dir, exist_ok=True)

    filename = sanitize_suite_filename_from_url(req.url)
    archive_path = os.path.join(save_dir, filename)

    if is_config_host_local(config):
        with runtime.global_state.suite_download_tasks_lock:
            for existing_task in runtime.global_state.suite_download_tasks.values():
                if existing_task.get("archive_path") == archive_path and existing_task.get("status") in {"queued", "downloading"}:
                    return JSONResponse(content={"success": True, "message": f"Download task exists: {filename}", "task_id": existing_task.get("task_id"), "archive_path": archive_path, "file_size": 0, "download_method": "local_async_existing"})

        if is_complete_archive_file(archive_path):
            file_size = os.path.getsize(archive_path)
            return JSONResponse(content={"success": True, "message": f"File already exists: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "local_existing"})
        if os.path.exists(archive_path):
            return error_response(f"Incomplete or corrupt archive exists: {archive_path}", 409)

        task_id = str(uuid.uuid4())
        with runtime.global_state.suite_download_tasks_lock:
            runtime.global_state.suite_download_tasks[task_id] = {
                "task_id": task_id, "status": "queued", "progress": 0,
                "url": req.url, "filename": filename, "archive_path": archive_path,
                "downloaded_size": 0, "total_size": 0, "message": f"Preparing download: {filename}",
                "created_at": time.time(), "updated_at": time.time(),
            }
        task = asyncio.create_task(_run_suite_download_task(task_id, req.url, archive_path))
        runtime.global_state.background_tasks.add(task)
        task.add_done_callback(runtime.global_state.background_tasks.discard)
        return JSONResponse(content={"success": True, "message": f"Download started: {filename}", "task_id": task_id, "archive_path": archive_path, "file_size": 0, "download_method": "local_async"})
    else:
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            cmd = f"curl -L -o '{archive_path}' '{req.url}' 2>&1"
            output, exit_code, _ = runtime.ssh_manager.execute_command(ssh, cmd, timeout=600)
            if exit_code != 0:
                return error_response(f"Download failed: {output}", 500)
            size_cmd = f"stat -c%s '{archive_path}' 2>/dev/null || stat -f%z '{archive_path}' 2>/dev/null || echo 0"
            size_output, _, _ = runtime.ssh_manager.execute_command(ssh, size_cmd, timeout=10)
            file_size = int(size_output.strip())
            return JSONResponse(content={"success": True, "message": f"Download complete: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "ssh"})


@router.get("/api/test/suites/download-status/{task_id}")
async def get_test_suite_download_status(task_id: str):
    _cleanup_old_suite_tasks()
    with runtime.global_state.suite_download_tasks_lock:
        task = dict(runtime.global_state.suite_download_tasks.get(task_id) or {})
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

    with runtime.ssh_manager.optional_connection(config) as ssh:
        if not ssh:
            return ssh_connection_failed_response()
        find_cmd = f"find {shlex.quote(base_path)} -maxdepth 1 -type f \\( -name '*.zip' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.tar.bz2' -o -name '*.tar' \\) -printf '%T@\\t%s\\t%f\\t%p\\n' 2>/dev/null | sort -nr"
        output, _, _ = runtime.ssh_manager.execute_command(ssh, find_cmd, timeout=20)
        archives = []
        for line in output.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                mtime, size, name, path = parts
                archives.append({"name": name, "path": path, "size": int(float(size)) if size else 0, "mtime": float(mtime) if mtime else 0, "default_dir_name": derive_suite_dir_name_from_archive(path)})
        return JSONResponse(content={"success": True, "archives": archives, "base_path": base_path})


@router.post("/api/test/suites/extract-start")
@handle_api_errors
async def start_test_suite_extract(req: TestSuiteExtractRequest):
    config = runtime.config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path))

    if not is_config_host_local(config):
        return error_response("Background extraction only supports local host mode", 400)

    if not os.path.exists(req.archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)
    if not is_complete_archive_file(req.archive_path):
        return error_response(f"Incomplete or unsupported archive: {req.archive_path}", 400)

    os.makedirs(extract_dir, exist_ok=True)
    task_id = str(uuid.uuid4())
    with runtime.global_state.suite_extract_tasks_lock:
        runtime.global_state.suite_extract_tasks[task_id] = {
            "task_id": task_id, "status": "queued", "progress": 0,
            "archive_path": req.archive_path, "extract_dir": extract_dir,
            "target_dir_name": target_dir_name, "extracted_count": 0, "total_count": 0,
            "message": f"Preparing extraction: {os.path.basename(req.archive_path)}",
            "created_at": time.time(), "updated_at": time.time(),
        }

    task = asyncio.create_task(_run_suite_extract_task(task_id, req.archive_path, extract_dir, target_dir_name))
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)
    return JSONResponse(content={"success": True, "task_id": task_id, "message": "Extraction started", "archive_path": req.archive_path, "target_dir_name": target_dir_name})


@router.get("/api/test/suites/extract-status/{task_id}")
async def get_test_suite_extract_status(task_id: str):
    _cleanup_old_suite_tasks()
    with runtime.global_state.suite_extract_tasks_lock:
        task = dict(runtime.global_state.suite_extract_tasks.get(task_id) or {})
    if not task:
        return error_response("Extract task not found", 404)
    return JSONResponse(content={"success": True, "task": task})


@router.post("/api/test/suites/extract")
@handle_api_errors
async def extract_test_suite_archive(req: TestSuiteExtractRequest):
    """Extract test suite archive."""
    config = runtime.config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path)) if req.target_dir_name else ""

    if is_config_host_local(config) and not os.path.exists(req.archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)

    os.makedirs(extract_dir, exist_ok=True)

    if is_config_host_local(config):
        try:
            result = await asyncio.to_thread(_extract_archive_local_with_progress, req.archive_path, extract_dir, target_dir_name)
            return JSONResponse(content={"success": True, **result})
        except Exception as e:
            return error_response(f"Extraction failed: {e!s}", 500)
    else:
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return ssh_connection_failed_response()
            remote_extract_dir = os.path.join(extract_dir, target_dir_name) if target_dir_name else extract_dir
            mkdir_cmd = f"mkdir -p {shlex.quote(remote_extract_dir)}"
            runtime.ssh_manager.execute_command(ssh, mkdir_cmd, timeout=20)
            cmd = f"tar -xf {shlex.quote(req.archive_path)} -C {shlex.quote(remote_extract_dir)} 2>&1"
            output, exit_code, _ = runtime.ssh_manager.execute_command(ssh, cmd, timeout=300)

            if exit_code != 0:
                return error_response(f"Extraction failed: {output}", 500)

            extracted_name = target_dir_name or derive_suite_dir_name_from_archive(req.archive_path)
            extracted_path = os.path.join(extract_dir, extracted_name)
            return JSONResponse(content={"success": True, "message": f"Extraction complete: {extracted_name}", "extracted_path": extracted_path, "extract_method": "ssh"})
