import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.auth import CurrentUser
from features.test_execution import status_api
from features.test_execution.status_api import _normalize_log_entry


def _agent_principal(owner_id="alice", scopes=frozenset({"tests.execute"})):
    return CurrentUser(
        id="agent:token-1",
        username="agent:token-1",
        role="agent_service",
        extra_permissions=scopes,
        resource_owner_id=owner_id,
    )


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
            "id": "job-1", "status": "running", "worker_id": "ats-worker-controller",
            "attempt_id": "attempt-1", "suite_key": "CTS:17",
            "devices": ["ats-worker-controller:SERIAL-1"], "created_at": "", "updated_at": "",
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
        self.assertEqual(payload["devices"], ["ats-worker-controller:SERIAL-1"])
        self.assertEqual(payload["active_jobs"][0]["id"], "job-1")

    async def test_status_reads_durable_jobs_by_agent_resource_owner(self):
        """Agent 主体的持久化任务按 resource_owner 账户过滤（ADR 0010）。

        client_id 推导出的是合成 actor id（agent:<token_id>）；若用它过滤，
        Agent 启动的任务在状态轮询里不可见。
        """
        request = SimpleNamespace(
            query_params={"logs": "false"},
            state=SimpleNamespace(current_user=_agent_principal("alice")),
        )
        with (
            patch.object(status_api.runtime, "generate_help_or_continue", return_value=None),
            patch.object(status_api.runtime, "get_client_id_from_request", return_value="agent:token-1"),
            patch.object(status_api, "get_or_create_user_state", return_value={"logs": []}),
            patch.object(status_api, "get_usb_monitor", return_value=None),
            patch.object(status_api, "_active_durable_jobs", return_value=[]) as active,
        ):
            await status_api.get_status(request)

        active.assert_called_once_with("alice")


class _FakeDurableRepository:
    def __init__(self, jobs):
        self.jobs = jobs
        self.owner_filters = []

    def list_jobs(self, limit=500, owner_id=""):
        self.owner_filters.append(owner_id)
        return [job for job in self.jobs if job.get("owner_id") == owner_id]

    def get_job(self, job_id):
        # 桥接解析时任务仍在运行；随后 Worker 上报完成（流循环随即排空退出）。
        return next(
            ({**job, "status": "completed"}
             for job in self.jobs if job["id"] == job_id),
            None,
        )

    def list_events(self, job_id, after=-1, limit=1000):
        if after < 0:
            return [{"sequence": 0, "message": "worker log line"}]
        return []


class TestLogStreamOwnerResolution(unittest.IsolatedAsyncioTestCase):
    async def test_stream_bridges_to_agent_resource_owner_job(self):
        """logs/stream 对 Agent 主体同样按 resource_owner 账户解析任务。"""
        repository = _FakeDurableRepository([{
            "id": "job-agent", "owner_id": "alice", "status": "running",
        }])
        request = SimpleNamespace(
            query_params={},
            state=SimpleNamespace(current_user=_agent_principal("alice")),
        )
        with (
            patch.object(
                status_api.runtime, "get_client_id_from_request",
                return_value="agent:token-1",
            ),
            patch("foundation.cluster_port.get_cluster_service") as cluster,
        ):
            cluster.return_value = SimpleNamespace(repository=repository)
            response = await status_api.stream_test_logs(request)
            chunks = [chunk async for chunk in response.body_iterator]

        self.assertEqual(repository.owner_filters, ["alice"])
        text = "".join(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for chunk in chunks
        )
        self.assertIn("worker log line", text)
        self.assertIn("=== Test complete ===", text)


if __name__ == "__main__":
    unittest.main()
