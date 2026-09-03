"""用户主机本地直连设备清单测试。

覆盖：物理直连设备全量展示（含已共享出去的设备）、TTL 缓存读取
语义、枚举失败的退避缓存，以及 foundation 端口的接线与回退。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import host_inventory
from foundation import devices_port


def _fake_usbip_manager(devices=None, error=""):
    manager = MagicMock()
    if error:
        manager.list_source_devices.return_value = {
            "success": False, "error": error,
        }
    else:
        manager.list_source_devices.return_value = {
            "success": True,
            "source_os": "windows",
            "devices": devices or [],
        }
    manager.config_manager.get_runtime_config.return_value = {}
    return manager


class HostLocalInventoryTests(unittest.TestCase):
    def setUp(self):
        host_inventory._cache.clear()
        host_inventory._inflight.clear()
        devices_port.configure_host_inventory_provider(None)

    def tearDown(self):
        host_inventory._cache.clear()
        host_inventory._inflight.clear()
        devices_port.configure_host_inventory_provider(None)

    def test_invalid_or_loopback_host_returns_none(self):
        self.assertIsNone(host_inventory.host_local_device_inventory(""))
        self.assertIsNone(host_inventory.host_local_device_inventory("10.0.0.5"))
        self.assertIsNone(
            host_inventory.host_local_device_inventory("hcq@127.0.0.1")
        )

    def test_missing_cache_returns_none_without_blocking(self):
        manager = _fake_usbip_manager()
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory.threading, "Thread",
        ) as thread_cls:
            thread_cls.return_value = MagicMock()
            self.assertIsNone(
                host_inventory.host_local_device_inventory("hcq@10.0.0.5")
            )
            # 首次读取只触发一次后台刷新，不同步执行 SSH 枚举。
            thread_cls.assert_called_once()
            thread_cls.return_value.start.assert_called_once()
            manager.list_source_devices.assert_not_called()

    def test_lists_all_physically_attached_devices(self):
        """直连设备全量展示：USB/IP / ADB Proxy 已共享的设备仍接在
        主机上，物理直连计数包含它们，不排除。"""
        manager = _fake_usbip_manager(devices=[
            {"busid": "1-2", "serial": "S1"},
            {"busid": "2-1", "serial": "S2"},
            {"busid": "3-1", "serial": ""},
        ])
        with patch.object(host_inventory, "usbip_manager", manager):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertTrue(result["available"])
        # 全部设备展示；无序列号设备回退显示 BUSID。
        self.assertEqual(result["devices"], ["S1", "S2", "3-1"])
        self.assertEqual(result["source_os"], "windows")

    def test_enumeration_failure_is_cached_with_backoff(self):
        manager = _fake_usbip_manager(error="未找到 hcq@10.0.0.5 的SSH凭据")
        with patch.object(host_inventory, "usbip_manager", manager):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertFalse(result["available"])
        self.assertEqual(result["devices"], [])
        self.assertIn("SSH凭据", result["error"])
        # 失败缓存未过期时再次读取不再触发刷新。
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory.threading, "Thread",
        ) as thread_cls:
            thread_cls.return_value = MagicMock()
            again = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
            self.assertFalse(again["available"])
            thread_cls.assert_not_called()

    def test_port_registration_and_unwired_fallback(self):
        self.assertIsNone(devices_port.host_local_device_inventory("hcq@10.0.0.5"))
        host_inventory.register_devices_port()
        self.assertIsNone(devices_port.host_local_device_inventory("10.0.0.5"))


if __name__ == "__main__":
    unittest.main()
