"""Durable device reservations spanning ATS flash and test stages."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(60, seconds))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class ClusterReservationRepositoryMixin:
    def _expire_device_reservations(self, conn, now: str | None = None) -> int:
        now = now or _now()
        rows = conn.execute(
            """SELECT reservation_id,device_id FROM cluster_device_reservations
               WHERE status='active' AND expires_at<=?""",
            (now,),
        ).fetchall()
        if not rows:
            return 0
        conn.execute(
            """UPDATE cluster_device_reservations
               SET status='expired',released_at=?
               WHERE status='active' AND expires_at<=?""",
            (now, now),
        )
        for row in rows:
            conn.execute(
                """UPDATE cluster_worker_devices SET state='available',updated_at=?
                   WHERE id=? AND state='reserved'
                     AND NOT EXISTS(SELECT 1 FROM device_leases
                                    WHERE device_id=? AND status='active')
                     AND NOT EXISTS(SELECT 1 FROM cluster_device_reservations
                                    WHERE device_id=? AND status='active')""",
                (now, row["device_id"], row["device_id"], row["device_id"]),
            )
        reservation_ids = {
            row["reservation_id"]
            for row in conn.execute(
                """SELECT DISTINCT reservation_id
                   FROM cluster_device_reservations
                   WHERE status='expired' AND released_at=?""",
                (now,),
            ).fetchall()
        }
        for reservation_id in reservation_ids:
            self.claims.release(
                f"reservation:{reservation_id}",
                status="expired",
            )
        return len(rows)

    def reserve_devices(
        self,
        worker_id: str,
        devices: list[str],
        *,
        owner_id: str,
        source_id: str,
        ttl_seconds: int = 6 * 60 * 60,
    ) -> dict[str, Any]:
        requested = []
        for raw in devices:
            value = str(raw).strip()
            if not value:
                continue
            device_id = value if value.startswith(f"{worker_id}:") else f"{worker_id}:{value}"
            if device_id not in requested:
                requested.append(device_id)
        if not requested:
            raise ValueError("at least one device is required")
        now = _now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_device_reservations(conn, now)
            existing = conn.execute(
                """SELECT * FROM cluster_device_reservations
                   WHERE source_id=? AND status='active' ORDER BY device_id""",
                (source_id,),
            ).fetchall()
            existing_ids = [row["device_id"] for row in existing]
            if existing and existing_ids == sorted(requested) and all(
                row["worker_id"] == worker_id and row["owner_id"] == owner_id
                for row in existing
            ):
                reservation_id = existing[0]["reservation_id"]
                conn.execute(
                    """UPDATE cluster_device_reservations
                       SET heartbeat_at=?,expires_at=?
                       WHERE reservation_id=? AND status='active'""",
                    (now, _expires(ttl_seconds), reservation_id),
                )
                claim_devices = self._claim_devices(worker_id, existing_ids)
                acquired, conflicts = self.claims.acquire(
                    claim_devices,
                    owner_id=owner_id,
                    username=owner_id,
                    source_type="cluster-reservation",
                    source_id=f"reservation:{reservation_id}",
                    ttl_seconds=ttl_seconds,
                )
                if not acquired:
                    owner = conflicts[0].get("username") or conflicts[0].get("owner_id")
                    raise ValueError(f"device is already claimed by {owner}")
                return self._reservation_payload(conn, reservation_id)
            if existing:
                raise ValueError("automation run already has a different active reservation")

            rows = []
            for device_id in requested:
                device = conn.execute(
                    "SELECT * FROM cluster_worker_devices WHERE id=? AND worker_id=?",
                    (device_id, worker_id),
                ).fetchone()
                if device is None:
                    raise ValueError(f"device not found on worker: {device_id}")
                if device["state"] != "available":
                    raise ValueError(f"device is not available: {device['serial']}")
                if conn.execute(
                    "SELECT 1 FROM device_leases WHERE device_id=? AND status='active'",
                    (device_id,),
                ).fetchone():
                    raise ValueError(f"device is already leased: {device['serial']}")
                rows.append(device)

            reservation_id = f"reservation-{uuid.uuid4().hex}"
            claim_devices = self._claim_devices(
                worker_id, [row["id"] for row in rows]
            )
            acquired, conflicts = self.claims.acquire(
                claim_devices,
                owner_id=owner_id,
                username=owner_id,
                source_type="cluster-reservation",
                source_id=f"reservation:{reservation_id}",
                ttl_seconds=ttl_seconds,
            )
            if not acquired:
                owner = conflicts[0].get("username") or conflicts[0].get("owner_id")
                raise ValueError(f"device is already claimed by {owner}")
            try:
                for device in rows:
                    conn.execute(
                        """INSERT INTO cluster_device_reservations
                           (id,reservation_id,device_id,worker_id,serial,owner_id,source_id,
                            status,acquired_at,heartbeat_at,expires_at,released_at)
                           VALUES(?,?,?,?,?,?,?,'active',?,?,?,'')""",
                        (
                            f"reservation-item-{uuid.uuid4().hex}", reservation_id,
                            device["id"], worker_id, device["serial"], owner_id,
                            source_id, now, now, _expires(ttl_seconds),
                        ),
                    )
                    conn.execute(
                        "UPDATE cluster_worker_devices SET state='reserved',updated_at=? WHERE id=?",
                        (now, device["id"]),
                    )
                return self._reservation_payload(conn, reservation_id)
            except Exception:
                self.claims.release(
                    f"reservation:{reservation_id}", status="failed"
                )
                raise

    @staticmethod
    def _reservation_payload(conn, reservation_id: str) -> dict[str, Any]:
        rows = conn.execute(
            """SELECT * FROM cluster_device_reservations
               WHERE reservation_id=? ORDER BY device_id""",
            (reservation_id,),
        ).fetchall()
        if not rows:
            return {}
        return {
            "id": reservation_id,
            "worker_id": rows[0]["worker_id"],
            "owner_id": rows[0]["owner_id"],
            "source_id": rows[0]["source_id"],
            "status": rows[0]["status"],
            "expires_at": min(row["expires_at"] for row in rows),
            "devices": [
                {"id": row["device_id"], "serial": row["serial"]}
                for row in rows
            ],
        }

    def get_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            payload = self._reservation_payload(conn, reservation_id)
        return payload or None

    def get_reservation_by_source(self, source_id: str) -> dict[str, Any] | None:
        if not source_id:
            return None
        now = _now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_device_reservations(conn, now)
            row = conn.execute(
                """SELECT reservation_id FROM cluster_device_reservations
                   WHERE source_id=? AND status='active' ORDER BY acquired_at DESC LIMIT 1""",
                (source_id,),
            ).fetchone()
            payload = self._reservation_payload(conn, row["reservation_id"]) if row else {}
        return payload or None

    def renew_reservation(self, reservation_id: str, ttl_seconds: int = 6 * 60 * 60) -> bool:
        now = _now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_device_reservations(conn, now)
            rows = conn.execute(
                """SELECT * FROM cluster_device_reservations
                   WHERE reservation_id=? AND status='active'
                   ORDER BY device_id""",
                (reservation_id,),
            ).fetchall()
            if not rows:
                return False
            acquired, _conflicts = self.claims.acquire(
                self._claim_devices(
                    rows[0]["worker_id"], [row["device_id"] for row in rows]
                ),
                owner_id=rows[0]["owner_id"],
                username=rows[0]["owner_id"],
                source_type="cluster-reservation",
                source_id=f"reservation:{reservation_id}",
                ttl_seconds=ttl_seconds,
            )
            if not acquired:
                conn.execute(
                    """UPDATE cluster_device_reservations
                       SET status='expired',released_at=?
                       WHERE reservation_id=? AND status='active'""",
                    (now, reservation_id),
                )
                for row in rows:
                    conn.execute(
                        """UPDATE cluster_worker_devices
                           SET state='available',updated_at=?
                           WHERE id=? AND state='reserved'
                             AND NOT EXISTS(SELECT 1 FROM device_leases
                                            WHERE device_id=? AND status='active')""",
                        (now, row["device_id"], row["device_id"]),
                    )
                return False
            conn.execute(
                """UPDATE cluster_device_reservations
                   SET heartbeat_at=?,expires_at=?
                   WHERE reservation_id=? AND status='active'""",
                (now, _expires(ttl_seconds), reservation_id),
            )
            return True

    def release_reservation(self, reservation_id: str, status: str = "released") -> bool:
        if status not in {"released", "cancelled", "converted", "expired"}:
            raise ValueError("invalid reservation release status")
        now = _now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT device_id FROM cluster_device_reservations
                   WHERE reservation_id=? AND status='active'""",
                (reservation_id,),
            ).fetchall()
            if not rows:
                return False
            conn.execute(
                """UPDATE cluster_device_reservations SET status=?,released_at=?
                   WHERE reservation_id=? AND status='active'""",
                (status, now, reservation_id),
            )
            for row in rows:
                conn.execute(
                    """UPDATE cluster_worker_devices SET state='available',updated_at=?
                       WHERE id=? AND state='reserved'
                         AND NOT EXISTS(SELECT 1 FROM device_leases
                                        WHERE device_id=? AND status='active')
                         AND NOT EXISTS(SELECT 1 FROM cluster_device_reservations
                                        WHERE device_id=? AND status='active')""",
                    (now, row["device_id"], row["device_id"], row["device_id"]),
                )
        self.claims.release(
            f"reservation:{reservation_id}",
            status=status,
        )
        return True
