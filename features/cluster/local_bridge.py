"""Local Worker Bridge — keeps the Controller's own host registered as
``worker-local`` inside the cluster database.

Without this bridge the cluster tables never contain a row for the
Controller host.  That breaks single-host mode because the frontend
queries ``/api/cluster/workers``, ``/api/cluster/devices`` and
``/api/cluster/suites`` for the local worker but receives empty results,
causing the host selector, device table and suite list to malfunction.

The bridge runs as a daemon thread started by ``configure_cluster``.  It
performs an initial registration, then periodically sends a heartbeat
that refreshes devices, suites and host metrics so that the cluster API
is always consistent — whether only ``worker-local`` exists (single
host) or remote Workers are also registered (multi host).
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

from .config import ClusterConfig
from .repository import ClusterRepository

logger = logging.getLogger(__name__)
AGENT_VERSION = "controller-0.1.0"


def _probe_devices() -> list[dict[str, Any]]:
    """Return locally attached ADB / Fastboot devices in Worker format."""
    import subprocess

    def run(argv: list[str], timeout: int = 8) -> str:
        try:
            return subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    devices: list[dict[str, Any]] = []
    for line in run(["adb", "devices", "-l"]).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] not in {"device", "offline", "unauthorized"}:
            continue
        serial, adb_state = parts[0], parts[1]
        properties: dict[str, Any] = {}
        for item in parts[2:]:
            key, sep, value = item.partition(":")
            if sep:
                properties[key] = value
        devices.append({
            "serial": serial,
            "transport": "local_usb",
            "state": "available" if adb_state == "device" else adb_state,
            "properties": properties,
        })
    known = {item["serial"] for item in devices}
    for line in run(["fastboot", "devices"]).splitlines():
        serial = line.split()[0] if line.split() else ""
        if serial and serial not in known:
            devices.append({"serial": serial, "transport": "local_usb",
                            "state": "fastboot", "properties": {}})
    return devices


def _suite_details(path: Path) -> tuple[str, str]:
    lowered = str(path).lower()
    suite_type = next(
        (name for name in ("CTS", "GTS", "VTS", "STS")
         if f"{name.lower()}-tradefed" in path.name.lower()
         or f"android-{name.lower()}" in lowered),
        "XTS",
    )
    match = re.search(r"(?:android-)?(?:cts|gts|vts|sts)[-_]([0-9]+(?:_r[0-9]+)?)", lowered)
    return suite_type, match.group(1) if match else ""


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
                suite_type, version = _suite_details(executable)
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
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        memory_percent = 100 * (1 - values["MemAvailable"] / values["MemTotal"])
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
    """Registers the Controller host as ``worker-local`` and heartbeats it."""

    def __init__(self, repository: ClusterRepository, config: ClusterConfig):
        self.repository = repository
        self.config = config
        self._suites: list[dict[str, Any]] = []
        self._last_suite_scan = 0.0
        self._suite_scan_interval = 300.0
        self._device_interval = 10.0
        self._heartbeat_interval = 15.0
        self._registered = False
        self._stop = threading.Event()

    @property
    def worker_id(self) -> str:
        return self.config.local_worker_id

    def _registration(self) -> dict[str, Any]:
        from foundation.config import config_manager

        config = config_manager.load_config()
        ubuntu_user = config_manager.get_ubuntu_user(config)
        ubuntu_host = config_manager.get_ubuntu_host(config) or socket.gethostname()
        return {
            "worker_id": self.worker_id,
            "name": f"{ubuntu_user}@{ubuntu_host}",
            "hostname": socket.gethostname(),
            "address": ubuntu_host,
            "agent_version": AGENT_VERSION,
            "max_jobs": int(os.getenv("GMS_LOCAL_WORKER_MAX_JOBS", "2")),
            "capabilities": {
                "adb": True, "fastboot": True, "tradefed": True,
                "cts": True, "gts": True, "vts": True, "sts": True,
                "ssh_user": ubuntu_user or getpass.getuser(),
                "novnc_port": int(os.getenv("GMS_NOVNC_PORT", "6080")),
            },
        }

    def _register(self) -> None:
        data = self._registration()
        self.repository.register_worker(data)
        self._registered = True
        logger.info("Local worker bridge registered as %s", self.worker_id)

    def _heartbeat(self) -> None:
        now_mono = time.monotonic()
        include_suites = (not self._suites
                          or now_mono - self._last_suite_scan >= self._suite_scan_interval)
        if include_suites:
            self._suites = _scan_suites(_suite_roots())
            self._last_suite_scan = now_mono
        payload: dict[str, Any] = {
            "agent_version": AGENT_VERSION,
            **_host_metrics(),
            "running_jobs": [],
            "devices": _probe_devices(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if include_suites:
            payload["suites"] = self._suites
        result = self.repository.heartbeat(self.worker_id, payload)
        if result is None:
            self._registered = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._registered:
                    self._register()
                self._heartbeat()
            except Exception:
                logger.exception("local worker bridge iteration failed")
                self._registered = False
            self._stop.wait(self._heartbeat_interval)

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="LocalWorkerBridge",
                                  daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop.set()


_bridge: LocalWorkerBridge | None = None


def start_local_bridge(repository: ClusterRepository, config: ClusterConfig) -> LocalWorkerBridge:
    global _bridge
    if _bridge is not None:
        return _bridge
    _bridge = LocalWorkerBridge(repository, config)
    _bridge.start()
    return _bridge


def stop_local_bridge() -> None:
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
