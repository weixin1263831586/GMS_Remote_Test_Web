"""workflows.start_cluster_test execution-unification tests.

The workflow must derive argv from the shared ExecutionSpec builder instead
of assembling its own command string; a second assembler would drift from
features/cluster/execution_spec.py and let the automation path bypass spec
validation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from features.cluster import ClusterRepository
from workflows.cluster_test_execution import start_cluster_test


def _repository(tmp: Path, tools_path: str = "/srv/GMS-Suite/android-cts/tools") -> ClusterRepository:
    repository = ClusterRepository(tmp / "cluster.sqlite3")
    repository.register_worker({
        "worker_id": "worker-1", "agent_version": "1", "max_jobs": 2,
        "name": "remote", "hostname": "host", "address": "10.0.0.1",
        "capabilities": {},
    })
    repository.heartbeat("worker-1", {
        "running_jobs": [],
        "devices": [{"serial": "ABC", "state": "available"}, {
            "serial": "DEF", "state": "available"}],
        "suites": [{
            "suite_type": "CTS", "suite_version": "17_r1", "suite_key": "CTS:17_r1",
            "tools_path": tools_path, "available": True,
        }],
    })
    return repository


def _request(**overrides):
    request = SimpleNamespace(
        worker_id="worker-1",
        test_suite="/srv/GMS-Suite/android-cts/tools",
        test_type="cts",
        test_module="CtsSecurityTestCases",
        test_case="",
        retry_dir="",
        devices=["worker-1:ABC", "worker-1:DEF"],
        local_server="",
        automation_run_id="",
        device_reservation_id="",
        build_id="",
        build_artifact_id="",
        gerrit_change_id="",
        gerrit_patchset="",
        redmine_issue_id="",
    )
    for key, value in overrides.items():
        setattr(request, key, value)
    return request


class StartClusterTestArgvUnificationTests(unittest.TestCase):
    def test_argv_is_built_from_shared_execution_spec_builder(self):

        with tempfile.TemporaryDirectory() as directory:
            repository = _repository(Path(directory))
            with (
                patch("workflows.cluster_test_execution.get_cluster_service")
                as get_service,
            ):
                get_service.return_value = SimpleNamespace(
                    repository=repository
                )
                result = start_cluster_test(_request(), "alice")

            self.assertEqual(result.status_code, 200, result.body)
            content = json.loads(result.body)
            self.assertTrue(content["success"], content)
            job_id = content["data"]["cluster_job_id"]
            commands = repository.poll_commands("worker-1", limit=10)
            payload = next(
                command["payload"]
                for command in commands
                if command.get("job_id") == job_id
            )

            # Sharding comes from the shared builder, not hand-rolled assembly.
            self.assertIn("--shard-count 2 -s ABC -s DEF", payload["argv"])
            self.assertEqual(
                payload["execution_spec"]["module"], "CtsSecurityTestCases"
            )
            # argv equals what the shared builder derives from the stored spec.
            from foundation.execution_spec import build_argv_from_spec

            self.assertEqual(
                payload["argv"], build_argv_from_spec(payload["execution_spec"])
            )

    def test_invalid_spec_returns_error_without_creating_job(self):

        with tempfile.TemporaryDirectory() as directory:
            repository = _repository(Path(directory))
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(
                    repository=repository
                )
                result = start_cluster_test(
                    _request(test_case="Case#test", test_module=""), "alice"
                )

        self.assertEqual(result.status_code, 400)
        self.assertIn(
            "test_case requires module", json.loads(result.body)["error"]
        )

    @staticmethod
    def _suite_with_testcases(directory: str) -> tuple[Path, str]:
        tools = Path(directory) / "android-cts" / "tools"
        testcases = tools.parent / "testcases"
        testcases.mkdir(parents=True, exist_ok=True)
        (testcases / "CtsHardwareTestCases.apk").write_bytes(b"apk")
        (testcases / "CtsCameraTestCases.apk").write_bytes(b"apk")
        return tools, str(tools)

    def test_instrumentation_package_module_is_rejected_with_suggestion(self):
        # android.hardware.cts 是 apk 内的 java 包名，不是 tradefed 模块名；
        # 必须在建任务前被拒绝并给出可用的模块建议。
        with tempfile.TemporaryDirectory() as directory:
            _tools, tools_path = self._suite_with_testcases(directory)
            repository = _repository(Path(directory), tools_path=tools_path)
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(
                    repository=repository
                )
                from foundation.cluster_port import get_local_worker_id

                local_id = get_local_worker_id()
                repository.register_worker({
                    "worker_id": local_id, "agent_version": "1", "max_jobs": 2,
                    "name": "local", "hostname": "local", "address": "127.0.0.1",
                    "capabilities": {},
                })
                repository.heartbeat(local_id, {
                    "running_jobs": [], "devices": [],
                    "suites": [{
                        "suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": tools_path,
                        "available": True,
                    }],
                })
                result = start_cluster_test(
                    _request(test_suite=tools_path, test_module="android.hardware.cts",
                             worker_id=local_id),
                    "alice",
                )

        self.assertEqual(result.status_code, 400, result.body)
        error = json.loads(result.body)["error"]
        self.assertIn("instrumentation", error)
        self.assertIn("CtsHardwareTestCases", error)
        # 未创建任何任务
        self.assertEqual(repository.list_jobs(), [])

    def test_unknown_module_is_rejected_when_testcases_dir_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            _tools, tools_path = self._suite_with_testcases(directory)
            repository = _repository(Path(directory), tools_path=tools_path)
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(
                    repository=repository
                )
                from foundation.cluster_port import get_local_worker_id

                local_id = get_local_worker_id()
                repository.register_worker({
                    "worker_id": local_id, "agent_version": "1", "max_jobs": 2,
                    "name": "local", "hostname": "local", "address": "127.0.0.1",
                    "capabilities": {},
                })
                repository.heartbeat(local_id, {
                    "running_jobs": [], "devices": [],
                    "suites": [{
                        "suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": tools_path,
                        "available": True,
                    }],
                })
                result = start_cluster_test(
                    _request(test_suite=tools_path, test_module="NoSuchModuleAnywhere",
                             worker_id=local_id),
                    "alice",
                )

        self.assertEqual(result.status_code, 400, result.body)
        self.assertIn("testcases", json.loads(result.body)["error"])

    def test_valid_module_passes_when_testcases_dir_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            _tools, tools_path = self._suite_with_testcases(directory)
            repository = _repository(Path(directory), tools_path=tools_path)
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(
                    repository=repository
                )
                from foundation.cluster_port import get_local_worker_id

                local_id = get_local_worker_id()
                repository.register_worker({
                    "worker_id": local_id, "agent_version": "1", "max_jobs": 2,
                    "name": "local", "hostname": "local", "address": "127.0.0.1",
                    "capabilities": {},
                })
                repository.heartbeat(local_id, {
                    "running_jobs": [], "devices": [
                        {"serial": "ABC", "state": "available"},
                        {"serial": "DEF", "state": "available"},
                    ],
                    "suites": [{
                        "suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": tools_path,
                        "available": True,
                    }],
                })
                result = start_cluster_test(
                    _request(test_suite=tools_path, test_module="CtsCameraTestCases",
                             worker_id=local_id,
                             devices=[f"{local_id}:ABC", f"{local_id}:DEF"]),
                    "alice",
                )

        self.assertEqual(result.status_code, 200, result.body)
        self.assertTrue(json.loads(result.body)["success"])


if __name__ == "__main__":
    unittest.main()


class StartClusterTestTransportPolicyTests(unittest.TestCase):
    def test_adb_proxy_device_rejects_physical_usb_module(self):
        """The /api/test/start workflow path must enforce the same
        transport compatibility policy as /api/cluster/jobs — a test that
        requires a physical USB channel cannot run on an ADB Proxy device."""
        with tempfile.TemporaryDirectory() as directory:
            repository = _repository(Path(directory))
            # Mark ABC as an ADB Proxy device via a heartbeat refresh that
            # preserves the suite inventory.
            repository.heartbeat("worker-1", {
                "running_jobs": [],
                "devices": [
                    {"serial": "ABC", "state": "available",
                     "transport": "adb_proxy"},
                    {"serial": "DEF", "state": "available"},
                ],
                "suites": [{
                    "suite_type": "CTS", "suite_version": "17_r1",
                    "suite_key": "CTS:17_r1",
                    "tools_path": "/srv/GMS-Suite/android-cts/tools",
                    "available": True,
                }],
            })
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(repository=repository)
                result = start_cluster_test(
                    _request(test_module="CtsUsbTests", test_case=""), "alice"
                )

        self.assertEqual(result.status_code, 409, result.body)
        self.assertIn("USB/Fastboot", json.loads(result.body)["error"])


class StartClusterTestDispatchCompensationTests(unittest.TestCase):
    def test_command_failure_fails_job_and_releases_claims(self):
        """When the dispatch-command write fails after the job was
        persisted, the job must be transitioned to failed and its claims
        released — instead of leaving an `assigned` job with no commands."""
        from foundation.command_result import CommandResult  # noqa: F401

        with tempfile.TemporaryDirectory() as directory:
            repository = _repository(Path(directory))
            with patch(
                "workflows.cluster_test_execution.get_cluster_service"
            ) as get_service:
                get_service.return_value = SimpleNamespace(repository=repository)
                def failing_create_command(data):
                    raise RuntimeError("disk full")

                repository.create_command = failing_create_command
                result = start_cluster_test(_request(), "alice")

            self.assertEqual(result.status_code, 503, result.body)
            body = json.loads(result.body)
            self.assertIn("失败", body["error"])

            # The error envelope doesn't carry the job id; look up the job
            # via repository state instead.
            jobs = repository.list_jobs(owner_id="alice")
            self.assertEqual(len(jobs), 1)
            job = jobs[0]
            self.assertEqual(job["status"], "failed")
            # Claims were released: devices are no longer claimed by the job.
            active_claims = [
                claim for claim in repository.claims.list_active()
                if claim.get("source_id") == f"job:{job['id']}"
            ]
            self.assertEqual(active_claims, [])
