"""Account-scoped submission receipts and process-safe creation guards."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager

from foundation.error_model import ApiError


def request_fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ClusterJobRequestRepositoryMixin:
    @contextmanager
    def job_request_guard(self, owner_id: str, key: str | None):
        """Serialize one key across Web processes; process exit releases the lock.

        A separate file lock avoids holding a SQLite write transaction across
        device claims and command commits. See ADR 0016.
        """
        if key is None:
            yield
            return
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise ApiError.malformed_request("Idempotency-Key must contain 1–128 ASCII letters, digits, or ._:-")
        lock_dir = self.db_path.parent / f".{self.db_path.name}.job-requests"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_name = request_fingerprint({"owner_id": owner_id, "key": key})
        with (lock_dir / lock_name).open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApiError.conflict(
                    "This job request is still being created",
                    next_actions=[{"action": "Retry with the same Idempotency-Key"}],
                ) from exc
            yield

    def find_job_request(self, owner_id: str, key: str, fingerprint: str) -> dict | None:
        with self.connect() as conn:
            receipt = conn.execute(
                "SELECT job_id,request_hash FROM cluster_job_requests WHERE owner_id=? AND request_key=?",
                (owner_id, key),
            ).fetchone()
        if receipt is None:
            return None
        if receipt["request_hash"] != fingerprint:
            raise ApiError.conflict("Idempotency-Key was already used with a different job request")
        job = self.get_job(receipt["job_id"])
        if job is None:
            raise ApiError.conflict("The job created by this Idempotency-Key has been deleted")
        return job

    def replay_job_command(self, job: dict) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT id FROM cluster_commands WHERE job_id=? AND attempt_id=?
                   AND command_type='start_test' ORDER BY created_at LIMIT 1""",
                (job["id"], job["current_attempt_id"]),
            ).fetchone()
            attempt = conn.execute(
                "SELECT attempt_number FROM cluster_job_attempts WHERE id=?", (job["current_attempt_id"],),
            ).fetchone()
        if row:
            return self.get_command(row["id"])
        if job["status"] != "assigned" or not attempt or attempt["attempt_number"] != 1:
            return None
        # Resume only the initial job/command commit gap. Later attempts and
        # terminal states are owned by the lifecycle, not submission retries.
        data = job.get("request") or {}
        lease_generations = {
            lease["device_id"]: lease["generation"]
            for lease in job.get("leases") or [] if lease["status"] == "active"
        }
        worker_id = job["assigned_worker_id"]
        requested_keys = {
            value if value.startswith(f"{worker_id}:") else f"{worker_id}:{value}"
            for value in (str(item).strip() for item in data.get("devices") or []) if value
        }
        fenced = bool(requested_keys) and requested_keys.issubset(lease_generations)
        for device in self._claim_devices(job["assigned_worker_id"], data.get("devices") or []):
            key = device["device_key"]
            claim = self.claims.active_claim(key)
            if (not claim or claim["source_id"] != f"job:{job['id']}"
                    or claim["owner_id"] != job["owner_id"]
                    or (key in lease_generations and claim["generation"] != lease_generations[key])):
                fenced = False
                break
        if not fenced:
            self.compensate_failed_dispatch(job["id"], ValueError("device claim was lost before dispatch"))
            raise ApiError.conflict(
                "The original job lost its device claim before dispatch",
                details={"job_id": job["id"]},
                next_actions=[{"action": "Inspect the failed job before submitting a new request"}],
            )
        try:
            return self.dispatch_job_start_command(
                job, argv=data["argv"], execution_spec=data.get("execution_spec"),
                env=data.get("env") or {}, devices=data.get("devices") or [],
            )
        except Exception as exc:
            self.compensate_failed_dispatch(job["id"], exc)
            raise ApiError.dependency_unavailable(
                "Could not queue the original job", details={"job_id": job["id"]},
            ) from exc
