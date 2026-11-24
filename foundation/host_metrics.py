"""Resource measurements shared by the Controller and Worker heartbeats."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from foundation.files import disk_metrics


logger = logging.getLogger(__name__)
_cpu_lock = threading.Lock()
_last_cpu_sample: tuple[int, int] | None = None


def _cpu_percent() -> float:
    global _last_cpu_sample
    with _cpu_lock:
        try:
            fields = Path("/proc/stat").read_text().splitlines()[0].split()
            # Guest time is already included in user and nice.
            ticks = [int(value) for value in fields[1:9]]
            idle = ticks[3] + ticks[4]
            total = sum(ticks)
        except (IndexError, OSError, ValueError):
            return 0.0
        previous = _last_cpu_sample
        _last_cpu_sample = (total, idle)
        if not previous or total <= previous[0]:
            return 0.0
        total_delta = total - previous[0]
        busy = total_delta - (idle - previous[1])
        return round(100 * max(0, min(total_delta, busy)) / total_delta, 2)


def collect_host_metrics(data_root: Path) -> dict[str, float | int | None]:
    """Report Linux CPU usage, available memory and the data filesystem."""
    metrics: dict[str, float | int | None] = {
        "cpu_percent": _cpu_percent(),
        "memory_percent": 0.0,
        "memory_total_gb": 0.0,
        "memory_available_gb": 0.0,
        "load_1m": 0.0,
        "disk_total_gb": 0.0,
        "disk_free_gb": 0.0,
        "disk_percent": None,
    }
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, _, value = line.partition(":")
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.split()[0])
        total, available = memory["MemTotal"], memory["MemAvailable"]
        metrics.update(
            memory_percent=round(100 * (total - available) / total, 2),
            memory_total_gb=round(total / 1024 ** 2, 2),
            memory_available_gb=round(available / 1024 ** 2, 2),
        )
    except (IndexError, KeyError, OSError, ValueError, ZeroDivisionError):
        logger.debug("Host memory metrics unavailable", exc_info=True)
    try:
        metrics["load_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        logger.debug("Host load metrics unavailable", exc_info=True)
    try:
        disk_path = next(
            (path for path in (data_root, *data_root.parents) if path.is_dir()),
            Path.home(),
        )
        metrics.update(disk_metrics(disk_path))
    except OSError:
        logger.debug("Host disk metrics unavailable", exc_info=True)
    return metrics
