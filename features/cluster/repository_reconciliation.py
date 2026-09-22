"""Crash split-brain reconciliation between cluster state and physical claims.

Reservation/job state (cluster DB) and physical device claims (claims DB)
span two databases and can never share one transaction; a crash between the
two commits leaves ghost claims or unfenced reservations/jobs (ADR 0011). ``reconcile_claims`` repairs that drift idempotently at startup —
per-source repair instead of whole-DB rebuild. Kept as a separate mixin so
``repository.py`` stays under its migration line limit.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClusterReconciliationRepositoryMixin:
    def reconcile_claims(self) -> dict[str, int]:
        """Repair cross-DB drift between cluster state and physical claims.

        Repairs, all idempotent:
        - reservation without its ``reservation:<id>`` claim → re-acquire
          the claim (a crash between claim-release and metadata update, or
          claim TTL expiry while the reservation is still active);
        - expired/released reservation with a live claim → release the
          claim (ghost claim fencing the device after a crash);
        - terminal job (completed/failed/cancelled) with a live
          ``job:<id>`` claim → release the claim;
        - non-terminal job without a claim → re-acquire fencing from the
          job's ``device_leases``; when the device is already taken by
          someone else the job is failed (fail closed) — a running job
          without physical fencing must never continue.

        Returns per-repair counters for observability/tests.
        """
        stats = {
            "reservation_claims_reacquired": 0,
            "reservation_claims_released": 0,
            "job_claims_released": 0,
            "job_claims_reacquired": 0,
            "unfenceable_jobs_failed": 0,
        }
        now = _now()
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_device_reservations(conn, now)
            reservation_rows = conn.execute(
                """SELECT DISTINCT reservation_id, worker_id, owner_id, status
                   FROM cluster_device_reservations"""
            ).fetchall()
            for row in reservation_rows:
                source_id = f"reservation:{row['reservation_id']}"
                claims = self.claims.list_by_source(source_id)
                if row["status"] == "active" and not claims:
                    # Active reservation lost its physical claim: re-fence.
                    device_rows = conn.execute(
                        """SELECT device_id FROM cluster_device_reservations
                           WHERE reservation_id=?""",
                        (row["reservation_id"],),
                    ).fetchall()
                    acquired, _conflicts = self.claims.acquire(
                        self._claim_devices(
                            row["worker_id"],
                            [item["device_id"] for item in device_rows],
                        ),
                        owner_id=row["owner_id"],
                        username=row["owner_id"],
                        source_type="cluster-reservation",
                        source_id=source_id,
                        ttl_seconds=6 * 60 * 60,
                    )
                    if acquired:
                        stats["reservation_claims_reacquired"] += 1
                elif row["status"] != "active" and claims:
                    # Ghost claim fencing devices of a finished reservation.
                    self.claims.release(source_id, status="reconciled")
                    stats["reservation_claims_released"] += 1
            terminal_jobs = conn.execute(
                """SELECT id, owner_id FROM cluster_jobs
                   WHERE status IN ('completed','failed','cancelled')"""
            ).fetchall()
            for job in terminal_jobs:
                source_id = f"job:{job['id']}"
                if self.claims.list_by_source(source_id):
                    self.claims.release(source_id, status="reconciled")
                    stats["job_claims_released"] += 1
            # 非终态 job 丢 claim：崩溃发生在
            # "job 状态已提交、claim 尚未写"或 claim 被误释放时，运行中的
            # 任务失去物理 fencing。按 device_leases 的设备清单重取 claim，
            # 恢复 fencing；拿不到（他方占用）时把 job 标记为 failed，
            # 绝不让无 fencing 的任务继续跑。
            active_jobs = conn.execute(
                """SELECT id, owner_id FROM cluster_jobs
                   WHERE status IN ('assigned','dispatching','running','stopping')"""
            ).fetchall()
            for job in active_jobs:
                source_id = f"job:{job['id']}"
                if self.claims.list_by_source(source_id):
                    continue
                lease_rows = conn.execute(
                    """SELECT DISTINCT device_id FROM device_leases
                       WHERE job_id=? AND status='active'""",
                    (job["id"],),
                ).fetchall()
                if not lease_rows:
                    continue
                worker_id = str(
                    lease_rows[0]["device_id"] or ""
                ).split(":", 1)[0]
                acquired, _conflicts = self.claims.acquire(
                    self._claim_devices(
                        worker_id,
                        [row["device_id"] for row in lease_rows],
                    ),
                    owner_id=job["owner_id"],
                    username=job["owner_id"],
                    source_type="cluster-job",
                    source_id=source_id,
                    ttl_seconds=self.claim_lease_ttl_seconds,
                )
                if acquired:
                    stats["job_claims_reacquired"] += 1
                else:
                    conn.execute(
                        """UPDATE cluster_jobs
                           SET status='failed', error=?, finished_at=?, updated_at=?
                           WHERE id=? AND status IN ('assigned','dispatching','running','stopping')""",
                        (
                            "device fencing lost during a crash and could not be "
                            "reacquired (reconciliation)",
                            now,
                            now,
                            job["id"],
                        ),
                    )
                    stats["unfenceable_jobs_failed"] += 1
        return stats


__all__ = ["ClusterReconciliationRepositoryMixin"]
