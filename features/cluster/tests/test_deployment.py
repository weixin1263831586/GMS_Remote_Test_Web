import asyncio
import json
import os
import subprocess
import tarfile
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

    def test_bundled_adbproxy_package_is_valid(self):
        from features.cluster.deployment_api import _validated_adbproxy_package

        project_root = Path(__file__).resolve().parents[3]
        package, checksum = _validated_adbproxy_package(project_root)

        self.assertTrue(package.is_file())
        self.assertTrue(checksum.is_file())
        self.assertEqual(
            package.name,
            "adbproxy-rs-linux-x86_64-musl.tar.gz",
        )
        with tarfile.open(package, "r:gz") as archive:
            build_info_file = archive.extractfile("./BUILDINFO")
            self.assertIsNotNone(build_info_file)
            build_info = build_info_file.read().decode("utf-8")
        self.assertIn("version=0.4.5", build_info)
        self.assertIn(
            "source_commit=f2beb4ff1bece8ab8f5d63c04dbfd6bf90aae8ee",
            build_info,
        )

    def test_rejects_tampered_adbproxy_package(self):
        from features.cluster.deployment_api import _validated_adbproxy_package

        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            dist = project_root / "tools/adbproxy-rs/dist"
            dist.mkdir(parents=True)
            package = dist / "adbproxy-rs-linux-x86_64-musl.tar.gz"
            package.write_bytes(b"tampered")
            package.with_name(f"{package.name}.sha256").write_text(
                f"{'0' * 64}  {package.name}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                _validated_adbproxy_package(project_root)

    def test_worker_bundle_contains_adbproxy_installer_package(self):
        from features.cluster.deployment_api import _add_adbproxy_package

        project_root = Path(__file__).resolve().parents[3]
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
            with tarfile.open(archive.name, "w:gz") as bundle:
                bundle.add(
                    project_root / "scripts/install_adbproxy_rs.sh",
                    arcname="scripts/install_adbproxy_rs.sh",
                )
                _add_adbproxy_package(bundle, project_root)
            with tarfile.open(archive.name, "r:gz") as bundle:
                names = set(bundle.getnames())

        self.assertIn("scripts/install_adbproxy_rs.sh", names)
        self.assertIn(
            "tools/adbproxy-rs/dist/"
            "adbproxy-rs-linux-x86_64-musl.tar.gz",
            names,
        )
        self.assertIn(
            "tools/adbproxy-rs/dist/"
            "adbproxy-rs-linux-x86_64-musl.tar.gz.sha256",
            names,
        )

    def test_adbproxy_installer_uses_the_offline_package(self):
        project_root = Path(__file__).resolve().parents[3]
        installer = project_root / "scripts/install_adbproxy_rs.sh"
        installer_source = installer.read_text(encoding="utf-8")
        self.assertNotIn("curl ", installer_source)
        self.assertNotIn("github.com", installer_source)
        with tempfile.TemporaryDirectory() as directory:
            install_dir = Path(directory) / "bin"
            environment = dict(os.environ)
            environment["GMS_ADB_PROXY_INSTALL_DIR"] = str(install_dir)
            environment.pop("GMS_ADB_PROXY_ARCHIVE_FILE", None)
            environment.pop("GMS_ADB_PROXY_ARCHIVE_SHA256", None)
            completed = subprocess.run(
                [str(installer)],
                cwd=project_root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertIn("0.4.5", completed.stdout)
            for binary_name in ("adb-proxy", "adb-hub", "adb-hubd"):
                self.assertTrue((install_dir / binary_name).is_file())

    def test_manual_deploy_command_includes_offline_adbproxy_package(self):
        page_js = (
            Path(__file__).resolve().parents[1] / "ui/page.js"
        ).read_text(encoding="utf-8")

        self.assertIn("scripts/install_adbproxy_rs.sh", page_js)
        self.assertIn("tools/adbproxy-rs/dist", page_js)

    def test_source_only_deployment_waits_for_adb_proxy_capability(self):
        from features.cluster.deployment_api import deploy_adb_proxy_source

        worker_id = "adb-source-192-0-2-10-12345678"
        cluster = SimpleNamespace(
            repository=SimpleNamespace(
                get_worker=lambda _worker_id: {
                    "last_heartbeat_at": "2026-01-01T00:00:02Z",
                    "capabilities": {
                        "adb_proxy": True,
                        "adb_proxy_source_only": True,
                    },
                }
            ),
            config=SimpleNamespace(worker_registration_timeout_seconds=1),
        )
        with patch(
            "features.cluster.deployment_api._adb_proxy_source_worker_id",
            return_value=worker_id,
        ), patch(
            "features.cluster.deployment_api.asyncio.to_thread",
            new=AsyncMock(return_value={
                "success": True,
                "worker_id": worker_id,
                "ssh_host": "user@192.0.2.10",
            }),
        ), patch(
            "features.cluster.deployment_api.service",
            return_value=cluster,
        ), patch(
            "features.cluster.deployment_api.utc_now",
            return_value="2026-01-01T00:00:01Z",
        ):
            result = asyncio.run(deploy_adb_proxy_source({
                "ssh_host": "user@192.0.2.10",
                "password": "secret",
                "controller_url": "https://controller.example",
            }))

        self.assertTrue(result["registered"])
        self.assertEqual(result["worker_id"], worker_id)

    def test_source_only_install_command_passes_token_by_file(self):
        from features.cluster.deployment_api import (
            _adb_proxy_source_install_command,
        )

        command = _adb_proxy_source_install_command(
            worker_id="adb-source-host",
            controller_url="https://controller.example",
            hostname="192.0.2.10",
            remote_archive="/tmp/source.tar.gz",
            remote_token="/tmp/source.token",
            controller_ca_arg="-",
            require_sudo=True,
        )

        self.assertIn("/tmp/source.token", command)
        self.assertNotIn("worker-secret", command)
        self.assertIn("rm -f /tmp/source.tar.gz /tmp/source.token", command)

    def test_source_only_installer_is_minimal_and_offline(self):
        script = (
            Path(__file__).resolve().parents[3]
            / "scripts/install_adb_proxy_source_worker.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("install_adbproxy_rs.sh", script)
        self.assertIn('"source_only": True', script)
        self.assertNotIn("GTS_CREDENTIAL", script)
        self.assertNotIn("x11vnc", script)
        self.assertNotIn("curl ", script)


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
