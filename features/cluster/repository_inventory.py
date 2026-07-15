"""Worker inventory persistence helpers for the cluster repository."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class ClusterInventoryRepositoryMixin:
    @staticmethod
    def _migrate_worker_metrics(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(cluster_workers)")}
        additions = {
            "memory_total_gb": "REAL NOT NULL DEFAULT 0",
            "memory_available_gb": "REAL NOT NULL DEFAULT 0",
            "load_1m": "REAL NOT NULL DEFAULT 0",
            "external_jobs": "INTEGER NOT NULL DEFAULT 0",
            "unknown_external_jobs": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE cluster_workers ADD COLUMN {name} {definition}"
                )

    def _replace_devices(
        self,
        conn,
        worker_id: str,
        devices: list[dict],
        now: str,
        external_serials: set[str] | None = None,
    ) -> None:
        external_serials = external_serials or set()
        seen = []
        for device in devices:
            serial = device["serial"]
            transport = device.get("transport", "local_usb")
            device_id = f"{worker_id}:{serial}"
            reported_state = (
                "external_busy"
                if serial in external_serials
                else device.get("state", "available")
            )
            seen.append(device_id)
            conn.execute(
                """
                INSERT INTO cluster_worker_devices
                    (id,worker_id,serial,transport,state,properties_json,
                     first_seen_at,last_seen_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id,serial,transport) DO UPDATE SET
                    state=CASE WHEN EXISTS(
                        SELECT 1 FROM device_leases
                        WHERE device_id=excluded.id AND status='active'
                    ) THEN 'allocated'
                    WHEN EXISTS(
                        SELECT 1 FROM cluster_device_reservations
                        WHERE device_id=excluded.id AND status='active'
                    ) THEN 'reserved'
                    WHEN excluded.serial IN ({external_placeholders}) THEN 'external_busy'
                    ELSE excluded.state END,
                    properties_json=excluded.properties_json,
                    last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at
                """.format(
                    external_placeholders=(
                        ",".join("?" for _ in external_serials) or "NULL"
                    )
                ),
                (
                    device_id,
                    worker_id,
                    serial,
                    transport,
                    reported_state,
                    json.dumps(device.get("properties", {}), separators=(",", ":")),
                    now,
                    now,
                    now,
                    *sorted(external_serials),
                ),
            )
        if seen:
            placeholders = ",".join("?" for _ in seen)
            # Ghost USB/IP devices (serial "localhost:XXXXX") accumulate because
            # each USB/IP attach uses a different TCP port.  Once the real device
            # disappears from adb they must be purged, not merely marked offline.
            conn.execute(
                f"DELETE FROM cluster_worker_devices "
                f"WHERE worker_id=? AND id NOT IN ({placeholders}) "
                f"AND (serial LIKE 'localhost:%' OR transport='usbip')",
                [worker_id, *seen],
            )
            conn.execute(
                f"UPDATE cluster_worker_devices SET state='offline',updated_at=? "
                f"WHERE worker_id=? AND id NOT IN ({placeholders})",
                [now, worker_id, *seen],
            )
        else:
            conn.execute(
                "DELETE FROM cluster_worker_devices "
                "WHERE worker_id=? AND (serial LIKE 'localhost:%' OR transport='usbip')",
                (worker_id,),
            )
            conn.execute(
                "UPDATE cluster_worker_devices SET state='offline',updated_at=? "
                "WHERE worker_id=?",
                (now, worker_id),
            )

    def _replace_worker_tests(
        self,
        conn,
        worker_id: str,
        tests: list[dict],
        now: str,
    ) -> None:
        conn.execute("DELETE FROM cluster_worker_tests WHERE worker_id=?", (worker_id,))
        for item in tests:
            conn.execute(
                """INSERT INTO cluster_worker_tests
                (worker_id,worker_job_id,source,status,suite_type,pid,devices_json,
                 payload_json,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    worker_id,
                    item.get("worker_job_id", ""),
                    item.get("source", "managed"),
                    item.get("status", "running"),
                    item.get("suite_type", ""),
                    item.get("pid"),
                    json.dumps(item.get("devices", []), separators=(",", ":")),
                    json.dumps(item, separators=(",", ":")),
                    now,
                ),
            )

    def list_worker_tests(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_tests"
        params: tuple[Any, ...] = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,last_seen_at,worker_job_id"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload_json"] or "{}")
            item["worker_id"] = row["worker_id"]
            item["last_seen_at"] = row["last_seen_at"]
            result.append(item)
        return result

    def _replace_suites(
        self,
        conn,
        worker_id: str,
        suites: list[dict],
        now: str,
    ) -> None:
        conn.execute(
            "UPDATE cluster_worker_suites SET available=0 WHERE worker_id=?",
            (worker_id,),
        )
        for suite in suites:
            conn.execute(
                """
                INSERT INTO cluster_worker_suites
                    (worker_id,suite_type,suite_version,suite_key,tools_path,checksum,
                     size_bytes,available,last_scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id,tools_path) DO UPDATE SET
                    suite_type=excluded.suite_type,suite_version=excluded.suite_version,
                    suite_key=excluded.suite_key,checksum=excluded.checksum,
                    size_bytes=excluded.size_bytes,available=excluded.available,
                    last_scanned_at=excluded.last_scanned_at
                """,
                (
                    worker_id,
                    suite.get("suite_type", ""),
                    suite.get("suite_version", ""),
                    suite.get("suite_key", ""),
                    suite["tools_path"],
                    suite.get("checksum", ""),
                    suite.get("size_bytes", 0),
                    int(suite.get("available", True)),
                    now,
                ),
            )

    def list_devices(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_devices"
        params: tuple[Any, ...] = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,serial"
        with self.connect() as conn:
            return [
                self._decode(row) or {}
                for row in conn.execute(sql, params).fetchall()
            ]

    def list_suites(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_suites"
        params: tuple[Any, ...] = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,suite_type,suite_version"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
