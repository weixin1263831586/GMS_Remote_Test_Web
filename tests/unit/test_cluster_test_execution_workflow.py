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


def _repository(tmp: Path) -> ClusterRepository:
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
            "tools_path": "/srv/GMS-Suite/android-cts/tools", "available": True,
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


if __name__ == "__main__":
    unittest.main()
