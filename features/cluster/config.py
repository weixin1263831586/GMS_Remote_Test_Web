from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClusterConfig:
    enabled: bool = False
    remote_dispatch_enabled: bool = False
    global_device_pool_enabled: bool = False
    lease_enforcement_enabled: bool = False
    local_worker_id: str = "worker-local"
    worker_offline_seconds: int = 45
    lease_ttl_seconds: int = 90

    @classmethod
    def load(cls) -> "ClusterConfig":
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
        )
