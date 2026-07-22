from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.users import config_api, navigation_preferences


class NavigationPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_root_patch = patch.object(
            navigation_preferences.runtime,
            "data_root",
            Path(self.tmp.name),
        )
        self.data_root_patch.start()
        app = FastAPI()

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "alice")
            request.state.current_user = CurrentUser(
                id=f"id-{username}",
                username=username,
                role="user",
            )
            return await call_next(request)

        app.include_router(config_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.data_root_patch.stop()
        self.tmp.cleanup()

    def test_sidebar_order_is_isolated_per_authenticated_user(self):
        alice_saved = self.client.post(
            "/api/sidebar-order",
            json={"order": ["reports", "test"], "visible_pages": ["reports"]},
        )
        bob_initial = self.client.get(
            "/api/sidebar-order",
            headers={"X-Test-User": "bob"},
        )
        bob_saved = self.client.post(
            "/api/sidebar-order",
            headers={"X-Test-User": "bob"},
            json={"order": ["devices", "test"], "visible_pages": ["devices"]},
        )
        alice_reloaded = self.client.get("/api/sidebar-order")

        self.assertEqual(alice_saved.status_code, 200)
        self.assertEqual(bob_initial.json()["data"]["order"], [])
        self.assertEqual(bob_saved.json()["data"]["order"], ["devices", "test"])
        self.assertEqual(
            alice_reloaded.json()["data"],
            {"order": ["reports", "test"], "visible_pages": ["reports"]},
        )

    def test_config_read_never_returns_wifi_password(self):
        class FakeConfigManager:
            @staticmethod
            def load_config():
                return {
                    "wifi": {"ssid": "Enterprise Lab", "password": "top-secret"},
                    "client_hosts": {},
                }

            @staticmethod
            def get_runtime_config():
                return {}

            @staticmethod
            def get_ubuntu_user(_config):
                return "tester"

        with patch.object(config_api, "config_manager", FakeConfigManager()), patch.object(
            config_api.runtime,
            "get_or_create_user_state",
            return_value={},
        ):
            response = self.client.get("/api/config/read")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["wifi"]["password"], "")
        self.assertTrue(response.json()["wifi"]["has_password"])
        self.assertEqual(response.json()["effective_ubuntu_user"], "tester")
        self.assertEqual(response.json()["effective_suites_path"], "/home/tester/GMS-Suite")


if __name__ == "__main__":
    unittest.main()
