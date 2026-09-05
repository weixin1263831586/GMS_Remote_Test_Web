"""设备指纹与失败现场取证。

- 任务开始时采集设备指纹（build fingerprint / model / 内核版本 /
  build date），随 job 事件上报，任务详情直接可见，用于识别
  "测的包 ≠ 刷的包"。
- 任务失败时自动采集设备现场证据并作为工件上传：
  ① 失败前后各 5 分钟 logcat（-v threadtime）
  ② 设备快照：getprop、/proc/version、dumpsys input/bluetooth_manager 关键段
  采集失败不阻塞结果上报（尽力而为）。
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger("gms-worker")

_LOGCAT_WINDOW_SECONDS = 5 * 60


def _adb(serial: str, *args: str, timeout: int = 30) -> str:
    """尽力而为的 adb 调用；失败返回空串（取证不能阻塞任务收尾）。"""
    try:
        completed = subprocess.run(
            ["adb", "-s", serial, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("adb %s failed: %s", " ".join(args[:2]), exc)
        return ""
    return completed.stdout or "" if completed.returncode == 0 else ""


def collect_device_fingerprint(serial: str) -> dict[str, str]:
    """任务开始时采集设备指纹（全部尽力而为，缺项为空串）。"""
    getprop = lambda name: _adb(serial, "shell", "getprop", name).strip()
    return {
        "build_fingerprint": getprop("ro.build.fingerprint"),
        "product_model": getprop("ro.product.model"),
        "product_device": getprop("ro.product.device"),
        "build_date": getprop("ro.build.date"),
        "sdk_version": getprop("ro.build.version.sdk"),
        "build_id": getprop("ro.build.id"),
        "kernel_version": _adb(
            serial, "shell", "cat", "/proc/version", timeout=10
        ).strip(),
    }


def _logcat_window_files(serial: str, work_dir: Path) -> list[Path]:
    """导出失败时刻前约 5 分钟的 logcat（-v threadtime）。

    使用 ``-t <n>m`` 相对时间窗，不依赖设备与主机的时钟同步。
    """
    files: list[Path] = []
    output = _adb(
        serial, "logcat", "-v", "threadtime", "-d",
        "-t", f"{_LOGCAT_WINDOW_SECONDS // 60}m", timeout=60,
    )
    if output.strip():
        path = work_dir / "logcat_before.txt"
        path.write_text(output, encoding="utf-8", errors="replace")
        files.append(path)
    return files


def _device_snapshot(serial: str, work_dir: Path) -> list[Path]:
    """设备快照：getprop、内核版本、dumpsys input/bluetooth_manager 关键段。"""
    files: list[Path] = []
    sections = {
        "snapshot_getprop.txt": ["shell", "getprop"],
        "snapshot_kernel.txt": ["shell", "cat", "/proc/version"],
        "dumpsys_input.txt": ["shell", "dumpsys", "input"],
        "dumpsys_bluetooth.txt": ["shell", "dumpsys", "bluetooth_manager"],
    }
    for filename, args in sections.items():
        output = _adb(serial, *args, timeout=60)
        if output.strip():
            path = work_dir / filename
            path.write_text(output, encoding="utf-8", errors="replace")
            files.append(path)
    return files


def collect_failure_evidence(
    job_id: str,
    attempt_id: str,
    serials: list[str],
    work_dir: Path,
    upload_artifact: Callable,
    retry: Any,
) -> list[dict[str, Any]]:
    """失败任务采集设备现场证据并上传为工件。

    ``upload_artifact(job_id, attempt_id, path, kind)`` / ``retry(action)``
    与 results 模块约定一致。返回上传清单（供日志与测试断言）。
    """
    uploaded: list[dict[str, Any]] = []
    evidence_dir = work_dir / "failure-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for serial in serials:
        device_dir = evidence_dir / serial.replace(":", "_")
        device_dir.mkdir(parents=True, exist_ok=True)
        collected: list[Path] = []
        try:
            collected.extend(_device_snapshot(serial, device_dir))
            collected.extend(_logcat_window_files(serial, device_dir))
        except Exception:
            logger.exception("failure evidence collection failed for %s", serial)
        for path in collected:
            try:
                retry(lambda p=path: upload_artifact(
                    job_id, attempt_id, p, "failure-evidence"
                ))
            except Exception:
                logger.warning("evidence upload failed: %s", path.name)
            uploaded.append({
                "serial": serial,
                "filename": path.name,
                "path": str(path),
            })
    return uploaded
