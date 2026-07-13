from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .repository import ClusterRepository
from .config import ClusterConfig


class ClusterService:
    def __init__(self, repository: ClusterRepository, offline_seconds: int = 45,
                 config: ClusterConfig | None = None):
        self.repository = repository
        self.config = config or ClusterConfig(enabled=True, remote_dispatch_enabled=True,
                                              global_device_pool_enabled=True,
                                              lease_enforcement_enabled=True,
                                              worker_offline_seconds=offline_seconds)
        self.offline_seconds = self.config.worker_offline_seconds
        self._runtime_enabled: bool | None = None

    @property
    def effective_enabled(self) -> bool:
        """True when the runtime override or the config flag is set."""
        return self._runtime_enabled if self._runtime_enabled is not None else self.config.enabled

    def set_runtime_enabled(self, enabled: bool) -> None:
        """Toggle cluster mode at runtime without restarting the service."""
        self._runtime_enabled = enabled

    def list_workers(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        workers = self.repository.list_workers()
        for worker in workers:
            try:
                last = datetime.fromisoformat(worker["last_heartbeat_at"].replace("Z", "+00:00"))
                if (now - last).total_seconds() > self.offline_seconds:
                    was_offline = worker.get("status") == "offline"
                    worker["status"] = "offline"
                    if not was_offline:
                        self.repository.mark_worker_offline(worker["id"])
            except (KeyError, ValueError, TypeError):
                worker["status"] = "offline"
        return workers

    def select_worker(self, suite_key: str, device_count: int = 1) -> tuple[str, list[str]]:
        """Select a healthy worker with a local suite and enough devices."""
        suites_by_worker = {}
        for suite in self.repository.list_suites():
            if suite["available"] and (not suite_key or suite["suite_key"] == suite_key):
                suites_by_worker.setdefault(suite["worker_id"], []).append(suite)
        devices_by_worker = {}
        for device in self.repository.list_devices():
            if device["state"] == "available":
                devices_by_worker.setdefault(device["worker_id"], []).append(device)
        candidates = []
        for worker in self.list_workers():
            devices = devices_by_worker.get(worker["id"], [])
            if (worker["status"] not in {"online", "busy"}
                    or worker["id"] not in suites_by_worker
                    or len(devices) < device_count
                    or worker["running_jobs"] >= worker["max_jobs"]):
                continue
            disk_score = min(10.0, float(worker["disk_free_gb"]) / 50)
            load_score = max(0.0, 10 - float(worker["cpu_percent"]) / 10)
            score = 40 + min(20, len(devices) * 5) + 20 + disk_score + load_score
            candidates.append((score, worker["id"], devices))
        if not candidates:
            raise ValueError("no worker has the requested suite and available devices")
        _, worker_id, devices = max(candidates, key=lambda item: (item[0], item[1]))
        return worker_id, [item["id"] for item in devices[:device_count]]
