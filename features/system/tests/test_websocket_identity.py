import unittest
from types import SimpleNamespace

from features.system import websocket_security


class WebSocketIdentityTests(unittest.TestCase):
    def test_authenticated_identity_uses_immutable_user_id_runtime_key(self):
        websocket = SimpleNamespace(
            cookies={"gms_auth_token": "token"},
            headers={},
            client=SimpleNamespace(host="172.16.14.66"),
        )
        user = SimpleNamespace(id="opaque-user-id", username="hcq")

        client_id, display_id, username = websocket_security.get_websocket_client_identity(
            websocket,
            "hcq",
            user,
        )

        self.assertEqual(client_id, "opaque-user-id")
        self.assertEqual(display_id, "hcq@172.16.14.66")
        self.assertEqual(username, "hcq")

    def test_terminal_runtime_key_is_namespaced_by_authenticated_user(self):
        websocket = SimpleNamespace(
            cookies={"gms_session": "token"},
            headers={},
            client=SimpleNamespace(host="172.16.14.66"),
        )
        user = SimpleNamespace(id="opaque-user-id", username="hcq")

        client_id, _display_id, _username = websocket_security.get_websocket_client_identity(
            websocket,
            "terminal_shared",
            user,
        )

        self.assertEqual(client_id, "opaque-user-id:terminal_shared")


if __name__ == "__main__":
    unittest.main()
