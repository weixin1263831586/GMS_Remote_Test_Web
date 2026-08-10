"""通过后台心跳将 Controller 主机注册为本地 Worker。"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker_agent.inventory import probe_devices
from worker_agent.process_inventory import discover_tradefed_processes
from worker_agent.suite_detection import suite_details

from .config import ClusterConfig
from .repository import ClusterRepository


logger = logging.getLogger(__name__)
AGENT_VERSION = "controller-0.1.0"


def _probe_devices() -> list[dict[str, Any]]:
    """Return locally attached ADB / Fastboot devices in Worker format."""
    return probe_devices(include_details=True)


def _scan_suites(roots: list[Path]) -> list[dict[str, Any]]:
    suites: list[dict[str, Any]] = []
    seen: set[str] = set()
    names = {"cts-tradefed", "gts-tradefed", "vts-tradefed", "sts-tradefed"}
    for root in roots:
        if not root.exists():
            continue
        for current, dirs, files in os.walk(root):
            depth = len(Path(current).relative_to(root).parts)
            if depth > 5:
                dirs[:] = []
                continue
            for filename in names.intersection(files):
                executable = Path(current) / filename
                tools_path = str(executable.parent)
                if tools_path in seen:
                    continue
                seen.add(tools_path)
                suite_type, version = suite_details(executable)
                suites.append({
                    "suite_type": suite_type,
                    "suite_version": version,
                    "suite_key": f"{suite_type}:{version or executable.parent.parent.name}",
                    "tools_path": tools_path,
                    "checksum": "",
                    "size_bytes": 0,
                    "available": os.access(executable, os.X_OK),
                })
    return suites


def _host_metrics() -> dict[str, float]:
    usage = shutil.disk_usage(Path.home())
    memory_percent = 0.0
    memory_total_gb = 0.0
    memory_available_gb = 0.0
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        memory_percent = 100 * (1 - values["MemAvailable"] / values["MemTotal"])
        memory_total_gb = values["MemTotal"] / 1024 ** 2
        memory_available_gb = values["MemAvailable"] / 1024 ** 2
    except Exception:
        pass
    try:
        load = os.getloadavg()[0]
        cpu_percent = min(100.0, load * 100 / max(1, os.cpu_count() or 1))
    except OSError:
        cpu_percent = 0.0
    return {
        "cpu_percent": round(cpu_percent, 2),
        "memory_percent": round(memory_percent, 2),
        "memory_total_gb": round(memory_total_gb, 2),
        "memory_available_gb": round(memory_available_gb, 2),
        "load_1m": round(load if 'load' in locals() else 0.0, 2),
        "disk_free_gb": round(usage.free / 1024 ** 3, 2),
    }


def _suite_roots() -> list[Path]:
    from foundation.config import config_manager

    config = config_manager.load_config()
    ubuntu_user = config_manager.get_ubuntu_user(config)
    default = Path(f"/home/{ubuntu_user}/GMS-Suite")
    configured = Path(config.get("suites_path") or default)
    return list({configured, default, Path("/opt/GMS-Suite"),
                 Path.home() / "GMS-Suite"})


class LocalWorkerBridge:
    """Registers the Controller host as ``ats-worker-controller`` and heartbeats it."""

    def __init__(self, repository: ClusterRepository, config: ClusterConfig):
        self.repository = repository
        self.config = config
        self._suites: list[dict[str, Any]] = []
        self._last_suite_scan = 0.0
        self._suite_scan_interval = 300.0
        self._device_interval = 10.0
        self._heartbeat_interval = 15.0
        self._initial_heartbeat_delay = 1.0
        self._registered = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        self._start_pending = False

    @property
    def worker_id(self) -> str:
        return self.config.local_worker_id

    def _registration(self) -> dict[str, Any]:
        from foundation.config import config_manager
        from worker_agent.adb_proxy import capability_status

        config = config_manager.load_config()
        ubuntu_user = config_manager.get_ubuntu_user(config)
        ubuntu_host = config_manager.get_ubuntu_host(config) or socket.gethostname()
        adb_proxy = capability_status()
        return {
            "worker_id": self.worker_id,
            "name": self.worker_id,
            "hostname": socket.gethostname(),
            "address": ubuntu_host,
            "agent_version": AGENT_VERSION,
            "max_jobs": int(os.getenv("GMS_LOCAL_WORKER_MAX_JOBS", str(ClusterConfig.load().default_max_jobs))),
            "capabilities": {
                "adb": True, "fastboot": True, "tradefed": True,
                "cts": True, "gts": True, "vts": True, "sts": True,
                "adb_proxy": bool(adb_proxy.get("installed")),
                "adb_proxy_version": str(adb_proxy.get("version") or ""),
                "adb_proxy_logs": True,
                "ssh_user": ubuntu_user or getpass.getuser(),
                "novnc_port": int(os.getenv("GMS_NOVNC_PORT", "6080")),
            },
        }

    def _register(self) -> None:
        if self._real_agent_active():
            return
        data = self._registration()
        self.repository.register_worker(data)
        self._registered = True
        logger.info("Local worker bridge registered as %s", self.worker_id)

    def _real_agent_active(self) -> bool:
        worker = self.repository.get_worker(self.worker_id)
        if not worker or str(worker.get("agent_version", "")).startswith("controller-"):
            return False
        try:
            heartbeat = datetime.fromisoformat(
                str(worker["last_heartbeat_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            return False
        return (datetime.now(timezone.utc) - heartbeat).total_seconds() <= 45

    def _heartbeat(self) -> None:
        if self._real_agent_active():
            return
        from features.devices import local_proxy_secret
        from worker_agent.adb_proxy import execute_adb_proxy_action, recover_managed_state

        try:
            recovery = recover_managed_state(secret=local_proxy_secret())
            if recovery["recovered"]:
                logger.info(
                    "recovered local ADB Proxy roles: %s",
                    ", ".join(recovery["recovered"]),
                )
            if recovery["errors"]:
                logger.debug(
                    "local ADB Proxy recovery pending: %s",
                    "; ".join(recovery["errors"]),
                )
        except Exception:
            logger.warning("local ADB Proxy recovery failed", exc_info=True)
        now_mono = time.monotonic()
        include_suites = (not self._suites
                          or now_mono - self._last_suite_scan >= self._suite_scan_interval)
        if include_suites:
            self._suites = _scan_suites(_suite_roots())
            self._last_suite_scan = now_mono
        try:
            adb_proxy_status = execute_adb_proxy_action("status")
        except Exception as exc:
            logger.warning("local ADB Proxy status probe failed: %s", exc)
            adb_proxy_status = {
                "transport_state": "failed",
                "protocol_state": "unknown",
                "readiness": "not_ready",
                "error": str(exc),
            }
        payload: dict[str, Any] = {
            "agent_version": AGENT_VERSION,
            **_host_metrics(),
            "running_jobs": discover_tradefed_processes(),
            "devices": _probe_devices(),
            "adb_proxy": adb_proxy_status,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if include_suites:
            payload["suites"] = self._suites
        result = self.repository.heartbeat(self.worker_id, payload)
        try:
            from features.devices import get_adb_proxy_service

            get_adb_proxy_service().observe_worker(
                self.worker_id, payload.get("adb_proxy") or {}
            )
        except Exception:
            logger.warning("local ADB Proxy status reconcile failed", exc_info=True)
        if result is None:
            self._registered = False

    def _loop(self) -> None:
        if self._stop.wait(self._initial_heartbeat_delay):
            return
        while not self._stop.is_set():
            try:
                if self._real_agent_active():
                    self._stop.wait(self._heartbeat_interval)
                    continue
                if not self._registered:
                    self._register()
                self._heartbeat()
            except Exception:
                logger.exception("local worker bridge iteration failed")
                self._registered = False
            self._stop.wait(self._heartbeat_interval)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name=f"LocalWorkerBridge:{self.worker_id}",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 20.0) -> bool:
        self._stop.set()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        stopped = not thread.is_alive()
        if stopped:
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
        else:
            logger.warning("Local worker bridge did not stop within %.1fs", timeout)
        return stopped

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def is_start_pending(self) -> bool:
        with self._lifecycle_lock:
            return self._start_pending


_bridge: LocalWorkerBridge | None = None
_bridge_lock = threading.Lock()


def _finish_bridge_replacement(
    previous: LocalWorkerBridge,
    current: LocalWorkerBridge,
) -> None:
    while not previous.stop(timeout=20.0):
        pass
    with _bridge_lock:
        with current._lifecycle_lock:
            current._start_pending = False
        if _bridge is current and not current._stop.is_set():
            current.start()


def start_local_bridge(repository: ClusterRepository, config: ClusterConfig) -> LocalWorkerBridge:
    global _bridge
    with _bridge_lock:
        if (
            _bridge is not None
            and _bridge.repository is repository
            and _bridge.config == config
            and not _bridge._stop.is_set()
            and (_bridge.is_running or _bridge.is_start_pending)
        ):
            return _bridge
        previous = _bridge
        current = LocalWorkerBridge(repository, config)
        _bridge = current
        if previous is not None and not previous.stop(timeout=0.25):
            with current._lifecycle_lock:
                current._start_pending = True
            threading.Thread(
                target=_finish_bridge_replacement,
                args=(previous, current),
                name="LocalWorkerBridgeHandoff",
                daemon=True,
            ).start()
            return current
        current.start()
        return current


def stop_local_bridge() -> None:
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            if _bridge.stop(timeout=5.0):
                _bridge = None
