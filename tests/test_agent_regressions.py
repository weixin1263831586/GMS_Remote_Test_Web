import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from fastapi.responses import PlainTextResponse

from features.assistant.api import _missing_required_params, _parse_user_intent
from features.assistant.executor import ActionExecutor, _json_body
from features.assistant.intent import resolve
from features.assistant.tools import registry
from features.test_execution.api import get_status


class AgentRegressionTests(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(
            headers={},
            cookies={},
            client=SimpleNamespace(host="127.0.0.1"),
            method="POST",
            state=SimpleNamespace(),
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

        legacy_intent = _parse_user_intent(
            "帮我测试VtsHalPowerTargetTest Power/PowerAidl#hasFixedPerformance/0_android_hardware_power_IPower_default"
        )
        self.assertEqual(legacy_intent["devices"], [])
        self.assertEqual(legacy_intent["device_count"], 1)

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

    def test_required_agent_params_treat_false_as_a_supplied_value(self):
        cluster_mode = registry.get("cluster_set_mode")
        cancel_run = registry.get("automation_run_cancel")

        self.assertEqual(_missing_required_params(cluster_mode, {"enabled": False}), [])
        self.assertEqual(
            [item["name"] for item in _missing_required_params(cancel_run, {})],
            ["run_id"],
        )


if __name__ == "__main__":
    unittest.main()
