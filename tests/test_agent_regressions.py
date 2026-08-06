import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from fastapi.responses import PlainTextResponse

from features.assistant import api as assistant_api
from features.assistant.api import _missing_required_params
from features.assistant.executor import ActionExecutor, _json_body
from features.assistant.intent import resolve
from features.assistant.tools import registry
from features.auth import CurrentUser
from features.test_execution.api import get_status


class AgentRegressionTests(unittest.TestCase):
    @staticmethod
    def _request(role="user"):
        return SimpleNamespace(
            headers={},
            cookies={},
            client=SimpleNamespace(host="127.0.0.1"),
            method="POST",
            state=SimpleNamespace(current_user=CurrentUser(
                id="owner-1", username="owner", role=role
            )),
            url="http://test/agent",
            query_params={},
        )

    def test_router_call_kwargs_convert_fastapi_help_defaults(self):
        tool = registry.get("test_suites")
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

        kwargs = ActionExecutor()._build_call_kwargs(get_status, tool, request, {})

        self.assertIs(kwargs["help"], False)
        self.assertIs(kwargs["h"], None)

    def test_plain_text_response_is_normalized_without_json_parse_exception(self):
        payload = _json_body(PlainTextResponse("Internal Server Error", status_code=500))

        self.assertFalse(payload["success"])
        self.assertEqual(payload["error"], "Internal Server Error")

    def test_help_me_test_specific_module_resolves_to_test_start(self):
        intent = resolve(
            "帮我测试VtsHalPowerTargetTest Power/PowerAidl#hasFixedPerformance/0_android_hardware_power_IPower_default",
            {},
        )

        self.assertEqual(intent.tool_name, "test_start")
        self.assertEqual(intent.params["test_type"], "VTS")
        self.assertEqual(intent.params["test_module"], "Power/PowerAidl")
        self.assertEqual(
            intent.params["test_case"],
            "hasFixedPerformance/0_android_hardware_power_IPower_default",
        )
        self.assertEqual(intent.params["devices"], [])

    def test_open_test_page_resolves_to_navigation(self):
        intent = resolve("打开测试界面", {})

        self.assertEqual(intent.tool_name, "navigate")
        self.assertEqual(intent.params["page"], "test")

    def test_generic_agent_router_passes_json_body_to_wiki_question(self):
        service = SimpleNamespace(
            ask=MagicMock(
                return_value={
                    "answer": "使用 gts-tradefed",
                    "contexts": [],
                    "mode": "retrieval",
                }
            )
        )
        with patch("features.knowledge.api._service", service):
            result = asyncio.run(
                ActionExecutor().execute(
                    {},
                    self._request(),
                    "knowledge_ask",
                    {"question": "GTS 怎么跑"},
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.data["answer"], "使用 gts-tradefed")
        service.ask.assert_called_once()

    def test_generic_agent_router_materializes_fastapi_query_defaults(self):
        store = SimpleNamespace(
            search=MagicMock(return_value=[]),
        )
        service = SimpleNamespace(store=store)
        with patch("features.knowledge.api._service", service):
            result = asyncio.run(
                ActionExecutor().execute(
                    {},
                    self._request(),
                    "knowledge_search",
                    {"q": "CTS"},
                )
            )

        self.assertTrue(result.success)
        store.search.assert_called_once_with(
            ANY, "CTS", space_id="", tag="", limit=50
        )

    def test_agent_cannot_bypass_admin_dependency_for_user_listing(self):
        result = asyncio.run(
            ActionExecutor().execute({}, self._request(), "users_list", {})
        )

        self.assertFalse(result.success)
        self.assertIn("elevation_required", result.error)

    def test_agent_cannot_bypass_elevation_dependency_for_config_update(self):
        result = asyncio.run(
            ActionExecutor().execute(
                {}, self._request(role="admin"), "config_update", {}
            )
        )

        self.assertFalse(result.success)
        self.assertIn("elevation_required", result.error)

    def test_required_agent_params_treat_false_as_a_supplied_value(self):
        cancel_run = registry.get("automation_run_cancel")

        self.assertIsNone(registry.get("cluster_set_mode"))
        self.assertEqual(
            [item["name"] for item in _missing_required_params(cancel_run, {})],
            ["run_id"],
        )

    def test_agent_monitors_every_active_cluster_job_stage_before_completion(self):
        active_statuses = [
            "created",
            "queued",
            "leasing",
            "assigned",
            "dispatching",
            "running",
            "stopping",
            "collecting",
            "worker_lost",
        ]
        jobs = iter([
            *(
                {
                    "id": "job-1",
                    "status": status,
                    "assigned_worker_id": "worker-1",
                    "current_attempt_id": "attempt-1",
                }
                for status in active_statuses
            ),
            {
                "id": "job-1",
                "status": "completed",
                "assigned_worker_id": "worker-1",
                "current_attempt_id": "attempt-1",
            },
        ])
        repository = SimpleNamespace(
            get_job=MagicMock(side_effect=lambda _job_id: next(jobs))
        )
        cluster = SimpleNamespace(repository=repository)
        session = {
            "session_id": "agent-cluster-monitor",
            "client_id": "alice",
            "status": "monitoring",
            "pending_plan": {"policy": {}},
            "active_run": {
                "cluster_job_id": "job-1",
                "attempt_id": "attempt-1",
            },
            "workspace_context": {},
            "messages": [],
            "steps": [],
        }
        report = {
            "timestamp": "cluster-job-1",
            "report_id": "cluster:job-1:attempt-1",
            "status": "completed",
            "fail": 0,
            "total": 1,
        }

        async def no_wait(_seconds):
            return None

        assistant_api._agent_sessions[session["session_id"]] = session
        try:
            with (
                patch("features.cluster.get_cluster_service", return_value=cluster),
                patch.object(
                    assistant_api, "_report_for_cluster_job", return_value=report
                ),
                patch.object(assistant_api.asyncio, "sleep", side_effect=no_wait),
            ):
                asyncio.run(
                    assistant_api._monitor_agent_run(
                        session["session_id"], SimpleNamespace()
                    )
                )
        finally:
            assistant_api._agent_sessions.pop(session["session_id"], None)
            assistant_api._agent_monitor_tasks.pop(session["session_id"], None)

        self.assertEqual(repository.get_job.call_count, len(active_statuses) + 1)
        self.assertEqual(session["status"], "done")
        self.assertIsNone(session["active_run"])
        self.assertEqual(
            session["workspace_context"]["report_timestamp"], "cluster-job-1"
        )
        self.assertTrue(
            any(
                "集群测试完成" in item.get("content", "")
                for item in session["messages"]
            )
        )

    def test_local_durable_job_keeps_agent_workspace_in_single_host_mode(self):
        session = {
            "session_id": "agent-local-durable",
            "active_run": {},
            "workspace_context": {},
            "steps": [],
        }
        plan = {
            "request": {
                "worker_id": "ats-worker-controller",
                "devices": ["SERIAL-1"],
                "test_type": "CTS",
                "test_suite": "/suite/tools",
            }
        }

        async def start_local_durable(_request, help=False, req=None):
            self.assertFalse(help)
            self.assertEqual(req.worker_id, "ats-worker-controller")
            return {
                "success": True,
                "data": {
                    "cluster_job_id": "job-local",
                    "attempt_id": "attempt-local",
                    "worker_id": "ats-worker-controller",
                },
            }

        with patch("features.test_execution.start_test", side_effect=start_local_durable):
            result = asyncio.run(
                assistant_api._start_test_with_plan(
                    session, SimpleNamespace(), plan
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(session["workspace_context"]["scope_mode"], "single")
        self.assertEqual(session["workspace_context"]["worker_id"], "ats-worker-controller")
        self.assertEqual(session["active_run"]["cluster_job_id"], "job-local")


if __name__ == "__main__":
    unittest.main()
