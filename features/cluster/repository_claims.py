"""Unified device-claim helpers shared by Cluster repository workflows."""

from __future__ import annotations

from typing import Any


class ClusterClaimRepositoryMixin:
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
                previous = conn.execute(
                    """SELECT COALESCE(MAX(generation),0)+1
                       FROM device_leases WHERE device_id=?""",
                    (device_key,),
                ).fetchone()[0]
                claimed.append({
                    "device_key": device_key,
                    "worker_id": worker_id,
                    "serial": device_key[len(worker_id) + 1:],
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
    ) -> list[dict[str, Any]]:
        acquired, records = self.claims.acquire(
            self._claim_devices(worker_id, devices),
            owner_id=owner_id,
            username=owner_id,
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
