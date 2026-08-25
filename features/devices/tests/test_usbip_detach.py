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
