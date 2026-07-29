from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service
from foundation.config import config_manager


class ElevatedRouteAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "auth.sqlite3"
        auth_service._initialized = False
        self.environment = patch.dict(
            "os.environ",
            {
                "GMS_AUTH_REQUIRED": "true",
                "GMS_SECURE_COOKIES": "false",
                "TRUSTED_HOSTS": "testserver",
            },
        )
        self.environment.start()
        self.client = TestClient(create_app())
        setup = self.client.post(
            "/api/auth/setup",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(setup.status_code, 200)
        auth_service.create_user("client", "clientpass1", role="user")
        self.client.post("/api/auth/logout", headers={"Origin": "http://testserver"})
        login = self.client.post(
            "/api/auth/login",
            headers={"Origin": "http://testserver"},
            json={"username": "client", "password": "clientpass1"},
        )
        self.assertEqual(login.status_code, 200)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        self.tmp.cleanup()

    def _assert_elevation_required(self, path: str, payload: dict) -> None:
        denied = self.client.post(
            path,
            headers={"Origin": "http://testserver"},
            json=payload,
        )
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])

    def _elevate(self) -> None:
        response = self.client.post(
            "/api/auth/elevate",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(response.status_code, 200)

    def test_file_browser_uses_temporary_admin_elevation(self):
        browse_root = Path(self.tmp.name) / "browse"
        browse_root.mkdir()
        (browse_root / "system.img").write_bytes(b"image")
        payload = {"path": str(browse_root)}
        self._assert_elevation_required("/api/files/list", payload)
        self._elevate()

        with (
            patch.object(config_manager, "load_config", return_value={}),
            patch.object(config_manager, "get_ubuntu_user", return_value="tester"),
            patch.object(config_manager, "is_config_host_local", return_value=True),
        ):
            allowed = self.client.post(
                "/api/files/list",
                headers={"Origin": "http://testserver"},
                json=payload,
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["files"][0]["name"], "system.img")

    def test_tradefed_results_use_temporary_admin_elevation(self):
        payload = {"suite_path": "/tmp/android-cts/tools"}
        self._assert_elevation_required("/api/test/suites/result", payload)
        self._elevate()

        with patch(
            "features.test_execution.transfers_api.collect_tradefed_results",
            new=AsyncMock(return_value={"success": True, "results": [], "count": 0}),
        ):
            allowed = self.client.post(
                "/api/test/suites/result",
                headers={"Origin": "http://testserver"},
                json=payload,
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["success"])

    def test_external_service_config_reads_as_user_and_writes_after_elevation(self):
        config = {
            "external_services": {
                "gms_assistant_url": "https://assistant.example.test/",
            },
        }
        with (
            patch.object(config_manager, "load_config", return_value=config),
            patch.object(
                config_manager,
                "update_runtime_config",
                return_value=True,
            ) as update_runtime_config,
        ):
            readable = self.client.get("/api/config/external-services")
            self.assertEqual(readable.status_code, 200)
            self.assertEqual(
                readable.json()["data"]["gms_assistant_url"],
                "https://assistant.example.test",
            )

            self._assert_elevation_required(
                "/api/config/external-services",
                {"gms_assistant_url": "https://new-assistant.example.test/chat"},
            )
            self._elevate()
            allowed = self.client.post(
                "/api/config/external-services",
                headers={"Origin": "http://testserver"},
                json={
                    "gms_assistant_url": "https://new-assistant.example.test/chat"
                },
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.json()["data"]["gms_assistant_url"],
            "https://new-assistant.example.test",
        )
        update_runtime_config.assert_called_once_with({
            "external_services": {
                "gms_assistant_url": "https://new-assistant.example.test",
            },
        })

    def test_worker_host_key_scan_uses_temporary_admin_elevation(self):
        payload = {"ssh_host": "worker@192.0.2.10"}
        self._assert_elevation_required(
            "/api/cluster/workers/ssh-host-key/scan", payload
        )
        self._elevate()

        with patch(
            "features.cluster.deployment_api.scan_ssh_host_keys",
            return_value=[{
                "key_type": "ssh-ed25519",
                "key_base64": "AAAA",
                "fingerprint": "SHA256:test",
            }],
        ):
            allowed = self.client.post(
                "/api/cluster/workers/ssh-host-key/scan",
                headers={"Origin": "http://testserver"},
                json=payload,
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["keys"][0]["fingerprint"], "SHA256:test")

    def test_adb_proxy_source_deployment_requires_temporary_admin_elevation(self):
        self._assert_elevation_required(
            "/api/cluster/workers/deploy-adb-proxy-source",
            {
                "ssh_host": "worker@192.0.2.10",
                "password": "secret",
                "controller_url": "https://controller.example",
            },
        )

    def test_local_software_reconfigure_uses_temporary_admin_elevation(self):
        import features.cluster.deployment_api as deployment

        path = "/api/cluster/workers/local/software/reconfigure"
        self._assert_elevation_required(path, {})
        self._elevate()
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="worker-local"),
            repository=SimpleNamespace(
                get_worker=MagicMock(return_value={"running_jobs": 0})
            ),
        )
        thread = MagicMock()
        with deployment._LOCAL_SOFTWARE_LOCK:
            deployment._LOCAL_SOFTWARE_TASKS.clear()
            deployment._LOCAL_SOFTWARE_ACTIVE_TASK = ""
        with (
            patch.object(deployment, "service", return_value=cluster),
            patch.object(
                deployment,
                "_local_worker_has_active_tests",
                return_value=False,
            ),
            patch.object(deployment.threading, "Thread", return_value=thread),
        ):
            allowed = self.client.post(
                path,
                headers={"Origin": "http://testserver"},
                json={},
            )

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.json()["accepted"])
        thread.start.assert_called_once_with()

    def test_worker_delete_uses_temporary_admin_elevation(self):
        path = "/api/cluster/workers/missing-worker"
        denied = self.client.delete(
            path,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])

        self._elevate()
        allowed = self.client.delete(
            path,
            headers={"Origin": "http://testserver"},
        )
        self.assertEqual(allowed.status_code, 404)
        self.assertEqual(allowed.json()["detail"], "worker not found")

    def test_legacy_admin_role_routes_use_temporary_elevation(self):
        denied = self.client.get("/api/auth/roles")
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])

        self._elevate()
        allowed = self.client.get("/api/auth/roles")

        self.assertEqual(allowed.status_code, 200)
        self.assertIn("admin", allowed.json()["roles"])
        self.assertEqual(
            self.client.get("/api/auth/status").json()["user"]["role"],
            "user",
        )

    def test_worker_config_uses_temporary_admin_elevation(self):
        path = "/api/cluster/workers/missing-worker/config"
        denied = self.client.get(path)
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])

        self._elevate()
        allowed = self.client.get(path)

        self.assertEqual(allowed.status_code, 404)
        self.assertEqual(allowed.json()["detail"], "worker not found")


if __name__ == "__main__":
    unittest.main()
