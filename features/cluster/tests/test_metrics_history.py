from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from features.cluster.repository import ClusterRepository


def _repository(tmp: str) -> ClusterRepository:
    repository = ClusterRepository(Path(tmp) / "cluster.sqlite3")
    repository.register_worker({
        "worker_id": "worker-246",
        "name": "remote",
        "hostname": "ats-246",
        "address": "192.0.2.10",
        "agent_version": "1",
        "max_jobs": 1,
        "capabilities": {},
    })
    return repository


def test_metrics_history_is_aggregated_into_five_minute_buckets():
    with TemporaryDirectory() as tmp:
        repository = _repository(tmp)
        with repository.connect() as conn:
            conn.executemany(
                """INSERT INTO cluster_worker_metrics
                   (worker_id,recorded_at,cpu_percent,memory_percent,disk_free_gb,
                    running_jobs,external_jobs)
                   VALUES('worker-246',strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?,?,?,?)""",
                [
                    (10, 20, 100, 0, 0),
                    (30, 40, 90, 1, 1),
                ],
            )

        history = repository.get_worker_metrics_history("worker-246")

        assert len(history) == 1
        assert history[0]["cpu_percent"] == 20
        assert history[0]["memory_percent"] == 30
        assert history[0]["disk_free_gb"] == 90
        assert history[0]["running_jobs"] == 1


def test_deleting_worker_removes_metrics_history():
    with TemporaryDirectory() as tmp:
        repository = _repository(tmp)
        repository.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [],
            "suites": [],
        })

        assert repository.delete_worker("worker-246")


def test_job_events_query_uses_index():
    """日志轮询 list_events 必须命中 (job_id, sequence) 索引，避免事件表增长后全表扫描。"""
    with TemporaryDirectory() as tmp:
        repository = _repository(tmp)
        with repository.connect() as conn:
            indexes = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='cluster_job_events'"
                )
            }
            assert "idx_cluster_job_events_job" in indexes

            plan_rows = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM cluster_job_events "
                "WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                ("job-x", 0, 500),
            ).fetchall()
            detail = " ".join(str(row[3]) for row in plan_rows)
        assert "idx_cluster_job_events_job" in detail
        assert "SCAN cluster_job_events" not in detail
        assert repository.get_worker_metrics_history("worker-246") == []
