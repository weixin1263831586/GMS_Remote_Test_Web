"""Unit tests for the ops foundation store (phase 1)."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class OpsStoreTests(unittest.TestCase):
    def _store(self, tmp: str):
        from features.ops.store import OpsStore

        return OpsStore(Path(tmp) / "ops/ops.sqlite3")

    def test_schema_is_idempotent_across_instances(self):
        with TemporaryDirectory() as tmp:
            first = self._store(tmp)
            second = self._store(tmp)  # re-run migration on existing file
            first.set_flag("advancement_paused", True, updated_by="tester")
            self.assertIs(second.get_flag("advancement_paused"), True)

    def test_flag_roundtrip_and_meta(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self.assertIsNone(store.get_flag("missing"))
            self.assertFalse(store.advancement_paused())
            store.set_flag("advancement_paused", True, updated_by="admin")
            self.assertTrue(store.advancement_paused())
            meta = store.flag_meta("advancement_paused")
            self.assertEqual(meta["updated_by"], "admin")
            self.assertTrue(meta["updated_at"])

    def test_incident_dedup_suppresses_fresh_duplicate(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            first = store.record_incident(
                severity="warning",
                kind="worker_offline",
                subject="worker-1",
                dedup_key="worker_offline:worker-1",
                message="worker offline",
                suppress_minutes=60,
            )
            second = store.record_incident(
                severity="warning",
                kind="worker_offline",
                subject="worker-1",
                dedup_key="worker_offline:worker-1",
                message="worker offline",
                suppress_minutes=60,
            )
            self.assertFalse(first["suppressed"])
            self.assertTrue(second["suppressed"])
            self.assertEqual(store.open_event_count(), 1)

    def test_incident_after_ack_records_again(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            first = store.record_incident(
                severity="critical",
                kind="consecutive_failures",
                subject="profile-a",
                dedup_key="consecutive_failures:profile-a:flash_failed",
                message="3 consecutive flash failures",
            )["event"]
            store.ack_event(first["id"], "admin")
            again = store.record_incident(
                severity="critical",
                kind="consecutive_failures",
                subject="profile-a",
                dedup_key="consecutive_failures:profile-a:flash_failed",
                message="3 consecutive flash failures",
            )
            self.assertFalse(again["suppressed"])

    def test_kill_switch_incident_is_never_suppressed(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for _ in range(2):
                store.record_incident(
                    severity="info",
                    kind="kill_switch",
                    dedup_key="kill_switch:True",
                    message="paused",
                    suppress_minutes=0,
                )
            self.assertEqual(store.open_event_count(), 2)

    def test_list_events_status_filter_and_ack(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            event = store.record_incident(
                severity="warning", kind="disk_pressure", message="low disk"
            )["event"]
            self.assertEqual(len(store.list_events(status="open")), 1)
            acked = store.ack_event(event["id"], "admin")
            self.assertEqual(acked["status"], "ack")
            self.assertEqual(acked["acknowledged_by"], "admin")
            self.assertEqual(store.list_events(status="open"), [])
            self.assertEqual(len(store.list_events(status="ack")), 1)
            self.assertIsNone(store.ack_event("ops-missing", "admin"))


if __name__ == "__main__":
    unittest.main()
