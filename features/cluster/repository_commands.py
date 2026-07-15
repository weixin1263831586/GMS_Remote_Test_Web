"""Command delivery and job synchronization persistence."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any


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
        """Replace transient or binary-heavy output with durable metadata."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE cluster_commands SET result_json=?,updated_at=? WHERE id=?",
                (json.dumps(result, separators=(",", ":")), _utc_now(), command_id),
            )

    def create_command(self, data: dict[str, Any]) -> dict[str, Any]:
        now = _utc_now()
        command_id = f"cmd-{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cluster_commands
                    (id,worker_id,command_type,job_id,attempt_id,dispatch_token,
                     payload_json,status,result_json,error,created_at,delivered_at,
                     acknowledged_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'queued','{}','',?,'','',?)""",
                (
                    command_id, data["worker_id"], data["command_type"],
                    data.get("job_id", ""), data.get("attempt_id", ""), token,
                    json.dumps(data.get("payload", {}), separators=(",", ":")),
                    now, now,
                ),
            )
        return self.get_command(command_id) or {}

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
            rank = {"queued": 0, "delivered": 1, "accepted": 2, "running": 3}
            if (
                current_status in terminal
                or rank.get(incoming_status, 4) < rank.get(current_status, 0)
            ):
                return self._decode(current)
            conn.execute(
                """UPDATE cluster_commands SET status=?,result_json=?,error=?,
                   acknowledged_at=?,updated_at=? WHERE id=? AND worker_id=?""",
                (
                    incoming_status,
                    json.dumps(data.get("result", {}), separators=(",", ":")),
                    data.get("error", ""), now, now, command_id, worker_id,
                ),
            )
        return self.get_command(command_id)

    def attach_command_to_job(
        self, job_id: str, command: dict[str, Any]
    ) -> None:
        now = _utc_now()
        with self.connect() as conn:
            job = conn.execute(
                "SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job:
                raise ValueError("job not found")
            conn.execute(
                "UPDATE cluster_jobs SET status='dispatching',updated_at=? WHERE id=?",
                (now, job_id),
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
        now = _utc_now()
        result = command.get("result", {})
        job_status = "running" if status == "running" else status
        if command.get("command_type") == "stop_test" and status == "completed":
            job_status = status = "cancelled"
        with self.connect() as conn:
            job = conn.execute(
                "SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not job:
                return
            conn.execute(
                """UPDATE cluster_job_attempts SET status=?,worker_job_id=?,
                   result_json=?,error=?,
                   started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,
                   finished_at=CASE WHEN ? IN ('completed','failed','cancelled')
                       THEN ? ELSE finished_at END WHERE id=?""",
                (
                    status, result.get("worker_job_id", ""),
                    json.dumps(result, separators=(",", ":")),
                    command.get("error", ""), status, now, status, now,
                    job["current_attempt_id"],
                ),
            )
            conn.execute(
                "UPDATE cluster_jobs SET status=?,updated_at=?,error=? WHERE id=?",
                (job_status, now, command.get("error", ""), job_id),
            )
            if status not in {"completed", "failed", "cancelled"}:
                return
            conn.execute(
                "UPDATE cluster_jobs SET finished_at=? WHERE id=?", (now, job_id)
            )
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
