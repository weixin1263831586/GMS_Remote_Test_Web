"""SQLite schema DDL and migrations for the cluster repository.

The repository class orchestrates when these run; this module owns the SQL so
schema changes stay reviewable in one place.
"""

from __future__ import annotations

import sqlite3

from .config import ClusterConfig


REQUIRED_TABLES = frozenset({
    "cluster_workers",
    "cluster_worker_devices",
    "cluster_worker_suites",
    "cluster_worker_tests",
    "cluster_commands",
    "cluster_command_events",
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


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_transfers(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(cluster_transfers)")}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE cluster_transfers ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
    if "metadata_json" not in columns:
        conn.execute(
            "ALTER TABLE cluster_transfers ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
        )


def migrate_recovery_observability(conn: sqlite3.Connection) -> None:
    for column, definition in {
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "connection_generation": "INTEGER NOT NULL DEFAULT 0",
        "disconnected_at": "TEXT NOT NULL DEFAULT ''",
        "last_recovered_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        ensure_column(conn, "cluster_workers", column, definition)
    for column, definition in {
        "operation_id": "TEXT NOT NULL DEFAULT ''",
        "trace_id": "TEXT NOT NULL DEFAULT ''",
        "terminal_notified_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        ensure_column(conn, "cluster_commands", column, definition)
    for column, definition in {
        "trace_id": "TEXT NOT NULL DEFAULT ''",
        "state_version": "INTEGER NOT NULL DEFAULT 1",
        "recovery_count": "INTEGER NOT NULL DEFAULT 0",
        "last_transition_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        ensure_column(conn, "cluster_jobs", column, definition)
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


def migrate_device_lease_unique_constraint(conn: sqlite3.Connection) -> None:
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


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_lease ON device_leases(device_id) WHERE status='active'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_reservation ON cluster_device_reservations(device_id) WHERE status='active'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cluster_commands_operation ON cluster_commands(worker_id,operation_id) WHERE operation_id!=''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_timeline_job ON cluster_timeline_events(job_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_timeline_trace ON cluster_timeline_events(trace_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_metrics_time ON cluster_worker_metrics(worker_id,recorded_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_job_events_job ON cluster_job_events(job_id,sequence)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_command_events_cmd ON cluster_command_events(command_id,sequence)")


def apply_schema(conn: sqlite3.Connection) -> None:
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
            disk_total_gb REAL NOT NULL DEFAULT 0,
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
            acknowledged_at TEXT NOT NULL,
            terminal_notified_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS cluster_command_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL,
            worker_id TEXT NOT NULL, sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
            message TEXT NOT NULL, payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(command_id, sequence)
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
        CREATE TABLE IF NOT EXISTS cluster_worker_metrics (
            worker_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            cpu_percent REAL NOT NULL DEFAULT 0,
            memory_percent REAL NOT NULL DEFAULT 0,
            disk_free_gb REAL NOT NULL DEFAULT 0,
            running_jobs INTEGER NOT NULL DEFAULT 0,
            external_jobs INTEGER NOT NULL DEFAULT 0
        );
    """.replace("__MAX_JOBS__", str(ClusterConfig.load().default_max_jobs))
    conn.executescript(schema)
