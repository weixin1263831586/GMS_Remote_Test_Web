"""Kill-switch gating and service-helper tests (phase 1)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


class AdvancementPausedTests(unittest.TestCase):
    def test_paused_flag_short_circuits_advancement(self):
        from features.automation import worker
        from features.ops.store import OpsStore

        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            store.set_flag("advancement_paused", True, updated_by="admin")
            with mock.patch(
                "foundation.automation_port.advancement_paused", return_value=True
            ), mock.patch.object(
                worker, "_resolve_services", return_value=(None, None)
            ) as resolver:
                result = worker.advance_runs_sync(
                    {"executor": "http"},
                    automation_service=mock.MagicMock(),
                )
            self.assertEqual(result, {"advanced_runs": 0, "paused_by_kill_switch": 1})
            resolver.assert_not_called()

    def test_not_paused_advances_normally(self):
        from features.automation import worker

        auto = mock.MagicMock()
        auto.worker_tick.return_value = None
        with mock.patch(
            "foundation.automation_port.advancement_paused",
            return_value=False,
        ):
            result = worker.advance_runs_sync(
                {"executor": "http"}, automation_service=auto
            )
        self.assertEqual(result, {"advanced_runs": 0})
        auto.worker_tick.assert_called_once_with("http")

    def test_ops_failure_fails_open(self):
        from features.automation import worker

        with mock.patch(
            "foundation.automation_port.advancement_paused",
            side_effect=RuntimeError("boom"),
        ):
            auto = mock.MagicMock()
            auto.worker_tick.return_value = None
            result = worker.advance_runs_sync(
                {"executor": "http"}, automation_service=auto
            )
        self.assertIn("advanced_runs", result)
        auto.worker_tick.assert_called_once()

    def test_set_advancement_paused_writes_flag_and_event(self):
        from features.ops import service
        from features.ops.store import OpsStore

        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            with mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ):
                result = service.set_advancement_paused(True, updated_by="admin")
                self.assertTrue(result["advancement_paused"])
                self.assertTrue(store.advancement_paused())
                events = store.list_events(status="open")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], "kill_switch")
                result = service.set_advancement_paused(False, updated_by="admin")
                self.assertFalse(result["advancement_paused"])
                self.assertFalse(store.advancement_paused())


if __name__ == "__main__":
    unittest.main()
