"""Sweep glue tests (phase 2): snapshot -> incidents -> dedup -> notify."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from features.ops.store import OpsStore
from features.ops.sweep import run_ops_sweep


class SweepTests(unittest.TestCase):
    def test_failed_old_event_is_attempted_only_once_per_sweep(self):
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            event = store.record_incident(severity="critical", kind="test", message="test")["event"]
            store.mark_delivery_state(event["id"], state="failed", attempts=12)
            with store._connect() as conn:
                conn.execute(
                    "UPDATE ops_events SET created_at='2000-01-01T00:00:00Z', "
                    "notify_retry_at='2000-01-01T00:00:00Z'"
                )
            with mock.patch("features.ops.sweep.build_snapshot", return_value={}), \
                    mock.patch("features.ops.service.get_ops_store", return_value=store), \
                    mock.patch("features.ops.sweep.notify_incident", return_value={"ok": False}) as notify:
                run_ops_sweep({"enabled": True})
            notify.assert_called_once()
            failed = store.get_event(event["id"])
            self.assertEqual(failed["notify_attempts"], 13)
            self.assertFalse(store.notify_backoff_ready(failed))

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

    def test_failed_delivery_is_retried_with_backoff_and_closed_loop(self):
        # 发送失败的事件必须进 pending/failed 状态并在后续
        # sweep 重试；成功后写 notified_at 形成闭环。
        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            cfg = {
                "enabled": True, "webhook_url": "https://x", "email_to": "",
                "suppress_minutes": 60, "consecutive_failure_threshold": 3,
                "min_free_gb": 20,
            }
            with mock.patch(
                "features.ops.sweep.notify_incident",
                return_value={"ok": False},
            ), mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ):
                run_ops_sweep(cfg)
                pending = store.pending_events()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["notify_state"], "failed")
                self.assertEqual(pending[0]["notify_attempts"], 1)

            # 退避窗口内不重试（attempts=1 → 至少等 60s）。
            with mock.patch(
                "features.ops.sweep.notify_incident",
                return_value={"ok": True},
            ) as notify, mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ):
                result = run_ops_sweep(cfg)
            notify.assert_not_called()
            self.assertEqual(result["notified"], 0)

            # 把事件年龄和下次重试时间拨回，模拟退避到期 → 重试成功 →
            # sent 闭环。同时快照仍含同 dedup_key 的活跃 incident，且原事件
            # 已超出抑制窗口：会重新入库一条新事件并立即投递（重报语义），
            # 因此 notify 恰好调用两次：重试的旧行 + 新行。
            import sqlite3

            with sqlite3.connect(str(Path(tmp) / "ops.sqlite3")) as conn:
                conn.execute(
                    "UPDATE ops_events SET created_at='2020-01-01T00:00:00Z', "
                    "notify_retry_at='2020-01-01T00:00:00Z'"
                )
            with mock.patch(
                "features.ops.sweep.notify_incident",
                return_value={"ok": True},
            ) as notify, mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ):
                result = run_ops_sweep(cfg)
            self.assertEqual(notify.call_count, 2)
            self.assertEqual(result["notified"], 2)
            self.assertEqual(store.pending_events(), [])
            states = sorted(e["notify_state"] for e in store.list_events())
            self.assertEqual(states, ["sent", "sent"])
            self.assertTrue(all(e["notified_at"] for e in store.list_events()))

    def test_kill_switch_audit_row_never_enters_delivery(self):
        # kill-switch 审计行是台账，不是告警：不进投递闭环。
        from features.ops import service as ops_service

        with TemporaryDirectory() as tmp:
            store = OpsStore(Path(tmp) / "ops.sqlite3")
            with mock.patch(
                "features.ops.service.get_ops_store", return_value=store
            ):
                ops_service.set_advancement_paused(True, updated_by="tester")
            self.assertEqual(store.pending_events(), [])
            self.assertEqual(store.list_events()[0]["notify_state"], "skipped")

    def test_sweep_never_raises_on_snapshot_failure(self):
        with mock.patch(
            "features.ops.sweep.build_snapshot",
            side_effect=RuntimeError("boom"),
        ):
            result = run_ops_sweep({"enabled": False})
        self.assertEqual(result, {"evaluated": 0, "recorded": 0, "notified": 0})


if __name__ == "__main__":
    unittest.main()
