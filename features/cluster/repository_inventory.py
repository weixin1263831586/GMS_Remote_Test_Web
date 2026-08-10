"""Worker inventory persistence helpers for the cluster repository."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import ClusterConfig
from .state_machine import InvalidJobTransitionError


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            "disk_total_gb": "REAL NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE cluster_workers ADD COLUMN {name} {definition}"
                )

    def register_worker(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT status,session_id,connection_generation FROM cluster_workers WHERE id=?",
                (data["worker_id"],),
            ).fetchone()
            reported_session = str(data.get("session_id") or "")
            previous_session = str(existing["session_id"] or "") if existing else ""
            session_id = reported_session or previous_session
            generation = int(existing["connection_generation"] or 0) if existing else 0
            if reported_session and reported_session != previous_session:
                generation += 1
            recovered = bool(
                existing
                and (
                    existing["status"] == "offline"
                    or (reported_session and reported_session != previous_session)
                )
            )
            conn.execute("""
                INSERT INTO cluster_workers
                    (id,name,hostname,address,agent_version,status,capabilities_json,
                     max_jobs,session_id,connection_generation,disconnected_at,
                     last_recovered_at,registered_at,last_heartbeat_at,updated_at)
                VALUES (?,?,?,?,?,'online',?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    hostname=excluded.hostname,address=excluded.address,
                    agent_version=excluded.agent_version,status='online',
                    capabilities_json=excluded.capabilities_json,max_jobs=excluded.max_jobs,
                    session_id=excluded.session_id,
                    connection_generation=excluded.connection_generation,
                    disconnected_at='',last_recovered_at=excluded.last_recovered_at,
                    last_heartbeat_at=excluded.last_heartbeat_at,updated_at=excluded.updated_at
            """, (
                data["worker_id"], data.get("name", ""), data.get("hostname", ""),
                data.get("address", ""), data.get("agent_version", ""),
                json.dumps(data.get("capabilities", {}), separators=(",", ":")),
                data.get("max_jobs", ClusterConfig.load().default_max_jobs), session_id, generation, "",
                now if recovered else "", now, now, now,
            ))
            self._append_timeline_conn(
                conn,
                worker_id=data["worker_id"],
                event_type="worker.reconnected" if recovered else "worker.registered",
                source="worker",
                message=("Worker reconnected" if recovered else "Worker registered"),
                payload={
                    "session_id": session_id,
                    "connection_generation": generation,
                    "previous_session_id": previous_session,
                },
            )
        return self.get_worker(data["worker_id"]) or {}

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._decode(conn.execute(
                "SELECT * FROM cluster_workers WHERE id=?", (worker_id,)
            ).fetchone())

    def list_workers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self._decode(row) or {} for row in conn.execute(
                "SELECT * FROM cluster_workers ORDER BY id"
            ).fetchall()]

    def get_worker_metrics_history(self, worker_id: str = "") -> list[dict[str, Any]]:
        # Heartbeats arrive every few seconds, while the dashboard spans 24
        # hours.  Aggregate into five-minute buckets to keep the response and
        # browser chart bounded as Worker count grows.
        sql = """SELECT worker_id,MAX(recorded_at) AS recorded_at,
                        ROUND(AVG(cpu_percent),2) AS cpu_percent,
                        ROUND(AVG(memory_percent),2) AS memory_percent,
                        MIN(disk_free_gb) AS disk_free_gb,
                        MAX(running_jobs) AS running_jobs,
                        MAX(external_jobs) AS external_jobs
                 FROM cluster_worker_metrics
                 WHERE recorded_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')"""
        params: list[Any] = []
        if worker_id:
            sql += " AND worker_id=?"
            params.append(worker_id)
        sql += " GROUP BY worker_id,CAST(strftime('%s',recorded_at)/300 AS INTEGER)"
        sql += " ORDER BY recorded_at"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def delete_worker(self, worker_id: str) -> bool:
        with self._lock, self.connect() as conn:
            active = conn.execute(
                """SELECT 1 FROM cluster_jobs WHERE assigned_worker_id=?
                   AND status NOT IN ('completed','failed','cancelled') LIMIT 1""",
                (worker_id,),
            ).fetchone()
            if active:
                return False
            conn.execute("DELETE FROM cluster_commands WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_devices WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_suites WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_tests WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_metrics WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_transfers WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_workers WHERE id=?", (worker_id,))
            return conn.total_changes > 0

    def heartbeat(self, worker_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        now = _utc_now()
        running_jobs = data.get("running_jobs", [])
        external_jobs = [item for item in running_jobs if item.get("source") == "external"]
        unknown_external_jobs = [item for item in external_jobs if not item.get("devices")]
        external_serials = {
            (str(device)[len(worker_id) + 1:]
             if str(device).startswith(f"{worker_id}:") else str(device))
            for item in external_jobs
            for device in item.get("devices", [])
        }
        status = ("draining" if unknown_external_jobs else
                  "busy" if running_jobs else "online")
        revoked_attempt_ids: set[str] = set()
        with self._lock, self.connect() as conn:
            session = conn.execute(
                "SELECT session_id,connection_generation FROM cluster_workers WHERE id=?",
                (worker_id,),
            ).fetchone()
            if session is not None and session["session_id"] and (
                str(session["session_id"]) != str(data.get("session_id") or "")
                or int(session["connection_generation"] or 0)
                != int(data.get("connection_generation") or 0)
            ):
                raise ValueError("stale worker session")
            cursor = conn.execute("""
                UPDATE cluster_workers SET status=?,agent_version=?,cpu_percent=?,
                    memory_percent=?,memory_total_gb=?,memory_available_gb=?,load_1m=?,
                    disk_free_gb=?,disk_total_gb=?,running_jobs=?,external_jobs=?,
                    unknown_external_jobs=?,last_heartbeat_at=?,updated_at=? WHERE id=?
            """, (
                status,
                data.get("agent_version", ""), data.get("cpu_percent", 0),
                data.get("memory_percent", 0), data.get("memory_total_gb", 0),
                data.get("memory_available_gb", 0), data.get("load_1m", 0),
                data.get("disk_free_gb", 0), data.get("disk_total_gb", 0),
                len(running_jobs), len(external_jobs),
                len(unknown_external_jobs), now, now, worker_id,
            ))
            if cursor.rowcount == 0:
                return None
            self._expire_device_reservations(conn, now)
            self._replace_devices(
                conn, worker_id, data.get("devices", []), now, external_serials
            )
            self._replace_worker_tests(conn, worker_id, running_jobs, now)
            if data.get("suites") is not None:
                self._replace_suites(conn, worker_id, data["suites"], now)
            for running in running_jobs:
                if running.get("source") == "external":
                    continue
                attempt_id = running.get("attempt_id", "")
                worker_job_id = running.get("worker_job_id", "")
                job = conn.execute(
                    """SELECT a.job_id,j.owner_id FROM cluster_job_attempts a
                       JOIN cluster_jobs j ON j.id=a.job_id
                       WHERE a.id=? AND a.worker_id=?""",
                    (attempt_id, worker_id),
                ).fetchone()
                if not job:
                    continue
                lease_rows = conn.execute(
                    """SELECT device_id FROM device_leases
                       WHERE attempt_id=? AND status IN ('active','orphaned')""",
                    (attempt_id,),
                ).fetchall()
                if not self.renew_job_device_claim(
                    job["job_id"],
                    job["owner_id"],
                    [item["device_id"] for item in lease_rows],
                ):
                    revoked_attempt_ids.add(attempt_id)
                    try:
                        self._transition_job_conn(
                            conn,
                            job["job_id"],
                            "failed",
                            source="controller-fencing",
                            message="Rejected running Attempt after its device claim expired",
                            error="device fencing claim expired or was superseded",
                            worker_id=worker_id,
                            payload={"attempt_id": attempt_id},
                        )
                    except InvalidJobTransitionError:
                        pass
                    conn.execute(
                        """UPDATE cluster_job_attempts
                           SET status='failed',finished_at=?,error=? WHERE id=?""",
                        (now, "device fencing claim expired or was superseded", attempt_id),
                    )
                    conn.execute(
                        """UPDATE device_leases SET status='revoked',released_at=?
                           WHERE attempt_id=? AND status IN ('active','orphaned')""",
                        (now, attempt_id),
                    )
                    self.claims.release(f"job:{job['job_id']}", status="failed")
                    continue
                conn.execute("""UPDATE cluster_job_attempts SET status='running',
                    worker_job_id=?,heartbeat_at=? WHERE id=? AND worker_id=?""",
                    (worker_job_id, now, attempt_id, worker_id))
                try:
                    self._transition_job_conn(
                        conn,
                        job["job_id"],
                        "running",
                        source="worker-heartbeat",
                        message="Worker reported the persisted Attempt running",
                        worker_id=worker_id,
                        payload={
                            "worker_job_id": worker_job_id,
                            "session_id": data.get("session_id", ""),
                            "connection_generation": data.get(
                                "connection_generation", 0
                            ),
                        },
                    )
                except InvalidJobTransitionError:
                    continue
                conn.execute("""UPDATE device_leases SET status='active',
                    heartbeat_at=?,expires_at=datetime('now',?),released_at=''
                    WHERE attempt_id=? AND worker_id=? AND status='orphaned'""",
                    (now, f"+{self.claim_lease_ttl_seconds} seconds", attempt_id, worker_id))
                conn.execute("""UPDATE cluster_worker_devices SET state='allocated',updated_at=?
                    WHERE id IN (SELECT device_id FROM device_leases
                        WHERE attempt_id=? AND status='active')""", (now, attempt_id))
                conn.execute("""UPDATE device_leases SET heartbeat_at=?,
                    expires_at=datetime('now',?)
                    WHERE attempt_id=? AND status='active'""", (
                        now, f"+{self.claim_lease_ttl_seconds} seconds", attempt_id,
                    ))
            conn.execute(
                """INSERT INTO cluster_worker_metrics
                   (worker_id,recorded_at,cpu_percent,memory_percent,
                    disk_free_gb,running_jobs,external_jobs)
                   VALUES(?,?,?,?,?,?,?)""",
                (worker_id, now, data.get("cpu_percent", 0),
                 data.get("memory_percent", 0), data.get("disk_free_gb", 0),
                 len(running_jobs), len(external_jobs)),
            )
            conn.execute(
                "DELETE FROM cluster_worker_metrics WHERE recorded_at < "
                "strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')"
            )
        worker = self.get_worker(worker_id)
        if worker is not None:
            worker["revoked_attempt_ids"] = sorted(revoked_attempt_ids)
        # Notify WebSocket clients that worker state has changed so the
        # frontend can refresh cluster views without polling.
        from foundation.events import EVENT_WORKER_UPDATED, event_bus

        event_bus.emit(EVENT_WORKER_UPDATED, {"worker_id": worker_id, "status": status})
        return worker

    def refresh_worker_devices(
        self,
        worker_id: str,
        devices: list[dict[str, Any]],
    ) -> None:
        """Apply a device snapshot for a Worker without waiting for the next heartbeat.

        Remote USB/IP attach/detach commands already return the Worker's current
        device list. Reusing that snapshot here keeps ``cluster_worker_devices``
        (and therefore the UI) consistent immediately, instead of lagging up to
        one heartbeat interval (~15s) after an operation completed.
        """
        now = _utc_now()
        with self._lock, self.connect() as conn:
            self._replace_devices(conn, worker_id, devices or [], now)

    def mark_worker_offline(self, worker_id: str) -> None:
        now = _utc_now()
        with self._lock, self.connect() as conn:
            worker = conn.execute(
                "SELECT session_id,connection_generation FROM cluster_workers WHERE id=?",
                (worker_id,),
            ).fetchone()
            conn.execute(
                """UPDATE cluster_workers SET status='offline',disconnected_at=?,
                   updated_at=? WHERE id=?""",
                (now, now, worker_id),
            )
            conn.execute(
                "UPDATE cluster_worker_devices SET state='unknown',updated_at=? WHERE worker_id=?",
                (now, worker_id),
            )
            jobs = conn.execute("""SELECT id FROM cluster_jobs WHERE assigned_worker_id=?
                AND status IN ('assigned','dispatching','running','stopping')""",
                (worker_id,)).fetchall()
            for job in jobs:
                try:
                    self._transition_job_conn(
                        conn,
                        job["id"],
                        "worker_lost",
                        source="controller-watchdog",
                        message="Worker heartbeat timed out; waiting for the same Attempt to reconnect",
                        error="worker heartbeat lost",
                        worker_id=worker_id,
                        payload={
                            "session_id": worker["session_id"] if worker else "",
                            "connection_generation": (
                                worker["connection_generation"] if worker else 0
                            ),
                        },
                    )
                except InvalidJobTransitionError:
                    continue
                conn.execute(
                    "UPDATE device_leases SET status='orphaned' WHERE job_id=? AND status='active'",
                    (job["id"],),
                )
            self._append_timeline_conn(
                conn,
                worker_id=worker_id,
                event_type="worker.disconnected",
                source="controller-watchdog",
                level="warning",
                message="Worker heartbeat timed out",
                payload={
                    "session_id": worker["session_id"] if worker else "",
                    "connection_generation": worker["connection_generation"] if worker else 0,
                    "affected_jobs": [item["id"] for item in jobs],
                },
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
                ON CONFLICT(id) DO UPDATE SET
                    transport=excluded.transport,
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
            # 删除 ADB 中已消失的 USB/IP 临时端口记录。
            conn.execute(
                f"DELETE FROM cluster_worker_devices "
                f"WHERE worker_id=? AND id NOT IN ({placeholders}) "
                f"AND (serial LIKE 'localhost:%' OR transport='usbip')",
                [worker_id, *seen],
            )
            # 本地 USB 设备断开后标记为 offline 而非删除，保留历史设备
            # 记录供离线设备计数和 UI 回溯。
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

    def replace_worker_suites(self, worker_id: str, suites: list[dict]) -> None:
        """Replace one Worker's suite inventory from a refresh command ACK."""
        if not worker_id:
            raise ValueError("worker_id is required")
        now = _utc_now()
        with self._lock, self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM cluster_workers WHERE id=?", (worker_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("worker not found")
            self._replace_suites(conn, worker_id, suites, now)

    def list_devices(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_devices"
        params: tuple[Any, ...] = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,serial"
        with self.connect() as conn:
            devices = [
                self._decode(row) or {}
                for row in conn.execute(sql, params).fetchall()
            ]
        # Protocol state and ownership are independent. A firmware claim must
        # remain visible while the same serial transitions ADB -> Fastboot ->
        # ADB, without replacing the reported protocol state.
        claims = {
            claim["device_key"]: claim
            for claim in self.claims.list_active(worker_id=worker_id or None)
        }
        for device in devices:
            claim = claims.get(str(device.get("id") or ""))
            device["claimed"] = claim is not None
            if claim is not None:
                device["claim_source_type"] = claim.get("source_type") or ""
                device["claim_owner_id"] = claim.get("owner_id") or ""
                device["claim_username"] = claim.get("username") or ""
        return devices

    def list_suites(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_suites"
        params: tuple[Any, ...] = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,suite_type,suite_version"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
