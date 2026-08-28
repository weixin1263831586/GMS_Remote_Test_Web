"""Tradefed result discovery and artifact upload for the Worker Agent.

从 app.py 拆出（2026-08 审核第七节）：解析 Tradefed stdout 定位结果目录
（CTS/GTS 直接打印 RESULT DIRECTORY；VTS 需要跟进 process final logs），
并上传报告/日志工件。模块级函数，由 WorkerAgent 调用。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path


logger = logging.getLogger("gms-worker")

_RESULT_DIR_RE = re.compile(r"RESULT DIRECTORY\s*:\s*(\S+)")
_FINAL_LOG_RE = re.compile(r"process final logs:\s*(/\S+)")
_RESULT_PATH_RE = re.compile(
    r"(/[^\s]+/results/\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+_\d+)?)"
)


def _result_dir_from_host_log(log_path: Path) -> Path | None:
    if not log_path.is_file():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for match in _RESULT_PATH_RE.finditer(text):
        candidate = Path(match.group(1)).resolve()
        if candidate.is_dir():
            return candidate
    return None


def _result_dir_from_inv_dir(inv_dir: Path) -> Path | None:
    try:
        entries = list(inv_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.name.startswith("end_host_log_") and entry.suffix == ".txt":
            result = _result_dir_from_host_log(entry)
            if result:
                return result
    return None


def find_tradefed_result_dir(stdout: str) -> Path | None:
    """Locate the Tradefed result directory from stdout.

    CTS/GTS print ``RESULT DIRECTORY:`` directly. VTS instead prints
    ``process final logs:`` pointing at a temp inv directory whose
    ``end_host_log_*.txt`` file contains the real results path.
    """
    # 1. CTS/GTS — direct RESULT DIRECTORY line
    for match in reversed(_RESULT_DIR_RE.findall(stdout)):
        path = Path(match).expanduser().resolve()
        if path.is_dir():
            return path

    # 2. VTS — follow process final logs into the inv dir, then read the
    #    end_host_log file for the actual results/<timestamp> path.
    for match in reversed(_FINAL_LOG_RE.findall(stdout)):
        final_path = Path(match).expanduser().resolve()
        result = _result_dir_from_host_log(final_path)
        if result:
            return result
        inv_dir = final_path.parent if final_path.is_file() else final_path
        if inv_dir.is_dir():
            result = _result_dir_from_inv_dir(inv_dir)
            if result:
                return result
    return None


def upload_tradefed_results(
    row,
    work_dir: Path,
    suite_roots,
    upload_artifact: Callable,
    retry: Callable,
) -> None:
    """Parse stdout for the result dir and upload report artifacts.

    ``upload_artifact(job_id, attempt_id, path, kind)`` and ``retry(action)``
    are supplied by the caller so this module stays free of client state.
    """
    stdout_path = work_dir / "stdout.log"
    with stdout_path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 4 * 1024 * 1024))
        stdout = handle.read().decode("utf-8", errors="replace")
    result_dir = find_tradefed_result_dir(stdout)
    if result_dir is None:
        logger.info("no tradefed result directory found in stdout for job %s", row.get("job_id"))
        return
    if not any(
        root.exists() and result_dir.is_relative_to(root.resolve())
        for root in suite_roots
    ):
        logger.warning("ignored result directory outside suite roots: %s", result_dir)
        return
    # VTS does not print "RESULT DIRECTORY:" in stdout; append it so the
    # controller can resolve the report name from the uploaded stdout.log.
    if "RESULT DIRECTORY" not in stdout:
        with stdout_path.open("a", encoding="utf-8") as handle:
            handle.write(f"\nRESULT DIRECTORY: {result_dir}\n")
        retry(lambda: upload_artifact(
            row["job_id"], row["attempt_id"], stdout_path, "log"))
    for name in ("test_result.xml", "test_result.html", "test_result_failures.html"):
        path = result_dir / name
        if path.is_file():
            retry(lambda p=path: upload_artifact(
                row["job_id"], row["attempt_id"], p, "report"))
    archive_base = work_dir / "tradefed-results"
    archive_path = Path(shutil.make_archive(str(archive_base), "zip", result_dir))
    retry(lambda: upload_artifact(
        row["job_id"], row["attempt_id"], archive_path, "report-archive"))
