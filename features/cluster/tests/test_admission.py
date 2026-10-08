"""Admission single-source-of-truth tests (UI directory vs scheduler).

Pinned by the external review: ``list_workers`` used to block only on
offline/draining while ``select_worker`` silently rejected the same Worker on
the disk threshold, and the memory floor was a UI-only warning. Both (plus
the claim re-check and the automation manual path) must consume
``features/cluster/admission.py`` so they can never disagree.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from features.cluster.admission import worker_admission_state
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService

from .test_cluster import ClusterRepositoryTests


def _base_worker(**overrides) -> dict:
    worker = {
        "id": "worker-246",
        "status": "online",
        "disk_free_gb": 500,
        "memory_available_gb": 64,
        "max_jobs": 2,
        "running_jobs": 0,
        "unknown_external_jobs": 0,
    }
    worker.update(overrides)
    return worker


class WorkerAdmissionStateTests(unittest.TestCase):
    def test_online_healthy_worker_is_admissible(self):
        state = worker_admission_state(_base_worker())
        self.assertEqual(state, {"blocked": False, "reasons": []})

    def test_offline_and_draining_block_with_matching_reason(self):
        self.assertEqual(
            worker_admission_state(_base_worker(status="offline"))["reasons"],
            ["offline"],
        )
        self.assertEqual(
            worker_admission_state(_base_worker(status="draining"))["reasons"],
            ["draining"],
        )
        # Any other status (including missing) fails closed as offline.
        self.assertEqual(
            worker_admission_state(_base_worker(status=""))["reasons"],
            ["offline"],
        )

    def test_low_disk_blocks(self):
        with mock.patch.dict(os.environ, {"GMS_CLUSTER_MIN_DISK_FREE_GB": "50"}):
            state = worker_admission_state(_base_worker(disk_free_gb=12.5))
        self.assertEqual(state["reasons"], ["low_disk"])
        self.assertTrue(state["blocked"])

    def test_low_memory_blocks_on_cluster_floor_or_request_floor(self):
        with mock.patch.dict(os.environ, {"GMS_CLUSTER_MIN_MEMORY_AVAILABLE_GB": "8"}):
            floor = worker_admission_state(_base_worker(memory_available_gb=4))
            request = worker_admission_state(
                _base_worker(memory_available_gb=16), required_memory_gb=28
            )
        self.assertEqual(floor["reasons"], ["low_memory"])
        self.assertEqual(request["reasons"], ["low_memory"])

    def test_max_jobs_and_external_tradefed_block(self):
        self.assertEqual(
            worker_admission_state(_base_worker(running_jobs=2))["reasons"],
            ["max_jobs"],
        )
        self.assertEqual(
            worker_admission_state(_base_worker(unknown_external_jobs=1))["reasons"],
            ["external_tradefed"],
        )

    def test_unknown_metrics_never_block(self):
        # 0/absent metrics mean "unknown" (Worker did not report), never a
        # silent hard block.
        state = worker_admission_state(
            _base_worker(disk_free_gb=0, memory_available_gb=0, max_jobs=0)
        )
        self.assertEqual(state, {"blocked": False, "reasons": []})

    def test_reasons_accumulate(self):
        state = worker_admission_state(
            _base_worker(
                status="offline", disk_free_gb=1, memory_available_gb=1,
                running_jobs=5, unknown_external_jobs=2,
            )
        )
        self.assertEqual(
            state["reasons"],
            ["offline", "low_disk", "low_memory", "max_jobs", "external_tradefed"],
        )


class AdmissionConsistencyTests(ClusterRepositoryTests):
    """The UI directory and the scheduler must agree on admission."""

    def _heartbeat_worker(self, metrics: dict):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{"suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": "/suite/tools",
                        "available": True}],
            **metrics,
        })

    def test_low_disk_worker_is_blocked_in_directory_and_scheduler(self):
        with mock.patch.dict(os.environ, {"GMS_CLUSTER_MIN_DISK_FREE_GB": "50"}):
            self._heartbeat_worker({"disk_free_gb": 10, "disk_total_gb": 100})
            service = ClusterService(self.repo)
            worker = next(
                item for item in service.list_workers()
                if item["id"] == "worker-246"
            )
            self.assertTrue(worker["admission_blocked"])
            self.assertIn("low_disk", worker["admission_reasons"])
            with self.assertRaises(ValueError):
                service.select_worker("CTS:17_r1", 1)

    def test_healthy_worker_is_admitted_in_directory_and_scheduler(self):
        self._heartbeat_worker({"disk_free_gb": 500, "memory_available_gb": 64})
        service = ClusterService(self.repo)
        worker = next(
            item for item in service.list_workers() if item["id"] == "worker-246"
        )
        self.assertFalse(worker["admission_blocked"])
        self.assertEqual(worker["admission_reasons"], [])
        worker_id, _devices = service.select_worker("CTS:17_r1", 1)
        self.assertEqual(worker_id, "worker-246")

    def test_claim_rejects_low_disk_worker_with_admission_reasons(self):
        with mock.patch.dict(os.environ, {"GMS_CLUSTER_MIN_DISK_FREE_GB": "50"}):
            self._heartbeat_worker({"disk_free_gb": 10, "disk_total_gb": 100})
            with self.assertRaises(ValueError) as ctx:
                self.repo.create_job_with_leases({
                    "worker_id": "worker-246", "owner_id": "tester",
                    "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
                })
            self.assertIn("low_disk", str(ctx.exception))

    def test_legacy_zero_capacity_agrees_in_directory_and_job_creation(self):
        self._heartbeat_worker({"disk_free_gb": 500, "memory_available_gb": 64})
        with self.repo.connect() as conn:
            conn.execute("UPDATE cluster_workers SET max_jobs=0 WHERE id='worker-246'")
        service = ClusterService(self.repo)
        self.assertFalse(service.list_workers()[0]["admission_blocked"])
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
        })
        self.assertEqual(job["assigned_worker_id"], "worker-246")


class WatchdogObservabilityTests(unittest.TestCase):
    def test_watchdog_failures_are_logged_rate_limited(self):
        records = self._watchdog_failures([1.0, 2.0])
        self.assertEqual(len(records), 1)
        self.assertIn("watchdog pass failed", records[0].getMessage())

    def test_watchdog_logs_again_at_the_rate_limit_boundary(self):
        records = self._watchdog_failures([0.0, 3599.0, 3600.0])
        self.assertEqual(len(records), 2)

    def _watchdog_failures(self, clock_ticks):
        repo = mock.Mock(spec=ClusterRepository)
        repo.list_workers.side_effect = RuntimeError("sqlite is locked")
        service = ClusterService(repo, offline_seconds=45)

        class InlineThread:
            def __init__(self, target, **kwargs):
                self._target = target

            def start(self):
                self._target()

            def is_alive(self):
                return False

            def join(self, timeout=None):
                return None

        # A fresh watchdog must log immediately, regardless of clock uptime.
        with mock.patch("features.cluster.service.threading.Thread", InlineThread), \
                mock.patch(
                    "features.cluster.service.WATCHDOG_ERROR_LOG_INTERVAL_SECONDS",
                    3600.0,
                ), \
                mock.patch("features.cluster.service.time.monotonic", side_effect=clock_ticks), \
                self.assertLogs("features.cluster.service", level="ERROR") as captured:
            service._watchdog_stop.wait = mock.Mock(side_effect=[False] * len(clock_ticks) + [True])
            service.start_watchdog()

        return captured.records


if __name__ == "__main__":
    unittest.main()
