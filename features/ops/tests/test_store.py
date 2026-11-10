"""Unit tests for the ops foundation store (phase 1)."""

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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

    def test_failed_retry_waits_from_latest_attempt_even_for_old_events(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            event = store.record_incident(severity="critical", kind="test", message="test")["event"]
            with store._connect() as conn:
                conn.execute("UPDATE ops_events SET created_at='2000-01-01T00:00:00Z'")
            store.mark_delivery_state(event["id"], state="failed", attempts=10000)
            failed = store.get_event(event["id"])
            self.assertFalse(store.notify_backoff_ready(failed))
            retry = datetime.fromisoformat(failed["notify_retry_at"].replace("Z", "+00:00"))
            self.assertGreater(retry, datetime.now(timezone.utc) + timedelta(minutes=29))
            self.assertEqual(store.pending_events(ready_only=True), [])
            self.assertEqual(len(store.pending_events()), 1)

    def test_events_in_backoff_do_not_hide_fresh_alerts(self):
        with TemporaryDirectory() as tmp:
            store = self._store(tmp)
            for _ in range(21):
                event = store.record_incident(severity="critical", kind="old", message="test")["event"]
                store.mark_delivery_state(event["id"], state="failed", attempts=8)
            fresh = store.record_incident(severity="critical", kind="fresh", message="test")["event"]
            self.assertEqual(
                [event["id"] for event in store.pending_events(limit=1, ready_only=True)],
                [fresh["id"]],
            )

    def test_legacy_delivery_migration_is_process_safe_and_idempotent(self):
        from features.ops.store import OpsStore

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.execute("""CREATE TABLE ops_events (
                    id TEXT PRIMARY KEY, created_at TEXT, status TEXT, dedup_key TEXT,
                    notify_state TEXT, notify_attempts INTEGER
                )""")
                conn.execute("INSERT INTO ops_events VALUES ('old', '2000-01-01T00:00:00Z', 'open', '', 'failed', 3)")
            with ThreadPoolExecutor(max_workers=4) as executor:
                stores = list(executor.map(OpsStore, [path] * 4))
            event = stores[0].get_event("old")
            self.assertEqual(event["notify_attempted_at"], "2000-01-01T00:00:00Z")
            self.assertEqual(event["notify_retry_at"], "2000-01-01T00:04:00Z")
            self.assertEqual(stores[-1].get_event("old"), event)

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
