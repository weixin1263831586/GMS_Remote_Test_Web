import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class AutomationApiTests(unittest.TestCase):
    def test_create_list_get_and_events(self):
        import routers.automation as automation_router
        from core.automation.store import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            with patch.object(automation_router, "automation_store", store):
                create_response = asyncio.run(automation_router.create_automation_run({
                    "profile_id": "manual-smoke",
                    "artifact_path": "/tmp/update.img",
                    "devices": ["ABC123"],
                    "test_plan": {"test_type": "CTS"},
                }))
                run_id = create_response["data"]["id"]
                list_response = asyncio.run(automation_router.list_automation_runs(status="", limit=50))
                get_response = asyncio.run(automation_router.get_automation_run(run_id))
                events_response = asyncio.run(automation_router.get_automation_run_events(run_id))

            self.assertEqual(create_response["success"], True)
            self.assertEqual(create_response["data"]["status"], "queued")
            self.assertEqual(list_response["success"], True)
            self.assertEqual(len(list_response["data"]["items"]), 1)
            self.assertEqual(get_response["data"]["id"], run_id)
            self.assertEqual(events_response["data"]["items"][0]["stage"], "queued")

    def test_worker_tick_advances_run(self):
        import routers.automation as automation_router
        from core.automation.store import AutomationStore

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            with patch.object(automation_router, "automation_store", store):
                create_response = asyncio.run(automation_router.create_automation_run({
                    "profile_id": "manual-smoke",
                    "artifact_path": "/tmp/update.img",
                    "devices": ["ABC123"],
                    "test_plan": {"test_type": "CTS"},
                }))
                tick_response = asyncio.run(automation_router.automation_worker_tick())

            self.assertEqual(create_response["data"]["status"], "queued")
            self.assertEqual(tick_response["success"], True)
            self.assertEqual(tick_response["data"]["status"], "waiting_device")

    def test_worker_tick_can_select_http_executor(self):
        import routers.automation as automation_router
        from core.automation.executors import HttpAutomationExecutor

        with patch.object(automation_router, "HttpAutomationExecutor") as http_cls:
            http_cls.return_value = HttpAutomationExecutor(base_url="http://127.0.0.1:5001")

            orchestrator = automation_router._orchestrator(executor_name="http")

        self.assertIsInstance(orchestrator.executor, HttpAutomationExecutor)

    def test_router_is_registered(self):
        import routers

        route_paths = []
        for router in routers.ALL_ROUTERS:
            route_paths.extend(getattr(route, "path", "") for route in getattr(router, "routes", []))

        self.assertIn("/api/automation/runs", route_paths)
        self.assertIn("/automation", route_paths)

    def test_index_template_has_gms_ats_nav_entry(self):
        template = Path("templates/index_fastapi.html").read_text(encoding="utf-8")

        self.assertIn('data-page="automation"', template)
        self.assertIn('id="page-automation"', template)
        self.assertIn('src="/automation"', template)
        self.assertIn("'automation': 'GMS ATS", template)

    def test_automation_page_exposes_workflow_controls(self):
        import routers.automation as automation_router

        response = asyncio.run(automation_router.automation_page())
        html = response.body.decode("utf-8")

        self.assertIn("Gerrit -> Jenkins -> 刷机 -> GMS 测试 -> 报告分析", html)
        self.assertIn('id="automation-create-run"', html)
        self.assertIn('id="automation-runs"', html)
        self.assertIn("/api/automation/runs", html)
        self.assertIn("/api/automation/gerrit/poll", html)
        self.assertIn("/api/automation/worker/tick", html)

    def test_gerrit_webhook_creates_idempotent_runs_for_matching_profiles(self):
        import routers.automation as automation_router
        from core.automation.store import AutomationStore

        profiles = [{
            "id": "rk3576-smoke",
            "name": "RK3576 Smoke",
            "enabled": True,
            "gerrit": {"project_regex": "rk3576.*", "branch_regex": "master"},
            "jenkins": {
                "job": "RK3576_ANDROID16",
                "parameters": {"GERRIT_CHANGE": "{gerrit_change_id}", "GERRIT_PATCHSET": "{gerrit_patchset}"},
                "artifact_pattern": ".*update.*\\.img$",
            },
            "test_plan": {"test_type": "CTS", "test_module": "CtsAppSecurityHostTestCases"},
            "flash": {"mode": "firmware", "wipe_data": True},
            "device_selector": {"min_count": 1},
            "reporting": {"gerrit_comment": True},
        }]
        payload = {
            "type": "patchset-created",
            "change": {
                "project": "rk3576_android16",
                "branch": "master",
                "number": "123",
                "subject": "Fix GMS",
                "owner": {"email": "dev@rock-chips.com"},
            },
            "patchSet": {"number": "7", "revision": "abcdef"},
        }

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            with patch.object(automation_router, "automation_store", store):
                with patch.object(automation_router, "_load_profiles_for_api", return_value=profiles):
                    first = asyncio.run(automation_router.handle_gerrit_webhook(payload))
                    second = asyncio.run(automation_router.handle_gerrit_webhook(payload))

            self.assertEqual(first["success"], True)
            self.assertEqual(len(first["data"]["created"]), 1)
            self.assertEqual(len(second["data"]["created"]), 0)
            self.assertEqual(len(second["data"]["existing"]), 1)
            run = store.list_runs(limit=10)[0]
            self.assertEqual(run["source_type"], "gerrit_webhook")
            self.assertEqual(run["profile_id"], "rk3576-smoke")
            self.assertEqual(run["jenkins_job"], "RK3576_ANDROID16")
            self.assertIn("CtsAppSecurityHostTestCases", run["test_plan_json"])

    def test_gerrit_poll_creates_runs_from_query_results(self):
        import routers.automation as automation_router
        from core.automation.store import AutomationStore

        profiles = [{
            "id": "rk3576-smoke",
            "name": "RK3576 Smoke",
            "enabled": True,
            "gerrit": {"project_regex": "rk3576.*", "branch_regex": "master", "query": "project:rk3576 status:open"},
            "jenkins": {"job": "RK3576_ANDROID16"},
            "test_plan": {"test_type": "CTS"},
        }]
        changes = [{
            "project": "rk3576_android16",
            "branch": "master",
            "number": "123",
            "subject": "Fix GMS",
            "owner": {"email": "dev@rock-chips.com"},
            "current_revision": "abcdef",
            "revisions": {"abcdef": {"_number": 7}},
        }]

        with TemporaryDirectory() as tmp:
            store = AutomationStore(Path(tmp) / "automation.sqlite3")
            with patch.object(automation_router, "automation_store", store):
                with patch.object(automation_router, "_load_profiles_for_api", return_value=profiles):
                    with patch.object(automation_router, "query_gerrit_changes_for_automation", return_value=changes):
                        result = asyncio.run(automation_router.poll_gerrit_changes())

            self.assertEqual(result["success"], True)
            self.assertEqual(result["data"]["created_count"], 1)
            self.assertEqual(store.list_runs(limit=10)[0]["gerrit_patchset"], "7")

    def test_profile_crud_and_dry_run(self):
        import routers.automation as automation_router

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.json"
            with patch.object(automation_router, "_profiles_path", return_value=path):
                created = asyncio.run(automation_router.save_automation_profile({
                    "id": "p1",
                    "name": "Profile 1",
                    "enabled": True,
                    "gerrit": {"project_regex": "rk3576.*", "branch_regex": "master"},
                    "jenkins": {"job": "J1"},
                    "test_plan": {"test_type": "CTS"},
                }))
                dry = asyncio.run(automation_router.dry_run_automation_profile("p1", {
                    "project": "rk3576_android16",
                    "branch": "master",
                    "change_id": "123",
                    "patchset": "7",
                }))

            self.assertEqual(created["success"], True)
            self.assertEqual(created["data"]["profile"]["id"], "p1")
            self.assertEqual(dry["success"], True)
            self.assertEqual(dry["data"]["matched"], True)
            self.assertEqual(dry["data"]["run_request"]["profile_id"], "p1")
