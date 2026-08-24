"""Liveness and cached readiness probes for production operation."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from foundation.automation_port import get_worker_status
from foundation.config import settings


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "expires_at": 0.0, "result": None}
_REQUIRED_CHECKS = frozenset({
    "storage",
    "database",
    "automation_worker",
    "local_worker",
})


def _runtime_data_root(app: Any) -> Path:
    services = getattr(getattr(app, "state", None), "services", None)
    runtime_settings = getattr(services, "settings", None)
    configured = getattr(runtime_settings, "data_root", settings.data_root)
    return Path(configured).resolve()


def _check_storage(root: Path) -> dict[str, Any]:
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


def _check_database_write(root: Path) -> dict[str, Any]:
    path = root / "health/health.sqlite3"
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
        from foundation.cluster_port import get_cluster_service

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
    state = getattr(app, "state", None)
    queue = getattr(state, "usb_event_queue", None)
    dispatcher = getattr(state, "usb_dispatch_task", None)
    backlog = int(queue.qsize()) if queue is not None else 0
    maximum = int(os.getenv("GMS_HEALTH_MAX_USB_QUEUE", "1000"))
    dispatcher_running = bool(dispatcher is not None and not dispatcher.done())
    return {
        "ok": bool(queue is not None and dispatcher_running and backlog <= maximum),
        "initialized": queue is not None,
        "dispatcher_running": dispatcher_running,
        "usb_event_backlog": backlog,
        "maximum": maximum,
    }


def readiness(app: Any, *, force: bool = False) -> dict[str, Any]:
    """Return cached dependency checks without exposing their details publicly."""
    cache_seconds = max(1, int(os.getenv("GMS_HEALTH_CACHE_SECONDS", "10")))
    now = time.monotonic()
    data_root = _runtime_data_root(app)
    cache_key = (id(app), str(data_root))
    with _CACHE_LOCK:
        if (
            not force
            and _CACHE["key"] == cache_key
            and _CACHE["result"] is not None
            and now < _CACHE["expires_at"]
        ):
            return deepcopy(_CACHE["result"])

        checks: dict[str, dict[str, Any]] = {}
        for name, check in (
            ("storage", lambda: _check_storage(data_root)),
            ("database", lambda: _check_database_write(data_root)),
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
        failed_required = sorted(
            name
            for name in _REQUIRED_CHECKS
            if not bool(checks[name].get("ok"))
        )
        degraded_checks = sorted(
            name
            for name, item in checks.items()
            if name not in _REQUIRED_CHECKS and not bool(item.get("ok"))
        )
        result = {
            # Storage and persistence determine whether the control plane can
            # safely accept work. Missing peripheral capabilities are exposed
            # as degradation instead of taking the entire HTTP service out of
            # rotation; their own endpoints still reject unavailable actions.
            "ready": not failed_required,
            "failed_required_checks": failed_required,
            "degraded_checks": degraded_checks,
            "checks": checks,
            "checked_at": time.time(),
            "data_root": str(data_root),
        }
        _CACHE["key"] = cache_key
        _CACHE["result"] = deepcopy(result)
        _CACHE["expires_at"] = now + cache_seconds
        return deepcopy(result)


def reset_health_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.update({"key": None, "expires_at": 0.0, "result": None})
