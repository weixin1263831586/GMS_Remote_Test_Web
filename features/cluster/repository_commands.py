"""Idempotent Worker command delivery and Cluster Job synchronization."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .state_machine import InvalidJobTransitionError


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterCommandRepositoryMixin:
    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            return self._decode(conn.execute(
                "SELECT * FROM cluster_commands WHERE id=?", (command_id,)
            ).fetchone())

    def find_correlated_command(
        self, worker_id: str, command_type: str, key: str, value: str
    ) -> dict[str, Any] | None:
        if not value or key not in {"automation_run_id", "transfer_id"}:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM cluster_commands
                   WHERE worker_id=? AND command_type=?
                     AND json_extract(payload_json, ?) = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (worker_id, command_type, f"$.{key}", value),
            ).fetchone()
        return self._decode(row)

    def compact_command_result(
        self, command_id: str, result: dict[str, Any]
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE cluster_commands SET result_json=?,updated_at=? WHERE id=?",
                (json.dumps(result, separators=(",", ":")), _utc_now(), command_id),
            )

    def create_command(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        command_id = f"cmd-{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        requested_operation_id = str(data.get("operation_id") or "")
        operation_id = requested_operation_id or command_id
        job_id = str(data.get("job_id") or "")
        attempt_id = str(data.get("attempt_id") or "")
        with self._lock, self.connect() as conn:
            if requested_operation_id:
                existing = conn.execute(
                    """SELECT * FROM cluster_commands
                       WHERE worker_id=? AND operation_id=?""",
                    (data["worker_id"], requested_operation_id),
                ).fetchone()
                if existing is not None:
                    return self._decode(existing) or {}
            job = conn.execute(
                "SELECT trace_id FROM cluster_jobs WHERE id=?", (job_id,)
            ).fetchone() if job_id else None
            trace_id = str(
                data.get("trace_id")
                or (job["trace_id"] if job else "")
                or (data.get("payload") or {}).get("automation_run_id")
                or f"trace-{uuid.uuid4().hex}"
            )
            conn.execute("""
                INSERT INTO cluster_commands
                    (id,worker_id,command_type,job_id,attempt_id,dispatch_token,
                     payload_json,operation_id,trace_id,status,result_json,error,created_at,
                     delivered_at,acknowledged_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'queued','{}','',?,'','',?)
            """, (
                command_id, data["worker_id"], data["command_type"], job_id,
                attempt_id, token,
                json.dumps(data.get("payload", {}), separators=(",", ":")),
                operation_id, trace_id, now, now,
            ))
            self._append_timeline_conn(
                conn,
                job_id=job_id,
                attempt_id=attempt_id,
                trace_id=trace_id,
                operation_id=operation_id,
                worker_id=data["worker_id"],
                event_type="command.queued",
                source="controller",
                from_state="",
                to_state="queued",
                message=f"Queued Worker command {data['command_type']}",
                payload={"command_id": command_id, "command_type": data["command_type"]},
            )
        return self.get_command(command_id) or {}

    def append_command_events(
        self, worker_id: str, command_id: str, events: list[dict[str, Any]]
    ) -> int:
        """Worker 命令过程日志（实时日志通道）。

        与 job events 平行：烧写/device_action 等长命令把 fastboot/
        upgrade_tool 逐行输出上报到这里；sequence 由 Worker 维护，
        UNIQUE(command_id, sequence) 幂等去重。
        """
        inserted = 0
        now = _utc_now()
        with self.connect() as conn:
            owner = conn.execute(
                "SELECT worker_id FROM cluster_commands WHERE id=?", (command_id,)
            ).fetchone()
            if owner is None or str(owner["worker_id"]) != worker_id:
                return 0
            for event in events:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO cluster_command_events
                        (command_id,worker_id,sequence,event_type,source,level,
                         message,payload_json,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        command_id,
                        worker_id,
                        int(event.get("sequence", 0)),
                        str(event.get("event_type", "log")),
                        str(event.get("source", "worker")),
                        str(event.get("level", "info")),
                        str(event.get("message", "")),
                        json.dumps(event.get("payload", {}), separators=(",", ":")),
                        now,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def list_command_events(
        self, command_id: str, after: int = -1, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM cluster_command_events
                   WHERE command_id=? AND sequence>? ORDER BY sequence LIMIT ?""",
                (command_id, after, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    def poll_commands(
        self, worker_id: str, limit: int = 5, redelivery_seconds: int = 120
    ) -> list[dict[str, Any]]:
        now = _utc_now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE cluster_commands SET status='queued',updated_at=?
                   WHERE worker_id=? AND status='delivered'
                     AND datetime(delivered_at) <= datetime('now', ?)""",
                (now, worker_id, f"-{max(1, redelivery_seconds)} seconds"),
            )
            rows = conn.execute(
                """SELECT * FROM cluster_commands
                   WHERE worker_id=? AND status='queued'
                   ORDER BY created_at LIMIT ?""",
                (worker_id, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                conn.executemany(
                    """UPDATE cluster_commands SET status='delivered',
                       delivered_at=?,updated_at=? WHERE id=?""",
                    [(now, now, item) for item in ids],
                )
                for row in rows:
                    self._append_timeline_conn(
                        conn,
                        job_id=row["job_id"],
                        attempt_id=row["attempt_id"],
                        trace_id=row["trace_id"],
                        operation_id=row["operation_id"],
                        worker_id=worker_id,
                        event_type="command.delivered",
                        source="controller",
                        from_state="queued",
                        to_state="delivered",
                        message=f"Delivered Worker command {row['command_type']}",
                        payload={"command_id": row["id"]},
                    )
            return [self._decode(row) or {} for row in rows]

    def ack_command(
        self, worker_id: str, command_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = _utc_now()
        with self._lock, self.connect() as conn:
            current = conn.execute(
                "SELECT * FROM cluster_commands WHERE id=? AND worker_id=?",
                (command_id, worker_id),
            ).fetchone()
            if current is None:
                return None
            current_status = current["status"]
            incoming_status = data["status"]
            terminal = {"completed", "failed", "cancelled"}
            progress_rank = {"queued": 0, "delivered": 1, "accepted": 2, "running": 3}
            if (
                current_status in terminal
                or progress_rank.get(incoming_status, 4)
                < progress_rank.get(current_status, 0)
            ):
                return self._decode(current)
            conn.execute("""
                UPDATE cluster_commands SET status=?,result_json=?,error=?,
                    acknowledged_at=?,updated_at=? WHERE id=? AND worker_id=?
            """, (
                incoming_status,
                json.dumps(data.get("result", {}), separators=(",", ":")),
                data.get("error", ""), now, now, command_id, worker_id,
            ))
            self._append_timeline_conn(
                conn,
                job_id=current["job_id"],
                attempt_id=current["attempt_id"],
                trace_id=current["trace_id"],
                operation_id=current["operation_id"],
                worker_id=worker_id,
                event_type="command.acknowledged",
                source="worker",
                level="error" if incoming_status == "failed" else "info",
                from_state=current_status,
                to_state=incoming_status,
                message=f"Worker command {current['command_type']} is {incoming_status}",
                payload={"command_id": command_id, "error": data.get("error", "")},
            )
        return self.get_command(command_id)

    def claim_terminal_notification(self, command_id: str) -> bool:
        """Atomically reserve the one terminal notification for a command."""
        now = _utc_now()
        with self._lock, self.connect() as conn:
            updated = conn.execute(
                """UPDATE cluster_commands
                   SET terminal_notified_at=?
                   WHERE id=? AND terminal_notified_at=''
                     AND status IN ('completed','failed','cancelled')""",
                (now, command_id),
            )
            return updated.rowcount == 1

    def attach_command_to_job(self, job_id: str, command: dict[str, Any]) -> None:
        with self._lock, self.connect() as conn:
            job = conn.execute(
                "SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job:
                raise ValueError("job not found")
            self._transition_job_conn(
                conn,
                job_id,
                "dispatching",
                source="controller",
                message="Start command attached to Cluster Job",
                operation_id=str(command.get("operation_id") or command.get("id") or ""),
                worker_id=str(command.get("worker_id") or ""),
                payload={"command_id": command.get("id", "")},
            )
            conn.execute(
                "UPDATE cluster_job_attempts SET status='dispatching' WHERE id=?",
                (job["current_attempt_id"],),
            )

    def sync_job_from_command(self, command: dict[str, Any]) -> None:
        job_id = command.get("job_id", "")
        status = command["status"]
        if not job_id or status not in {"running", "completed", "failed", "cancelled"}:
            return
        result = command.get("result", {})
        job_status = "running" if status == "running" else status
        if command.get("command_type") == "stop_test" and status == "completed":
            job_status = status = "cancelled"
        now = _utc_now()
        release_claim = False
        with self._lock, self.connect() as conn:
            job = conn.execute(
                "SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job:
                return
            if command.get("attempt_id") and command["attempt_id"] != job["current_attempt_id"]:
                self._append_timeline_conn(
                    conn,
                    job_id=job_id,
                    attempt_id=str(command.get("attempt_id") or ""),
                    trace_id=str(command.get("trace_id") or ""),
                    operation_id=str(command.get("operation_id") or ""),
                    worker_id=str(command.get("worker_id") or ""),
                    event_type="command.stale_attempt",
                    source="controller",
                    level="warning",
                    message="Ignored command result from a stale Attempt",
                    payload={"command_id": command.get("id", "")},
                )
                return
            try:
                transitioned = self._transition_job_conn(
                    conn,
                    job_id,
                    job_status,
                    source="worker-ack",
                    message=f"Worker command moved Cluster Job to {job_status}",
                    error=str(command.get("error") or ""),
                    operation_id=str(command.get("operation_id") or ""),
                    worker_id=str(command.get("worker_id") or ""),
                    payload={
                        "command_id": command.get("id", ""),
                        "command_type": command.get("command_type", ""),
                    },
                )
            except InvalidJobTransitionError:
                return
            worker_job_id = result.get("worker_job_id", "")
            conn.execute("""UPDATE cluster_job_attempts SET status=?,worker_job_id=?,
                result_json=?,error=?,
                started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,
                finished_at=CASE WHEN ? IN ('completed','failed','cancelled')
                    THEN ? ELSE finished_at END WHERE id=?""", (
                status, worker_job_id, json.dumps(result, separators=(",", ":")),
                command.get("error", ""), status, now, status, now,
                job["current_attempt_id"],
            ))
            if transitioned and status in {"completed", "failed", "cancelled"}:
                release_claim = True
                leases = conn.execute(
                    """SELECT device_id FROM device_leases
                       WHERE job_id=? AND status='active'""",
                    (job_id,),
                ).fetchall()
                conn.execute(
                    """UPDATE device_leases SET status='released',released_at=?
                       WHERE job_id=? AND status='active'""",
                    (now, job_id),
                )
                conn.executemany(
                    """UPDATE cluster_worker_devices
                       SET state='available',updated_at=? WHERE id=?""",
                    [(now, item["device_id"]) for item in leases],
                )
        if release_claim:
            self.claims.release(
                f"job:{job_id}",
                status="cancelled" if status == "cancelled" else "released",
            )
