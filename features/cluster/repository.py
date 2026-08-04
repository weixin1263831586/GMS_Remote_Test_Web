from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from foundation.device_claims import DeviceClaimRegistry

from .config import ClusterConfig
from .repository_claims import ClusterClaimRepositoryMixin
from .repository_commands import ClusterCommandRepositoryMixin
from .repository_inventory import ClusterInventoryRepositoryMixin
from .repository_observability import ClusterObservabilityRepositoryMixin
from .repository_reservations import ClusterReservationRepositoryMixin
from .repository_transfers import ClusterTransferRepositoryMixin


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterRepository(
    ClusterObservabilityRepositoryMixin,
    ClusterClaimRepositoryMixin,
    ClusterCommandRepositoryMixin,
    ClusterInventoryRepositoryMixin,
    ClusterReservationRepositoryMixin,
    ClusterTransferRepositoryMixin,
):
    _REQUIRED_TABLES = frozenset({
        "cluster_workers",
        "cluster_worker_devices",
        "cluster_worker_suites",
        "cluster_worker_tests",
        "cluster_commands",
        "cluster_job_events",
        "cluster_jobs",
        "cluster_timeline_events",
        "cluster_job_attempts",
        "device_leases",
        "cluster_device_reservations",
        "cluster_job_artifacts",
        "cluster_artifact_uploads",
        "cluster_transfers",
    })

    def __init__(
        self,
        db_path: str | Path,
        claim_db_path: str | Path | None = None,
        claim_lease_ttl_seconds: int = 90,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if claim_db_path is None:
            claim_db_path = (
                self.db_path.parent.parent / "device_claims.sqlite3"
                if self.db_path.parent.name == "cluster"
                else self.db_path.parent / "device_claims.sqlite3"
            )
        self.claims = DeviceClaimRegistry(claim_db_path)
        self.claim_lease_ttl_seconds = max(30, int(claim_lease_ttl_seconds))
        self._lock = threading.RLock()
        self._init_schema()

    def _open_connection(self) -> sqlite3.Connection:
        # Self-heal parent dir so a runtime data/ clear-out doesn't turn
        # every cluster request into a 500 ("unable to open database file").
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not self._REQUIRED_TABLES.issubset(existing_tables):
            conn.close()
            self._init_schema()
            conn = self._open_connection()
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._open_connection() as conn:
            schema = """
                CREATE TABLE IF NOT EXISTS cluster_workers (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT NOT NULL,
                    address TEXT NOT NULL, agent_version TEXT NOT NULL,
                    status TEXT NOT NULL, capabilities_json TEXT NOT NULL,
                    cpu_percent REAL NOT NULL DEFAULT 0,
                    memory_percent REAL NOT NULL DEFAULT 0,
                    memory_total_gb REAL NOT NULL DEFAULT 0,
                    memory_available_gb REAL NOT NULL DEFAULT 0,
                    load_1m REAL NOT NULL DEFAULT 0,
                    disk_free_gb REAL NOT NULL DEFAULT 0,
                    max_jobs INTEGER NOT NULL DEFAULT __MAX_JOBS__,
                    running_jobs INTEGER NOT NULL DEFAULT 0,
                    external_jobs INTEGER NOT NULL DEFAULT 0,
                    unknown_external_jobs INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT NOT NULL DEFAULT '',
                    connection_generation INTEGER NOT NULL DEFAULT 0,
                    disconnected_at TEXT NOT NULL DEFAULT '',
                    last_recovered_at TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS cluster_worker_tests (
                    worker_id TEXT NOT NULL, worker_job_id TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL, suite_type TEXT NOT NULL,
                    pid INTEGER, devices_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, PRIMARY KEY(worker_id, worker_job_id),
                    FOREIGN KEY(worker_id) REFERENCES cluster_workers(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS cluster_commands (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, command_type TEXT NOT NULL,
                    job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    dispatch_token TEXT NOT NULL, payload_json TEXT NOT NULL,
                    operation_id TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '',
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
                    trace_id TEXT NOT NULL DEFAULT '', state_version INTEGER NOT NULL DEFAULT 1,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    last_transition_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, queued_at TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_jobs_status
                    ON cluster_jobs(status, priority, created_at);
                CREATE TABLE IF NOT EXISTS cluster_timeline_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL DEFAULT '', attempt_id TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '', operation_id TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '', event_type TEXT NOT NULL,
                    source TEXT NOT NULL, level TEXT NOT NULL,
                    from_state TEXT NOT NULL DEFAULT '', to_state TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS cluster_device_reservations (
                    id TEXT PRIMARY KEY, reservation_id TEXT NOT NULL,
                    device_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                    serial TEXT NOT NULL, owner_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, status TEXT NOT NULL,
                    acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, released_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cluster_reservations_source
                    ON cluster_device_reservations(source_id,status);
                CREATE TABLE IF NOT EXISTS cluster_job_artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL, filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL, artifact_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(attempt_id, filename)
                );
                CREATE TABLE IF NOT EXISTS cluster_artifact_uploads (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL, filename TEXT NOT NULL,
                    artifact_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, chunk_size INTEGER NOT NULL,
                    chunk_count INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    UNIQUE(attempt_id, filename)
                );
                CREATE TABLE IF NOT EXISTS cluster_transfers (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, transfer_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '', metadata_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL, filename TEXT NOT NULL, relative_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL,
                    error TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
            """.replace("__MAX_JOBS__", str(ClusterConfig.load().default_max_jobs))
            conn.executescript(schema)
            self._migrate_worker_metrics(conn)
            self._migrate_transfers(conn)
            self._migrate_device_lease_unique_constraint(conn)
            self._migrate_recovery_observability(conn)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_lease ON device_leases(device_id) WHERE status='active'")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_reservation ON cluster_device_reservations(device_id) WHERE status='active'")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_commands_operation ON cluster_commands(worker_id,operation_id) WHERE operation_id!=''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_timeline_job ON cluster_timeline_events(job_id,id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_timeline_trace ON cluster_timeline_events(trace_id,id)")

    @staticmethod
    def _migrate_transfers(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(cluster_transfers)")}
        if "owner_id" not in columns:
            conn.execute("ALTER TABLE cluster_transfers ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
        if "metadata_json" not in columns:
            conn.execute(
                "ALTER TABLE cluster_transfers ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @classmethod
    def _migrate_recovery_observability(cls, conn: sqlite3.Connection) -> None:
        for column, definition in {
            "session_id": "TEXT NOT NULL DEFAULT ''",
            "connection_generation": "INTEGER NOT NULL DEFAULT 0",
            "disconnected_at": "TEXT NOT NULL DEFAULT ''",
            "last_recovered_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            cls._ensure_column(conn, "cluster_workers", column, definition)
        for column, definition in {
            "operation_id": "TEXT NOT NULL DEFAULT ''",
            "trace_id": "TEXT NOT NULL DEFAULT ''",
        }.items():
            cls._ensure_column(conn, "cluster_commands", column, definition)
        for column, definition in {
            "trace_id": "TEXT NOT NULL DEFAULT ''",
            "state_version": "INTEGER NOT NULL DEFAULT 1",
            "recovery_count": "INTEGER NOT NULL DEFAULT 0",
            "last_transition_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            cls._ensure_column(conn, "cluster_jobs", column, definition)
        conn.execute(
            """UPDATE cluster_jobs SET trace_id=COALESCE(
                   NULLIF(json_extract(request_json, '$.trace_id'), ''),
                   NULLIF(json_extract(request_json, '$.automation_run_id'), ''),
                   'trace-' || id)
               WHERE trace_id=''"""
        )
        conn.execute(
            "UPDATE cluster_commands SET operation_id=id WHERE operation_id=''"
        )
        conn.execute(
            """UPDATE cluster_commands SET trace_id=COALESCE(
                   (SELECT NULLIF(trace_id, '') FROM cluster_jobs
                    WHERE cluster_jobs.id=cluster_commands.job_id),
                   NULLIF(json_extract(payload_json, '$.automation_run_id'), ''),
                   'trace-' || id)
               WHERE trace_id=''"""
        )

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
        for column in (
            "capabilities_json", "properties_json", "payload_json", "result_json",
            "metadata_json",
        ):
            if column in result:
                key = column.removesuffix("_json")
                try:
                    result[key] = json.loads(result.pop(column) or "{}")
                except json.JSONDecodeError:
                    result[key] = {}
        return result

    def delete_job(self, job_id: str) -> bool:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT status FROM cluster_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in {"completed", "failed", "cancelled"}:
                return False
            for table in ("cluster_job_events", "cluster_job_artifacts", "cluster_artifact_uploads", "device_leases",
                          "cluster_commands", "cluster_job_attempts", "cluster_timeline_events"):
                conn.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM cluster_jobs WHERE id=?", (job_id,))
            return True

    def create_job_with_leases(self, data: dict[str, Any]) -> dict[str, Any]:
        job_id = f"job-{uuid.uuid4().hex}"
        worker_id = data["worker_id"]
        worker = self.get_worker(worker_id)
        if worker is None:
            raise ValueError("worker not found")
        if worker.get("status") not in {"online", "busy"}:
            raise ValueError("worker is not online")
        devices = self._claim_devices(worker_id, data.get("devices", []))
        owner_id = str(data.get("owner_id") or "").strip()
        if not owner_id:
            raise ValueError("authenticated owner_id is required")
        owner_username = str(data.get("owner_username") or "").strip()
        reservation_id = str(data.get("device_reservation_id") or "")
        job_source = f"job:{job_id}"
        reservation_source = f"reservation:{reservation_id}"
        device_keys = [item["device_key"] for item in devices]
        transferred = False
        claim_records: list[dict[str, Any]] = []
        if reservation_id:
            reservation = self.get_reservation(reservation_id)
            if not reservation or reservation.get("status") != "active":
                raise ValueError("device reservation is missing or expired")
            if reservation.get("worker_id") != worker_id:
                raise ValueError("device reservation belongs to another Worker")
            if reservation.get("owner_id") != owner_id:
                raise ValueError("device reservation belongs to another owner")
            automation_run_id = str(data.get("automation_run_id") or "")
            if automation_run_id and reservation.get("source_id") != automation_run_id:
                raise ValueError("device reservation belongs to another automation run")
            reservation_devices = {
                item["id"] for item in reservation.get("devices") or []
            }
            if reservation_devices != set(device_keys):
                raise ValueError("test devices do not match the active reservation")
            transferred = self.claims.transfer(
                reservation_source,
                job_source,
                source_type="cluster-job",
                ttl_seconds=self.claim_lease_ttl_seconds,
                owner_id=owner_id,
                device_keys=device_keys,
            ) > 0
            if transferred:
                claim_records = [
                    claim for key in device_keys
                    if (claim := self.claims.active_claim(key)) is not None
                ]
        if not transferred:
            acquired, claim_records = self.claims.acquire(
                devices,
                owner_id=owner_id,
                username=owner_username or owner_id,
                source_type="cluster-job",
                source_id=job_source,
                ttl_seconds=self.claim_lease_ttl_seconds,
            )
            if not acquired:
                owner = claim_records[0].get("username") or claim_records[0].get("owner_id")
                raise ValueError(f"device is already claimed by {owner}")
        try:
            return self._create_job_with_leases_metadata(
                {**data, "_job_id": job_id, "_claim_records": claim_records}
            )
        except Exception:
            if transferred:
                restored = self.claims.transfer(
                    job_source,
                    reservation_source,
                    source_type="cluster-reservation",
                    ttl_seconds=6 * 60 * 60,
                    owner_id=owner_id,
                    device_keys=device_keys,
                )
                if restored <= 0:
                    self.claims.release(job_source, status="failed")
            else:
                self.claims.release(job_source, status="failed")
            raise

    def _create_job_with_leases_metadata(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        job_id = data["_job_id"]
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        lease_ids = []
        worker_id = data["worker_id"]
        requested_devices = data.get("devices", [])
        reservation_id = str(data.get("device_reservation_id") or "")
        trace_id = str(
            data.get("trace_id")
            or data.get("automation_run_id")
            or f"trace-{uuid.uuid4().hex}"
        )
        request_data = {
            **{key: value for key, value in data.items() if not key.startswith("_")},
            "trace_id": trace_id,
        }
        claim_records = {
            item["device_key"]: item for item in data.get("_claim_records") or []
        }
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_device_reservations(conn, now)
            worker = conn.execute("SELECT * FROM cluster_workers WHERE id=?", (worker_id,)).fetchone()
            if worker is None:
                raise ValueError("worker not found")
            if worker["status"] not in {"online", "busy"}:
                raise ValueError("worker is not online")
            if int(worker["running_jobs"]) >= int(worker["max_jobs"]):
                raise ValueError("worker capacity is exhausted")
            minimum_disk = float(os.getenv("GMS_CLUSTER_MIN_DISK_FREE_GB", "50"))
            if float(worker["disk_free_gb"] or 0) > 0 and float(worker["disk_free_gb"]) < minimum_disk:
                raise ValueError(
                    f"worker has less than {minimum_disk:.1f} GB free disk"
                )
            required_memory_gb = float(data.get("required_memory_gb", 0) or 0)
            available_memory_gb = float(worker["memory_available_gb"] or 0)
            if required_memory_gb and available_memory_gb and available_memory_gb < required_memory_gb:
                raise ValueError(
                    f"worker has {available_memory_gb:.1f} GB available memory; "
                    f"{required_memory_gb:.1f} GB is required"
                )
            active_rows = conn.execute("""SELECT request_json FROM cluster_jobs
                WHERE assigned_worker_id=? AND status IN ('assigned','dispatching','running','stopping')""",
                (worker_id,)).fetchall()
            if len(active_rows) >= int(worker["max_jobs"]):
                raise ValueError("worker already has the maximum number of active jobs")
            existing_exclusive = any(
                bool(json.loads(row["request_json"] or "{}").get("exclusive_host"))
                for row in active_rows
            )
            if existing_exclusive or (data.get("exclusive_host") and
                                      (active_rows or int(worker["running_jobs"]) > 0)):
                raise ValueError("a full-suite test requires exclusive use of the worker")
            reserved_ids: set[str] = set()
            if reservation_id:
                reservation_rows = conn.execute(
                    """SELECT * FROM cluster_device_reservations
                       WHERE reservation_id=? AND status='active'""",
                    (reservation_id,),
                ).fetchall()
                if not reservation_rows:
                    raise ValueError("device reservation is missing or expired")
                if any(row["worker_id"] != worker_id for row in reservation_rows):
                    raise ValueError("device reservation belongs to another Worker")
                source_id = str(data.get("automation_run_id") or "")
                if source_id and any(row["source_id"] != source_id for row in reservation_rows):
                    raise ValueError("device reservation belongs to another automation run")
                owner_id = str(data.get("owner_id") or "")
                if owner_id and any(row["owner_id"] != owner_id for row in reservation_rows):
                    raise ValueError("device reservation belongs to another owner")
                reserved_ids = {row["device_id"] for row in reservation_rows}
                normalized_requested = {
                    str(value) if str(value).startswith(f"{worker_id}:") else f"{worker_id}:{value}"
                    for value in requested_devices
                }
                if normalized_requested != reserved_ids:
                    raise ValueError("test devices do not match the active reservation")
            conn.execute("""INSERT INTO cluster_jobs
                (id,owner_id,source_type,requested_worker_id,assigned_worker_id,
                 suite_key,suite_path,request_json,status,priority,current_attempt_id,
                 trace_id,state_version,last_transition_at,created_at,queued_at,
                 started_at,finished_at,updated_at,error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,'','',?,'')""",
                (job_id, data.get("owner_id", ""), data.get("source_type", "manual"),
                 worker_id, worker_id, data.get("suite_key", ""), data.get("suite_path", ""),
                 json.dumps(request_data, separators=(",", ":")), "assigned",
                 data.get("priority", 100), attempt_id, trace_id, now, now, now, now))
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
                if device["state"] != "available" and device_id not in reserved_ids:
                    raise ValueError(f"device is not available: {raw_id}")
                if conn.execute("SELECT 1 FROM device_leases WHERE device_id=? AND status='active'", (device_id,)).fetchone():
                    raise ValueError(f"device is already leased: {raw_id}")
                claim = claim_records.get(device_id) or {}
                lease_id = str(claim.get("id") or f"lease-{uuid.uuid4().hex}")
                lease_ids.append(lease_id)
                generation = int(claim.get("generation") or 0)
                if generation <= 0:
                    generation = int(conn.execute(
                        """SELECT COALESCE(MAX(generation),0)+1
                           FROM device_leases WHERE device_id=?""",
                        (device_id,),
                    ).fetchone()[0] or 1)
                # SQLite computes expiry to keep lease timestamps consistent.
                conn.execute("""INSERT INTO device_leases
                    (id,device_id,worker_id,serial,job_id,attempt_id,owner_id,status,
                     generation,acquired_at,heartbeat_at,expires_at,released_at)
                    VALUES(?,?,?,?,?,?,?,'active',?,?,?,datetime('now',?),'')""",
                    (lease_id, device_id, worker_id, device["serial"], job_id, attempt_id,
                     data.get("owner_id", ""), generation, now, now,
                     f"+{self.claim_lease_ttl_seconds} seconds"))
                conn.execute("UPDATE cluster_worker_devices SET state='allocated',updated_at=? WHERE id=?", (now, device_id))
            if reservation_id:
                conn.execute(
                    """UPDATE cluster_device_reservations SET status='converted',released_at=?
                       WHERE reservation_id=? AND status='active'""",
                    (now, reservation_id),
                )
            self._append_timeline_conn(
                conn,
                job_id=job_id,
                attempt_id=attempt_id,
                trace_id=trace_id,
                worker_id=worker_id,
                event_type="job.created",
                source="controller",
                from_state="",
                to_state="assigned",
                message="Cluster Job created and device leases acquired",
                payload={
                    "device_ids": [
                        raw if str(raw).startswith(f"{worker_id}:") else f"{worker_id}:{raw}"
                        for raw in requested_devices
                    ],
                    "automation_run_id": data.get("automation_run_id", ""),
                },
            )
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

    def get_job_by_automation_run(self, automation_run_id: str) -> dict[str, Any] | None:
        if not automation_run_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id FROM cluster_jobs
                   WHERE json_extract(request_json, '$.automation_run_id')=?
                   ORDER BY created_at DESC LIMIT 1""",
                (automation_run_id,),
            ).fetchone()
        return self.get_job(row["id"]) if row else None

    def list_jobs(self, limit: int = 100, owner_id: str = "") -> list[dict[str, Any]]:
        with self.connect() as conn:
            if owner_id:
                rows = conn.execute(
                    "SELECT id FROM cluster_jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",
                    (owner_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM cluster_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [job for row in rows if (job := self.get_job(row["id"]))]

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
