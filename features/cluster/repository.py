from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cluster_workers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT NOT NULL,
                    address TEXT NOT NULL, agent_version TEXT NOT NULL,
                    status TEXT NOT NULL, capabilities_json TEXT NOT NULL,
                    cpu_percent REAL NOT NULL DEFAULT 0,
                    memory_percent REAL NOT NULL DEFAULT 0,
                    disk_free_gb REAL NOT NULL DEFAULT 0,
                    max_jobs INTEGER NOT NULL DEFAULT 1,
                    running_jobs INTEGER NOT NULL DEFAULT 0,
                    registered_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cluster_worker_devices (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, serial TEXT NOT NULL,
                    transport TEXT NOT NULL, state TEXT NOT NULL,
                    properties_json TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(worker_id, serial, transport),
                    FOREIGN KEY(worker_id) REFERENCES cluster_workers(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_devices_worker_state
                    ON cluster_worker_devices(worker_id, state);
                CREATE TABLE IF NOT EXISTS cluster_worker_suites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id TEXT NOT NULL,
                    suite_type TEXT NOT NULL, suite_version TEXT NOT NULL,
                    suite_key TEXT NOT NULL, tools_path TEXT NOT NULL,
                    checksum TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0,
                    available INTEGER NOT NULL DEFAULT 1, last_scanned_at TEXT NOT NULL,
                    UNIQUE(worker_id, tools_path),
                    FOREIGN KEY(worker_id) REFERENCES cluster_workers(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cluster_commands (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, command_type TEXT NOT NULL,
                    job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    dispatch_token TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT NOT NULL, error TEXT NOT NULL,
                    created_at TEXT NOT NULL, delivered_at TEXT NOT NULL,
                    acknowledged_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES cluster_workers(id)
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_commands_poll
                    ON cluster_commands(worker_id, status, created_at);
                CREATE TABLE IF NOT EXISTS cluster_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
                    message TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(attempt_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS cluster_jobs (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_type TEXT NOT NULL,
                    requested_worker_id TEXT NOT NULL, assigned_worker_id TEXT NOT NULL,
                    suite_key TEXT NOT NULL, suite_path TEXT NOT NULL,
                    request_json TEXT NOT NULL, status TEXT NOT NULL,
                    priority INTEGER NOT NULL, current_attempt_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_jobs_status
                    ON cluster_jobs(status, priority, created_at);
                CREATE TABLE IF NOT EXISTS cluster_job_attempts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL, worker_id TEXT NOT NULL,
                    worker_job_id TEXT NOT NULL, dispatch_token TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT NOT NULL,
                    started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL, error TEXT NOT NULL,
                    UNIQUE(job_id, attempt_number),
                    FOREIGN KEY(job_id) REFERENCES cluster_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS device_leases (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL, serial TEXT NOT NULL,
                    job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL, status TEXT NOT NULL,
                    generation INTEGER NOT NULL, acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    released_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES cluster_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cluster_job_artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL, filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL, artifact_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(attempt_id, filename)
                );
                CREATE TABLE IF NOT EXISTS cluster_transfers (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, transfer_type TEXT NOT NULL,
                    status TEXT NOT NULL, filename TEXT NOT NULL, relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL,
                    error TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
            """)
            self._migrate_device_lease_unique_constraint(conn)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_lease ON device_leases(device_id) WHERE status='active'")

    @staticmethod
    def _migrate_device_lease_unique_constraint(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='device_leases'"
        ).fetchone()
        sql = str(row["sql"] if row else "")
        if "device_id TEXT NOT NULL UNIQUE" not in sql:
            return
        conn.execute("DROP INDEX IF EXISTS idx_active_device_lease")
        conn.execute("ALTER TABLE device_leases RENAME TO device_leases_legacy")
        conn.execute("""CREATE TABLE device_leases (
            id TEXT PRIMARY KEY, device_id TEXT NOT NULL, worker_id TEXT NOT NULL,
            serial TEXT NOT NULL, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL,
            acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, released_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES cluster_jobs(id) ON DELETE CASCADE)""")
        conn.execute("INSERT INTO device_leases SELECT * FROM device_leases_legacy")
        conn.execute("DROP TABLE device_leases_legacy")
        conn.execute("CREATE UNIQUE INDEX idx_active_device_lease ON device_leases(device_id) WHERE status='active'")

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for column in ("capabilities_json", "properties_json", "payload_json", "result_json"):
            if column in result:
                key = column.removesuffix("_json")
                try:
                    result[key] = json.loads(result.pop(column) or "{}")
                except json.JSONDecodeError:
                    result[key] = {}
        return result

    def register_worker(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self.connect() as conn:
            conn.execute("""
                INSERT INTO cluster_workers
                    (id,name,hostname,address,agent_version,status,capabilities_json,
                     max_jobs,registered_at,last_heartbeat_at,updated_at)
                VALUES (?,?,?,?,?,'online',?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                    hostname=excluded.hostname,address=excluded.address,
                    agent_version=excluded.agent_version,status='online',
                    capabilities_json=excluded.capabilities_json,max_jobs=excluded.max_jobs,
                    last_heartbeat_at=excluded.last_heartbeat_at,updated_at=excluded.updated_at
            """, (
                data["worker_id"], data.get("name", ""), data.get("hostname", ""),
                data.get("address", ""), data.get("agent_version", ""),
                json.dumps(data.get("capabilities", {}), separators=(",", ":")),
                data.get("max_jobs", 1), now, now, now,
            ))
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

    def delete_worker(self, worker_id: str) -> bool:
        with self._lock, self.connect() as conn:
            active = conn.execute("SELECT 1 FROM cluster_jobs WHERE assigned_worker_id=? "
                                  "AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                                  (worker_id,)).fetchone()
            if active:
                return False
            # Commands reference workers without ON DELETE CASCADE. Once no
            # job is active they are delivery history and must not prevent a
            # host from being removed; job/event history remains intact.
            conn.execute("DELETE FROM cluster_commands WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_devices WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_worker_suites WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_transfers WHERE worker_id=?", (worker_id,))
            conn.execute("DELETE FROM cluster_workers WHERE id=?", (worker_id,))
            return conn.total_changes > 0

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._decode(conn.execute(
                "SELECT * FROM cluster_commands WHERE id=?", (command_id,)
            ).fetchone())

    def compact_command_result(self, command_id: str, result: dict[str, Any]) -> None:
        """Replace transient/binary-heavy command output with durable metadata."""
        with self.connect() as conn:
            conn.execute("UPDATE cluster_commands SET result_json=?,updated_at=? WHERE id=?",
                         (json.dumps(result, separators=(",", ":")), utc_now(), command_id))

    def create_transfer(self, worker_id: str, transfer_type: str = "suite_export") -> dict[str, Any]:
        transfer_id = f"transfer-{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as conn:
            conn.execute("""INSERT INTO cluster_transfers
                (id,worker_id,transfer_type,status,filename,relative_path,size_bytes,sha256,
                 error,created_at,updated_at,completed_at) VALUES(?,?,?,'created','','',0,'','',?,?, '')""",
                         (transfer_id, worker_id, transfer_type, now, now))
        return self.get_transfer(transfer_id) or {}

    def get_transfer(self, transfer_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cluster_transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row) if row else None

    def update_transfer(self, transfer_id: str, **values: Any) -> dict[str, Any] | None:
        allowed = {"status", "filename", "relative_path", "size_bytes", "sha256", "error", "completed_at"}
        fields = [(key, value) for key, value in values.items() if key in allowed]
        if fields:
            assignments = ",".join(f"{key}=?" for key, _ in fields)
            with self.connect() as conn:
                conn.execute(f"UPDATE cluster_transfers SET {assignments},updated_at=? WHERE id=?",
                             ([value for _, value in fields] + [utc_now(), transfer_id]))
        return self.get_transfer(transfer_id)

    def heartbeat(self, worker_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self._lock, self.connect() as conn:
            cursor = conn.execute("""
                UPDATE cluster_workers SET status=?,agent_version=?,cpu_percent=?,
                    memory_percent=?,disk_free_gb=?,running_jobs=?,
                    last_heartbeat_at=?,updated_at=? WHERE id=?
            """, (
                "busy" if data.get("running_jobs") else "online",
                data.get("agent_version", ""), data.get("cpu_percent", 0),
                data.get("memory_percent", 0), data.get("disk_free_gb", 0),
                len(data.get("running_jobs", [])), now, now, worker_id,
            ))
            if cursor.rowcount == 0:
                return None
            self._replace_devices(conn, worker_id, data.get("devices", []), now)
            if data.get("suites") is not None:
                self._replace_suites(conn, worker_id, data["suites"], now)
            for running in data.get("running_jobs", []):
                attempt_id = running.get("attempt_id", "")
                worker_job_id = running.get("worker_job_id", "")
                conn.execute("""UPDATE cluster_job_attempts SET status='running',
                    worker_job_id=?,heartbeat_at=? WHERE id=? AND worker_id=?""",
                    (worker_job_id, now, attempt_id, worker_id))
                job = conn.execute("SELECT job_id FROM cluster_job_attempts WHERE id=?", (attempt_id,)).fetchone()
                if job:
                    conn.execute("""UPDATE cluster_jobs SET status='running',updated_at=?,
                        error=CASE WHEN status='worker_lost' THEN '' ELSE error END
                        WHERE id=? AND status NOT IN ('completed','failed','cancelled')""",
                                 (now, job["job_id"]))
                    # A lost Worker keeps leases orphaned so another task cannot
                    # silently assume ownership. When that exact Attempt is
                    # reported running again, atomically restore its leases.
                    conn.execute("""UPDATE device_leases SET status='active',
                        heartbeat_at=?,expires_at=datetime('now','+90 seconds'),released_at=''
                        WHERE attempt_id=? AND worker_id=? AND status='orphaned'""",
                                 (now, attempt_id, worker_id))
                    conn.execute("""UPDATE cluster_worker_devices SET state='allocated',updated_at=?
                        WHERE id IN (SELECT device_id FROM device_leases
                            WHERE attempt_id=? AND status='active')""", (now, attempt_id))
                    conn.execute("""UPDATE device_leases SET heartbeat_at=?,
                        expires_at=datetime('now','+90 seconds')
                        WHERE attempt_id=? AND status='active'""", (now, attempt_id))
        return self.get_worker(worker_id)

    def _replace_devices(self, conn, worker_id: str, devices: list[dict], now: str) -> None:
        seen = []
        for device in devices:
            serial = device["serial"]
            transport = device.get("transport", "local_usb")
            device_id = f"{worker_id}:{serial}"
            seen.append(device_id)
            conn.execute("""
                INSERT INTO cluster_worker_devices
                    (id,worker_id,serial,transport,state,properties_json,
                     first_seen_at,last_seen_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id,serial,transport) DO UPDATE SET
                    state=CASE WHEN EXISTS(
                        SELECT 1 FROM device_leases
                        WHERE device_id=excluded.id AND status='active'
                    ) THEN 'allocated' ELSE excluded.state END,
                    properties_json=excluded.properties_json,
                    last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at
            """, (device_id, worker_id, serial, transport,
                    device.get("state", "available"),
                    json.dumps(device.get("properties", {}), separators=(",", ":")),
                    now, now, now))
        if seen:
            placeholders = ",".join("?" for _ in seen)
            conn.execute(
                f"UPDATE cluster_worker_devices SET state='offline',updated_at=? "
                f"WHERE worker_id=? AND id NOT IN ({placeholders})", [now, worker_id, *seen]
            )
        else:
            conn.execute("UPDATE cluster_worker_devices SET state='offline',updated_at=? WHERE worker_id=?", (now, worker_id))

    def _replace_suites(self, conn, worker_id: str, suites: list[dict], now: str) -> None:
        conn.execute("UPDATE cluster_worker_suites SET available=0 WHERE worker_id=?", (worker_id,))
        for suite in suites:
            conn.execute("""
                INSERT INTO cluster_worker_suites
                    (worker_id,suite_type,suite_version,suite_key,tools_path,checksum,
                     size_bytes,available,last_scanned_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(worker_id,tools_path) DO UPDATE SET
                    suite_type=excluded.suite_type,suite_version=excluded.suite_version,
                    suite_key=excluded.suite_key,checksum=excluded.checksum,
                    size_bytes=excluded.size_bytes,available=excluded.available,
                    last_scanned_at=excluded.last_scanned_at
            """, (worker_id, suite.get("suite_type", ""), suite.get("suite_version", ""),
                    suite.get("suite_key", ""), suite["tools_path"], suite.get("checksum", ""),
                    suite.get("size_bytes", 0), int(suite.get("available", True)), now))

    def list_devices(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_devices"
        params: tuple = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,serial"
        with self.connect() as conn:
            return [self._decode(row) or {} for row in conn.execute(sql, params).fetchall()]

    def list_suites(self, worker_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cluster_worker_suites"
        params: tuple = ()
        if worker_id:
            sql += " WHERE worker_id=?"
            params = (worker_id,)
        sql += " ORDER BY worker_id,suite_type,suite_version"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT status FROM cluster_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"completed", "failed", "cancelled"}:
                return False
            for table in ("cluster_job_events", "cluster_job_artifacts", "device_leases",
                          "cluster_commands", "cluster_job_attempts"):
                conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM cluster_jobs WHERE id=?", (job_id,))
            return True

    def create_command(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        command_id = f"cmd-{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO cluster_commands
                    (id,worker_id,command_type,job_id,attempt_id,dispatch_token,
                     payload_json,status,result_json,error,created_at,delivered_at,
                     acknowledged_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'queued','{}','',?,'','',?)
            """, (command_id, data["worker_id"], data["command_type"],
                    data.get("job_id", ""), data.get("attempt_id", ""), token,
                    json.dumps(data.get("payload", {}), separators=(",", ":")), now, now))
        return self.get_command(command_id) or {}

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._decode(conn.execute("SELECT * FROM cluster_commands WHERE id=?", (command_id,)).fetchone())

    def poll_commands(self, worker_id: str, limit: int = 5) -> list[dict[str, Any]]:
        now = utc_now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT * FROM cluster_commands WHERE worker_id=? AND status='queued'
                ORDER BY created_at LIMIT ?
            """, (worker_id, limit)).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany(
                    "UPDATE cluster_commands SET status='delivered',delivered_at=?,updated_at=? WHERE id=?",
                    [(now, now, item) for item in ids],
                )
            return [self._decode(row) or {} for row in rows]

    def ack_command(self, worker_id: str, command_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute("""
                UPDATE cluster_commands SET status=?,result_json=?,error=?,
                    acknowledged_at=?,updated_at=? WHERE id=? AND worker_id=?
            """, (data["status"], json.dumps(data.get("result", {}), separators=(",", ":")),
                    data.get("error", ""), now, now, command_id, worker_id))
            if cursor.rowcount == 0:
                return None
        return self.get_command(command_id)

    def create_job_with_leases(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        job_id = f"job-{uuid.uuid4().hex}"
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        lease_ids = []
        worker_id = data["worker_id"]
        requested_devices = data.get("devices", [])
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute("SELECT * FROM cluster_workers WHERE id=?", (worker_id,)).fetchone()
            if worker is None:
                raise ValueError("worker not found")
            if worker["status"] not in {"online", "busy"}:
                raise ValueError("worker is not online")
            if int(worker["running_jobs"]) >= int(worker["max_jobs"]):
                raise ValueError("worker capacity is exhausted")
            active_jobs = conn.execute("""SELECT COUNT(*) FROM cluster_jobs
                WHERE assigned_worker_id=? AND status IN ('assigned','dispatching','running','stopping')""",
                (worker_id,)).fetchone()[0]
            if active_jobs >= int(worker["max_jobs"]):
                raise ValueError("worker already has the maximum number of active jobs")
            conn.execute("""INSERT INTO cluster_jobs
                (id,owner_id,source_type,requested_worker_id,assigned_worker_id,
                 suite_key,suite_path,request_json,status,priority,current_attempt_id,
                 created_at,queued_at,started_at,finished_at,updated_at,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'','',?,'')""",
                (job_id, data.get("owner_id", ""), data.get("source_type", "manual"),
                 worker_id, worker_id, data.get("suite_key", ""), data.get("suite_path", ""),
                 json.dumps(data, separators=(",", ":")), "assigned", data.get("priority", 100),
                 attempt_id, now, now, now))
            dispatch_token = uuid.uuid4().hex
            conn.execute("""INSERT INTO cluster_job_attempts
                (id,job_id,attempt_number,worker_id,worker_job_id,dispatch_token,
                 status,result_json,started_at,heartbeat_at,finished_at,error)
                VALUES(?,?,1,?,'',?,'assigned','{}','','','','')""",
                (attempt_id, job_id, worker_id, dispatch_token))
            for raw_id in requested_devices:
                device_id = raw_id if raw_id.startswith(f"{worker_id}:") else f"{worker_id}:{raw_id}"
                device = conn.execute(
                    "SELECT * FROM cluster_worker_devices WHERE id=? AND worker_id=?",
                    (device_id, worker_id),
                ).fetchone()
                if device is None:
                    raise ValueError(f"device not found on worker: {raw_id}")
                if device["state"] != "available":
                    raise ValueError(f"device is not available: {raw_id}")
                if conn.execute("SELECT 1 FROM device_leases WHERE device_id=? AND status='active'", (device_id,)).fetchone():
                    raise ValueError(f"device is already leased: {raw_id}")
                lease_id = f"lease-{uuid.uuid4().hex}"
                lease_ids.append(lease_id)
                # SQLite computes expiry to keep lease timestamps consistent.
                conn.execute("""INSERT INTO device_leases
                    (id,device_id,worker_id,serial,job_id,attempt_id,owner_id,status,
                     generation,acquired_at,heartbeat_at,expires_at,released_at)
                    VALUES(?,?,?,?,?,?,?,'active',1,?,?,datetime('now','+90 seconds'),'')""",
                    (lease_id, device_id, worker_id, device["serial"], job_id, attempt_id,
                     data.get("owner_id", ""), now, now))
                conn.execute("UPDATE cluster_worker_devices SET state='allocated',updated_at=? WHERE id=?", (now, device_id))
        job = self.get_job(job_id) or {}
        job["lease_ids"] = lease_ids
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cluster_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["request"] = json.loads(result.pop("request_json") or "{}")
            attempt = conn.execute("SELECT * FROM cluster_job_attempts WHERE id=?", (result["current_attempt_id"],)).fetchone()
            result["attempt"] = dict(attempt) if attempt else None
            if result["attempt"]:
                result["attempt"]["result"] = json.loads(result["attempt"].pop("result_json") or "{}")
            result["leases"] = [dict(item) for item in conn.execute("SELECT * FROM device_leases WHERE job_id=?", (job_id,)).fetchall()]
            return result

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM cluster_jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [job for row in rows if (job := self.get_job(row["id"]))]

    def attach_command_to_job(self, job_id: str, command: dict[str, Any]) -> None:
        now = utc_now()
        with self.connect() as conn:
            job = conn.execute("SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise ValueError("job not found")
            conn.execute("UPDATE cluster_jobs SET status='dispatching',updated_at=? WHERE id=?", (now, job_id))
            conn.execute("UPDATE cluster_job_attempts SET status='dispatching' WHERE id=?", (job["current_attempt_id"],))

    def sync_job_from_command(self, command: dict[str, Any]) -> None:
        job_id = command.get("job_id", "")
        if not job_id:
            return
        status = command["status"]
        if status not in {"running", "completed", "failed", "cancelled"}:
            return
        now = utc_now()
        result = command.get("result", {})
        job_status = "running" if status == "running" else status
        if command.get("command_type") == "stop_test" and status == "completed":
            job_status = status = "cancelled"
        with self.connect() as conn:
            job = conn.execute("SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return
            worker_job_id = result.get("worker_job_id", "")
            conn.execute("""UPDATE cluster_job_attempts SET status=?,worker_job_id=?,
                result_json=?,error=?,started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,
                finished_at=CASE WHEN ? IN ('completed','failed','cancelled') THEN ? ELSE finished_at END
                WHERE id=?""", (status, worker_job_id, json.dumps(result, separators=(",", ":")),
                    command.get("error", ""), status, now, status, now, job["current_attempt_id"]))
            conn.execute("UPDATE cluster_jobs SET status=?,updated_at=?,error=? WHERE id=?",
                         (job_status, now, command.get("error", ""), job_id))
            if status in {"completed", "failed", "cancelled"}:
                conn.execute("UPDATE cluster_jobs SET finished_at=? WHERE id=?", (now, job_id))
                leases = conn.execute("SELECT device_id FROM device_leases WHERE job_id=? AND status='active'", (job_id,)).fetchall()
                conn.execute("UPDATE device_leases SET status='released',released_at=? WHERE job_id=? AND status='active'", (now, job_id))
                conn.executemany("UPDATE cluster_worker_devices SET state='available',updated_at=? WHERE id=?",
                                 [(now, item["device_id"]) for item in leases])

    def mark_worker_offline(self, worker_id: str) -> None:
        now = utc_now()
        with self._lock, self.connect() as conn:
            conn.execute("UPDATE cluster_workers SET status='offline',updated_at=? WHERE id=?", (now, worker_id))
            conn.execute("UPDATE cluster_worker_devices SET state='unknown',updated_at=? WHERE worker_id=?", (now, worker_id))
            jobs = conn.execute("""SELECT id FROM cluster_jobs WHERE assigned_worker_id=?
                AND status IN ('assigned','dispatching','running','stopping')""", (worker_id,)).fetchall()
            for job in jobs:
                conn.execute("UPDATE cluster_jobs SET status='worker_lost',updated_at=?,error='worker heartbeat lost' WHERE id=?",
                             (now, job["id"]))
                conn.execute("UPDATE device_leases SET status='orphaned' WHERE job_id=? AND status='active'", (job["id"],))

    def add_events(self, job_id: str, attempt_id: str, events: list[dict[str, Any]]) -> int:
        inserted = 0
        with self.connect() as conn:
            for event in events:
                cursor = conn.execute("""INSERT OR IGNORE INTO cluster_job_events
                    (job_id,attempt_id,sequence,event_type,source,level,message,payload_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (job_id, attempt_id, event["sequence"],
                    event.get("event_type", "log"), event.get("source", "worker"),
                    event.get("level", "info"), event.get("message", ""),
                    json.dumps(event.get("payload", {}), separators=(",", ":")), utc_now()))
                inserted += cursor.rowcount
        return inserted

    def list_events(self, job_id: str, after: int = -1, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("""SELECT * FROM cluster_job_events
                WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?""", (job_id, after, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    def record_artifact(self, data: dict[str, Any]) -> dict[str, Any]:
        artifact_id = f"artifact-{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.execute("""INSERT INTO cluster_job_artifacts
                (id,job_id,attempt_id,worker_id,filename,relative_path,artifact_type,
                 size_bytes,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(attempt_id,filename) DO UPDATE SET
                    relative_path=excluded.relative_path,artifact_type=excluded.artifact_type,
                    size_bytes=excluded.size_bytes,sha256=excluded.sha256,
                    created_at=excluded.created_at""", (artifact_id, data["job_id"],
                    data["attempt_id"], data["worker_id"], data["filename"],
                    data["relative_path"], data.get("artifact_type", "file"),
                    data["size_bytes"], data["sha256"], utc_now()))
            row = conn.execute("SELECT * FROM cluster_job_artifacts WHERE attempt_id=? AND filename=?",
                               (data["attempt_id"], data["filename"])).fetchone()
        return dict(row)

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM cluster_job_artifacts WHERE job_id=? ORDER BY created_at,filename",
                (job_id,),
            ).fetchall()]
