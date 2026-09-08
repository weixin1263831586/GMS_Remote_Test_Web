"""Unified device-claim helpers shared by Cluster repository workflows."""

from __future__ import annotations

import json
from typing import Any


class ClusterClaimRepositoryMixin:
    def resolve_worker_device(
        self, worker_id: str, value: str
    ) -> dict[str, Any] | None:
        """按 inventory 精确解析设备；serial 可能含 ":"，禁止前缀切分猜测。"""
        value = str(value or "").strip()
        if not value:
            return None
        for device in self.list_devices(worker_id):
            if value in {str(device.get("id") or ""), str(device.get("serial") or "")}:
                return device
        return None

    def _physical_alias_keys(self, device_key: str) -> list[str]:
        """R01: every claim key that routes to the same physical device.

        An ADB-Proxy alias row carries adb_proxy_source_worker_id /
        adb_proxy_source_serial pointing at the source worker's local-USB
        row; claiming through either route must contend for the same
        physical hardware. Returns the requested key itself plus every
        alias key found in the current inventory.
        """
        alias_keys = [device_key]
        with self.connect() as conn:
            row = conn.execute(
                "SELECT transport, properties_json FROM cluster_worker_devices WHERE id=?",
                (device_key,),
            ).fetchone()
            if row is None:
                return alias_keys
            try:
                properties = json.loads(row["properties_json"] or "{}")
            except json.JSONDecodeError:
                properties = {}
            transport = str(row["transport"] or "")
            if transport == "adb_proxy":
                source_worker = str(properties.get("adb_proxy_source_worker_id") or "")
                source_serial = str(properties.get("adb_proxy_source_serial") or "")
                if source_worker and source_serial:
                    alias_keys.append(f"{source_worker}:{source_serial}")
            else:
                source_serial = device_key.split(":", 1)[1] if ":" in device_key else ""
                proxy_rows = conn.execute(
                    "SELECT id, properties_json FROM cluster_worker_devices "
                    "WHERE transport='adb_proxy'",
                ).fetchall()
                for proxy_row in proxy_rows:
                    try:
                        proxy_properties = json.loads(proxy_row["properties_json"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    if (
                        str(proxy_properties.get("adb_proxy_source_worker_id") or "")
                        == device_key.split(":", 1)[0]
                        and str(proxy_properties.get("adb_proxy_source_serial") or "")
                        == source_serial
                    ):
                        alias_keys.append(str(proxy_row["id"]))
        return list(dict.fromkeys(alias_keys))

    def _claim_devices(
        self, worker_id: str, devices: list[str]
    ) -> list[dict[str, Any]]:
        claimed = []
        values = dict.fromkeys(
            str(item).strip() for item in devices if str(item).strip()
        )
        with self.connect() as conn:
            for value in values:
                device_key = (
                    value if value.startswith(f"{worker_id}:")
                    else f"{worker_id}:{value}"
                )
                # R01: claim the requested alias AND its physical sibling
                # keys in the same atomic acquire, so an operation claim on
                # the source device conflicts with a job on the proxy alias
                # (and vice versa) instead of letting both run at once.
                for key in self._physical_alias_keys(device_key):
                    previous = conn.execute(
                        """SELECT COALESCE(MAX(generation),0)+1
                           FROM device_leases WHERE device_id=?""",
                        (key,),
                    ).fetchone()[0]
                    key_worker, _, key_serial = key.partition(":")
                    claimed.append({
                        "device_key": key,
                        "worker_id": key_worker,
                        "serial": key_serial,
                        "generation_floor": int(previous or 1),
                    })
        return claimed

    def acquire_device_operation_claim(
        self,
        worker_id: str,
        devices: list[str],
        *,
        owner_id: str,
        source_type: str,
        source_id: str,
        ttl_seconds: int = 3600,
        username: str = "",
    ) -> list[dict[str, Any]]:
        acquired, records = self.claims.acquire(
            self._claim_devices(worker_id, devices),
            owner_id=owner_id,
            username=username or owner_id,
            source_type=source_type,
            source_id=source_id,
            ttl_seconds=ttl_seconds,
        )
        if not acquired:
            owner = records[0].get("username") or records[0].get("owner_id")
            raise ValueError(f"device is already claimed by {owner}")
        return records

    def renew_job_device_claim(
        self,
        job_id: str,
        owner_id: str,
        device_ids: list[str],
    ) -> bool:
        expected = {str(item) for item in device_ids if str(item)}
        if not expected:
            return False
        source_id = f"job:{job_id}"
        records = [self.claims.active_claim(device_id) for device_id in expected]
        if any(record is None for record in records):
            return False
        if any(
            record["source_id"] != source_id or record["owner_id"] != owner_id
            for record in records
        ):
            return False
        return self.claims.renew(
            source_id,
            self.claim_lease_ttl_seconds,
            device_keys=sorted(expected),
        ) == len(expected)

    @staticmethod
    def claim_fencing_tokens(
        records: list[dict[str, Any]], attempt_id: str
    ) -> list[dict[str, Any]]:
        return [
            {
                "lease_id": record["id"],
                "device_id": record["device_key"],
                "generation": record["generation"],
                "attempt_id": attempt_id,
            }
            for record in records
        ]
