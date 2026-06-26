import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_api_requires_authentication(self):
        response = self.client.get("/api/users/current")

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["auth_required"])
        self.assertTrue(response.json()["setup_required"])

    def test_setup_creates_admin_session_and_header_cannot_spoof_identity(self):
        setup = self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1", "display_name": "Admin"},
        )
        self.assertEqual(setup.status_code, 200)
        setup_payload = setup.json()
        self.assertEqual(setup_payload["user"]["role"], "admin")

        response = self.client.get(
            "/api/users/current",
            headers={"X-Client-Username": "attacker"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["client_id"], setup_payload["user"]["id"])
        self.assertEqual(payload["username"], "admin")

    def test_login_after_setup(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")

        login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["authenticated"])


if __name__ == "__main__":
    unittest.main()
