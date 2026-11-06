"""Sweep glue tests (phase 2): snapshot -> incidents -> dedup -> notify."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.ops.store import OpsStore
from features.ops.sweep import run_ops_sweep


class SweepTests(unittest.TestCase):
    def test_sweep_records_and_notifies_once(self):
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            cfg = {
                "enabled": True, "webhook_url": "https://x", "email_to": "",
                "suppress_minutes": 60, "consecutive_failure_threshold": 3,
                "min_free_gb": 20,
            }
            runs = [{"profile_id": "p", "status": s} for s in (
                "flash_failed", "flash_failed", "flash_failed",
            )]
            snapshot = {"recent_runs": runs, "workers": [], "disk_free_gb": 100.0}
            with mock.patch(
                "features.ops.sweep.build_snapshot", return_value=snapshot
            ), mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ), mock.patch(
                "features.ops.sweep.notify_incident",
                return_value={"sent": [{"transport": "webhook", "sent": True}]},
            ) as notify:
                first = run_ops_sweep(cfg)
                second = run_ops_sweep(cfg)
            self.assertEqual(first, {"evaluated": 1, "recorded": 1, "notified": 1})
            self.assertEqual(second["recorded"], 1)
            self.assertEqual(second["notified"], 0)  # dedup suppresses notify
            notify.assert_called_once()

    def test_sweep_never_raises_on_snapshot_failure(self):
        with mock.patch(
            "features.ops.sweep.build_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            result = run_ops_sweep({"enabled": False})
        self.assertEqual(result, {"evaluated": 0, "recorded": 0, "notified": 0})


if __name__ == "__main__":
    unittest.main()
