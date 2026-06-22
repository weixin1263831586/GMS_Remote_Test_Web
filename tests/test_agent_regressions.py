import unittest
from types import SimpleNamespace

from fastapi.responses import PlainTextResponse

from core.agent_executor import ActionExecutor, _json_body
from core.agent_intent import resolve
from core.agent_tools import registry
from routers.agent import _parse_user_intent
from routers.tests import get_status


class AgentRegressionTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
