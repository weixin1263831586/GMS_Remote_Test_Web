import unittest

from features.system.network import has_active_vpn_connection


class NetworkTests(unittest.TestCase):
    def test_active_vpn_requires_vpn_type_column(self):
        output = "Wired connection 1:ethernet:activated\nvpn-like-name:ethernet:activated\n"

        self.assertFalse(has_active_vpn_connection(output))

    def test_active_vpn_detects_real_vpn_type(self):
        output = "corp-vpn:vpn:activated\nWired connection 1:ethernet:activated\n"

        self.assertTrue(has_active_vpn_connection(output))


if __name__ == "__main__":
    unittest.main()
