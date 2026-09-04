import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from features.system.models import VPNConnectRequest
from foundation.command_result import CommandResult


class VpnIntegrationTests(unittest.TestCase):
    def _connect(
        self,
        active_output: str,
        activation_result=CommandResult(
            stdout="Connection successfully activated", stderr="", code=0,
        ),
    ):
        import features.system.integrations as integrations

        class FakeConfigManager:
            def load_config(self):
                return {}

            def is_config_host_local(self, config):
                return True

            def get_ubuntu_user(self, config):
                return "gms-user"

        commands = AsyncMock(side_effect=[
            activation_result,
            CommandResult(stdout=active_output, stderr="", code=0),
        ])
        with patch.object(integrations, "config_manager", FakeConfigManager()), patch.object(
            integrations,
            "execute_config_host_command",
            commands,
        ), patch.object(integrations.asyncio, "sleep", AsyncMock()):
            response = asyncio.run(
                integrations.connect_vpn(VPNConnectRequest(vpn_name="corp-vpn"))
            )
        return json.loads(response.body.decode("utf-8")), commands

    def test_connect_does_not_treat_tailscale_tunnel_as_selected_vpn(self):
        body, commands = self._connect("tailscale0:tun:activated\n")

        self.assertFalse(body["success"])
        self.assertFalse(body["connected"])
        self.assertIn("corp-vpn 未出现在活动 VPN 列表中", body["message"])
        self.assertIn("connection show --active", commands.await_args_list[1].args[2])

    def test_connect_confirms_selected_vpn_is_active(self):
        body, _ = self._connect(
            "other-vpn:vpn:activated\ncorp-vpn:vpn:activated\n"
        )

        self.assertTrue(body["success"])
        self.assertTrue(body["connected"])
        self.assertEqual(body["vpn_connection_name"], "corp-vpn")

    def test_connect_permission_error_includes_policy_setup_command(self):
        body, _ = self._connect(
            "",
            activation_result=CommandResult(
                stdout="",
                stderr="Not authorized to control networking",
                code=4,
            ),
        )

        self.assertFalse(body["success"])
        self.assertIn("install_networkmanager_policy.sh gms-user", body["message"])


if __name__ == "__main__":
    unittest.main()
