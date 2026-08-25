"""State transitions, correlation ids, and durable Cluster timelines."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .state_machine import InvalidJobTransitionError, validate_job_transition


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

    def fail_abandoned_worker_lost_jobs(self, min_age_seconds: int = 3600) -> list[str]:
        """Fail ``worker_lost`` Cluster Jobs whose Worker never resumed them.

        ``worker_lost`` waits for the same Attempt to resume once the Worker
        reconnects.  A Worker that returns reports every persisted Attempt in
        its first heartbeat, so a Job still ``worker_lost`` long after the
        outage can never resume (the Worker lost it, or never received it).
        Terminalize it and release residual leases and the device claim so
        the owner stops showing as testing.
        """
        if min_age_seconds < 0:
            return []
        notified: list[tuple[str, str]] = []
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """SELECT id,owner_id FROM cluster_jobs
                   WHERE status='worker_lost' AND last_transition_at!=''
                     AND datetime(last_transition_at) <= datetime('now', ?)""",
                (f"-{int(min_age_seconds)} seconds",),
            ).fetchall()
            for row in rows:
                job_id = str(row["id"])
                error = "worker lost and did not reconnect in time"
                try:
                    transitioned = self._transition_job_conn(
                        conn,
                        job_id,
                        "failed",
                        source="controller-watchdog",
                        message=(
                            "Worker did not resume the lost Attempt in time; "
                            "failing the stale worker_lost Cluster Job"
                        ),
                        error=error,
                    )
                except InvalidJobTransitionError:
                    continue
                if not transitioned:
                    continue
                notified.append((job_id, str(row["owner_id"] or "")))
                now = _utc_now()
                attempt = conn.execute(
                    "SELECT current_attempt_id FROM cluster_jobs WHERE id=?", (job_id,)
                ).fetchone()
                if attempt is not None and attempt["current_attempt_id"]:
                    conn.execute(
                        """UPDATE cluster_job_attempts SET status='failed',
                           finished_at=?, error=? WHERE id=?
                           AND status NOT IN ('completed','failed','cancelled')""",
                        (now, error, attempt["current_attempt_id"]),
                    )
                leases = conn.execute(
                    """SELECT device_id FROM device_leases
                       WHERE job_id=? AND status IN ('active','orphaned')""",
                    (job_id,),
                ).fetchall()
                conn.execute(
                    """UPDATE device_leases SET status='released',released_at=?
                       WHERE job_id=? AND status IN ('active','orphaned')""",
                    (now, job_id),
                )
                conn.executemany(
                    """UPDATE cluster_worker_devices SET state='available',updated_at=?
                       WHERE id=?""",
                    [(now, item["device_id"]) for item in leases],
                )
                self.claims.release(f"job:{job_id}", status="failed")
        # Mirror transition_job(): notify the owner's frontend after commit.
        if notified:
            from foundation.events import EVENT_JOB_TRANSITION, event_bus

            for job_id, owner_id in notified:
                if owner_id:
                    event_bus.emit(
                        EVENT_JOB_TRANSITION,
                        {
                            "job_id": job_id,
                            "status": "failed",
                            "worker_id": "",
                            "_target_client_id": owner_id,
                        },
                    )
        return [job_id for job_id, _ in notified]

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
