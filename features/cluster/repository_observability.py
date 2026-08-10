"""State transitions, correlation ids, and durable Cluster timelines."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .state_machine import validate_job_transition


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterObservabilityRepositoryMixin:
    @staticmethod
    def _append_timeline_conn(
        conn: sqlite3.Connection,
        *,
        event_type: str,
        source: str,
        message: str,
        job_id: str = "",
        attempt_id: str = "",
        trace_id: str = "",
        operation_id: str = "",
        worker_id: str = "",
        level: str = "info",
        from_state: str = "",
        to_state: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO cluster_timeline_events
               (job_id,attempt_id,trace_id,operation_id,worker_id,event_type,
                source,level,from_state,to_state,message,payload_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                attempt_id,
                trace_id,
                operation_id,
                worker_id,
                event_type,
                source,
                level,
                from_state,
                to_state,
                message,
                json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                _utc_now(),
            ),
        )

    def append_timeline(self, **event: Any) -> None:
        with self.connect() as conn:
            self._append_timeline_conn(conn, **event)

    def list_timeline(
        self,
        *,
        job_id: str = "",
        worker_id: str = "",
        trace_id: str = "",
        after: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        where = ["id>?"]
        params: list[Any] = [max(0, int(after or 0))]
        if job_id:
            where.append("job_id=?")
            params.append(job_id)
        if worker_id:
            where.append("worker_id=?")
            params.append(worker_id)
        if trace_id:
            where.append("trace_id=?")
            params.append(trace_id)
        params.append(max(1, min(int(limit or 500), 2000)))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM cluster_timeline_events WHERE {' AND '.join(where)} "
                "ORDER BY id LIMIT ?",
                params,
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            try:
                event["payload"] = json.loads(event.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                event["payload"] = {}
            events.append(event)
        return events

    def _transition_job_conn(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        to_status: str,
        *,
        source: str,
        message: str,
        error: str = "",
        operation_id: str = "",
        worker_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        job = conn.execute(
            """SELECT status,current_attempt_id,trace_id,state_version,
                      assigned_worker_id FROM cluster_jobs WHERE id=?""",
            (job_id,),
        ).fetchone()
        if job is None:
            return False
        from_status = str(job["status"] or "")
        if from_status == to_status:
            return False
        validate_job_transition(from_status, to_status)
        now = _utc_now()
        terminal = to_status in {"completed", "failed", "cancelled"}
        cursor = conn.execute(
            """UPDATE cluster_jobs SET status=?,error=?,updated_at=?,
                      last_transition_at=?,state_version=state_version+1,
                      started_at=CASE WHEN ?='running' AND started_at='' THEN ? ELSE started_at END,
                      finished_at=CASE WHEN ? THEN ? ELSE finished_at END,
                      recovery_count=recovery_count+CASE
                          WHEN status='worker_lost' AND ?='running' THEN 1 ELSE 0 END
               WHERE id=? AND state_version=?""",
            (
                to_status,
                error,
                now,
                now,
                to_status,
                now,
                1 if terminal else 0,
                now,
                to_status,
                job_id,
                job["state_version"],
            ),
        )
        if cursor.rowcount != 1:
            return False
        self._append_timeline_conn(
            conn,
            job_id=job_id,
            attempt_id=job["current_attempt_id"],
            trace_id=job["trace_id"],
            operation_id=operation_id,
            worker_id=worker_id or job["assigned_worker_id"],
            event_type="job.transition",
            source=source,
            level="error" if to_status in {"failed", "worker_lost"} else "info",
            from_state=from_status,
            to_state=to_status,
            message=message,
            payload=payload,
        )
        return True

    def transition_job(
        self,
        job_id: str,
        to_status: str,
        *,
        source: str = "controller",
        message: str = "",
        error: str = "",
        operation_id: str = "",
        worker_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock, self.connect() as conn:
            result = self._transition_job_conn(
                conn,
                job_id,
                to_status,
                source=source,
                message=message or f"Cluster Job entered {to_status}",
                error=error,
                operation_id=operation_id,
                worker_id=worker_id,
                payload=payload,
            )
        if result:
            from foundation.events import EVENT_JOB_TRANSITION, event_bus

            job = self.get_job(job_id) or {}
            owner_id = str(job.get("owner_id") or "")
            if owner_id:
                event_bus.emit(
                    EVENT_JOB_TRANSITION,
                    {
                        "job_id": job_id,
                        "status": to_status,
                        "worker_id": worker_id,
                        "_target_client_id": owner_id,
                    },
                )
        return result

    def validate_worker_session(
        self, worker_id: str, session_id: str = "", generation: int = 0
    ) -> bool:
        worker = self.get_worker(worker_id)
        if not worker:
            return False
        expected_session = str(worker.get("session_id") or "")
        expected_generation = int(worker.get("connection_generation") or 0)
        if not expected_session:
            return not session_id and not generation
        return (
            expected_session == str(session_id or "")
            and expected_generation == int(generation or 0)
        )
