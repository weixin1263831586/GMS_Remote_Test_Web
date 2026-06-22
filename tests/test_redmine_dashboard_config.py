import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.config import ConfigManager


class SidebarNavigationConfigTests(unittest.TestCase):
    def test_sidebar_visible_pages_are_normalized(self):
        from routers.config import normalize_sidebar_visible_pages

        visible_pages = normalize_sidebar_visible_pages([
            "test",
            "redmine-agent",
            "test",
            "",
            123,
            "unknown-page",
        ])

        self.assertEqual(visible_pages, ["test", "redmine-agent"])

    def test_sidebar_order_endpoint_persists_visible_pages(self):
        import asyncio
        import routers.config as config_router

        runtime = {"sidebar_order": ["test", "redmine-agent"]}

        class FakeConfigManager:
            def get_runtime_config(self):
                return runtime.copy()

            def save_runtime_config(self, data):
                runtime.clear()
                runtime.update(data)
                return True

        with patch.object(config_router, "config_manager", FakeConfigManager()):
            save_result = asyncio.run(config_router.save_sidebar_order({
                "order": ["redmine-agent", "test"],
                "visible_pages": ["redmine-agent", "bad-page", "redmine-agent"],
            }))
            get_result = asyncio.run(config_router.get_sidebar_order())

        self.assertEqual(save_result.body.decode("utf-8").count("redmine-agent"), 2)
        self.assertEqual(runtime["sidebar_order"], ["redmine-agent", "test"])
        self.assertEqual(runtime["sidebar_visible_pages"], ["redmine-agent"])
        self.assertIn('"visible_pages":["redmine-agent"]', get_result.body.decode("utf-8"))

    def test_sidebar_visibility_modal_is_wired_in_template(self):
        template = Path("templates/index_fastapi.html").read_text(encoding="utf-8")

        self.assertIn('onclick="openSidebarVisibilityModal()"', template)
        self.assertIn('id="sidebar-visibility-modal"', template)
        self.assertIn("function applySidebarVisibility", template)
        self.assertIn("function saveSidebarVisibilityFromModal", template)
        self.assertIn("visible_pages", template)


class RedmineDashboardConfigTests(unittest.TestCase):
    def test_save_redmine_stats_config_writes_runtime_override(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({"redmine_stats": {"stale_days": 3, "window_days": 0, "cache_ttl": 600}}),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text(
                json.dumps({"sidebar_order": ["test"], "redmine_stats": {"stale_days": 3}}),
                encoding="utf-8",
            )

            manager = ConfigManager(base_dir=str(root / "core"))

            self.assertTrue(manager.save_redmine_stats_config({"stale_days": 20, "window_days": 60, "cache_ttl": 0}))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["sidebar_order"], ["test"])
            expected = {"stale_days": 20, "window_days": 60, "cache_ttl": 0, "chart_start_dates": {}, "chart_date_ranges": {}}
            self.assertEqual(runtime["redmine_stats"], expected)
            self.assertEqual(manager.get_redmine_stats_config(), expected)

    def test_redmine_dashboard_profiles_are_configurable_with_defaults(self):
        from core.redmine_dashboard_config import normalize_redmine_dashboard_profiles

        profiles = normalize_redmine_dashboard_profiles({
            "dashboard_profiles": [
                {"id": "sys-1", "name": "系统一部", "user_ids": [8744, "3317"], "stale_days": 20}
            ],
            "department_defaults": {"list_limit": 80, "issue_limit": 900},
        })

        self.assertEqual(profiles["defaults"]["list_limit"], 80)
        self.assertEqual(profiles["defaults"]["issue_limit"], 900)
        self.assertEqual(profiles["profiles"][0]["id"], "sys-1")
        self.assertEqual(profiles["profiles"][0]["name"], "系统一部")
        self.assertEqual(profiles["profiles"][0]["user_ids"], ["8744", "3317"])
        self.assertEqual(profiles["profiles"][0]["stale_days"], 20)

    def test_redmine_dashboard_supports_project_profiles_and_chart_date_ranges(self):
        from core.redmine_dashboard_config import (
            add_project_profile,
            normalize_redmine_dashboard_profiles,
            normalize_redmine_stats_config,
        )

        cfg = normalize_redmine_dashboard_profiles({
            "project_profiles": [
                {"id": "rk3572-android-16-sdk", "name": "RK3572 Android 16 SDK", "project_id": "rk3572-android-16-sdk"}
            ],
            "email": {"smtp_host": "smtphz.qiye.163.com"},
        })
        stats = normalize_redmine_stats_config({
            "chart_date_ranges": {
                "personal_daily": {"start": "2026-06-01", "end": "2026-06-13"},
                "department_daily": {"start": "bad", "end": "2026-06-13"},
            }
        })
        cfg = add_project_profile(cfg, "RK3588 Android 16 SDK", "https://redmine.rock-chips.com/projects/rk3588-android-16-sdk")

        self.assertEqual(cfg["project_profiles"][0]["project_id"], "rk3572-android-16-sdk")
        self.assertEqual(cfg["project_profiles"][1]["project_id"], "rk3588-android-16-sdk")
        self.assertEqual(cfg["email"]["smtp_host"], "smtphz.qiye.163.com")
        self.assertEqual(stats["chart_date_ranges"], {
            "personal_daily": {"start": "2026-06-01", "end": "2026-06-13"},
            "department_daily": {"end": "2026-06-13"},
        })

    def test_redmine_issue_copy_text_uses_configured_base_url(self):
        from core.redmine_dashboard_config import issue_url_list, issue_url_text

        issues = [{"issue_id": 621586}, {"issue_id": "617378"}, {"issue_id": ""}]

        urls = issue_url_list(issues, "https://redmine.rock-chips.com/")

        self.assertEqual(urls, [
            "https://redmine.rock-chips.com/issues/621586",
            "https://redmine.rock-chips.com/issues/617378",
        ])
        self.assertEqual(issue_url_text(issues, "https://redmine.rock-chips.com/"), "\n".join(urls))

    def test_save_redmine_dashboard_config_writes_runtime_profiles(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine_dashboard": {
                        "department_defaults": {"list_limit": 50, "issue_limit": 500},
                        "dashboard_profiles": [{"id": "system-1", "name": "系统一部", "user_ids": []}],
                    }
                }),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text(json.dumps({"sidebar_order": ["test"]}), encoding="utf-8")

            manager = ConfigManager(base_dir=str(root / "core"))

            self.assertTrue(manager.save_redmine_dashboard_config({
                "department_defaults": {"list_limit": 80, "issue_limit": 900},
                "dashboard_profiles": [
                    {"id": "system-1", "name": "系统一部", "user_ids": ["8744"]},
                    {"id": "new-dept", "name": "新部门", "user_ids": []},
                ],
            }))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["sidebar_order"], ["test"])
            self.assertEqual(runtime["redmine_dashboard"]["dashboard_profiles"][0]["user_ids"], ["8744"])
            self.assertEqual(manager.get_redmine_dashboard_config()["profiles"][1]["id"], "new-dept")

    def test_denormalize_redmine_dashboard_config_preserves_email_config(self):
        from core.redmine_dashboard_config import denormalize_redmine_dashboard_config

        cfg = denormalize_redmine_dashboard_config({
            "email": {"smtp_host": "smtp.example.com", "from_addr": "trac@rock-chips.com"},
            "dashboard_profiles": [{"id": "all", "name": "全部部门", "user_ids": []}],
        })

        self.assertEqual(cfg["email"]["from_addr"], "trac@rock-chips.com")
        self.assertEqual(cfg["email"]["smtp_host"], "smtp.example.com")

    def test_department_profile_helpers_add_distinct_departments_and_members(self):
        from core.redmine_dashboard_config import (
            add_department_profile,
            assign_user_to_profiles,
            filter_users_for_profile,
        )

        cfg = {
            "defaults": {"list_limit": 50, "issue_limit": 500},
            "profiles": [
                {"id": "system-1", "name": "系统一部", "user_ids": [], "stale_days": 20, "window_days": 60, "list_limit": 50, "issue_limit": 500},
                {"id": "system-2", "name": "系统二部", "user_ids": [], "stale_days": 20, "window_days": 60, "list_limit": 50, "issue_limit": 500},
            ],
        }

        cfg = add_department_profile(cfg, "系统三部", "system-3")
        cfg = assign_user_to_profiles(cfg, "8744", ["system-1"])

        self.assertEqual([p["id"] for p in cfg["profiles"]], ["system-1", "system-2", "system-3"])
        self.assertEqual(cfg["profiles"][0]["user_ids"], ["8744"])
        self.assertEqual(cfg["profiles"][1]["user_ids"], [])
        self.assertEqual(filter_users_for_profile([{"id": "8744", "name": "张三"}], cfg["profiles"][1]), [])

    def test_filter_users_for_profile_can_use_user_department_fields(self):
        from core.redmine_dashboard_config import filter_users_for_profile

        users = [
            {"id": "8744", "name": "张三", "department_id": "system-2", "department": "系统二部"},
            {"id": "3317", "name": "李四", "department_id": "system-1", "department": "系统一部"},
        ]
        profile = {
            "id": "system-2",
            "name": "系统二部",
            "user_ids": [],
            "aliases": [],
        }

        self.assertEqual([user["id"] for user in filter_users_for_profile(users, profile)], ["8744"])

    def test_reminder_email_uses_smtp_from_trac_by_default(self):
        import routers.redmine_agent as redmine_router

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine_dashboard": {
                        "email": {
                            "smtp_host": "smtp.example.com",
                            "smtp_port": 25,
                            "use_tls": False,
                            "default_from_addr": "trac@rock-chips.com",
                        }
                    }
                }),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text("{}", encoding="utf-8")

            manager = redmine_router.config_manager
            old_config_path = manager.config_path
            old_runtime_path = manager.runtime_config_path
            try:
                manager.config_path = str(configs / "config.json")
                manager.runtime_config_path = str(configs / "config_runtime.json")
                manager.invalidate_cache()
                with patch("routers.redmine_agent.smtplib.SMTP") as smtp_cls:
                    smtp = smtp_cls.return_value.__enter__.return_value

                    result = redmine_router._send_reminder_email("dev@example.com", "提醒", "body")

                    self.assertEqual(result, {"sent": True, "mode": "smtp"})
                    message = smtp.send_message.call_args.args[0]
                    self.assertEqual(message["From"], "trac@rock-chips.com")
                    self.assertEqual(message["To"], "dev@example.com")
                    self.assertNotIn("mailto", result)
            finally:
                manager.config_path = old_config_path
                manager.runtime_config_path = old_runtime_path
                manager.invalidate_cache()

    def test_reminder_email_requires_dedicated_smtp_password_not_redmine_credentials(self):
        """163 企业邮的 SMTP 授权码 与 Redmine 登录密码是两回事，不能互相兜底。

        未配置 SMTP 授权码时应返回明确的 unconfigured 错误，而不是拿 Redmine 登录
        密码去连 SMTP（错误凭据会被服务器直接断开连接）。
        """
        import routers.redmine_agent as redmine_router

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine_dashboard": {
                        "email": {
                            "smtp_host": "smtphz.qiye.163.com",
                            "smtp_port": 465,
                            "from_addr": "chaoqun.huang@rock-chips.com",
                            "username": "chaoqun.huang@rock-chips.com",
                        }
                    }
                }),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text("{}", encoding="utf-8")

            manager = redmine_router.config_manager
            old_config_path = manager.config_path
            old_runtime_path = manager.runtime_config_path
            try:
                manager.config_path = str(configs / "config.json")
                manager.runtime_config_path = str(configs / "config_runtime.json")
                manager.invalidate_cache()
                # 即便 Redmine 凭证里有登录密码，也不应被用作 SMTP 授权码
                with patch.object(manager, "load_redmine_credentials", return_value={"username": "chaoqun.huang@rock-chips.com", "password": "redmine-login-secret"}):
                    with patch("routers.redmine_agent.smtplib.SMTP_SSL") as smtp_cls:
                        result = redmine_router._send_reminder_email("dev@example.com", "提醒", "body")

                        self.assertFalse(result["sent"])
                        self.assertEqual(result["mode"], "unconfigured")
                        # 没有发起任何 SMTP 登录
                        smtp_cls.return_value.__enter__.return_value.login.assert_not_called()

                # 配置了专用 SMTP 授权码后应正常发送
                (configs / "config.json").write_text(
                    json.dumps({
                        "redmine_dashboard": {
                            "email": {
                                "smtp_host": "smtphz.qiye.163.com",
                                "smtp_port": 465,
                                "from_addr": "chaoqun.huang@rock-chips.com",
                                "username": "chaoqun.huang@rock-chips.com",
                                "password": "smtp-auth-code",
                            }
                        }
                    }),
                    encoding="utf-8",
                )
                manager.invalidate_cache()
                with patch("routers.redmine_agent.smtplib.SMTP_SSL") as smtp_cls:
                    smtp = smtp_cls.return_value.__enter__.return_value

                    result = redmine_router._send_reminder_email("dev@example.com", "提醒", "body")

                    self.assertEqual(result, {"sent": True, "mode": "smtp"})
                    smtp.login.assert_called_once_with("chaoqun.huang@rock-chips.com", "smtp-auth-code")
                    message = smtp.send_message.call_args.args[0]
                    self.assertEqual(message["From"], "chaoqun.huang@rock-chips.com")
            finally:
                manager.config_path = old_config_path
                manager.runtime_config_path = old_runtime_path
                manager.invalidate_cache()

    def test_qiye_163_smtp_from_address_matches_login_user(self):
        import routers.redmine_agent as redmine_router

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine_dashboard": {
                        "email": {
                            "smtp_host": "smtphz.qiye.163.com",
                            "smtp_port": 465,
                            "from_addr": "trac@rock-chips.com",
                            "username": "chaoqun.huang@rock-chips.com",
                            "password": "secret",
                        }
                    }
                }),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text("{}", encoding="utf-8")

            manager = redmine_router.config_manager
            old_config_path = manager.config_path
            old_runtime_path = manager.runtime_config_path
            try:
                manager.config_path = str(configs / "config.json")
                manager.runtime_config_path = str(configs / "config_runtime.json")
                manager.invalidate_cache()
                with patch("routers.redmine_agent.smtplib.SMTP_SSL") as smtp_cls:
                    smtp = smtp_cls.return_value.__enter__.return_value

                    result = redmine_router._send_reminder_email("dev@example.com", "提醒", "body")

                    self.assertEqual(result, {"sent": True, "mode": "smtp"})
                    message = smtp.send_message.call_args.args[0]
                    self.assertEqual(message["From"], "chaoqun.huang@rock-chips.com")
            finally:
                manager.config_path = old_config_path
                manager.runtime_config_path = old_runtime_path
                manager.invalidate_cache()

    def test_smtp_disconnect_is_reported_as_configuration_error(self):
        import smtplib
        import routers.redmine_agent as redmine_router

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(
                json.dumps({
                    "redmine_dashboard": {
                        "email": {
                            "smtp_host": "smtphz.qiye.163.com",
                            "smtp_port": 465,
                            "from_addr": "chaoqun.huang@rock-chips.com",
                            "username": "chaoqun.huang@rock-chips.com",
                            "password": "bad-secret",
                        }
                    }
                }),
                encoding="utf-8",
            )
            (configs / "config_runtime.json").write_text("{}", encoding="utf-8")

            manager = redmine_router.config_manager
            old_config_path = manager.config_path
            old_runtime_path = manager.runtime_config_path
            try:
                manager.config_path = str(configs / "config.json")
                manager.runtime_config_path = str(configs / "config_runtime.json")
                manager.invalidate_cache()
                with patch("routers.redmine_agent.smtplib.SMTP_SSL", side_effect=smtplib.SMTPServerDisconnected("Connection unexpectedly closed")):
                    result = redmine_router._send_reminder_email("dev@example.com", "提醒", "body")

                    self.assertFalse(result["sent"])
                    self.assertIn("连接被服务器关闭", result["error"])
                    self.assertIn("SMTP授权码", result["error"])
            finally:
                manager.config_path = old_config_path
                manager.runtime_config_path = old_runtime_path
                manager.invalidate_cache()

    def test_gerrit_dashboard_config_has_runtime_safe_defaults(self):
        from core.gerrit_dashboard_config import normalize_gerrit_dashboard_config

        cfg = normalize_gerrit_dashboard_config({
            "base_url": "https://gerrit.example.com/r/",
            "rest_username": "dev",
            "rest_password": "secret",
            "ssh_identity_file": "/tmp/gerrit_key",
            "department_defaults": {"list_limit": 80, "query_limit": 900, "query_page_size": 250, "max_history_changes": 0},
            "department_profiles": [
                {"id": "sys-1", "name": "系统一部", "owners": ["a@example.com", "b@example.com"]}
            ],
            "ssh_host": "gerrit.example.com",
            "ssh_port": "29418",
            "query_limit": 250,
            "dashboard_profiles": [{"id": "mine", "name": "我的变更", "query": "owner:self status:open"}],
        })

        self.assertEqual(cfg["base_url"], "https://gerrit.example.com/r")
        self.assertEqual(cfg["rest_username"], "dev")
        self.assertEqual(cfg["rest_password"], "secret")
        self.assertEqual(cfg["ssh_port"], 29418)
        self.assertEqual(cfg["ssh_identity_file"], "/tmp/gerrit_key")
        self.assertEqual(cfg["query_limit"], 250)
        self.assertEqual(cfg["defaults"]["list_limit"], 80)
        self.assertEqual(cfg["defaults"]["query_limit"], 900)
        self.assertEqual(cfg["defaults"]["query_page_size"], 250)
        self.assertEqual(cfg["defaults"]["max_history_changes"], 0)
        self.assertEqual(cfg["department_profiles"][0]["owners"], ["a@example.com", "b@example.com"])
        self.assertEqual(cfg["dashboard_profiles"][0]["query"], "owner:self status:open")

    def test_gerrit_profile_helpers_add_people_and_departments(self):
        from core.gerrit_dashboard_config import (
            add_gerrit_department_profile,
            add_gerrit_personal_profile,
            assign_owner_to_gerrit_department,
            normalize_gerrit_dashboard_config,
        )

        cfg = normalize_gerrit_dashboard_config({"personal_profiles": [], "department_profiles": []})
        cfg = add_gerrit_personal_profile(cfg, "张三", "zhangsan@example.com")
        cfg = add_gerrit_department_profile(cfg, "系统一部", profile_id="sys-1", owners=["a@example.com"])
        cfg = assign_owner_to_gerrit_department(cfg, "sys-1", "b@example.com")

        self.assertEqual(cfg["personal_profiles"][-1]["owner"], "zhangsan@example.com")
        self.assertEqual(cfg["department_profiles"][-1]["id"], "sys-1")
        self.assertEqual(cfg["department_profiles"][-1]["owners"], ["a@example.com", "b@example.com"])

    def test_gerrit_personal_profile_can_be_assigned_to_department(self):
        from core.gerrit_dashboard_config import (
            add_gerrit_department_profile,
            add_gerrit_personal_profile,
            normalize_gerrit_dashboard_config,
        )

        cfg = normalize_gerrit_dashboard_config({"personal_profiles": [], "department_profiles": []})
        cfg = add_gerrit_department_profile(cfg, "系统一部", profile_id="sys-1")
        cfg = add_gerrit_personal_profile(cfg, "李四", "lisi@example.com", department_id="sys-1")

        self.assertEqual(cfg["personal_profiles"][-1]["department_id"], "sys-1")
        self.assertEqual(cfg["personal_profiles"][-1]["department"], "系统一部")
        self.assertEqual(cfg["department_profiles"][-1]["owners"], ["lisi@example.com"])

    def test_gerrit_can_create_missing_department_before_adding_member(self):
        from routers.gerrit_dashboard import _ensure_gerrit_department_profile
        from core.gerrit_dashboard_config import normalize_gerrit_dashboard_config

        cfg = normalize_gerrit_dashboard_config({"personal_profiles": [], "department_profiles": []})
        cfg = _ensure_gerrit_department_profile(cfg, "sys-2", "系统二部")

        self.assertEqual(cfg["department_profiles"][-1]["id"], "sys-2")
        self.assertEqual(cfg["department_profiles"][-1]["name"], "系统二部")

    def test_gerrit_syncs_members_from_redmine_user_map_and_can_remove_owner(self):
        from core.gerrit_dashboard_config import (
            normalize_gerrit_dashboard_config,
            remove_owner_from_gerrit_department,
            sync_gerrit_members_from_redmine_users,
        )

        cfg = normalize_gerrit_dashboard_config({"personal_profiles": [], "department_profiles": []})
        cfg = sync_gerrit_members_from_redmine_users(cfg, [
            {"name": "卞金晨", "email": "kenjc.bian@rock-chips.com", "department_id": "system-2", "department": "系统二部"},
            {"name": "黄超群", "email": "chaoqun.huang@rock-chips.com", "department_id": "system-2", "department": "系统二部"},
        ])

        sys2 = next(profile for profile in cfg["department_profiles"] if profile["id"] == "system-2")
        self.assertEqual(sys2["owners"], ["kenjc.bian@rock-chips.com", "chaoqun.huang@rock-chips.com"])
        self.assertEqual({p["owner"]: p["department_id"] for p in cfg["personal_profiles"]}, {
            "kenjc.bian@rock-chips.com": "system-2",
            "chaoqun.huang@rock-chips.com": "system-2",
        })

        cfg = remove_owner_from_gerrit_department(cfg, "system-2", "kenjc.bian@rock-chips.com")
        sys2 = next(profile for profile in cfg["department_profiles"] if profile["id"] == "system-2")
        self.assertEqual(sys2["owners"], ["chaoqun.huang@rock-chips.com"])

    def test_gerrit_chart_date_ranges_are_normalized(self):
        from core.gerrit_dashboard_config import normalize_gerrit_dashboard_config

        cfg = normalize_gerrit_dashboard_config({
            "chart_date_ranges": {
                "personal_daily": {"start": "2026-06-13", "end": "2026-06-01"},
                "bad": {"start": "nope"},
            }
        })

        self.assertEqual(cfg["chart_date_ranges"], {"personal_daily": {"start": "2026-06-01", "end": "2026-06-13"}})

    def test_gerrit_dashboard_runtime_save_preserves_other_runtime_config(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text(json.dumps({"gerrit_dashboard": {"base_url": "https://old.example.com"}}), encoding="utf-8")
            (configs / "config_runtime.json").write_text(json.dumps({"sidebar_order": ["test"]}), encoding="utf-8")

            manager = ConfigManager(base_dir=str(root / "core"))

            self.assertTrue(manager.save_gerrit_dashboard_config({
                "base_url": "https://10.10.10.29/",
                "rest_username": "dev",
                "rest_password": "secret",
                "ssh_identity_file": "/tmp/key",
                "department_profiles": [{"id": "sys-1", "name": "系统一部", "owners": ["dev@example.com"]}],
            }))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["sidebar_order"], ["test"])
            self.assertEqual(runtime["gerrit_dashboard"]["base_url"], "https://10.10.10.29")
            self.assertEqual(runtime["gerrit_dashboard"]["ssh_identity_file"], "/tmp/key")
            self.assertEqual(runtime["gerrit_dashboard"]["department_profiles"][0]["owners"], ["dev@example.com"])


if __name__ == "__main__":
    unittest.main()
