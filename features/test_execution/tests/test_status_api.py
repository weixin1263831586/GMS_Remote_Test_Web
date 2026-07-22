import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.test_execution import status_api
from features.test_execution.status_api import _normalize_log_entry


class TestStatusApiLogNormalization(unittest.TestCase):
    def test_old_module_log_string_is_classified_as_module(self):
        entry = _normalize_log_entry(
            "[11:01:41] 06-25 11:01:41 I/ModuleListener: "
            "[1/1] RK3576GMS1 SdcardfsTest#testSdcardfsNotPresent IGNORED"
        )

        self.assertEqual(entry["source"], "module")
        self.assertEqual(entry["type"], "info")
        self.assertIn("ModuleListener", entry["msg"])

    def test_plain_system_log_string_is_classified_as_system(self):
        entry = _normalize_log_entry("[11:01:41] 测试已启动")

        self.assertEqual(entry["source"], "system")
        self.assertEqual(entry["msg"], "测试已启动")

    def test_explicit_source_is_preserved(self):
        entry = _normalize_log_entry(
            {"msg": "Executing command: run_GMS_Test_Auto.sh CTS", "type": "info", "source": "system"}
        )

        self.assertEqual(entry["source"], "system")


class TestStatusApiPolling(unittest.IsolatedAsyncioTestCase):
    async def test_logs_false_still_returns_log_count_for_websocket_fallback(self):
        request = SimpleNamespace(
            query_params={"logs": "false"},
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        user_state = {
            "running": True,
            "logs": ["first", "second"],
            "devices": [],
        }
        with (
            patch.object(status_api.runtime, "generate_help_or_continue", return_value=None),
            patch.object(status_api.runtime, "get_client_id_from_request", return_value="alice"),
            patch.object(status_api, "get_or_create_user_state", return_value=user_state),
            patch.object(status_api, "get_usb_monitor", return_value=None),
            patch.object(status_api, "_active_durable_jobs", return_value=[]),
        ):
            response = await status_api.get_status(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["log_count"], 2)
        self.assertNotIn("logs", payload)

    async def test_status_recovers_only_authenticated_owners_durable_jobs(self):
        request = SimpleNamespace(
            query_params={"logs": "false"},
            app=SimpleNamespace(state=SimpleNamespace()),
        )
        durable_jobs = [{
            "id": "job-1", "status": "running", "worker_id": "worker-local",
            "attempt_id": "attempt-1", "suite_key": "CTS:17",
            "devices": ["worker-local:SERIAL-1"], "created_at": "", "updated_at": "",
        }]
        with (
            patch.object(status_api.runtime, "generate_help_or_continue", return_value=None),
            patch.object(status_api.runtime, "get_client_id_from_request", return_value="user-uuid"),
            patch.object(status_api, "get_or_create_user_state", return_value={"logs": []}),
            patch.object(status_api, "get_usb_monitor", return_value=None),
            patch.object(status_api, "_active_durable_jobs", return_value=durable_jobs) as active,
        ):
            response = await status_api.get_status(request)

        payload = json.loads(response.body)
        active.assert_called_once_with("user-uuid")
        self.assertTrue(payload["running"])
        self.assertEqual(payload["devices"], ["worker-local:SERIAL-1"])
        self.assertEqual(payload["active_jobs"][0]["id"], "job-1")
