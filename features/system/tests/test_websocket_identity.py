import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.system import api


class WebSocketIdentityTests(unittest.TestCase):
    def test_authenticated_identity_uses_username_runtime_key(self):
        websocket = SimpleNamespace(
            cookies={"gms_auth_token": "token"},
            headers={},
            client=SimpleNamespace(host="172.16.14.66"),
        )
        user = SimpleNamespace(id="opaque-user-id", username="hcq")

        with patch.object(api.auth_service, "get_user_for_token", return_value=user):
            client_id, display_id, username = api._get_websocket_client_identity(websocket, "hcq")

        self.assertEqual(client_id, "hcq")
        self.assertEqual(display_id, "hcq@172.16.14.66")
        self.assertEqual(username, "hcq")


if __name__ == "__main__":
    unittest.main()
