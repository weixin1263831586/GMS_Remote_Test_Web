from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service


class EmailApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        auth_service.db_path = Path(self.tmp.name) / "platform_auth.sqlite3"
        auth_service._initialized = False
        self.client = TestClient(create_app())

    def tearDown(self):
        self.tmp.cleanup()

    def _login(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1", "display_name": "Admin"},
        )

    def test_send_email_requires_authentication(self):
        response = self.client.post(
            "/api/email/send",
            json={"to": "dev@example.com", "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 401)

    @patch("features.email.api.send_email")
    def test_authenticated_send_invokes_service(self, send_email_mock):
        self._login()
        send_email_mock.return_value = {
            "sent": True,
            "mode": "smtp",
            "to": ["dev@example.com"],
            "cc": [],
            "recipients": ["dev@example.com"],
        }

        response = self.client.post(
            "/api/email/send",
            json={"to": "dev@example.com", "subject": "s", "body": "b"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        send_email_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
