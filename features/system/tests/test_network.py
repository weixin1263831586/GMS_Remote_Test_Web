import unittest

from features.system.network import (
    _generate_route_commands,
    has_active_vpn_connection,
    parse_vpn_connection_names,
)


class NetworkTests(unittest.TestCase):
    def test_active_vpn_requires_vpn_type_column(self):
        output = "Wired connection 1:ethernet:activated\nvpn-like-name:ethernet:activated\n"

        self.assertFalse(has_active_vpn_connection(output))

    def test_active_vpn_detects_real_vpn_type(self):
        output = "corp-vpn:vpn:activated\nWired connection 1:ethernet:activated\n"

        self.assertTrue(has_active_vpn_connection(output))

    def test_active_vpn_ignores_tailscale_tunnel(self):
        output = "tailscale0:tun:activated\nWired connection 1:ethernet:activated\n"

        self.assertFalse(has_active_vpn_connection(output))
        self.assertEqual(parse_vpn_connection_names(output), [])

    def test_vpn_connection_name_preserves_escaped_colon(self):
        output = r"corp\:vpn:vpn:activated"

        self.assertTrue(has_active_vpn_connection(output))
        self.assertEqual(parse_vpn_connection_names(output), ["corp:vpn"])

    def test_non_vpn_connection_name_with_escaped_vpn_text_is_not_active_vpn(self):
        output = r"corp\:vpn:ethernet:activated"

        self.assertFalse(has_active_vpn_connection(output))

    def test_route_gateway_uses_last_octet_one(self):
        commands = _generate_route_commands("192.168.14.0", "192.168.20.0", "192.168.14.9")

        self.assertIn("via 192.168.14.1", commands["linux"][2])


if __name__ == "__main__":
    unittest.main()
