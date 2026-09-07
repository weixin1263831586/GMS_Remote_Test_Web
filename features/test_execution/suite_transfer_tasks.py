"""Suite download/extract background task bodies.

从 transfers_api.py 拆出（2026-09 全局审核）：HTTP 路由与后台任务
执行体分离，使 transfers_api.py 回到 600 行预算内。任务状态一律经
``runtime.suite_task_store`` 读写，本模块不持有独立状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from . import runtime
from .suite_archives import extract_archive_local_with_progress
from .suite_download_security import (
    curl_resolve_arguments as _curl_resolve_arguments,
)
from .suite_download_security import (
    resolve_suite_download_target as _resolve_suite_download_target,
)


logger = logging.getLogger(__name__)

_SUITE_TASK_TTL = 3600
MAX_SUITE_ARCHIVE_BYTES = 50 * 1024 * 1024 * 1024
_last_suite_cleanup = 0.0


def update_suite_download_task(task_id: str, **updates):
    runtime.suite_task_store.update(task_id, **updates)


def update_suite_extract_task(task_id: str, **updates):
    runtime.suite_task_store.update(task_id, **updates)


def cleanup_old_suite_tasks():
    global _last_suite_cleanup
    now = time.time()
    if now - _last_suite_cleanup < 60:
        return
    _last_suite_cleanup = now
    runtime.suite_task_store.delete_finished_before(now - _SUITE_TASK_TTL)


def schedule_suite_task(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)
    return task


def _parse_curl_size(s: str) -> float:
    s = s.rstrip(",").lower()
    if s.endswith("g"):
        return float(s[:-1]) * 1024 ** 3
    if s.endswith("m"):
        return float(s[:-1]) * 1024 ** 2
    if s.endswith("k"):
        return float(s[:-1]) * 1024
    return float(s)


async def run_suite_download_task(task_id: str, url: str, archive_path: str):
    part_path = archive_path + ".part"
    filename = os.path.basename(archive_path)
    # 禁止重定向，防止 Location 绕过 SSRF 地址校验。
    try:
        target = await asyncio.to_thread(_resolve_suite_download_target, url)
    except ValueError as exc:
        update_suite_download_task(
            task_id,
            status="error",
            error=f"Download target rejected: {exc}",
            retryable=False,
        )
        return
    cmd = ["curl", "--proto", "=http,https", "--max-redirs", "0", *_curl_resolve_arguments(target), "-C", "-", "--max-filesize", str(MAX_SUITE_ARCHIVE_BYTES), "--connect-timeout", "30", "--max-time", "7200", "--retry", "3", "--retry-delay", "5", "-o", part_path, target.url]
    update_suite_download_task(task_id, status="downloading", progress=0, message=f"Downloading: {filename}")

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
                        update_suite_download_task(task_id, progress=min(progress, 99.0), downloaded_size=int(downloaded), total_size=int(total), speed_bps=int(speed))
                    except (ValueError, ZeroDivisionError):
                        pass

        await process.wait()
        if process.returncode != 0:
            # curl exit 63 means --max-filesize was exceeded; discard that
            # oversized partial. Network failures retain .part so the next
            # task can actually resume via -C -.
            if process.returncode == 63 and os.path.exists(part_path):
                with contextlib.suppress(OSError):
                    os.remove(part_path)
            update_suite_download_task(
                task_id,
                status="error",
                progress=0,
                error=f"Download failed (exit code {process.returncode})",
                resumable=os.path.exists(part_path),
            )
            return

        os.replace(part_path, archive_path)
        file_size = os.path.getsize(archive_path)
        update_suite_download_task(task_id, status="completed", progress=100, downloaded_size=file_size, archive_path=archive_path, speed_bps=0, message=f"Download complete: {filename}")
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        update_suite_download_task(
            task_id,
            status="recovering",
            message="Download paused during Controller shutdown",
            resumable=os.path.exists(part_path),
        )
        raise
    except Exception as e:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        update_suite_download_task(task_id, status="error", error=f"Download error: {e!s}")


def _extract_archive_with_progress(archive_path: str, extract_dir: str, target_dir_name: str, task_id: str | None = None) -> dict:
    return extract_archive_local_with_progress(
        archive_path,
        extract_dir,
        target_dir_name,
        task_id,
        update_suite_extract_task,
    )


async def run_suite_extract_task(task_id: str, archive_path: str, extract_dir: str, target_dir_name: str):
    try:
        update_suite_extract_task(task_id, status="extracting", progress=0, message="Extracting...")
        result = await asyncio.to_thread(
            _extract_archive_with_progress,
            archive_path, extract_dir, target_dir_name, task_id)
        update_suite_extract_task(task_id, status="completed", progress=100, **result)
    except Exception as e:
        logger.error(f"[Suite Extract] Failed: {e}")
        update_suite_extract_task(task_id, status="error", error=f"Extraction failed: {e!s}")
