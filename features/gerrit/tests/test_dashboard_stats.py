import asyncio
import unittest
from importlib import import_module
from unittest.mock import AsyncMock, Mock, patch

from starlette.requests import Request


class GerritDashboardStatsTests(unittest.TestCase):
    def test_created_date_detail_filter_matches_trend_buckets(self):
        filter_by_created = import_module(
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

        items = filter_by_created(changes, "2026-06-12", "2026-06-13")

        self.assertEqual([item["number"] for item in items], ["101"])

    def test_department_stats_merge_members(self):
        summarize = import_module(
            "features.gerrit.config"
        ).summarize_gerrit_department_results
        data = summarize([
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

    def test_ssh_query_removes_status_any_and_adds_start(self):
        query_for_ssh = import_module("features.gerrit.service")._query_for_ssh

        self.assertEqual(
            query_for_ssh("owner:a@example.com status:any limit:500", limit=200, start=400),
            "owner:a@example.com limit:200 --start 400",
        )

    def test_effective_limits_support_unbounded_history(self):
        effective_limit = import_module(
            "features.gerrit.api"
        )._effective_history_limit

        self.assertIsNone(effective_limit(
            {"max_history_changes": 0, "query_limit": 500},
            {"max_history_changes": 0},
        ))
        self.assertEqual(effective_limit(
            {"max_history_changes": 300, "query_limit": 500},
            {},
        ), 300)

    def test_all_department_uses_all_department_owners(self):
        owners_for_profile = import_module(
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
            owners_for_profile(cfg, cfg["department_profiles"][0]),
            ["all@example.com", "a@example.com", "b@example.com"],
        )

    def test_connectivity_probe_never_interpolates_host_into_shell(self):
        api = import_module("features.gerrit.api")
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/gerrit/connectivity",
            "headers": [],
        })
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"200", b""))
        writer = Mock()
        writer.wait_closed = AsyncMock()
        hostile_host = "gerrit.example;touch-pwn"

        with patch.object(
            api,
            "_dashboard_config_for_request",
            return_value={"ssh_host": hostile_host, "ssh_port": 29418},
        ), patch.object(
            api.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as create_process, patch.object(
            api.asyncio,
            "open_connection",
            new=AsyncMock(return_value=(Mock(), writer)),
        ) as open_connection:
            result = asyncio.run(api.check_gerrit_connectivity(request))

        commands = [call.args for call in create_process.call_args_list]
        self.assertEqual(commands[0][:7], (
            "ping", "-c", "2", "-W", "2", "--", hostile_host
        ))
        self.assertEqual(commands[1][0], "curl")
        self.assertEqual(commands[1][-1], f"https://{hostile_host}/")
        self.assertNotIn("bash", {command[0] for command in commands})
        open_connection.assert_awaited_once_with(hostile_host, 29418)
        self.assertTrue(result["data"]["ssh_port_ok"])


if __name__ == "__main__":
    unittest.main()
