from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any

from .config import ClusterConfig
from .repository import ClusterRepository


class ClusterService:
    def __init__(self, repository: ClusterRepository, offline_seconds: int = 45,
                 config: ClusterConfig | None = None):
        self.repository = repository
        self.config = config or ClusterConfig(enabled=True, remote_dispatch_enabled=True,
                                              global_device_pool_enabled=True,
                                              lease_enforcement_enabled=True,
                                              worker_offline_seconds=offline_seconds)
        self.offline_seconds = self.config.worker_offline_seconds
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: threading.Thread | None = None

    def start_watchdog(self) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()

        def monitor() -> None:
            interval = max(5.0, min(15.0, self.offline_seconds / 3))
            while not self._watchdog_stop.wait(interval):
                try:
                    self.list_workers()
                    self.repository.fail_abandoned_worker_lost_jobs(
                        self.config.worker_lost_fail_seconds
                    )
                except Exception:
                    # The next pass retries; never terminate the watchdog on a
                    # transient SQLite or clock parsing failure.
                    continue

        self._watchdog_thread = threading.Thread(
            target=monitor,
            name="ClusterWorkerWatchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=1)
        self._watchdog_thread = None

    @property
    def effective_enabled(self) -> bool:
        """Whether this deployment exposes remote cluster capability."""
        return self.config.enabled

    def list_workers(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        workers = self.repository.list_workers()
        tests_by_worker: dict[str, list[dict[str, Any]]] = {}
        for item in self.repository.list_worker_tests():
            tests_by_worker.setdefault(item["worker_id"], []).append(item)
        minimum_disk = float(os.getenv("GMS_CLUSTER_MIN_DISK_FREE_GB", "50"))
        minimum_memory = float(os.getenv("GMS_CLUSTER_MIN_MEMORY_AVAILABLE_GB", "8"))
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
            warnings = [item["warning"] for item in tests_by_worker.get(worker["id"], [])
                        if item.get("warning")]
            disk_free = float(worker.get("disk_free_gb") or 0)
            memory_available = float(worker.get("memory_available_gb") or 0)
            if disk_free and disk_free < minimum_disk:
                warnings.append(
                    f"Only {disk_free:.1f} GB disk is free (admission threshold {minimum_disk:.1f} GB)"
                )
            if memory_available and memory_available < minimum_memory:
                warnings.append(
                    f"Only {memory_available:.1f} GB memory is available"
                )
            if int(worker.get("unknown_external_jobs") or 0):
                warnings.append("An external Tradefed process has no identifiable device; new tests are blocked")
            worker["warnings"] = list(dict.fromkeys(warnings))
            worker["admission_blocked"] = worker.get("status") in {"offline", "draining"}
        # All Worker selectors consume this directory directly or through
        # /api/cluster/hosts. Promote the Controller/Local Worker while the
        # stable sort preserves the repository order of every remote Worker.
        workers.sort(key=lambda worker: worker.get("id") != self.config.local_worker_id)
        return workers

    def select_worker(
        self,
        suite_key: str,
        device_count: int = 1,
        include_local: bool = True,
        require_agent: bool = False,
        excluded_transports: set[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Select a healthy worker with a local suite and enough devices."""
        blocked_transports = {
            str(value).strip().lower()
            for value in (excluded_transports or set())
            if str(value).strip()
        }
        suites_by_worker = {}
        for suite in self.repository.list_suites():
            if suite["available"] and (not suite_key or suite["suite_key"] == suite_key):
                suites_by_worker.setdefault(suite["worker_id"], []).append(suite)
        devices_by_worker = {}
        for device in self.repository.list_devices():
            if (
                device["state"] == "available"
                and str(device.get("transport") or "").strip().lower()
                not in blocked_transports
            ):
                devices_by_worker.setdefault(device["worker_id"], []).append(device)
        candidates = []
        minimum_disk = float(os.getenv("GMS_CLUSTER_MIN_DISK_FREE_GB", "50"))
        for worker in self.list_workers():
            devices = devices_by_worker.get(worker["id"], [])
            if (worker["status"] not in {"online", "busy"}
                    or (not include_local and worker["id"] == self.config.local_worker_id)
                    or (require_agent
                        and worker["id"] == self.config.local_worker_id
                        and str(worker.get("agent_version", "")).startswith("controller-"))
                    or worker["id"] not in suites_by_worker
                    or len(devices) < device_count
                    or worker["running_jobs"] >= worker["max_jobs"]
                    or (float(worker.get("disk_free_gb") or 0) > 0
                        and float(worker["disk_free_gb"]) < minimum_disk)):
                continue
            disk_score = min(10.0, float(worker["disk_free_gb"]) / 50)
            load_score = max(0.0, 10 - float(worker["cpu_percent"]) / 10)
            score = 40 + min(20, len(devices) * 5) + 20 + disk_score + load_score
            candidates.append((score, worker["id"], devices))
        if not candidates:
            raise ValueError("no worker has the requested suite and available devices")
        _, worker_id, devices = max(candidates, key=lambda item: (item[0], item[1]))
        return worker_id, [item["id"] for item in devices[:device_count]]

    def has_command_agent(self, worker_id: str) -> bool:
        """Return whether a Worker has an Agent that consumes queued commands."""
        worker = self.repository.get_worker(worker_id)
        if not worker or worker.get("status") not in {"online", "busy"}:
            return False
        return not (
            worker_id == self.config.local_worker_id
            and str(worker.get("agent_version", "")).startswith("controller-")
        )
