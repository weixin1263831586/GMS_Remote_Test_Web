"""Operational health probes, health endpoints and authenticated metrics."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from features.auth import CurrentUser, require_role
from features.system.metrics import metrics_token, render_metrics
from foundation.automation_port import get_worker_status
from foundation.config import settings
from foundation.product import APPLICATION_VERSION, SERVICE_NAME
from foundation.security import bearer_token


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


router = APIRouter()


@router.get('/api/system/health')
async def health_check():
    """Compatibility liveness probe; dependency readiness has its own route."""
    return JSONResponse(content={
        'status': 'alive',
        'service': SERVICE_NAME,
        'version': APPLICATION_VERSION,
        'timestamp': datetime.now().isoformat(),
    })


@router.get('/api/system/health/live')
async def liveness_check():
    return {'status': 'alive', 'timestamp': datetime.now().isoformat()}


@router.get('/api/system/health/ready')
async def readiness_check(request: Request):
    result = await asyncio.to_thread(readiness, request.app)
    return JSONResponse(
        status_code=200 if result['ready'] else 503,
        content={
            'status': 'ready' if result['ready'] else 'not_ready',
            'degraded': bool(result['degraded_checks']),
            'timestamp': datetime.now().isoformat(),
        },
    )


@router.get('/api/system/health/details')
async def health_details(
    request: Request,
    _admin: CurrentUser = Depends(require_role('admin')),
):
    result = await asyncio.to_thread(readiness, request.app, force=True)
    return JSONResponse(
        status_code=200 if result['ready'] else 503,
        content=result,
    )


@router.get('/metrics', include_in_schema=False)
async def prometheus_metrics(
    authorization: str = Header(default='', alias='Authorization'),
):
    expected = metrics_token()
    if not expected:
        raise HTTPException(status_code=503, detail='GMS_METRICS_TOKEN is required')
    token = bearer_token(authorization)
    if (
        token is None
        # 摘要后比较：原始串比较在长度不同时常量时间保证不完整（泄露
        # 令牌长度），与 worker_auth 同口径。
        or not hmac.compare_digest(
            hashlib.sha256(token.encode()).digest(),
            hashlib.sha256(expected.encode()).digest(),
        )
    ):
        raise HTTPException(status_code=401, detail='Invalid metrics credential')
    return Response(
        content=await asyncio.to_thread(render_metrics),
        media_type='text/plain; version=0.0.4; charset=utf-8',
        headers={'Cache-Control': 'no-store'},
    )
