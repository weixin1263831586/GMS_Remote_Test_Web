import unittest
from unittest.mock import MagicMock, patch

from features.system.vnc import (
    NOVNC_WEB_PORT,
    VNC_PORT,
    VNCManager,
    novnc_url,
    vnc_password_temp_path,
)
from foundation.command_result import CommandResult
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
                [
                    "/usr/bin/websockify",
                    "--web=/opt/noVNC",
                    "127.0.0.1:6080",
                    "localhost:5900",
                ],
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
                    return CommandResult(stdout="ready\n", stderr="", code=0)
                if "pgrep" in command:
                    return CommandResult(stdout="NOT_RUNNING\n", stderr="", code=1)
                if "ss -ltn" in command:
                    return CommandResult(stdout="VNC_READY\nNOVNC_READY\n", stderr="", code=0)
                return CommandResult(stdout="exists\n", stderr="", code=0)

        manager = VNCManager()
        manager.ssh_manager = FakeSshManager()

        with patch("features.system.vnc_remote.time.sleep"):
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
        self.assertIn("-threads -noxdamage -wait 5 -defer 5", joined)
        self.assertIn("-noshm", joined)
        self.assertIn("-repeat", joined)
        self.assertIn("xset -display :0 r on", joined)
        self.assertIn("-clear_mods -skip_lockkeys", joined)
        self.assertNotIn("-clear_all", joined)
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

    def test_local_vnc_status_does_not_open_an_ssh_connection(self):
        manager = VNCManager()
        with patch.object(
            manager.config_manager,
            "load_config",
            return_value={"ubuntu_host": "172.16.14.233"},
        ), patch(
            "features.system.vnc.is_local_host",
            return_value=True,
        ), patch.object(
            manager,
            "_is_local_process_running",
            side_effect=lambda pattern: pattern in {
                "x11vnc.*:0",
                "websockify.*6080",
            },
        ), patch.object(
            manager,
            "_is_local_port_listening",
            return_value=True,
        ), patch.object(
            manager,
            "_is_local_rfb_healthy",
            return_value=True,
        ), patch.object(
            manager.ssh_manager,
            "get_connection",
        ) as get_connection:
            result = manager.get_vnc_status()

        self.assertTrue(result["running"])
        self.assertTrue(result["local"])
        self.assertEqual(result["vnc_count"], 1)
        self.assertTrue(result["port_listening"])
        get_connection.assert_not_called()

    def test_local_vnc_status_requires_the_vnc_port_to_listen(self):
        manager = VNCManager()
        with patch.object(
            manager.config_manager,
            "load_config",
            return_value={"ubuntu_host": "172.16.14.233"},
        ), patch(
            "features.system.vnc.is_local_host",
            return_value=True,
        ), patch.object(
            manager,
            "_is_local_process_running",
            return_value=True,
        ), patch.object(
            manager,
            "_is_local_port_listening",
            side_effect=lambda port: port == NOVNC_WEB_PORT,
        ), patch.object(
            manager,
            "_is_local_rfb_healthy",
            return_value=False,
        ):
            result = manager.get_vnc_status()

        self.assertFalse(result["running"])
        self.assertEqual(result["vnc_count"], 0)
        self.assertTrue(result["port_listening"])

    def test_local_rfb_health_requires_rfb_greeting(self):
        connection = MagicMock()
        connection.recv.return_value = b"RFB 003.008\n"
        connection.__enter__.return_value = connection
        with patch("features.system.vnc.socket.create_connection", return_value=connection):
            self.assertTrue(VNCManager._is_local_rfb_healthy())

        connection.recv.return_value = b""
        with patch("features.system.vnc.socket.create_connection", return_value=connection):
            self.assertFalse(VNCManager._is_local_rfb_healthy())

    def test_local_rfb_health_accepts_fragmented_greeting(self):
        connection = MagicMock()
        connection.recv.side_effect = [b"RF", b"B 003.", b"008\n"]
        connection.__enter__.return_value = connection

        with patch("features.system.vnc.socket.create_connection", return_value=connection):
            self.assertTrue(VNCManager._is_local_rfb_healthy())

    def test_local_start_restarts_stuck_x11vnc_before_relaunching(self):
        manager = VNCManager()
        with patch.object(manager, "_find_local_novnc_web_dir", return_value="/opt/noVNC"), \
             patch("features.system.vnc.shutil.which", return_value="/usr/bin/x11vnc"), \
             patch.object(manager, "_has_local_websockify", return_value=True), \
             patch.object(manager, "_is_local_process_running", return_value=True), \
             patch.object(manager, "_is_local_port_listening", return_value=True), \
             patch.object(manager, "_is_local_rfb_healthy", side_effect=[False, True]), \
             patch.object(manager, "_kill_local_processes") as kill_processes, \
             patch("features.system.vnc.subprocess.run"), \
             patch("features.system.vnc.start_detached_process"), \
             patch("features.system.vnc.time.sleep"):
            result = manager._start_local_vnc()

        self.assertTrue(result["success"])
        kill_processes.assert_called_once_with("x11vnc.*:0", force=True)

    def test_local_start_restarts_stuck_x11vnc_when_its_socket_backlog_is_full(self):
        manager = VNCManager()
        with patch.object(manager, "_find_local_novnc_web_dir", return_value="/opt/noVNC"), \
             patch("features.system.vnc.shutil.which", return_value="/usr/bin/x11vnc"), \
             patch.object(manager, "_has_local_websockify", return_value=True), \
             patch.object(manager, "_is_local_process_running", return_value=True), \
             patch.object(manager, "_is_local_port_listening", side_effect=[False, True, True, True]), \
             patch.object(manager, "_is_local_rfb_healthy", side_effect=[True]), \
             patch.object(manager, "_kill_local_processes") as kill_processes, \
             patch("features.system.vnc.subprocess.run"), \
             patch("features.system.vnc.start_detached_process"), \
             patch("features.system.vnc.time.sleep"):
            result = manager._start_local_vnc()

        self.assertTrue(result["success"])
        kill_processes.assert_called_once_with("x11vnc.*:0", force=True)


if __name__ == "__main__":
    unittest.main()
