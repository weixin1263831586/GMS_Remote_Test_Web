import unittest
from unittest.mock import patch

from features.system.vnc import (
    NOVNC_WEB_PORT,
    VNC_PORT,
    VNCManager,
    novnc_url,
    vnc_password_temp_path,
)
from foundation.processes import command_reports_running


class VNCManagerTests(unittest.TestCase):
    def test_command_reports_running_requires_exact_line(self):
        self.assertTrue(command_reports_running("123\nRUNNING\n"))
        self.assertFalse(command_reports_running("NOT_RUNNING\n"))

    def test_vnc_helpers_keep_default_ports_centralized(self):
        self.assertEqual(novnc_url("192.168.0.2"), "http://192.168.0.2:6080/vnc.html?autoconnect=true&resize=scale")
        self.assertEqual(novnc_url("192.168.0.2", autoconnect=False), "http://192.168.0.2:6080/vnc.html?resize=scale")
        self.assertEqual(novnc_url("192.168.0.2", resize='off'), "http://192.168.0.2:6080/vnc.html?autoconnect=true&resize=off")
        self.assertEqual(VNC_PORT, 5900)
        self.assertEqual(NOVNC_WEB_PORT, 6080)
        self.assertRegex(vnc_password_temp_path(), r"^/tmp/\.gms_vnc_passwd_[0-9a-f]{32}$")

    def test_local_websockify_command_uses_centralized_ports(self):
        with patch.object(VNCManager, "_websockify_standalone", "/usr/bin/websockify"):
            self.assertEqual(
                VNCManager._build_local_websockify_cmd("/opt/noVNC"),
                ["/usr/bin/websockify", "--web=/opt/noVNC", "6080", "localhost:5900"],
            )

    def test_remote_vnc_commands_quote_user_and_use_scoped_patterns(self):
        commands = []
        connection_configs = []

        class FakeSshManager:
            def get_connection(self, config):
                connection_configs.append(config)
                return object()

            def return_connection(self, _ssh):
                commands.append("__returned__")

            def execute_command(self, _ssh, command, timeout=None):
                commands.append(command)
                if "xprop -root" in command:
                    return "ready\n", "", 0
                if "pgrep" in command:
                    return "NOT_RUNNING\n", "", 1
                if "ss -ltn" in command:
                    return "VNC_READY\nNOVNC_READY\n", "", 0
                return "exists\n", "", 0

        manager = VNCManager()
        manager.ssh_manager = FakeSshManager()

        with patch("features.system.vnc.time.sleep"):
            result = manager._start_remote_vnc(
                "user@192.168.0.2",
                password="",
                vnc_password="",
                config={"ubuntu_user": "test user"},
            )

        self.assertTrue(result["success"])
        joined = "\n".join(commands)
        self.assertIn("pgrep -f -- 'x11vnc.*:0'", joined)
        self.assertIn("pgrep -f -- 'websockify.*6080'", joined)
        self.assertIn("export XAUTHORITY=/home/'test user'/.Xauthority", joined)
        self.assertIn("x11vnc -display :0 -forever -shared -rfbport 5900", joined)
        self.assertIn("./utils/websockify/run --web /opt/noVNC 6080 localhost:5900", joined)
        self.assertIn("cd /opt/noVNC", joined)
        self.assertIn("mkdir -p ~/logs ~/.vnc", joined)
        self.assertIn("__returned__", commands)

    def test_start_vnc_uses_selected_remote_host_credentials(self):
        manager = VNCManager()
        with patch.object(manager.config_manager, "load_config", return_value={"ubuntu_host": "10.0.0.1", "ubuntu_user": "default"}), \
             patch.object(manager, "_start_remote_vnc", return_value={"success": True}) as start_remote:
            result = manager.start_vnc("wlq@172.16.14.244", "secret", "")

        self.assertTrue(result["success"])
        remote_config = start_remote.call_args.args[3]
        self.assertEqual(remote_config["hostname"], "172.16.14.244")
        self.assertEqual(remote_config["username"], "wlq")
        self.assertEqual(remote_config["password"], "secret")


if __name__ == "__main__":
    unittest.main()
