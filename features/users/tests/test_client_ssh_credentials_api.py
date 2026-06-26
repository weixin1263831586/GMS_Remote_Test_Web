import asyncio
import json
import unittest


class ClientSshCredentialsApiTests(unittest.TestCase):
    """GET（脱敏）/ POST（upsert）/ DELETE（按 host 过滤）三个端点的最小用例。"""

    def _install_fake(self, runtime_state, saved_sink):
        import features.users.config_api as config_api

        class FakeConfigManager:
            def get_runtime_config(self):
                return runtime_state

            def upsert_device_host_password(self, device_host, password):
                # 仿 foundation/config.py 行为：按 device_host 增改，写回 saved_sink
                username, _, hostname = device_host.partition("@")
                updated = False
                nxt = []
                for cred in saved_sink["list"]:
                    if (cred.get("device_host") == device_host
                            or (cred.get("username") == username and cred.get("host") == hostname)):
                        nxt.append({**cred, "device_host": device_host,
                                    "username": username, "host": hostname, "password": password})
                        updated = True
                    else:
                        nxt.append(cred)
                if not updated:
                    nxt.append({"device_host": device_host, "username": username,
                                "host": hostname, "password": password})
                saved_sink["list"] = nxt
                return True

            def save_client_ssh_credentials(self, credentials):
                saved_sink["list"] = list(credentials)
                return True

        fake = FakeConfigManager()
        self._old = config_api.config_manager
        config_api.config_manager = fake
        return config_api

    def _restore(self, config_api):
        config_api.config_manager = self._old

    def test_get_masks_password_and_keeps_has_password_flag(self):
        runtime = {"client_ssh_credentials": [
            {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "password": "secret"},
            {"device_host": "gms@1.2.3.4", "username": "gms", "host": "1.2.3.4"},
        ]}
        saved = {"list": runtime["client_ssh_credentials"]}
        api = self._install_fake(runtime, saved)
        try:
            resp = asyncio.run(api.list_client_ssh_credentials())
        finally:
            self._restore(api)

        body = json.loads(resp.body.decode("utf-8"))
        creds = body["data"]["credentials"]
        self.assertEqual(len(creds), 2)
        # 密码永不回传明文
        self.assertNotIn("password", creds[0])
        self.assertNotIn("secret", json.dumps(creds))
        self.assertTrue(creds[0]["has_password"])
        self.assertFalse(creds[1]["has_password"])

    def test_post_validates_format_and_persists(self):
        runtime = {"client_ssh_credentials": []}
        saved = {"list": []}
        api = self._install_fake(runtime, saved)
        try:
            # 格式错误被拒
            bad = asyncio.run(api.upsert_client_ssh_credential({"device_host": "noatsign", "password": "p"}))
            self.assertEqual(json.loads(bad.body.decode("utf-8"))["success"], False)
            # HTML/inline handler 片段不能进入持久化配置
            xss = asyncio.run(api.upsert_client_ssh_credential({"device_host": "gms@<img src=x onerror=alert(1)>", "password": "p"}))
            self.assertEqual(json.loads(xss.body.decode("utf-8"))["success"], False)
            # 空密码被拒
            empty = asyncio.run(api.upsert_client_ssh_credential({"device_host": "gms@1.2.3.4", "password": ""}))
            self.assertEqual(json.loads(empty.body.decode("utf-8"))["success"], False)
            # 正常 upsert
            ok = asyncio.run(api.upsert_client_ssh_credential({"device_host": "gms@1.2.3.4", "password": "pw"}))
            self.assertEqual(json.loads(ok.body.decode("utf-8"))["success"], True)
        finally:
            self._restore(api)
        self.assertEqual(len(saved["list"]), 1)
        self.assertEqual(saved["list"][0]["password"], "pw")

    def test_delete_removes_matching_host_only(self):
        runtime = {"client_ssh_credentials": [
            {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "password": "a"},
            {"device_host": "gms@1.2.3.4", "username": "gms", "host": "1.2.3.4", "password": "b"},
        ]}
        saved = {"list": runtime["client_ssh_credentials"]}
        api = self._install_fake(runtime, saved)
        try:
            resp = asyncio.run(api.delete_client_ssh_credential({"device_host": "hcq@172.16.14.66"}))
        finally:
            self._restore(api)
        self.assertEqual(json.loads(resp.body.decode("utf-8"))["success"], True)
        self.assertEqual(len(saved["list"]), 1)
        self.assertEqual(saved["list"][0]["device_host"], "gms@1.2.3.4")


if __name__ == "__main__":
    unittest.main()
