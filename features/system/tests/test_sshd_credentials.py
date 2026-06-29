import asyncio
import json
import unittest


class SshdCredentialTests(unittest.TestCase):
    def test_check_sshd_requests_password_when_no_host_or_static_password(self):
        import features.system.integrations as integrations

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_host": "hcq@172.16.14.66",
                    "device_pswd": "",
                    "client_ssh_credentials": [],
                }

            def find_device_host_password(self, device_host, config):
                return None

        old_config_manager = integrations.config_manager
        integrations.config_manager = FakeConfigManager()
        try:
            response = asyncio.run(
                integrations.check_ssh_sshd(request=None, device_host="hcq@172.16.14.66")
            )
        finally:
            integrations.config_manager = old_config_manager

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(body["success"])
        self.assertTrue(body["need_password"])
        self.assertEqual(body["device_host"], "hcq@172.16.14.66")

    def test_check_sshd_uses_static_device_password_when_host_credential_missing(self):
        import features.system.integrations as integrations

        calls = {}

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_host": "hcq@172.16.14.66",
                    "device_pswd": "rockchip",
                    "client_ssh_credentials": [],
                }

            def find_device_host_password(self, device_host, config):
                calls["lookup"] = (device_host, dict(config))
                return None

        class FakeStdout:
            def __init__(self, text):
                self._text = text

            def read(self):
                return self._text.encode("utf-8")

        class FakeSsh:
            def exec_command(self, cmd, timeout=10):
                calls.setdefault("commands", []).append(cmd)
                if "where sshd.exe" in cmd:
                    return None, FakeStdout("C:\\Windows\\System32\\OpenSSH\\sshd.exe"), None
                if "RUNNING" in cmd:
                    return None, FakeStdout("RUNNING"), None
                return None, FakeStdout(""), None

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                calls["device_password"] = config["device_pswd"]

            def __enter__(self):
                return FakeSsh()

            def __exit__(self, exc_type, exc, tb):
                return False

        old_config_manager = integrations.config_manager
        old_device_ssh = integrations.DeviceSSHConnection
        integrations.config_manager = FakeConfigManager()
        integrations.DeviceSSHConnection = FakeDeviceSSHConnection
        try:
            response = asyncio.run(
                integrations.check_ssh_sshd(request=None, device_host="hcq@172.16.14.66")
            )
        finally:
            integrations.config_manager = old_config_manager
            integrations.DeviceSSHConnection = old_device_ssh

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertTrue(body["installed"])
        self.assertTrue(body["running"])
        self.assertEqual(calls["device_password"], "rockchip")


if __name__ == "__main__":
    unittest.main()
