from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def configured_max_bytes(env_name: str, configured: int) -> int:
    """Resolve a positive capacity limit from product config, then env."""
    return max(1, int(os.getenv(env_name, str(configured))))


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool = False
    remote_dispatch_enabled: bool = False
    global_device_pool_enabled: bool = False
    lease_enforcement_enabled: bool = False
    local_worker_id: str = "worker-local"
    worker_offline_seconds: int = 45
    lease_ttl_seconds: int = 90
    worker_registration_timeout_seconds: int = 45
    artifact_max_bytes: int = 20 * 1024**3
    firmware_max_bytes: int = 20 * 1024**3
    transfer_max_bytes: int = 20 * 1024**3
    log_analysis_max_bytes: int = 5 * 1024**3
    default_max_jobs: int = 6

    @classmethod
    def load(cls) -> ClusterConfig:
        default_path = Path(__file__).resolve().parents[2] / "configs/cluster.json"
        path = Path(os.getenv("GMS_CLUSTER_CONFIG", default_path))
        raw = {}
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
        env_enabled = os.getenv("GMS_CLUSTER_ENABLED")
        enabled = (env_enabled.lower() in {"1", "true", "yes", "on"}
                   if env_enabled is not None else bool(raw.get("enabled", False)))
        return cls(
            enabled=enabled,
            remote_dispatch_enabled=enabled and bool(raw.get("remote_dispatch_enabled", False)),
            global_device_pool_enabled=enabled and bool(raw.get("global_device_pool_enabled", False)),
            lease_enforcement_enabled=enabled and bool(raw.get("lease_enforcement_enabled", False)),
            local_worker_id=str(raw.get("local_worker_id") or "worker-local"),
            worker_offline_seconds=max(15, int(raw.get("worker_offline_seconds", 45))),
            lease_ttl_seconds=max(30, int(raw.get("lease_ttl_seconds", 90))),
            worker_registration_timeout_seconds=max(
                15, int(raw.get("worker_registration_timeout_seconds", 45))
            ),
            artifact_max_bytes=max(
                1, int(raw.get("artifact_max_bytes", 20 * 1024**3))
            ),
            firmware_max_bytes=max(
                1, int(raw.get("firmware_max_bytes", 20 * 1024**3))
            ),
            transfer_max_bytes=max(
                1, int(raw.get("transfer_max_bytes", 20 * 1024**3))
            ),
            log_analysis_max_bytes=max(
                1, int(raw.get("log_analysis_max_bytes", 5 * 1024**3))
            ),
            default_max_jobs=max(1, min(32, int(raw.get("default_max_jobs", 6)))),
        )
