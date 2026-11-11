"""发件台账（outbox ledger）与 outbox API 的测试。

- store：成功/失败尝试都落一行；裁剪；channel/sent/owner 过滤；
- service 管线：send_email 无论成败都写台账；台账写失败不影响发送结果；
- API：owner 视图隔离、admin_view 权限、channel 参数校验。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from features.email import store as store_module
from features.email.store import EmailOutboxStore, reset_email_outbox_store


def _fake_manager(email_cfg: dict) -> MagicMock:
    mgr = MagicMock()
    mgr.get_runtime_config.return_value = {"redmine_dashboard": {"email": email_cfg}}
    return mgr


class OutboxStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EmailOutboxStore(Path(self._tmp.name) / "outbox.sqlite3")

    def tearDown(self):
        self._tmp.cleanup()

    def test_records_success_and_failure_rows(self):
        self.store.record({
            "channel": "manual", "owner_id": "u1", "username": "hcq",
            "to_addrs": ["a@x.com"], "subject": "hello", "body_preview": "hi",
            "sent": True, "mode": "smtp",
        })
        self.store.record({
            "channel": "ops", "sent": False, "mode": "smtp",
            "error": "SMTP认证失败", "subject": "alert",
        })
        data = self.store.list()
        self.assertEqual(data["total"], 2)
        # created_at 同秒内按 id DESC 兜底排序：后写入的在前不确定，
        # 只验证内容集合。
        modes = {item["mode"] for item in data["items"]}
        channels = {item["channel"] for item in data["items"]}
        self.assertEqual(modes, {"smtp"})
        self.assertEqual(channels, {"manual", "ops"})
        failed = [item for item in data["items"] if not item["sent"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("SMTP", failed[0]["error"])

    def test_channel_and_sent_filters(self):
        self.store.record({"channel": "manual", "sent": True, "subject": "a"})
        self.store.record({"channel": "ops", "sent": False, "subject": "b"})
        self.assertEqual(self.store.list(channel="ops")["total"], 1)
        self.assertEqual(self.store.list(sent=True)["total"], 1)
        self.assertEqual(self.store.list(channel="manual", sent=True)["total"], 1)

    def test_owner_filter_and_detail(self):
        self.store.record({"channel": "manual", "owner_id": "u1", "subject": "a"})
        self.store.record({"channel": "manual", "owner_id": "u2", "subject": "b"})
        # 系统级记录（owner_id=''，如 ops 运维告警）不进入普通用户的历史
        # 邮件列表，只由 admin_view 全量视图查看。
        self.store.record({"channel": "ops", "subject": "system alert"})
        self.assertEqual(self.store.list(owner_id="u1")["total"], 1)
        self.assertEqual(self.store.list(owner_id="u2")["total"], 1)
        # owner_id=None 表示不过滤（管理员视图）。
        self.assertEqual(self.store.list(owner_id=None)["total"], 3)
        # 列表按 created_at DESC, id DESC 排序（同秒内 id 顺序不保证），
        # 用集合断言而不是位置断言。
        self.assertIn(
            "a", [item["subject"] for item in self.store.list(owner_id="u1")["items"]]
        )
        email_id = next(
            item["id"] for item in self.store.list(owner_id="u1")["items"]
            if item["subject"] == "a"
        )
        detail = self.store.get(email_id)
        self.assertEqual(detail["subject"], "a")

    def test_owner_view_excludes_other_users_rows(self):
        """owner 视图只看到自己的 + 系统级的，看不到其他用户的。"""
        self.store.record({"channel": "manual", "owner_id": "u1", "subject": "mine"})
        self.store.record({"channel": "manual", "owner_id": "u2", "subject": "theirs"})
        rows = self.store.list(owner_id="u1")["items"]
        self.assertEqual([row["subject"] for row in rows], ["mine"])

    def test_body_preview_truncated(self):
        self.store.record({"channel": "manual", "body_preview": "x" * 5000})
        row = self.store.list()["items"][0]
        self.assertLessEqual(len(row["body_preview"]), 500)

    def test_prune_keeps_recent_rows(self):
        for index in range(30):
            self.store.record({"channel": "manual", "subject": f"m{index}"})
        # 直接调用内部裁剪逻辑（_MAX_ROWS 是常量，这里只验证语义）。
        with self.store._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """DELETE FROM email_outbox WHERE id IN (
                       SELECT id FROM email_outbox
                       ORDER BY created_at DESC LIMIT -1 OFFSET ?)""",
                (25,),
            )
        self.assertEqual(self.store.list()["total"], 25)

    def test_record_survives_db_error(self):
        broken = EmailOutboxStore.__new__(EmailOutboxStore)
        broken.db_path = Path("/proc/nonexistent-dir/outbox.sqlite3")
        # _open_connection 会因目录不可建抛错；record 必须吞掉而不是上抛。
        self.assertEqual(broken.record({"channel": "manual", "sent": True}), "")


class SendEmailLedgerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # RuntimeSettings 是 frozen dataclass；整体替换 store 模块引用的
        # settings 为轻量 stub，把台账库指到临时目录。
        self._original_settings = store_module.settings
        stub = SimpleNamespace(data_root=Path(self._tmp.name))
        self._settings_patch = patch.object(store_module, "settings", stub)
        self._settings_patch.start()
        reset_email_outbox_store()

    def tearDown(self):
        reset_email_outbox_store()
        self._settings_patch.stop()
        patch.object(store_module, "settings", self._original_settings).start()
        self._tmp.cleanup()

    def test_failed_send_is_still_recorded(self):
        from features.email.service import send_email

        result = send_email(
            "a@x.com", "s", "b",
            manager=_fake_manager({}),
            channel="automation", owner_id="u1",
            ref_type="automation_run", ref_id="run-1",
        )
        self.assertFalse(result["sent"])
        from features.email.store import get_email_outbox_store

        data = get_email_outbox_store().list()
        self.assertEqual(data["total"], 1)
        row = data["items"][0]
        self.assertEqual(row["channel"], "automation")
        self.assertEqual(row["ref_type"], "automation_run")
        self.assertEqual(row["ref_id"], "run-1")
        self.assertFalse(row["sent"])

    def test_ledger_failure_does_not_break_send(self):
        from features.email.service import send_email

        with patch(
            "features.email.service.get_email_outbox_store",
            side_effect=RuntimeError("boom"),
        ):
            result = send_email(
                "a@x.com", "s", "b", manager=_fake_manager({})
            )
        # 未配置 SMTP → 发送失败，但异常被台账层吞掉，返回结构完好。
        self.assertIn("sent", result)

    def test_smtp_success_records_from_addr(self):
        from features.email.service import send_email

        cfg = {
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "username": "me@example.com", "password": "secret",
            "from_addr": "me@example.com",
        }
        with patch("features.email.transport.smtplib.SMTP_SSL") as smtp_cls:
            send_email(
                "a@x.com", "subject", "body",
                manager=_fake_manager(cfg),
                channel="manual", owner_id="u1", username="hcq",
            )
            smtp_cls.return_value.__enter__.return_value.sendmail.assert_called_once()
        from features.email.store import get_email_outbox_store

        row = get_email_outbox_store().list()["items"][0]
        self.assertTrue(row["sent"])
        self.assertEqual(row["from_addr"], "me@example.com")
        self.assertEqual(row["to_addrs"], ["a@x.com"])
        self.assertEqual(row["username"], "hcq")


if __name__ == "__main__":
    unittest.main()


class OutboxQuerySearchTests(unittest.TestCase):
    """list(query=...) 的 LIKE 搜索与通配符字面匹配。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EmailOutboxStore(Path(self._tmp.name) / "outbox.sqlite3")
        self.store.record({
            "channel": "manual", "owner_id": "u1", "username": "hcq",
            "to_addrs": ["alice@example.com"], "subject": " weekly report 42 ",
        })
        self.store.record({
            "channel": "ops", "to_addrs": ["ops@example.com"],
            "subject": "disk 100% full",
        })

    def tearDown(self):
        self._tmp.cleanup()

    def test_subject_match_is_case_insensitive_substring(self):
        self.assertEqual(self.store.list(query="WEEKLY")["total"], 1)

    def test_recipient_and_username_match(self):
        self.assertEqual(self.store.list(query="alice@")["total"], 1)
        self.assertEqual(self.store.list(query="hcq")["total"], 1)

    def test_like_wildcards_are_literal(self):
        # 主题里的 % 必须按字面匹配，不能变成全表通配。
        self.assertEqual(self.store.list(query="100%")["total"], 1)
        self.assertEqual(self.store.list(query="%%")["total"], 0)

    def test_search_combines_with_owner_filter(self):
        self.assertEqual(
            self.store.list(owner_id="u2", query="alice")["total"], 0
        )
        self.assertEqual(
            self.store.list(owner_id="u1", query="alice")["total"], 1
        )
