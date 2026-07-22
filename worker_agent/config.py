from __future__ import annotations

import json
import os
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class WorkerConfig:
    worker_id: str
    controller_url: str
    token: str
    name: str = ""
    address: str = ""
    ssh_user: str = ""
    controller_ca: str = ""
    heartbeat_interval: int = 15
    suite_scan_interval: int = 300
    max_jobs: int = 1
    data_root: Path = Path.home() / "gms-worker-data"
    suite_roots: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls) -> WorkerConfig:
        config_path = Path(os.getenv("GMS_WORKER_CONFIG", Path.home() / ".config/gms-worker/config.json"))
        raw = {}
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        token = os.getenv("GMS_WORKER_TOKEN", "")
        token_file = Path(raw.get("worker_token_file", "")) if raw.get("worker_token_file") else None
        if not token and token_file and token_file.exists():
            if stat.S_IMODE(token_file.stat().st_mode) & 0o077:
                raise RuntimeError("worker token file permissions must be 0600")
            token = token_file.read_text(encoding="utf-8").strip()
        worker_id = os.getenv("GMS_WORKER_ID", raw.get("worker_id", ""))
        controller_url = os.getenv("GMS_CONTROLLER_URL", raw.get("controller_url", ""))
        if not worker_id or not controller_url or not token:
            raise RuntimeError("worker_id, controller_url and worker token are required")
        parsed_controller = urlsplit(controller_url)
        if parsed_controller.scheme not in {"http", "https"} or not parsed_controller.hostname:
            raise RuntimeError("controller_url must be an absolute HTTP(S) URL")
        production = os.getenv("GMS_ENV", "development").strip().lower() == "production"
        if production and parsed_controller.scheme != "https":
            raise RuntimeError("production Workers require an HTTPS Controller URL")
        controller_ca = os.getenv("GMS_CONTROLLER_CA", raw.get("controller_ca", ""))
        if controller_ca and not Path(controller_ca).is_file():
            raise RuntimeError("configured Controller CA file does not exist")
        roots = raw.get("suite_roots") or [str(Path.home() / "GMS-Suite"), "/opt/GMS-Suite"]
        return cls(
            worker_id=worker_id,
            controller_url=controller_url.rstrip("/"),
            token=token,
            name=raw.get("name") or socket.gethostname(),
            address=os.getenv("GMS_WORKER_ADDRESS", raw.get("address", "")),
            ssh_user=os.getenv("GMS_WORKER_SSH_USER", raw.get("ssh_user", "")),
            controller_ca=controller_ca,
            heartbeat_interval=int(raw.get("heartbeat_interval_seconds", 15)),
            suite_scan_interval=int(raw.get("suite_scan_interval_seconds", 300)),
            max_jobs=int(raw.get("max_jobs", 1)),
            data_root=Path(raw.get("data_root", Path.home() / "gms-worker-data")),
            suite_roots=[Path(item).expanduser() for item in roots],
        )
