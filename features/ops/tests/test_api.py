"""Ops API route tests (phase 1), direct-call style per repo convention."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.auth import CurrentUser
from features.ops.api import KillSwitchRequest, ack_ops_event, ops_status, set_kill_switch
from features.ops.store import OpsStore


ADMIN = CurrentUser(id="u-admin", username="admin", role="admin")


class OpsApiTests(unittest.TestCase):
    def test_status_envelope_shape(self):
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            store.record_incident(severity="warning", kind="x", message="m")
            with mock.patch(
                "features.automation.get_worker_status",
                return_value={"running": True, "interval_seconds": 15,
                              "last_tick_seconds_ago": 1, "executor": "http"},
            ), mock.patch("features.ops.service.get_ops_store", return_value=store):
                data = ops_status(_user=None)["data"]
            self.assertFalse(data["advancement_paused"])
            self.assertEqual(data["open_alerts"], 1)
            self.assertTrue(data["worker"]["running"])
            self.assertIn("available_gb", data["disk"])

    def test_kill_switch_route_updates_store(self):
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            with mock.patch("features.ops.service.get_ops_store", return_value=store):
                data = set_kill_switch(
                    KillSwitchRequest(paused=True, reason="hold"),
                    admin=ADMIN,
                )["data"]
            self.assertTrue(data["advancement_paused"])
            self.assertEqual(data["reason"], "hold")
            self.assertTrue(store.advancement_paused())
            events = store.list_events(status="open")
            self.assertEqual(events[0]["kind"], "kill_switch")
            self.assertEqual(events[0]["subject"], "admin")

    def test_ack_unknown_event_raises_not_found(self):
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            with mock.patch("features.ops.service.get_ops_store", return_value=store):
                with self.assertRaises(Exception) as caught:
                    ack_ops_event("ops-missing", mock.MagicMock(), admin=ADMIN)
                self.assertEqual(getattr(caught.exception, "code", ""), "NOT_FOUND")

    def test_automation_page_registers_ops_assets(self):
        ui_dir = Path(__file__).resolve().parents[1].parent / "automation" / "ui"
        html = (ui_dir / "page.html").read_text(encoding="utf-8")
        self.assertIn('data-workflow="ops"', html)
        self.assertIn('/automation/ops-panel.js', html)
        self.assertIn('id="ops-kill-switch"', html)
        self.assertIn('id="ops-events"', html)
        state = (ui_dir / "state.js").read_text(encoding="utf-8")
        self.assertIn("'ops'", state)
        from features.automation import automation_ui_asset
        from foundation.error_model import ApiError

        js = automation_ui_asset("ops-panel.js")
        self.assertEqual(js.media_type, "application/javascript")
        with self.assertRaises(ApiError):
            automation_ui_asset("not-a-chunk.js")


if __name__ == "__main__":
    unittest.main()
