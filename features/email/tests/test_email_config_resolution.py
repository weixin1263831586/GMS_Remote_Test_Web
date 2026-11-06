"""resolve_email_config 合并规则测试：全局默认 + 兼容兜底 + owner 覆盖。"""

from __future__ import annotations

import unittest
from unittest import mock


def _manager(load_return: dict):
    mgr = mock.MagicMock()
    mgr.load_config.return_value = load_return
    return mgr


class ResolveEmailConfigTests(unittest.TestCase):
    def test_owner_only_config_still_works(self):
        from features.email.service import resolve_email_config

        manager = _manager({"redmine_dashboard": {"email": {"smtp_host": "owner.host"}}})
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value={}
        ):
            cfg = resolve_email_config(manager)
        self.assertEqual(cfg["smtp_host"], "owner.host")

    def test_global_default_used_without_manager(self):
        from features.email.service import resolve_email_config

        global_config = {
            "redmine_dashboard": {"email": {"smtp_host": "global.host", "smtp_port": 465}}
        }
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value=global_config
        ):
            cfg = resolve_email_config(None)
        self.assertEqual(cfg["smtp_host"], "global.host")

    def test_owner_overrides_global_only_where_non_empty(self):
        from features.email.service import resolve_email_config

        global_config = {
            "redmine_dashboard": {"email": {
                "smtp_host": "global.host", "smtp_port": 465, "username": "ops@x.com",
            }}
        }
        manager = _manager({"redmine_dashboard": {"email": {"smtp_host": "owner.host"}}})
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value=global_config
        ):
            cfg = resolve_email_config(manager)
        self.assertEqual(cfg["smtp_host"], "owner.host")       # owner 覆盖
        self.assertEqual(cfg["smtp_port"], 465)                 # 全局补齐
        self.assertEqual(cfg["username"], "ops@x.com")          # 全局补齐

    def test_empty_owner_fields_do_not_shadow_global(self):
        from features.email.service import resolve_email_config

        global_config = {
            "redmine_dashboard": {"email": {"smtp_host": "global.host", "username": "ops@x.com"}}
        }
        manager = _manager({"redmine_dashboard": {"email": {"smtp_host": "", "username": ""}}})
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value=global_config
        ):
            cfg = resolve_email_config(manager)
        self.assertEqual(cfg["smtp_host"], "global.host")
        self.assertEqual(cfg["username"], "ops@x.com")

    def test_legacy_static_dashboard_email_used_as_fallback(self):
        """静态遗留 redmine_dashboard.email（无 smtp 全局块时）仍然生效。"""
        from features.email.service import resolve_email_config

        legacy = {
            "redmine_dashboard": {"email": {"smtp_host": "legacy.host", "smtp_port": 993}}
        }
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value=legacy
        ):
            cfg = resolve_email_config(None)
        self.assertEqual(cfg["smtp_host"], "legacy.host")

    def test_broken_manager_fail_safe_to_global(self):
        from features.email.service import resolve_email_config

        manager = mock.MagicMock()
        manager.load_config.side_effect = RuntimeError("boom")
        global_config = {"smtp": {"smtp_host": "global.host"}}
        with mock.patch(
            "foundation.config.config_manager.load_config", return_value=global_config
        ):
            cfg = resolve_email_config(manager)
        self.assertEqual(cfg["smtp_host"], "global.host")


if __name__ == "__main__":
    unittest.main()
