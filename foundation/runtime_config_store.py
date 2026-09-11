"""Partition global runtime configuration without changing its public JSON shape.

A process lock and an undo journal keep multi-file reads and writes consistent.
Owner-specific ConfigManager paths retain their existing isolated single file.
"""

from __future__ import annotations

import copy
import fcntl
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from foundation.config_paths import runtime_data_root
from foundation.private_config import read_json_object, write_private_json


DEPLOYMENT_KEYS = frozenset({
    "ubuntu_user", "ubuntu_host", "local_server", "use_key_auth", "private_key_path",
    "suites_path", "script_path", "gsi_scripts", "scrcpy_path", "install_host_ip",
    "install_port", "static_routes", "firmware_shares",
    "ssh_port", "ubuntu_port",
})
DEVICE_STATE_KEYS = frozenset({
    "usbip_devices_source", "usbip_cluster_assignments", "adb_proxy_assignments",
    "usbip_network_quality_history", "usbip_source_os",
})
CREDENTIAL_KEYS = frozenset({"client_ssh_credentials", "redmine_auth"})


def is_secret_field(key: str) -> bool:
    name = key.lower()
    if name.endswith("_static_config"):
        name = name.removesuffix("_static_config")
    if name.endswith(("_file", "_path", "_env")):
        return False
    return name in {"password", "token", "secret", "api_key", "authorization", "ubuntu_pswd"} or name.endswith(
        ("_password", "_pswd", "_token", "_secret", "_api_key", "_password_encrypted", "_api_key_encrypted")
    )


def _extract_secrets(value: dict) -> tuple[dict, dict]:
    public, private = {}, {}
    for key, item in value.items():
        if key in CREDENTIAL_KEYS or is_secret_field(key):
            private[key] = copy.deepcopy(item)
        elif isinstance(item, list) and _contains_secret(item):
            # Keep list identity/order intact: splitting by index would make
            # deletions and reordering accidentally attach credentials elsewhere.
            private[key] = copy.deepcopy(item)
        elif isinstance(item, dict):
            ordinary, secrets = _extract_secrets(item)
            public[key] = ordinary
            if secrets:
                private[key] = secrets
        else:
            public[key] = copy.deepcopy(item)
    return public, private


def _contains_secret(value) -> bool:
    if isinstance(value, dict):
        return any(key in CREDENTIAL_KEYS or is_secret_field(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def overlay_sections(base: dict, extra: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = overlay_sections(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def partition_runtime(payload: dict) -> dict[str, dict]:
    ordinary, credentials = _extract_secrets(payload)
    parts = {"preferences": {}, "deployment": {}, "devices": {}, "credentials": credentials}
    for key, value in ordinary.items():
        section = "deployment" if key in DEPLOYMENT_KEYS else "devices" if key in DEVICE_STATE_KEYS else "preferences"
        parts[section][key] = value
    return parts


class RuntimeConfigStore:
    def __init__(self, project_root: Path):
        data_root = runtime_data_root(project_root)
        self.paths = {
            "preferences": data_root / "settings/preferences.json",
            "deployment": project_root / "configs/local/deployment.json",
            "devices": data_root / "devices/runtime.json",
            "credentials": project_root / "configs/secrets/runtime_credentials.json",
        }
        self.journal = project_root / "configs/secrets/runtime-transaction.json"
        self.lock_path = project_root / "configs/secrets/runtime.lock"
        self._lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def locked(self):
        with self._lock:
            if getattr(self._local, "active", False):
                yield
                return
            self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                self._local.active = True
                self._recover()
                yield
            finally:
                self._local.active = False
                os.close(descriptor)

    def _recover(self) -> None:
        if self.journal.exists():
            previous = read_json_object(self.journal)
            if set(previous) != set(self.paths):
                raise ValueError("Invalid runtime configuration transaction journal")
            if any(value is not None and not isinstance(value, dict) for value in previous.values()):
                raise ValueError("Invalid runtime configuration transaction entry")
            for name, path in self.paths.items():
                value = previous[name]
                if value is None:
                    path.unlink(missing_ok=True)
                elif isinstance(value, dict):
                    write_private_json(path, value)
            self._finish_transaction()

    def _finish_transaction(self) -> None:
        self.journal.unlink()
        descriptor = os.open(self.journal.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read(self) -> dict[str, Any]:
        with self.locked():
            combined = {}
            for path in self.paths.values():
                combined = overlay_sections(combined, read_json_object(path))
            return combined

    def write(self, payload: dict[str, Any]) -> None:
        with self.locked():
            parts = partition_runtime(payload)
            previous = {name: read_json_object(path) if path.exists() else None for name, path in self.paths.items()}
            write_private_json(self.journal, previous)
            try:
                for name, path in self.paths.items():
                    write_private_json(path, parts[name])
            except Exception:
                self._recover()
                raise
            self._finish_transaction()

    def stamp(self) -> tuple[int, ...]:
        with self.locked():
            return tuple(path.stat().st_mtime_ns if path.exists() else 0 for path in self.paths.values())
