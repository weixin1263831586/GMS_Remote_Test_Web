"""Installing usbipd on a client host is an elevated, host-authorized action."""

import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.devices import usbip_install_api as api


class UsbipInstallAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.manager = MagicMock()
        self.manager.install_usbipd.return_value = {"success": True, "running": True}
        self.app = FastAPI()

        @self.app.middleware("http")
        async def authenticate(request, call_next):
            if self.role:
                request.state.current_user = CurrentUser(
                    id="test", username="test", role=self.role,
                    extra_permissions=frozenset({"devices.lease"}),
                )
                request.state.auth_method = self.method
                request.state.is_elevated = self.elevated
            return await call_next(request)

        self.role = None
        self.method = "session"
        self.elevated = False
        self.app.include_router(api.router)
        self.client = TestClient(self.app)

    def _install(self):
        runtime = SimpleNamespace(
            config_manager=SimpleNamespace(load_config=lambda: {"usbip_device_host": "admin@192.0.2.10"}),
            get_client_id_from_request=lambda _request: "client-a",
            resolve_tailscale_device_host=lambda _request, _client: (None, None),
        )
        with patch.object(api, "runtime", runtime), \
                patch.object(api, "usbip_manager", self.manager), \
                patch.object(api, "find_device_host_password", return_value=""), \
                patch.object(api, "DeviceSSHConnection", lambda _config: contextlib.nullcontext(MagicMock())):
            return self.client.post("/api/usbip/install")

    def test_unauthenticated_or_non_elevated_callers_are_rejected(self):
        import os
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"GMS_AUTH_REQUIRED": "true"}):
            for role, method, elevated, expected in (
                (None, "session", False, 401),
                ("user", "session", False, 403),
                ("admin", "session", False, 403),
                ("user", "agent_token", True, 403),
            ):
                with self.subTest(role=role, method=method, elevated=elevated):
                    self.role, self.method, self.elevated = role, method, elevated
                    self.assertEqual(self._install().status_code, expected)
        self.manager.install_usbipd.assert_not_called()

    def test_elevated_admin_installs_on_the_authorized_host(self):
        self.role, self.method, self.elevated = "admin", "session", True
        response = self._install()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["success"])
        self.assertEqual(self.manager.install_usbipd.call_count, 1)


if __name__ == "__main__":
    unittest.main()
