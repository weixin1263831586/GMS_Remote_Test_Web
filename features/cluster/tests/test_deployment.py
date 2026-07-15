import asyncio
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import paramiko


class WorkerDeploymentTests(unittest.TestCase):
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
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(deploy_worker(self._body()))

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("未产生新心跳", raised.exception.detail)

    def test_reports_authentication_failure_with_target(self):
        from features.cluster.deployment_api import deploy_worker

        with patch(
            "features.system.ssh_manager.create_connection",
            side_effect=paramiko.AuthenticationException(),
        ):
            with self.assertRaises(HTTPException) as raised:
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


if __name__ == "__main__":
    unittest.main()
