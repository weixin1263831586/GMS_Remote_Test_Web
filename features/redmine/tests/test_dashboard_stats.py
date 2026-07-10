import re
import subprocess
import unittest
from datetime import datetime
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from features.redmine.repository import RedmineAgentDB


def _issue(issue_id, assigned_to_name, status_name="新建", closed_on="", journals=None):
    return {
        "issue_id": issue_id,
        "subject": f"Issue {issue_id}",
        "status_name": status_name,
        "priority_name": "正常",
        "assigned_to_name": assigned_to_name,
        "created_on": "2026-05-01T00:00:00",
        "updated_on": "2026-06-10T00:00:00",
        "closed_on": closed_on,
        "journals_json": journals or [],
        "attachments_json": [],
        "failures_json": [],
        "is_resolved": 1 if status_name in ("已解决", "已关闭") else 0,
        "last_scanned_at": "2026-06-10T00:00:00",
    }


class RedmineDashboardStatsTests(unittest.TestCase):
    def test_workload_stale_list_means_older_than_threshold(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(1, "张三", journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "请处理"}]))
            db.upsert_issue(_issue(2, "张三", journals=[{"user": "客户", "created_on": "2026-06-12T00:00:00", "notes": "请处理"}]))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["张三"], stale_days=3, list_limit=10)

            self.assertEqual(stats["waiting_my_reply"], 2)
            self.assertEqual(stats["no_reply_3_days"], 1)
            self.assertEqual([item["issue_id"] for item in stats["lists"]["no_reply_3_days"]], [1])

    def test_hangup_issue_is_not_counted_as_waiting_rk_reply(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                632190,
                "黄 超群",
                status_name="HangUp",
                journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "目前是没有问题了，后续再 closed。"}],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["黄 超群"], stale_days=3, list_limit=10)

            self.assertEqual(stats["open_count"], 1)
            self.assertEqual(stats["waiting_my_reply"], 0)
            self.assertEqual(stats["no_reply_3_days"], 0)
            self.assertEqual(stats["lists"]["no_reply_3_days"], [])

    def test_owner_field_activity_is_not_counted_as_waiting_owner_reply(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                629401,
                "黄 超群",
                journals=[
                    {"user": "诺米视显 nomi", "created_on": "2026-05-21T02:02:18", "notes": "黄工，麻烦回复一下"},
                    {
                        "user": "黄 超群",
                        "created_on": "2026-05-29T07:03:40",
                        "notes": "",
                        "details": [{"name": "status", "old_value": "New", "new_value": "Confirmed"}],
                    },
                ],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["黄 超群"], stale_days=3, list_limit=10)

            self.assertEqual(stats["waiting_my_reply"], 0)
            self.assertEqual(stats["no_reply_3_days"], 0)
            self.assertEqual(stats["waiting_customer_reply"], 1)
            self.assertEqual(stats["customer_no_reply_3_days"], 1)
            self.assertEqual([item["issue_id"] for item in stats["lists"]["customer_no_reply_3_days"]], [629401])

    def test_unmapped_rockchip_email_suffix_is_rk_colleague(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                201,
                "黄 超群",
                journals=[
                    {
                        "user": "未配置RK同事",
                        "user_email": "unknown.rk@rock-chips.com",
                        "created_on": "2026-06-01T10:00:00",
                        "notes": "请黄工看一下",
                    },
                ],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["黄 超群"], stale_days=3, list_limit=10)

            self.assertEqual(stats["no_reply_3_days"], 1)
            self.assertEqual(stats["rk_colleague_no_reply_3_days"], 1)
            self.assertEqual(stats["customer_no_reply_3_days"], 0)
            self.assertEqual(stats["lists"]["rk_colleague_no_reply_3_days"][0]["last_external_reply_by"], "未配置RK同事")

    def test_redmine_user_map_name_with_site_suffix_marks_last_replier_as_rk_colleague(self):
        from features.redmine.repository import _looks_like_rk_actor

        self.assertTrue(_looks_like_rk_actor({"user": "吴 良清（福州）", "user_email": ""}))
        self.assertTrue(_looks_like_rk_actor({"user": "", "user_email": "dev@rock-chips.com"}))

    def test_unmapped_department_suffix_actor_is_counted_as_customer_reply(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                635620,
                "黄 超群",
                status_name="Confirmed",
                journals=[
                    {"user": "黄 超群", "created_on": "2026-06-18T09:51:02", "notes": "手动打开 Settings -- Battery -- Battery Saver 试下"},
                    {"user": "秋雨 电子", "created_on": "2026-06-18T10:11:33", "notes": "是不是关于节电这一块有什么配置不对，我抓取了一下logcat"},
                ],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 23, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = db.get_workload_statistics(owner_names=["黄 超群"], stale_days=3, list_limit=10)

            self.assertEqual(stats["no_reply_3_days"], 1)
            self.assertEqual(stats["customer_no_reply_3_days"], 0)
            self.assertEqual(stats["rk_colleague_no_reply_3_days"], 0)
            self.assertEqual(stats["lists"]["no_reply_3_days"][0]["issue_id"], 635620)

    def test_display_names_from_mapping_generates_spaced_chinese_variant(self):
        from features.redmine.repository import display_names_from_mapping

        self.assertEqual(
            display_names_from_mapping({"name": "韩金锋", "email": "jinfeng.han@rock-chips.com"}),
            ["韩金锋", "韩 金锋", "jinfeng.han@rock-chips.com"],
        )

    def test_unknown_chinese_person_actor_without_email_defaults_to_customer(self):
        from features.redmine.repository import _looks_like_rk_actor

        self.assertFalse(_looks_like_rk_actor({"user": "谢 娟红", "user_email": ""}))
        self.assertFalse(_looks_like_rk_actor({"user": "李 测试（福州）", "user_email": ""}))
        self.assertFalse(_looks_like_rk_actor({"user": "广东 天波", "user_email": ""}))

    def test_department_user_stats_exposes_owner_names_for_trend_detail(self):
        import asyncio

        from features.redmine.repository import compute_user_overdue_stats

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 0, "open_count": 0, "closed_count": 0}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            stats = asyncio.run(compute_user_overdue_stats(
                Client(),
                db,
                {"id": 1, "name": "韩金锋"},
                stale_days=3,
            ))

        self.assertEqual(stats["owner_names"], ["韩金锋", "韩 金锋"])

    def test_department_user_stats_uses_live_open_issue_snapshots_when_local_db_empty(self):
        import asyncio

        from features.redmine.repository import compute_user_overdue_stats

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 1, "closed_count": 0}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return [_issue(
                    301,
                    "韩 金锋",
                    journals=[{"user": "客户", "created_on": "2020-01-01T00:00:00", "notes": "请处理"}],
                )]

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            stats = asyncio.run(compute_user_overdue_stats(
                Client(),
                db,
                {"id": 1, "name": "韩金锋"},
                stale_days=3,
                issue_limit=50,
            ))

        self.assertEqual(stats["scanned_open_count"], 1)
        self.assertEqual(stats["waiting_my_reply"], 1)
        self.assertEqual(stats["no_reply_3_days"], 1)
        self.assertEqual(stats["max_unreplied_days"] > 0, True)

    def test_department_user_stats_force_refresh_updates_existing_local_snapshot(self):
        import asyncio

        from features.redmine.repository import compute_user_overdue_stats

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 1, "closed_count": 0}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return [_issue(
                    632190,
                    "黄 超群",
                    status_name="HangUp",
                    journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "目前没有问题，后续再 closed。"}],
                )]

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                632190,
                "黄 超群",
                status_name="新建",
                journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "请处理"}],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = asyncio.run(compute_user_overdue_stats(
                    Client(),
                    db,
                    {"id": 1, "name": "黄 超群"},
                    stale_days=3,
                    issue_limit=50,
                    force_refresh=True,
                ))
                refreshed = db.get_issue(632190)

            self.assertEqual(stats["no_reply_3_days"], 0)
            self.assertEqual(stats["overdue_issues"], [])
            self.assertEqual(refreshed["status_name"], "HangUp")

    def test_department_user_stats_force_refresh_rechecks_stale_issue_metadata(self):
        import asyncio

        from features.redmine.repository import compute_user_overdue_stats

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 1, "closed_count": 0}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return []

            async def fetch_resolved_issues_by_assignee(self, assignee_id, start="", end="", limit=2000):
                return []

            async def fetch_issue_metadata_snapshot(self, issue_id):
                assert issue_id == 637669
                return _issue(
                    637669,
                    "黄 超群",
                    status_name="Confirmed",
                    journals=[
                        {"user": "成 者", "created_on": "2026-07-02T08:26:55", "notes": "黄工，你好。麻烦抽时间帮看下这个问题，感谢。"},
                        {"user": "黄 超群", "created_on": "2026-07-07T07:44:20", "notes": "具体是哪个测试项有问题？"},
                    ],
                )

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                637669,
                "黄 超群",
                status_name="Confirmed",
                journals=[{"user": "成 者", "created_on": "2026-07-02T08:26:55", "notes": "黄工，你好。麻烦抽时间帮看下这个问题，感谢。"}],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 7, 7, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = asyncio.run(compute_user_overdue_stats(
                    Client(),
                    db,
                    {"id": 1, "name": "黄 超群"},
                    stale_days=3,
                    issue_limit=50,
                    force_refresh=True,
                ))

            self.assertEqual(stats["no_reply_3_days"], 0)
            self.assertEqual(stats["overdue_issues"], [])

    def test_department_user_stats_force_refresh_marks_closed_snapshot_resolved(self):
        import asyncio

        from features.redmine.repository import compute_user_overdue_stats

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 0, "closed_count": 1}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return []

            async def fetch_resolved_issues_by_assignee(self, assignee_id, start="", end="", limit=2000):
                return [{
                    "issue_id": 635620,
                    "subject": "RK3576 Android16 cts测试关于battery的fail",
                    "status_name": "Closed",
                    "assigned_to_name": "黄 超群",
                    "closed_on": "2026-06-30",
                    "updated_on": "2026-06-30T10:00:00",
                }]

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                635620,
                "黄 超群",
                status_name="Confirmed",
                journals=[{"user": "秋雨 电子", "created_on": "2026-06-18T10:11:33", "notes": "是不是关于节电这一块有什么配置不对"}],
            ))

            with patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 30, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                stats = asyncio.run(compute_user_overdue_stats(
                    Client(),
                    db,
                    {"id": 1, "name": "黄 超群"},
                    stale_days=3,
                    issue_limit=50,
                    force_refresh=True,
                ))
                refreshed = db.get_issue(635620)

            self.assertEqual(stats["no_reply_3_days"], 0)
            self.assertEqual(stats["overdue_issues"], [])
            self.assertEqual(refreshed["status_name"], "Closed")
            self.assertEqual(refreshed["is_resolved"], 1)
            self.assertEqual(refreshed["journals_json"][0]["user"], "秋雨 电子")

    def test_personal_workload_refresh_updates_existing_local_snapshot(self):
        import asyncio

        import features.redmine.api as redmine_router
        from features.auth import CurrentUser

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 1, "closed_count": 0}

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return [_issue(
                    632190,
                    "黄 超群",
                    status_name="HangUp",
                    journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "目前没有问题，后续再 closed。"}],
                )]

            async def close(self):
                pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                632190,
                "黄 超群",
                status_name="新建",
                journals=[{"user": "客户", "created_on": "2026-06-01T00:00:00", "notes": "请处理"}],
            ))
            service = SimpleNamespace(
                repository=db,
                agent=SimpleNamespace(_make_client=lambda: Client()),
            )
            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
                cookies={},
            )
            stats_api = redmine_router._statistics_api
            with patch.object(stats_api, "_service_for_request", return_value=service), patch.object(
                stats_api,
                "_user_map_for_request",
                return_value=[{"id": 1, "name": "黄 超群"}],
            ), patch.object(
                stats_api,
                "_get_redmine_stats_config",
                return_value={"stale_days": 3, "window_days": 60, "cache_ttl": 600},
            ), patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 6, 13, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                result = asyncio.run(stats_api.get_workload_statistics(
                    request,
                    stale_days=3,
                    list_limit=30,
                    name="黄 超群",
                    refresh=True,
                ))

            self.assertTrue(result["success"])
            self.assertEqual(result["data"]["no_reply_3_days"], 0)
            self.assertEqual(result["data"]["lists"]["no_reply_3_days"], [])
            self.assertEqual(db.get_issue(632190)["status_name"], "HangUp")

    def test_personal_workload_refresh_rechecks_stale_issue_metadata(self):
        import asyncio

        import features.redmine.api as redmine_router
        from features.auth import CurrentUser

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 1, "open_count": 1, "closed_count": 0}

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return {}

            async def fetch_open_issue_snapshots_by_assignee(self, assignee_id, limit, window_days):
                return []

            async def close(self):
                pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                634719,
                "黄 超群",
                status_name="Confirmed",
                journals=[{"user": "美格 智能", "created_on": "2026-07-01T02:25:00", "notes": "build log 也帮忙确认下"}],
            ))

            async def refresh_issue_metadata(issue_id):
                self.assertEqual(issue_id, 634719)
                db.upsert_issue(_issue(
                    634719,
                    "黄 超群",
                    status_name="Confirmed",
                    journals=[
                        {"user": "美格 智能", "created_on": "2026-07-01T02:25:00", "notes": "build log 也帮忙确认下"},
                        {"user": "黄 超群", "created_on": "2026-07-06T09:30:00", "notes": "已回复客户"},
                    ],
                ))
                return {"success": True}

            service = SimpleNamespace(
                repository=db,
                agent=SimpleNamespace(_make_client=lambda: Client()),
                refresh_issue_metadata=refresh_issue_metadata,
            )
            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
                cookies={},
            )
            stats_api = redmine_router._statistics_api
            with patch.object(stats_api, "_service_for_request", return_value=service), patch.object(
                stats_api,
                "_user_map_for_request",
                return_value=[{"id": 1, "name": "黄 超群", "aliases": ["黄超群"]}],
            ), patch.object(
                stats_api,
                "_get_redmine_stats_config",
                return_value={"stale_days": 3, "window_days": 60, "cache_ttl": 600},
            ), patch("features.redmine.repository_queries.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = datetime(2026, 7, 7, 12, 0, 0)
                mocked_datetime.min = datetime.min
                mocked_datetime.fromisoformat = datetime.fromisoformat
                result = asyncio.run(stats_api.get_workload_statistics(
                    request,
                    stale_days=3,
                    list_limit=30,
                    name="黄 超群",
                    refresh=True,
                ))

            self.assertTrue(result["success"])
            self.assertEqual(result["data"]["no_reply_3_days"], 0)
            self.assertEqual(result["data"]["lists"]["no_reply_3_days"], [])

    def test_personal_workload_uses_live_resolved_trends_over_incomplete_db(self):
        """Personal dashboard bars must reflect ALL closed issues from Redmine,
        not just the subset synced to the local DB. The live trend channel
        (resolved_trends_by_assignee) overrides the local-DB bars so a user
        like 黄超群 sees the same full history Gerrit shows."""
        import asyncio

        import features.redmine.api as redmine_router
        from features.auth import CurrentUser

        live_trends = {
            "resolved_daily": [{"date": "2026-06-04", "count": 10}, {"date": "2026-05-18", "count": 9}],
            "resolved_weekly": [{"week": "2026-W23", "count": 18}],
            "resolved_monthly": [{"month": "2026-06", "count": 62}, {"month": "2026-05", "count": 32}],
            "resolved_yearly": [{"year": "2026", "count": 101}],
        }

        class Client:
            async def count_issues_by_assignee(self, user_id):
                return {"total_owned": 101, "open_count": 4, "closed_count": 97}

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                return live_trends

            async def close(self):
                pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            # Local DB only knows about ONE closed issue — far less than the 97
            # Redmine actually has. Without the live override the bars would
            # show count=1 instead of the full history.
            db.upsert_issue(_issue(700001, "黄 超群", status_name="已解决", closed_on="2026-06-12"))
            service = SimpleNamespace(
                repository=db,
                agent=SimpleNamespace(_make_client=lambda: Client()),
            )
            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
                cookies={},
            )
            stats_api = redmine_router._statistics_api
            with patch.object(stats_api, "_service_for_request", return_value=service), patch.object(
                stats_api,
                "_user_map_for_request",
                return_value=[{"id": 1, "name": "黄 超群"}],
            ), patch.object(
                stats_api,
                "_get_redmine_stats_config",
                return_value={"stale_days": 3, "window_days": 60, "cache_ttl": 600},
            ):
                result = asyncio.run(stats_api.get_workload_statistics(
                    request,
                    stale_days=3,
                    list_limit=30,
                    name="黄 超群",
                ))

            self.assertTrue(result["success"])
            # Live counts override the partial local snapshot.
            self.assertEqual(result["data"]["total_owned"], 101)
            self.assertEqual(result["data"]["closed_count"], 97)
            # Live full-history trends override the incomplete local-DB bars.
            self.assertEqual(result["data"]["resolved_monthly"], [{"month": "2026-06", "count": 62}, {"month": "2026-05", "count": 32}])
            self.assertEqual(result["data"]["resolved_yearly"], [{"year": "2026", "count": 101}])
            self.assertEqual(result["data"]["resolved_daily"], live_trends["resolved_daily"])

    def test_personal_workload_falls_back_to_db_trends_when_live_fails(self):
        """When the live trend fetch fails, the personal dashboard must keep
        showing the local-DB trends instead of going blank."""
        import asyncio

        import features.redmine.api as redmine_router
        from features.auth import CurrentUser

        class Client:
            async def count_issues_by_assignee(self, user_id):
                raise RuntimeError("redmine unreachable")

            async def resolved_trends_by_assignee(self, user_id, freshness_days=180, limit=5000):
                raise RuntimeError("redmine unreachable")

            async def close(self):
                pass

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(700002, "黄 超群", status_name="已解决", closed_on="2026-06-12"))
            service = SimpleNamespace(
                repository=db,
                agent=SimpleNamespace(_make_client=lambda: Client()),
            )
            request = SimpleNamespace(
                state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
                cookies={},
            )
            stats_api = redmine_router._statistics_api
            with patch.object(stats_api, "_service_for_request", return_value=service), patch.object(
                stats_api,
                "_user_map_for_request",
                return_value=[{"id": 1, "name": "黄 超群"}],
            ), patch.object(
                stats_api,
                "_get_redmine_stats_config",
                return_value={"stale_days": 3, "window_days": 60, "cache_ttl": 600},
            ):
                result = asyncio.run(stats_api.get_workload_statistics(
                    request,
                    stale_days=3,
                    list_limit=30,
                    name="黄 超群",
                ))

            self.assertTrue(result["success"])
            # Live fetch failed → local-DB trend (1 closed issue on 2026-06-12) survives.
            self.assertEqual(result["data"]["resolved_daily"], [{"date": "2026-06-12", "count": 1}])


        import asyncio

        import features.redmine.api as redmine_router

        class ResolvedIssuesClient:
            def __init__(self):
                self.calls = []

            async def fetch_resolved_issues_by_assignee(self, assignee_id, start, end, limit):
                self.calls.append((assignee_id, start, end, limit))
                return [{
                    "issue_id": assignee_id * 10,
                    "subject": f"Issue {assignee_id}",
                    "assigned_to_name": str(assignee_id),
                    "resolved_on": "2026-06-12T10:00:00",
                    "closed_on": "2026-06-12",
                }]

            async def close(self):
                pass

        client = ResolvedIssuesClient()
        from features.auth import CurrentUser

        request = SimpleNamespace(
            state=SimpleNamespace(current_user=CurrentUser("alice", "alice", "user")),
            cookies={},
        )
        stats_api = redmine_router._statistics_api
        department_users = [
            {"id": 1, "name": "韩金锋", "department_id": "system-2", "department": "系统二部"},
            {"id": 2, "name": "黄超群", "department_id": "system-2", "department": "系统二部"},
        ]
        with patch.object(
            stats_api,
            "_dashboard_config_for_request",
            return_value={
                "profiles": [{"id": "system-2", "name": "系统二部", "user_ids": [], "aliases": []}],
                "defaults": {"list_limit": 50, "issue_limit": 500},
            },
        ), patch.object(
            stats_api,
            "_user_map_for_request",
            return_value=department_users,
        ), patch.object(
            stats_api,
            "_service_for_request",
            return_value=SimpleNamespace(agent=SimpleNamespace(_make_client=lambda: client)),
        ):
            result = asyncio.run(stats_api.get_resolved_issues_by_date(
                request,
                start="2026-06-12",
                end="2026-06-13",
                names="",
                profile_id="system-2",
                limit=500,
            ))

        self.assertTrue(result["success"])
        self.assertEqual([item["issue_id"] for item in result["data"]["items"]], [20, 10])
        self.assertEqual([call[0] for call in client.calls], [1, 2])

    def test_resolved_detail_uses_journal_resolution_date_when_closed_on_is_empty(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            db.upsert_issue(_issue(
                101,
                "黄 超群",
                status_name="已解决",
                closed_on="",
                journals=[
                    {
                        "user": "黄 超群",
                        "created_on": "2026-06-12T09:30:00",
                        "notes": "",
                        "details": [{"name": "status", "old_value": "Confirmed", "new_value": "已解决"}],
                    }
                ],
            ))

            stats = db.get_workload_statistics(owner_names=["黄 超群"], stale_days=3, list_limit=10)
            issues = db.get_resolved_issues_by_date(owner_names=["黄 超群"], start="2026-06-12", end="2026-06-13")

            self.assertEqual(stats["resolved_daily"], [{"date": "2026-06-12", "count": 1}])
            self.assertEqual([item["issue_id"] for item in issues], [101])
            self.assertEqual(issues[0]["resolved_on"][:10], "2026-06-12")

    def test_redmine_week_trend_click_uses_iso_week_start(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        match = re.search(r"function utcDateText\(date\) \{.*?function trendLabelToDateRange\(granularity, label\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + "\nconsole.log(JSON.stringify(trendLabelToDateRange('week', '2026-W24')));"
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, '["2026-06-08","2026-06-15"]')

    def test_redmine_daily_trend_click_uses_next_day_as_exclusive_end(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        match = re.search(r"function utcDateText\(date\) \{.*?function trendLabelToDateRange\(granularity, label\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + "\nconsole.log(JSON.stringify(trendLabelToDateRange('date', '2026-06-12')));"
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, '["2026-06-12","2026-06-13"]')

    def test_redmine_trend_detail_title_displays_inclusive_end_date(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        match = re.search(r"function utcDateText\(date\) \{.*?function displayTrendRange\(range\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + "\nconsole.log(displayTrendRange(['2026-06-12', '2026-06-13']));"
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, "2026-06-12 至 2026-06-12")

    def test_personal_trend_uses_meta_owner_names_when_selected_name_is_empty(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        match = re.search(r"function updateRedmineTrendNames\(selectedName, meta\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = (
            "let redmineTrendNames = [];\n"
            + match.group(0)
            + "\nupdateRedmineTrendNames('', {owner_names: ['黄 超群', 'chaoqun.huang@rock-chips.com']});"
            + "\nconsole.log(JSON.stringify(redmineTrendNames));"
        )
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, '["黄 超群","chaoqun.huang@rock-chips.com"]')

    def test_redmine_department_trend_binds_department_names_in_click_handler(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        match = re.search(r"function renderTrend\(title, items, keyName, chartKey, detailNames, detailProfileId\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = (
            "function esc(s) { return String(s == null ? '' : s).replace(/[&<>\"']/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]; }); }\n"
            + match.group(0)
            + "\nfunction filterTrendItems(items){ return items; }"
            + "\nfunction trendStartDate(){ return ''; }"
            + "\nfunction trendEndDate(){ return ''; }"
            + "\nconst html = renderTrend('每天解决Redmine问题', [{date:'2026-06-10', count:5}], 'date', 'department_daily', ['黄 超群', '韩 金锋'], 'system-2');"
            + "\nconsole.log(html.includes(\"黄 超群,韩 金锋\") && html.includes(\"system-2\"));"
        )
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, "true")

    def test_redmine_department_trend_detail_uses_profile_id(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        self.assertIn("profile_id=' + encodeURIComponent(profileId)", source)
        self.assertIn("departmentProfileId)", source)

    def test_redmine_trend_detail_issue_number_links_to_redmine(self):
        source = Path("features/redmine/ui/page.js").read_text(encoding="utf-8")
        self.assertIn("redmineIssueUrl(issueId)", source)
        self.assertIn('target="_blank" rel="noopener"', source)
        self.assertIn("'#' + id", source)

    def test_gerrit_week_trend_click_uses_iso_week_start(self):
        source = Path("features/gerrit/ui/page.html").read_text(encoding="utf-8")
        match = re.search(r"function utcDateText\(date\) \{.*?function trendLabelToDateRange\(granularity, label\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + "\nconsole.log(JSON.stringify(trendLabelToDateRange('week', '2026-W24')));"
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, '["2026-06-08","2026-06-15"]')

    def test_gerrit_trend_detail_title_displays_inclusive_end_date(self):
        source = Path("features/gerrit/ui/page.html").read_text(encoding="utf-8")
        match = re.search(r"function utcDateText\(date\) \{.*?function displayTrendRange\(range\) \{.*?\n\}", source, re.S)
        self.assertIsNotNone(match)
        script = match.group(0) + "\nconsole.log(displayTrendRange(['2026-06-08', '2026-06-15']));"
        output = subprocess.check_output(["node", "-e", script], text=True).strip()
        self.assertEqual(output, "2026-06-08 至 2026-06-14")

    def test_gerrit_trend_detail_uses_created_date_endpoint(self):
        source = Path("features/gerrit/ui/page.html").read_text(encoding="utf-8")
        self.assertIn("/api/gerrit-dashboard/changes-by-date?", source)
        self.assertIn("owners: owners.join(',')", source)
        self.assertIn("scope: trendScope || currentTab || ''", source)

    def test_department_trends_merge_daily_weekly_monthly_yearly(self):
        from features.redmine.dashboard import merge_resolved_trends

        merged = merge_resolved_trends([
            {
                "resolved_daily": [{"date": "2026-06-01", "count": 2}],
                "resolved_weekly": [{"week": "2026-W23", "count": 2}],
                "resolved_monthly": [{"month": "2026-06", "count": 2}],
                "resolved_yearly": [{"year": "2026", "count": 2}],
            },
            {
                "resolved_daily": [{"date": "2026-06-01", "count": 3}, {"date": "2026-06-02", "count": 1}],
                "resolved_weekly": [{"week": "2026-W23", "count": 4}],
                "resolved_monthly": [{"month": "2026-06", "count": 4}],
                "resolved_yearly": [{"year": "2026", "count": 4}],
            },
        ])

        self.assertEqual(merged["resolved_daily"][0], {"date": "2026-06-01", "count": 5})
        self.assertEqual(merged["resolved_weekly"], [{"week": "2026-W23", "count": 6}])
        self.assertEqual(merged["resolved_monthly"], [{"month": "2026-06", "count": 6}])
        self.assertEqual(merged["resolved_yearly"], [{"year": "2026", "count": 6}])

    def test_redmine_agent_db_creates_dashboard_indexes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = RedmineAgentDB(db_path=root / "redmine.sqlite3", docs_dir=root / "docs")
            with db.connect() as conn:
                indexes = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='redmine_agent_issues'"
                    ).fetchall()
                }

        self.assertIn("idx_redmine_agent_issues_assignee_status", indexes)
        self.assertIn("idx_redmine_agent_issues_updated", indexes)
        self.assertIn("idx_redmine_agent_issues_resolved_closed", indexes)

    def test_redmine_agent_db_reinitializes_if_sqlite_file_is_deleted_while_process_runs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "redmine.sqlite3"
            db = RedmineAgentDB(db_path=db_path, docs_dir=root / "docs")
            db.upsert_issue(_issue(1, "张三"))
            db_path.unlink()

            stats = db.get_issue_statistics()

            self.assertEqual(stats["total"], 0)
            self.assertEqual(stats["unresolved"], 0)

    def test_project_issue_stats_group_by_assignee_name(self):
        from features.redmine.dashboard import summarize_project_issues

        issues = [
            SimpleNamespace(
                id=101,
                subject="open a",
                assigned_to=SimpleNamespace(id=2, name="bob"),
                status=SimpleNamespace(name="新建"),
                priority=SimpleNamespace(name="正常"),
                updated_on=datetime(2026, 6, 10, 12, 0, 0),
            ),
            SimpleNamespace(
                id=102,
                subject="closed",
                assigned_to=SimpleNamespace(id=1, name="alice"),
                status=SimpleNamespace(name="已关闭"),
                priority=SimpleNamespace(name="高"),
                updated_on=datetime(2026, 6, 11, 12, 0, 0),
                closed_on=datetime(2026, 6, 12, 12, 0, 0),
            ),
            SimpleNamespace(
                id=103,
                subject="open b",
                assigned_to=SimpleNamespace(id=1, name="alice"),
                status=SimpleNamespace(name="进行中"),
                priority=SimpleNamespace(name="正常"),
                updated_on=datetime(2026, 6, 12, 12, 0, 0),
            ),
        ]

        data = summarize_project_issues(issues, list_limit=15)

        self.assertEqual(data["summary"], {"issue_count": 3, "assignee_count": 2, "open_count": 2, "closed_count": 1})
        self.assertEqual([row["name"] for row in data["assignees"]], ["alice", "bob"])
        self.assertEqual(data["assignees"][0]["total_owned"], 2)
        self.assertEqual(data["assignees"][0]["open_count"], 1)
        self.assertEqual(data["assignees"][0]["closed_count"], 1)

    def test_gerrit_change_stats_group_statuses_and_trends(self):
        summarize_gerrit_changes = import_module(
            "features.gerrit.config"
        ).summarize_gerrit_changes

        changes = [
            {
                "number": 101,
                "subject": "merged",
                "project": "p",
                "branch": "main",
                "status": "MERGED",
                "created": "2026-06-01 10:00:00.000000000",
                "updated": "2026-06-03 10:00:00.000000000",
            },
            {
                "_number": 102,
                "subject": "waiting",
                "project": "p",
                "branch": "main",
                "status": "NEW",
                "created": "2026-06-02T10:00:00Z",
                "labels": {"Code-Review": {"all": [{"value": 0}]}},
            },
            {
                "number": 103,
                "subject": "approved",
                "project": "p",
                "branch": "main",
                "status": "NEW",
                "createdOn": 1780400000,
                "submitRecords": [{"status": "OK"}],
            },
            {
                "number": 104,
                "subject": "abandoned",
                "project": "p",
                "branch": "main",
                "status": "ABANDONED",
                "created": "2025-12-31 10:00:00.000000000",
            },
        ]

        data = summarize_gerrit_changes(changes, list_limit=10)

        self.assertEqual(data["summary"]["total_count"], 4)
        self.assertEqual(data["summary"]["merged_count"], 1)
        self.assertEqual(data["summary"]["open_count"], 2)
        self.assertEqual(data["summary"]["abandoned_count"], 1)
        self.assertEqual(data["summary"]["pending_review_count"], 1)
        self.assertEqual(data["trends"]["daily"][0], {"date": "2025-12-31", "count": 1})
        self.assertIn({"month": "2026-06", "count": 3}, data["trends"]["monthly"])
        self.assertEqual([item["number"] for item in data["lists"]["pending_review"]], ["102"])

    def test_gerrit_created_date_detail_filter_matches_trend_buckets(self):
        filter_gerrit_changes_by_created_date = import_module(
            "features.gerrit.config"
        ).filter_gerrit_changes_by_created_date

        changes = [
            {
                "number": 101,
                "subject": "created in range but updated later",
                "status": "NEW",
                "created": "2026-06-12 10:00:00.000000000",
                "updated": "2026-06-20 10:00:00.000000000",
            },
            {
                "number": 102,
                "subject": "created before range but updated in range",
                "status": "NEW",
                "created": "2026-06-11 10:00:00.000000000",
                "updated": "2026-06-12 10:00:00.000000000",
            },
        ]

        items = filter_gerrit_changes_by_created_date(changes, "2026-06-12", "2026-06-13")

        self.assertEqual([item["number"] for item in items], ["101"])

    def test_gerrit_department_stats_merge_members(self):
        summarize_gerrit_department_results = import_module(
            "features.gerrit.config"
        ).summarize_gerrit_department_results

        data = summarize_gerrit_department_results([
            {
                "owner": "a@example.com",
                "summary": {"total_count": 2, "merged_count": 1, "open_count": 1, "abandoned_count": 0, "pending_review_count": 1},
                "trends": {"daily": [{"date": "2026-06-01", "count": 2}], "weekly": [], "monthly": [], "yearly": []},
            },
            {
                "owner": "b@example.com",
                "summary": {"total_count": 3, "merged_count": 2, "open_count": 1, "abandoned_count": 0, "pending_review_count": 0},
                "trends": {"daily": [{"date": "2026-06-01", "count": 1}], "weekly": [], "monthly": [], "yearly": []},
            },
        ])

        self.assertEqual(data["summary"]["total_count"], 5)
        self.assertEqual(data["summary"]["pending_review_count"], 1)
        self.assertEqual(data["trends"]["daily"], [{"date": "2026-06-01", "count": 3}])

    def test_gerrit_ssh_query_removes_status_any_and_adds_start(self):
        _query_for_ssh = import_module(
            "features.gerrit.service"
        )._query_for_ssh

        self.assertEqual(
            _query_for_ssh("owner:a@example.com status:any limit:500", limit=200, start=400),
            "owner:a@example.com limit:200 --start 400",
        )

    def test_gerrit_effective_limits_support_unbounded_history(self):
        _effective_history_limit = import_module(
            "features.gerrit.api"
        )._effective_history_limit

        self.assertIsNone(_effective_history_limit({"max_history_changes": 0, "query_limit": 500}, {"max_history_changes": 0}))
        self.assertEqual(_effective_history_limit({"max_history_changes": 300, "query_limit": 500}, {}), 300)

    def test_gerrit_all_department_uses_all_department_owners(self):
        _owners_for_department_profile = import_module(
            "features.gerrit.api"
        )._owners_for_department_profile

        cfg = {
            "department_profiles": [
                {"id": "all", "name": "全部部门", "owners": ["all@example.com"]},
                {"id": "sys-1", "name": "系统一部", "owners": ["a@example.com"]},
                {"id": "sys-2", "name": "系统二部", "owners": ["a@example.com", "b@example.com"]},
            ]
        }

        self.assertEqual(
            _owners_for_department_profile(cfg, cfg["department_profiles"][0]),
            ["all@example.com", "a@example.com", "b@example.com"],
        )

    def test_redmine_department_profiles_are_derived_from_user_map_when_runtime_config_empty(self):
        dashboard = import_module("features.redmine.dashboard")
        users = [
            {"id": 1, "name": "Alice", "department_id": "system-2", "department": "系统二部"},
            {"id": 2, "name": "Bob", "department_id": "system-1", "department": "系统一部"},
        ]

        cfg = dashboard.with_department_profiles_from_users({}, users)
        profile = dashboard.select_redmine_dashboard_profile(cfg, "system-2")
        selected = dashboard.filter_users_for_profile(users, profile)

        self.assertEqual(profile["name"], "系统二部")
        self.assertEqual([user["name"] for user in selected], ["Alice"])


if __name__ == "__main__":
    unittest.main()
