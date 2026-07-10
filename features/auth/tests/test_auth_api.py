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

    def test_current_user_allows_anonymous_client_identity(self):
        response = self.client.get("/api/users/current")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsNone(payload["user"])
        self.assertTrue(payload["client_id"])

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
        self.assertEqual(payload["client_id"], "admin")
        self.assertNotEqual(payload["client_id"], "attacker")
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

    def _setup_admin_and_user(self):
        """Create an admin (via setup) + a normal user, return session cookies."""
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        # Create a normal user directly in the store, then log it in.
        auth_service.create_user("alice", "alicepass1", role="user")
        admin_cookie = self.client.cookies.get("gms_session")
        # Log out admin, log in alice to grab her cookie.
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "alice", "password": "alicepass1"})
        user_cookie = self.client.cookies.get("gms_session")
        return admin_cookie, user_cookie

    def _set_session(self, cookie_value):
        if cookie_value:
            self.client.cookies.set("gms_session", cookie_value)
        else:
            self.client.cookies.clear()

    def test_admin_login_is_elevated_for_whole_session(self):
        # Admin created via setup is immediately elevated (no re-prompt needed).
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertTrue(self.client.get("/api/auth/status").json()["elevated"])

        # A sensitive endpoint does NOT return 403-elevation for a just-logged-in
        # admin (it may 404 for a non-existent ip, but NOT 403-elevation).
        resp = self.client.request("DELETE", "/api/users/remove", json={"ip": "1.2.3.4"})
        self.assertNotEqual(
            (resp.status_code, resp.json().get("detail", {}).get("elevation_required")),
            (403, True),
        )

    def test_admin_relogin_stays_elevated(self):
        # Setup creates + logs in the admin (elevated), then we log out and back
        # in via /login — elevation must be re-granted on the new session.
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"username": "admin", "password": "strongpass1"})
        self.assertTrue(self.client.get("/api/auth/status").json()["elevated"])

    def test_normal_user_not_elevated_and_blocked_from_sensitive_endpoint(self):
        # The elevation-on-login only applies to admins; a normal user stays
        # non-elevated and the sensitive endpoint still demands elevation.
        _admin_cookie, user_cookie = self._setup_admin_and_user()
        self._set_session(user_cookie)

        self.assertFalse(self.client.get("/api/auth/status").json()["elevated"])
        resp = self.client.request("DELETE", "/api/users/remove", json={"ip": "1.2.3.4"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()["detail"]["elevation_required"])

    def test_elevation_rejects_non_admin(self):
        _admin_cookie, user_cookie = self._setup_admin_and_user()
        self._set_session(user_cookie)
        # A normal user cannot elevate even with their own correct password.
        elev = self.client.post(
            "/api/auth/elevate",
            json={"username": "alice", "password": "alicepass1"},
        )
        self.assertEqual(elev.status_code, 403)
        self.assertFalse(elev.json().get("elevated", False))

    def test_elevation_can_start_from_anonymous_session(self):
        self.client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "strongpass1"},
        )
        self.client.post("/api/auth/logout")

        elev = self.client.post(
            "/api/auth/elevate",
            json={"username": "admin", "password": "strongpass1"},
        )

        self.assertEqual(elev.status_code, 200)
        self.assertTrue(elev.json()["elevated"])
        self.assertTrue(self.client.cookies.get("gms_session"))


if __name__ == "__main__":
    unittest.main()
