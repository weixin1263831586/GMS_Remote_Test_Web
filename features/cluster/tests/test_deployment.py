import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import paramiko
from fastapi import HTTPException


class WorkerDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.credential = Path(self.temporary_directory.name) / "gts.json"
        self.credential.write_text(
            json.dumps({
                "type": "service_account",
                "client_email": "worker@example.invalid",
                "private_key": (
                    "-----BEGIN PRIVATE KEY-----\nplaceholder\n"
                    "-----END PRIVATE KEY-----\n"
                ),
            }),
            encoding="utf-8",
        )
        self.credential.chmod(0o600)
        self.environment = patch.dict(
            "os.environ",
            {"GMS_GTS_CREDENTIAL_FILE": str(self.credential)},
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _body():
        return {
            "worker_id": "worker-1",
            "ssh_host": "user@192.0.2.10",
            "password": "secret",
            "token": "worker-token",
            "controller_url": "https://controller.example",
            "suite_root": "~/GMS-Suite",
        }

    def test_requires_heartbeat_newer_than_completed_installation(self):
        from features.cluster.deployment_api import deploy_worker

        repository = SimpleNamespace(
            get_worker=MagicMock(
                side_effect=[
                    {"last_heartbeat_at": "2026-01-01T00:00:00Z"},
                    {"last_heartbeat_at": "2026-01-01T00:00:02Z"},
                ]
            )
        )
        cluster = SimpleNamespace(
            repository=repository,
            config=SimpleNamespace(worker_registration_timeout_seconds=2),
        )
        sleep = AsyncMock()
        with patch(
            "features.cluster.deployment_api.asyncio.to_thread",
            new=AsyncMock(return_value={"success": True, "worker_id": "worker-1"}),
        ), patch(
            "features.cluster.deployment_api.asyncio.sleep", new=sleep
        ), patch(
            "features.cluster.deployment_api.service", return_value=cluster
        ), patch(
            "features.cluster.deployment_api.utc_now",
            return_value="2026-01-01T00:00:01Z",
        ):
            result = asyncio.run(deploy_worker(self._body()))

        self.assertTrue(result["registered"])
        self.assertEqual(result["last_heartbeat_at"], "2026-01-01T00:00:02Z")
        sleep.assert_awaited_once_with(1)

    def test_stale_worker_row_does_not_report_deployment_success(self):
        from features.cluster.deployment_api import deploy_worker

        cluster = SimpleNamespace(
            repository=SimpleNamespace(
                get_worker=lambda _worker_id: {
                    "last_heartbeat_at": "2026-01-01T00:00:00Z"
                }
            ),
            config=SimpleNamespace(worker_registration_timeout_seconds=2),
        )
        with patch(
            "features.cluster.deployment_api.asyncio.to_thread",
            new=AsyncMock(return_value={"success": True, "worker_id": "worker-1"}),
        ), patch(
            "features.cluster.deployment_api.asyncio.sleep", new=AsyncMock()
        ), patch(
            "features.cluster.deployment_api.service", return_value=cluster
        ), patch(
            "features.cluster.deployment_api.utc_now",
            return_value="2026-01-01T00:00:01Z",
        ), self.assertRaises(HTTPException) as raised:
            asyncio.run(deploy_worker(self._body()))

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("未产生新心跳", raised.exception.detail)

    def test_reports_authentication_failure_with_target(self):
        from features.cluster.deployment_api import deploy_worker

        with patch(
            "features.system.ssh_manager.create_connection",
            side_effect=paramiko.AuthenticationException(),
        ), self.assertRaises(HTTPException) as raised:
            asyncio.run(deploy_worker(self._body()))

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn(
            "SSH authentication failed for user@192.0.2.10",
            raised.exception.detail,
        )

    def test_installer_expands_quoted_home_relative_suite_root(self):
        script = (
            Path(__file__).resolve().parents[3]
            / "scripts/install_cluster_worker.sh"
        )
        source = script.read_text(encoding="utf-8")
        start = source.index('case "${SUITE_ROOT}" in')
        normalization = source[start : source.index("esac", start) + 4]

        completed = subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail; HOME=/home/worker; SUITE_ROOT="$1"; '
                + normalization
                + '; printf "%s" "${SUITE_ROOT}"',
                "installer-test",
                "~/GMS-Suite",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout, "/home/worker/GMS-Suite")


class LocalSoftwareReconfigurationTests(unittest.TestCase):
    def setUp(self):
        import features.cluster.deployment_api as deployment

        with deployment._LOCAL_SOFTWARE_LOCK:
            deployment._LOCAL_SOFTWARE_TASKS.clear()
            deployment._LOCAL_SOFTWARE_ACTIVE_TASK = ""

    def test_starts_the_restricted_local_software_unit(self):
        from features.cluster.deployment_api import _reconfigure_local_software

        loaded = SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        completed = SimpleNamespace(returncode=0, stdout="configured", stderr="")
        with patch(
            "features.cluster.deployment_api.subprocess.run",
            side_effect=[loaded, completed],
        ) as run:
            output = _reconfigure_local_software()

        self.assertEqual(output, "configured")
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[0], "sudo")
        self.assertEqual(command[-2:], ["start", "gms-web-app-local-software.service"])

    def test_runs_local_script_when_unit_is_not_installed(self):
        from features.cluster.deployment_api import _reconfigure_local_software

        missing = SimpleNamespace(returncode=1, stdout="not-found\n", stderr="")
        completed = SimpleNamespace(returncode=0, stdout="configured", stderr="")
        with patch(
            "features.cluster.deployment_api.subprocess.run",
            side_effect=[missing, completed],
        ) as run:
            output = _reconfigure_local_software()

        self.assertEqual(output, "configured")
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[0], "/bin/bash")
        self.assertTrue(command[1].endswith("configure_local_worker_software.sh"))

    def test_reconfigures_software_when_local_worker_is_idle(self):
        from features.cluster.deployment_api import (
            reconfigure_local_worker_software,
        )

        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="worker-local"),
            repository=SimpleNamespace(
                get_worker=MagicMock(return_value={"running_jobs": 0})
            ),
        )
        thread = MagicMock()
        with patch(
            "features.cluster.deployment_api.service", return_value=cluster
        ), patch(
            "features.cluster.deployment_api._local_worker_has_active_tests",
            return_value=False,
        ), patch(
            "features.cluster.deployment_api.threading.Thread",
            return_value=thread,
        ):
            result = asyncio.run(reconfigure_local_worker_software())

        self.assertTrue(result["success"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["task"]["worker_id"], "worker-local")
        thread.start.assert_called_once_with()

    def test_rejects_reconfiguration_while_local_test_is_running(self):
        from features.cluster.deployment_api import (
            reconfigure_local_worker_software,
        )

        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="worker-local"),
            repository=SimpleNamespace(
                get_worker=MagicMock(return_value={"running_jobs": 1})
            ),
        )
        with patch(
            "features.cluster.deployment_api.service", return_value=cluster
        ), patch(
            "features.cluster.deployment_api._local_worker_has_active_tests",
            return_value=True,
        ), self.assertRaises(HTTPException) as raised:
            asyncio.run(reconfigure_local_worker_software())

        self.assertEqual(raised.exception.status_code, 409)

    def test_reuses_the_active_software_task(self):
        import features.cluster.deployment_api as deployment

        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="worker-local"),
            repository=SimpleNamespace(
                get_worker=MagicMock(return_value={"running_jobs": 0})
            ),
        )
        thread = MagicMock()
        with patch.object(deployment, "service", return_value=cluster), patch.object(
            deployment, "_local_worker_has_active_tests", return_value=False
        ), patch.object(deployment.threading, "Thread", return_value=thread):
            first = asyncio.run(deployment.reconfigure_local_worker_software())
            second = asyncio.run(deployment.reconfigure_local_worker_software())

        self.assertEqual(second["task"]["id"], first["task"]["id"])
        self.assertTrue(second["already_running"])
        thread.start.assert_called_once_with()


class GtsCredentialResolutionTests(unittest.TestCase):
    """The deployment API must keep working when GMS_GTS_CREDENTIAL_FILE is
    unset, falling back to the bundled tools/GMS-Host-Tools/gts-rockchip.json
    instead of failing the deployment with HTTP 503."""

    def _write_service_account(self, path: Path) -> None:
        path.write_text(
            json.dumps({
                "type": "service_account",
                "client_email": "worker@example.invalid",
                "private_key": (
                    "-----BEGIN PRIVATE KEY-----\nplaceholder\n"
                    "-----END PRIVATE KEY-----\n"
                ),
            }),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def test_explicit_env_takes_precedence(self):
        from features.cluster.deployment_api import _resolve_gts_credential

        with tempfile.TemporaryDirectory() as directory:
            explicit = Path(directory) / "explicit-gts.json"
            self._write_service_account(explicit)
            with patch.dict(
                "os.environ",
                {"GMS_GTS_CREDENTIAL_FILE": str(explicit)},
            ):
                result = _resolve_gts_credential()
            self.assertEqual(result, explicit.resolve())

    def test_falls_back_to_bundled_credential_when_env_unset(self):
        from features.cluster.deployment_api import _resolve_gts_credential

        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            bundled = (
                project_root
                / "tools"
                / "GMS-Host-Tools"
                / "gts-rockchip.json"
            )
            bundled.parent.mkdir(parents=True)
            self._write_service_account(bundled)
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GMS_GTS_CREDENTIAL_FILE", None)
                result = _resolve_gts_credential(project_root=project_root)
            self.assertEqual(result, bundled.resolve())


if __name__ == "__main__":
    unittest.main()
