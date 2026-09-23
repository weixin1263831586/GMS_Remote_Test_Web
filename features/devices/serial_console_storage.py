"""Validation and atomic binding persistence for the serial console."""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.cluster_port import get_local_worker_id


logger = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 1_500_000
PORT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
SUPPORTED_NEWLINES = {"cr": "\r", "lf": "\n", "crlf": "\r\n", "none": ""}


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_serial_module():
    """Load pyserial lazily so a package install is picked up without restart."""
    try:
        return importlib.import_module("serial")
    except ImportError:
        raise RuntimeError("缺少 pyserial 依赖，请安装 requirements.txt") from None


def validate_port_key(port_key: str) -> str:
    value = str(port_key or "").strip()
    if not PORT_KEY_RE.fullmatch(value) or ".." in value:
        raise ValueError("无效的串口标识")
    return value


def validate_baudrate(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("波特率必须是整数")
    try:
        baudrate = int(value)
    except (TypeError, ValueError):
        raise ValueError("波特率必须是整数") from None
    if baudrate < 300 or baudrate > 4_000_000:
        raise ValueError("波特率必须在 300 到 4000000 之间")
    return baudrate


def validate_newline(value: Any) -> str:
    newline = str(value or "cr").lower()
    if newline not in SUPPORTED_NEWLINES:
        raise ValueError("换行模式必须是 cr、lf、crlf 或 none")
    return newline


class BindingStore:
    """Thread-safe JSON store with atomic replacement writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._bindings = self._load_unlocked()

    def _load_unlocked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Serial console bindings are unreadable; using an empty store")
            return {}
        bindings = value.get("bindings", value) if isinstance(value, dict) else {}
        return bindings if isinstance(bindings, dict) else {}

    def _save_unlocked(self, bindings: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    {"schema_version": 2, "bindings": bindings},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def list(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: self._normalized(value)
                for key, value in self._bindings.items()
                if isinstance(value, dict)
            }

    @staticmethod
    def _normalized(value: dict[str, Any]) -> dict[str, Any]:
        """Expose legacy v1 labels as low-confidence device identities."""
        result = dict(value)
        explicit_device = str(result.get("device_id") or "").strip()
        legacy_device = str(result.get("label") or "").strip()
        result.setdefault("device_id", explicit_device or legacy_device)
        result.setdefault(
            "worker_id", get_local_worker_id() if result["device_id"] else ""
        )
        result.setdefault("binding_version", 1)
        result["identity_verified"] = bool(
            result.get("binding_version", 1) >= 2
            and explicit_device
            and str(result.get("worker_id") or "").strip()
        )
        return result

    def get(self, port_key: str) -> dict[str, Any] | None:
        binding = self.list().get(validate_port_key(port_key))
        return dict(binding) if binding else None

    def upsert(self, port_key: str, value: dict[str, Any]) -> dict[str, Any]:
        key = validate_port_key(port_key)
        device_id = str(value.get("device_id") or "").strip()[:200]
        worker_id = str(value.get("worker_id") or "").strip()[:120]
        if device_id and not worker_id:
            worker_id = get_local_worker_id()
        binding = {
            "label": str(value.get("label") or "").strip()[:120],
            "device_id": device_id,
            "worker_id": worker_id,
            "note": str(value.get("note") or "").strip()[:500],
            "baudrate": validate_baudrate(value.get("baudrate", DEFAULT_BAUDRATE)),
            "capture_enabled": bool(value.get("capture_enabled", False)),
            "newline": validate_newline(value.get("newline", "cr")),
            "updated_at": utc_now(),
            "binding_version": 2,
        }
        binding["identity_verified"] = bool(device_id and worker_id)
        with self._lock:
            self._bindings[key] = binding
            self._save_unlocked(self._bindings)
        return dict(binding)

    def delete(self, port_key: str) -> bool:
        key = validate_port_key(port_key)
        with self._lock:
            existed = key in self._bindings
            self._bindings.pop(key, None)
            if existed:
                self._save_unlocked(self._bindings)
            return existed
