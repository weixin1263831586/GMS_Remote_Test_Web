"""Liveness and cached readiness probes for production operation."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from foundation.config import settings


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"expires_at": 0.0, "result": None}


def _check_storage() -> dict[str, Any]:
    root = settings.data_root
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(root)
    used_percent = round((usage.used / max(usage.total, 1)) * 100, 2)
    maximum = float(os.getenv("GMS_DISK_MAX_USED_PERCENT", "90"))
    return {
        "ok": used_percent < maximum,
        "used_percent": used_percent,
        "free_gb": round(usage.free / (1024**3), 2),
        "maximum_used_percent": maximum,
    }


def _check_database_write() -> dict[str, Any]:
    path = settings.data_root / "health/health.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS readiness_probe "
            "(probe_id INTEGER PRIMARY KEY CHECK(probe_id=1), checked_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO readiness_probe(probe_id, checked_at) VALUES(1, ?) "
            "ON CONFLICT(probe_id) DO UPDATE SET checked_at=excluded.checked_at",
            (time.time(),),
        )
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        connection.commit()
    return {
        "ok": quick_check == "ok",
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "quick_check": quick_check,
    }


def _check_adb() -> dict[str, Any]:
    executable = shutil.which("adb")
    if not executable:
        return {"ok": False, "error": "adb executable not found"}
    try:
        result = subprocess.run(
            [executable, "devices"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "device_count": max(0, len([
            line for line in result.stdout.splitlines()[1:]
            if line.strip() and "\t" in line
        ])),
        "error": result.stderr.strip()[:200],
    }


def _check_ssh() -> dict[str, Any]:
    executable = shutil.which("ssh")
    configured = os.getenv("GMS_SSH_KNOWN_HOSTS", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path.home() / ".ssh/known_hosts",
        Path("/etc/ssh/ssh_known_hosts"),
    ]
    known_hosts = next((path for path in candidates if path and path.is_file()), None)
    return {
        "ok": bool(executable and known_hosts),
        "client_available": bool(executable),
        "known_hosts_configured": bool(known_hosts),
    }


def _check_automation_worker() -> dict[str, Any]:
    try:
        from features.automation import get_worker_status

        status = get_worker_status()
        return {
            "ok": not status.get("enabled") or bool(status.get("running")),
            "enabled": bool(status.get("enabled")),
            "running": bool(status.get("running")),
            "last_tick_seconds_ago": status.get("last_tick_seconds_ago"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _check_local_worker() -> dict[str, Any]:
    try:
        from features.cluster import get_cluster_service

        service = get_cluster_service()
        if not service.effective_enabled:
            return {"ok": True, "required": False}
        local_worker_id = service.config.local_worker_id
        worker = next(
            (item for item in service.list_workers() if item.get("id") == local_worker_id),
            None,
        )
        has_agent = bool(worker and service.has_command_agent(local_worker_id))
        return {
            "ok": has_agent,
            "required": True,
            "registered": bool(worker),
            "status": str((worker or {}).get("status") or "missing"),
            "agent_connected": has_agent,
        }
    except Exception as exc:
        return {"ok": False, "required": True, "error": str(exc)}


def _check_runtime_queues(app: Any) -> dict[str, Any]:
    queue = getattr(getattr(app, "state", None), "usb_event_queue", None)
    backlog = int(queue.qsize()) if queue is not None else 0
    maximum = int(os.getenv("GMS_HEALTH_MAX_USB_QUEUE", "1000"))
    return {"ok": backlog <= maximum, "usb_event_backlog": backlog, "maximum": maximum}


def readiness(app: Any, *, force: bool = False) -> dict[str, Any]:
    """Return cached dependency checks without exposing their details publicly."""
    cache_seconds = max(1, int(os.getenv("GMS_HEALTH_CACHE_SECONDS", "10")))
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE["result"] is not None and now < _CACHE["expires_at"]:
            return dict(_CACHE["result"])

        checks: dict[str, dict[str, Any]] = {}
        for name, check in (
            ("storage", _check_storage),
            ("database", _check_database_write),
            ("adb", _check_adb),
            ("ssh", _check_ssh),
            ("automation_worker", _check_automation_worker),
            ("local_worker", _check_local_worker),
        ):
            try:
                checks[name] = check()
            except Exception as exc:
                checks[name] = {"ok": False, "error": str(exc)}
        checks["runtime_queues"] = _check_runtime_queues(app)
        result = {
            "ready": all(bool(item.get("ok")) for item in checks.values()),
            "checks": checks,
            "checked_at": time.time(),
        }
        _CACHE["result"] = result
        _CACHE["expires_at"] = now + cache_seconds
        return dict(result)


def reset_health_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.update({"expires_at": 0.0, "result": None})
