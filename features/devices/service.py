from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import Any

from .locks import DeviceLockManager


class DeviceService:
    """Coordinate device selection, locking, and operation cleanup."""

    def __init__(self, *, lock_manager: DeviceLockManager):
        self.lock_manager = lock_manager

    def run_locked(
        self,
        device_ids: Iterable[str],
        *,
        client_id: str,
        username: str,
        operation: Callable[[str], Any],
    ) -> dict[str, Any]:
        selected = list(dict.fromkeys(
            str(device_id or "").strip()
            for device_id in device_ids
            if str(device_id or "").strip()
        ))
        source_id = f"operation:{client_id}:{uuid.uuid4().hex}"
        acquired: list[str] = []
        for device_id in selected:
            success, message = self.lock_manager.lock_device(
                device_id,
                client_id,
                username,
                source_id=source_id,
                source_type="device-operation",
            )
            if not success:
                self._release(acquired, client_id, source_id)
                return {
                    "success": False,
                    "error": message,
                    "device": device_id,
                    "results": [],
                }
            acquired.append(device_id)

        try:
            results = [
                {"device": device_id, "result": operation(device_id)}
                for device_id in selected
            ]
            return {"success": True, "results": results}
        finally:
            self._release(acquired, client_id, source_id)

    def _release(
        self,
        device_ids: Iterable[str],
        client_id: str,
        source_id: str,
    ) -> None:
        for device_id in device_ids:
            self.lock_manager.unlock_device(
                device_id,
                client_id,
                source_id=source_id,
            )
