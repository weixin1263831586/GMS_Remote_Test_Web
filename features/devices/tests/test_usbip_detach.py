"""USB/IP detach confirmation semantics tests.

detach_ubuntu_usbip_ports historically reported a port as detached even when
``sudo usbip detach`` failed, misleading callers into attaching over a port
that was never released.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import usbip


def _fake_ssh_manager(commands: dict[str, tuple[str, str, int]]):
    """Return a usbip_manager-like stub answering fixed command results."""
    manager = MagicMock()
    manager.ssh_manager.execute_command.side_effect = (
        lambda ssh, cmd, timeout=None: commands.get(cmd, ("", "", 0))
    )
    return manager


PORT_LISTING = (
    "List of attached gadgets\n"
    "Port 00: <Port in Use>\n"
    "    1-2 | 05ac:12a8 | Remix Mini | Remote USB/IP host 10.0.0.5\n"
)


class ParseUsbipPortEntriesTests(unittest.TestCase):
    """结构化解析：host/busid 全等匹配，杜绝 substring 误 detach。"""

    MULTI_HOST_LISTING = (
        "List of attached gadgets\n"
        "Port 00: <Port in Use>\n"
        "    1-2 | 05ac:12a8 | Remix Mini | Remote USB/IP host 10.10.10.1\n"
        "Port 01: <Port in Use>\n"
        "    1-21 | 05ac:12a8 | Other Dev | Remote USB/IP host 10.10.10.10\n"
        "Port 02: <Port in Use>\n"
        "    2-1 | 18d1:4ee7 | Nexus | Remote USB/IP host 10.10.10.1\n"
    )

    def test_parses_entries_structurally(self):
        entries = usbip.parse_usbip_port_entries(self.MULTI_HOST_LISTING)
        self.assertEqual(entries, [
            {"port": "00", "busid": "1-2", "host": "10.10.10.1"},
            {"port": "01", "busid": "1-21", "host": "10.10.10.10"},
            {"port": "02", "busid": "2-1", "host": "10.10.10.1"},
        ])

    def test_parses_standard_linux_usbip_url_format(self):
        listing = (
            "Port 03: <Port in Use> at High Speed(480Mbps)\n"
            "       Google Inc. : Pixel (18d1:4ee7)\n"
            "       2-1 -> usbip://10.0.0.5:3240/1-2\n"
            "           -> remote bus/dev 001/002\n"
        )
        self.assertEqual(
            usbip.parse_usbip_port_entries(listing),
            [{"port": "03", "busid": "1-2", "host": "10.0.0.5"}],
        )

    def test_host_prefix_does_not_match_longer_host(self):
        manager = _fake_ssh_manager({
            "usbip port": (self.MULTI_HOST_LISTING, "", 0),
            "sudo usbip detach -p 00": ("", "", 0),
            "sudo usbip detach -p 01": ("", "", 0),
            "sudo usbip detach -p 02": ("", "", 0),
        })
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager), patch(
            "features.devices.usbip.time.sleep"
        ):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.10.10.10"
            )
        # 10.10.10.1 是 10.10.10.10 的前缀但不是同一主机，不能误删。
        self.assertEqual(detached, ["01"])

    def test_busid_prefix_does_not_match_longer_busid(self):
        manager = _fake_ssh_manager({
            "usbip port": (self.MULTI_HOST_LISTING, "", 0),
            "sudo usbip detach -p 00": ("", "", 0),
            "sudo usbip detach -p 02": ("", "", 0),
        })
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager), patch(
            "features.devices.usbip.time.sleep"
        ):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.10.10.1", busids=["1-2"]
            )
        # 1-21 含子串 "1-2"，但 busid 全等匹配时不能命中。
        self.assertEqual(detached, ["00"])

    def test_unparseable_device_lines_are_never_detached_selectively(self):
        listing = (
            "List of attached gadgets\n"
            "Port 03: <Port in Use>\n"
            "    unrecognised future format 10.0.0.5\n"
        )
        manager = _fake_ssh_manager({"usbip port": (listing, "", 0)})
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.0.0.5"
            )
        # 解析不出结构化 host 时宁可漏 detach，也不对未知格式做 substring 猜测。
        self.assertEqual(detached, [])

class DetachUbuntuUsbipPortsTests(unittest.TestCase):
    def test_failed_detach_is_not_reported_as_detached(self):
        manager = _fake_ssh_manager({
            "usbip port": (PORT_LISTING, "", 0),
            "sudo usbip detach -p 00": ("", "usbip: error", 1),
        })
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager), patch(
            "features.devices.usbip.time.sleep"
        ):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.0.0.5"
            )

        self.assertEqual(detached, [])

    def test_successful_detach_is_reported(self):
        manager = _fake_ssh_manager({
            "usbip port": (PORT_LISTING, "", 0),
            "sudo usbip detach -p 00": ("", "", 0),
        })
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager), patch(
            "features.devices.usbip.time.sleep"
        ):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.0.0.5"
            )

        self.assertEqual(detached, ["00"])

    def test_unparsed_port_is_not_mistaken_for_gone_after_failed_detach(self):
        listing = (
            "Port 03: <Port in Use>\n"
            "    future output format\n"
        )
        manager = _fake_ssh_manager({
            "usbip port": (listing, "", 0),
            "sudo usbip detach -p 03": ("", "usbip: error", 1),
        })
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), detach_all=True
            )
        self.assertEqual(detached, [])

    def test_failed_detach_confirmed_gone_still_counts(self):
        """Detach 非零退出但端口确实消失（并发释放）时仍视为成功。"""
        manager = _fake_ssh_manager({
            "usbip port": (PORT_LISTING, "", 0),
            "sudo usbip detach -p 00": ("", "no such port", 1),
        })
        # The re-check listing after the failed detach shows no attached ports.
        listings = iter([
            (PORT_LISTING, "", 0),
            ("List of attached gadgets\n", "", 0),
            ("List of attached gadgets\n", "", 0),
        ])
        manager.ssh_manager.execute_command.side_effect = (
            lambda ssh, cmd, timeout=None: next(listings) if cmd == "usbip port"
            else ("", "no such port", 1)
        )
        with patch.object(usbip.usbip_manager, "ssh_manager", manager.ssh_manager), patch(
            "features.devices.usbip.time.sleep"
        ):
            detached = usbip.detach_ubuntu_usbip_ports(
                MagicMock(), remote_host="10.0.0.5"
            )

        self.assertEqual(detached, ["00"])


if __name__ == "__main__":
    unittest.main()
