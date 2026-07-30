"""Shared device lock facade backed by the global device claim registry."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from features.auth import auth_service
from foundation.config import settings
from foundation.device_claims import DeviceClaimRegistry


LOCK_TTL_SECONDS = 3600


class DeviceLockManager:
    """Compatibility facade for local-device callers.

    Claims are keyed by ``local_worker_id + serial`` so local UI/test/firmware
    calls conflict atomically with Controller leases for the local Worker.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        local_worker_id: str = "worker-local",
    ):
        self.db_path = Path(db_path) if db_path is not None else None
        self.local_worker_id = local_worker_id
        self.registry = DeviceClaimRegistry(self.db_path)

    def configure_local_worker(self, worker_id: str) -> None:
        self.local_worker_id = str(worker_id or "worker-local").strip() or "worker-local"

    def _device(self, serial: str) -> dict[str, str]:
        serial = str(serial or "").strip()
        return {
            "device_key": f"{self.local_worker_id}:{serial}",
            "worker_id": self.local_worker_id,
            "serial": serial,
        }

    def _display_username(self, client_id: str, username: str | None) -> str:
        cleaned = str(username or "").strip()
        with contextlib.suppress(Exception):
            from features.users import resolve_client_display_id

            return resolve_client_display_id(client_id, cleaned)
        if cleaned and cleaned not in {"unknown", client_id}:
            return cleaned
        with contextlib.suppress(Exception):
            for user in auth_service.list_users():
                if str(user.get("id") or "") == str(client_id):
                    return str(user.get("username") or user.get("display_name") or client_id)
        return cleaned or client_id

    @staticmethod
    def _source_id(client_id: str, source_id: str | None) -> str:
        return str(source_id or f"local:{client_id}").strip()

    def lock_device(
        self,
        device_id: str,
        client_id: str,
        username: str = "unknown",
        *,
        source_id: str | None = None,
        source_type: str = "local",
        ttl_seconds: int = LOCK_TTL_SECONDS,
    ) -> tuple[bool, str | None]:
        source = self._source_id(client_id, source_id)
        success, records = self.registry.acquire(
            [self._device(device_id)],
            owner_id=client_id,
            username=username,
            source_type=source_type,
            source_id=source,
            ttl_seconds=ttl_seconds,
        )
        if success:
            return True, f"设备 {device_id} 锁定成功"
        conflict = records[0]
        owner = self._display_username(conflict["owner_id"], conflict["username"])
        return False, f"设备 {device_id} 已被 {owner} 锁定"

    def lock_devices(
        self,
        device_ids: list[str],
        client_id: str,
        username: str = "unknown",
        *,
        source_id: str,
        source_type: str,
        ttl_seconds: int = LOCK_TTL_SECONDS,
        allow_existing_source: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        return self.registry.acquire(
            [self._device(device_id) for device_id in device_ids],
            owner_id=client_id,
            username=username,
            source_type=source_type,
            source_id=source_id,
            ttl_seconds=ttl_seconds,
            allow_existing_source=allow_existing_source,
        )

    def unlock_device(
        self,
        device_id: str,
        client_id: str,
        *,
        source_id: str | None = None,
    ) -> tuple[bool, str | None]:
        device = self._device(device_id)
        claim = self.registry.active_claim(device["device_key"])
        if not claim:
            return True, f"设备 {device_id} 未锁定"
        if claim["owner_id"] != client_id:
            return False, f"设备 {device_id} 被其他用户锁定，无法解锁"
        source = self._source_id(client_id, source_id)
        if source_id and claim["source_id"] != source:
            return False, f"设备 {device_id} 属于另一个运行中的操作"
        self.registry.release(claim["source_id"], device_keys=[device["device_key"]])
        return True, f"设备 {device_id} 解锁成功"

    def force_unlock_device(self, device_id: str) -> tuple[bool, str | None]:
        device_key = self._device(device_id)["device_key"]
        active = self.registry.active_claim(device_key)
        if active and (
            str(active.get("source_type") or "").startswith("cluster-")
            or str(active.get("source_id") or "").startswith(("job:", "reservation:"))
        ):
            return (
                False,
                f"设备 {device_id} 正由集群任务或预约占用，请先取消对应任务或预约",
            )
        claim = self.registry.force_release(device_key)
        if not claim:
            return True, f"设备 {device_id} 未锁定"
        return True, f"设备 {device_id} 已强制释放"

    def get_lock_status(self, device_id: str) -> dict[str, Any] | None:
        claim = self.registry.active_claim(self._device(device_id)["device_key"])
        if not claim:
            return None
        username = self._display_username(claim["owner_id"], claim["username"])
        return {
            "device_id": device_id,
            "locked": True,
            "locked_by": username,
            "client_id": claim["owner_id"],
            "username": username,
            "locked_at": claim["acquired_at"],
            "source_id": claim["source_id"],
            "source_type": claim["source_type"],
            "lease_id": claim["id"],
            "generation": claim["generation"],
        }

    def get_all_locks(self) -> dict[str, dict[str, Any]]:
        records = self.registry.list_active(worker_id=self.local_worker_id)
        return {
            row["serial"]: {
                "device_id": row["serial"],
                "client_id": row["owner_id"],
                "username": self._display_username(row["owner_id"], row["username"]),
                "timestamp": row["acquired_at"],
                "source_id": row["source_id"],
                "source_type": row["source_type"],
                "lease_id": row["id"],
                "generation": row["generation"],
            }
            for row in records
        }

    def refresh_locks(
        self,
        client_id: str,
        device_ids: list[str],
        *,
        source_id: str | None = None,
    ) -> int:
        source = self._source_id(client_id, source_id)
        owned = {
            row["serial"]
            for row in self.registry.list_active(
                worker_id=self.local_worker_id,
                owner_id=client_id,
            )
            if row["source_id"] == source
        }
        requested = {str(item).strip() for item in device_ids if str(item).strip()}
        renewable = requested & owned
        if not renewable:
            return 0
        return self.registry.renew(
            source,
            LOCK_TTL_SECONDS,
            device_keys=[self._device(item)["device_key"] for item in renewable],
        )

    def unlock_all(self, client_id: str, *, source_id: str | None = None) -> int:
        records = self.registry.list_active(
            worker_id=self.local_worker_id,
            owner_id=client_id,
        )
        if source_id:
            records = [row for row in records if row["source_id"] == source_id]
        released = 0
        for source in {row["source_id"] for row in records}:
            released += self.registry.release(source)
        return released


device_lock_manager = DeviceLockManager(
    settings.data_root / "device_claims.sqlite3",
)
