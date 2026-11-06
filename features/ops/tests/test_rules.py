"""Alert rule engine tests (phase 2)."""

import unittest

from features.ops.rules import evaluate


class RuleEngineTests(unittest.TestCase):
    def test_offline_worker_produces_warning(self):
        incidents = evaluate({"workers": [
            {"id": "worker-a", "status": "offline"},
            {"id": "worker-b", "status": "online"},
        ]})
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "worker_offline")
        self.assertEqual(incidents[0]["severity"], "warning")
        self.assertEqual(incidents[0]["dedup_key"], "worker_offline:worker-a")

    def test_consecutive_failures_critical_after_threshold(self):
        runs = [{"profile_id": "p", "status": s} for s in (
            "flash_failed", "flash_failed", "flash_failed",
        )]
        incidents = evaluate({"recent_runs": runs})
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "consecutive_failures")
        self.assertEqual(incidents[0]["severity"], "critical")

    def test_success_run_breaks_streak(self):
        runs = [{"profile_id": "p", "status": s} for s in (
            "flash_failed", "flash_failed", "completed", "flash_failed",
        )]
        self.assertEqual(evaluate({"recent_runs": runs}), [])

    def test_disk_pressure_below_threshold(self):
        incidents = evaluate({"disk_free_gb": 5.0}, min_free_gb=20)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["kind"], "disk_pressure")
        self.assertEqual(evaluate({"disk_free_gb": 25.0}, min_free_gb=20), [])

    def test_worker_loop_not_running_is_critical(self):
        incidents = evaluate({"worker_status": {"running": False}})
        self.assertEqual(incidents[0]["kind"], "worker_loop_stalled")

    def test_worker_loop_stalled_tick(self):
        incidents = evaluate({
            "worker_status": {"running": True, "interval_seconds": 10,
                              "last_tick_seconds_ago": 120},
        })
        self.assertEqual(len(incidents), 1)
        self.assertEqual(evaluate({
            "worker_status": {"running": True, "interval_seconds": 10,
                              "last_tick_seconds_ago": 20},
        }), [])

    def test_empty_snapshot_is_quiet(self):
        self.assertEqual(evaluate({}), [])


if __name__ == "__main__":
    unittest.main()
