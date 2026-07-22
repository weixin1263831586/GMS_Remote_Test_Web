"""Durable, process-safe device claims shared by every execution mode."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires(ttl_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(30, int(ttl_seconds)))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeviceClaimRegistry:
    """Atomic ownership registry for local, cluster, and automation devices."""

    def __init__(self, db_path: str | Path | None):
        self.db_path = Path(db_path) if db_path is not None else None
        self._lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            conn = self._memory_conn
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        schema_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='device_claims'"""
        ).fetchone() is not None
        if not schema_exists:
            self._initialize(conn)
        else:
            self._initialized = True
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_claims (
                id TEXT PRIMARY KEY,
                device_key TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                serial TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                username TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL,
                generation INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_device_claim
                ON device_claims(device_key) WHERE status='active';
            CREATE INDEX IF NOT EXISTS idx_device_claim_source
                ON device_claims(source_id,status);
            CREATE INDEX IF NOT EXISTS idx_device_claim_owner
                ON device_claims(owner_id,status);
            """
        )
        conn.commit()
        self._initialized = True

    def _close(self, conn: sqlite3.Connection) -> None:
        if self.db_path is not None:
            conn.close()

    @staticmethod
    def _claim_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _normalize_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in devices:
            key = str(item.get("device_key") or "").strip()
            worker_id = str(item.get("worker_id") or "").strip()
            serial = str(item.get("serial") or "").strip()
            if not key or not worker_id or not serial or key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "device_key": key,
                    "worker_id": worker_id,
                    "serial": serial,
                    "generation_floor": max(
                        1, int(item.get("generation_floor") or 1)
                    ),
                }
            )
        if not normalized:
            raise ValueError("at least one device claim is required")
        return normalized

    def _expire(self, conn: sqlite3.Connection, now: str) -> int:
        cursor = conn.execute(
            """UPDATE device_claims SET status='expired',released_at=?
               WHERE status='active' AND expires_at<=?""",
            (now, now),
        )
        return cursor.rowcount

    def acquire(
        self,
        devices: list[dict[str, Any]],
        *,
        owner_id: str,
        username: str,
        source_type: str,
        source_id: str,
        ttl_seconds: int,
        allow_existing_source: bool = True,
    ) -> tuple[bool, list[dict[str, Any]]]:
        requested = self._normalize_devices(devices)
        owner_id = str(owner_id or "").strip()
        source_id = str(source_id or "").strip()
        if not owner_id or not source_id:
            raise ValueError("device claim owner and source are required")
        now = _now()
        expires_at = _expires(ttl_seconds)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._expire(conn, now)
                conflicts: list[dict[str, Any]] = []
                existing_by_key: dict[str, sqlite3.Row] = {}
                for device in requested:
                    row = conn.execute(
                        "SELECT * FROM device_claims WHERE device_key=? AND status='active'",
                        (device["device_key"],),
                    ).fetchone()
                    if row:
                        existing_by_key[device["device_key"]] = row
                        if (
                            row["owner_id"] != owner_id
                            or row["source_id"] != source_id
                            or not allow_existing_source
                        ):
                            conflicts.append(self._claim_dict(row))
                if conflicts:
                    conn.rollback()
                    return False, conflicts

                records = []
                for device in requested:
                    existing = existing_by_key.get(device["device_key"])
                    if existing:
                        conn.execute(
                            """UPDATE device_claims SET heartbeat_at=?,expires_at=?
                               WHERE id=? AND status='active'""",
                            (now, expires_at, existing["id"]),
                        )
                        records.append(
                            {
                                **self._claim_dict(existing),
                                "heartbeat_at": now,
                                "expires_at": expires_at,
                            }
                        )
                        continue
                    generation = max(
                        int(device["generation_floor"]),
                        int(
                        conn.execute(
                            """SELECT COALESCE(MAX(generation),0)+1
                               FROM device_claims WHERE device_key=?""",
                            (device["device_key"],),
                        ).fetchone()[0]
                        ),
                    )
                    claim_id = f"claim-{uuid.uuid4().hex}"
                    conn.execute(
                        """INSERT INTO device_claims
                           (id,device_key,worker_id,serial,owner_id,username,
                            source_type,source_id,status,generation,acquired_at,
                            heartbeat_at,expires_at,released_at)
                           VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,'')""",
                        (
                            claim_id,
                            device["device_key"],
                            device["worker_id"],
                            device["serial"],
                            owner_id,
                            str(username or owner_id),
                            str(source_type or "unknown"),
                            source_id,
                            generation,
                            now,
                            now,
                            expires_at,
                        ),
                    )
                    records.append(
                        dict(
                            id=claim_id,
                            **{
                                key: value for key, value in device.items()
                                if key != "generation_floor"
                            },
                            owner_id=owner_id,
                            username=str(username or owner_id),
                            source_type=str(source_type or "unknown"),
                            source_id=source_id,
                            status="active",
                            generation=generation,
                            acquired_at=now,
                            heartbeat_at=now,
                            expires_at=expires_at,
                            released_at="",
                        )
                    )
                conn.commit()
                return True, records
            finally:
                self._close(conn)

    def active_claim(self, device_key: str) -> dict[str, Any] | None:
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                self._expire(conn, now)
                row = conn.execute(
                    "SELECT * FROM device_claims WHERE device_key=? AND status='active'",
                    (device_key,),
                ).fetchone()
                conn.commit()
                return self._claim_dict(row) if row else None
            finally:
                self._close(conn)

    def list_active(
        self,
        *,
        worker_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = _now()
        clauses = ["status='active'"]
        values: list[str] = []
        if worker_id:
            clauses.append("worker_id=?")
            values.append(worker_id)
        if owner_id:
            clauses.append("owner_id=?")
            values.append(owner_id)
        with self._lock:
            conn = self._connect()
            try:
                self._expire(conn, now)
                rows = conn.execute(
                    f"SELECT * FROM device_claims WHERE {' AND '.join(clauses)}",
                    values,
                ).fetchall()
                conn.commit()
                return [self._claim_dict(row) for row in rows]
            finally:
                self._close(conn)

    def renew(
        self,
        source_id: str,
        ttl_seconds: int,
        *,
        device_keys: list[str] | None = None,
    ) -> int:
        now = _now()
        clauses = ["source_id=?", "status='active'"]
        values: list[Any] = [source_id]
        if device_keys:
            placeholders = ",".join("?" for _ in device_keys)
            clauses.append(f"device_key IN ({placeholders})")
            values.extend(device_keys)
        with self._lock:
            conn = self._connect()
            try:
                self._expire(conn, now)
                cursor = conn.execute(
                    f"""UPDATE device_claims SET heartbeat_at=?,expires_at=?
                        WHERE {' AND '.join(clauses)}""",
                    [now, _expires(ttl_seconds), *values],
                )
                conn.commit()
                return cursor.rowcount
            finally:
                self._close(conn)

    def transfer(
        self,
        old_source_id: str,
        new_source_id: str,
        *,
        source_type: str,
        ttl_seconds: int,
        owner_id: str | None = None,
        device_keys: list[str] | None = None,
    ) -> int:
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._expire(conn, now)
                rows = conn.execute(
                    "SELECT * FROM device_claims WHERE source_id=? AND status='active'",
                    (old_source_id,),
                ).fetchall()
                if not rows:
                    conn.rollback()
                    return 0
                if owner_id and any(row["owner_id"] != owner_id for row in rows):
                    conn.rollback()
                    return 0
                if device_keys and {row["device_key"] for row in rows} != set(device_keys):
                    conn.rollback()
                    return 0
                cursor = conn.execute(
                    """UPDATE device_claims SET source_id=?,source_type=?,
                           heartbeat_at=?,expires_at=?
                       WHERE source_id=? AND status='active'""",
                    (
                        new_source_id,
                        source_type,
                        now,
                        _expires(ttl_seconds),
                        old_source_id,
                    ),
                )
                conn.commit()
                return cursor.rowcount
            finally:
                self._close(conn)

    def release(
        self,
        source_id: str,
        *,
        status: str = "released",
        device_keys: list[str] | None = None,
    ) -> int:
        if status not in {"released", "cancelled", "converted", "expired", "failed"}:
            raise ValueError("invalid device claim release status")
        clauses = ["source_id=?", "status='active'"]
        values: list[Any] = [source_id]
        if device_keys:
            placeholders = ",".join("?" for _ in device_keys)
            clauses.append(f"device_key IN ({placeholders})")
            values.extend(device_keys)
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    f"""UPDATE device_claims SET status=?,released_at=?
                        WHERE {' AND '.join(clauses)}""",
                    [status, now, *values],
                )
                conn.commit()
                return cursor.rowcount
            finally:
                self._close(conn)

    def force_release(self, device_key: str) -> dict[str, Any] | None:
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM device_claims WHERE device_key=? AND status='active'",
                    (device_key,),
                ).fetchone()
                if row:
                    conn.execute(
                        """UPDATE device_claims SET status='cancelled',released_at=?
                           WHERE id=? AND status='active'""",
                        (now, row["id"]),
                    )
                    conn.commit()
                return self._claim_dict(row) if row else None
            finally:
                self._close(conn)
