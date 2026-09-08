"""用户主机本地直连设备清单测试。

覆盖：物理直连设备全量展示（含已共享出去的设备）、TTL 缓存读取
语义、枚举失败的退避缓存，以及 foundation 端口的接线与回退。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
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
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory, "_attached_usbip_serial_map", return_value={},
        ):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertTrue(result["available"])
        # 全部设备展示；无序列号且无 USB/IP 映射时回退显示 BUSID。
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


def _shell_result(stdout="", ok=True):
    return SimpleNamespace(ok=ok, stdout=stdout)


class AttachedUsbipSerialBackfillTests(unittest.TestCase):
    """USB/IP 共享期间的序列号回填（直连设备列显示真实序列号）。"""

    def setUp(self):
        host_inventory._cache.clear()
        host_inventory._inflight.clear()

    def tearDown(self):
        host_inventory._cache.clear()
        host_inventory._inflight.clear()

    def test_backfills_serial_for_device_attached_via_usbip(self):
        manager = _fake_usbip_manager(devices=[
            {"busid": "1-1", "serial": ""},
            {"busid": "2-1", "serial": "S2"},
        ])
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory,
            "_attached_usbip_serial_map",
            return_value={("10.0.0.5", "1-1"): "RK3562GMS7"},
        ):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertEqual(result["devices"], ["RK3562GMS7", "S2"])

    def test_unique_busid_backfills_even_with_unknown_attach_host(self):
        # usbip port 报告的 host 是 attach 侧地址（可能是 Tailscale IP）。
        # BUSID 只在来源主机内唯一：映射键的主机无法确认等于本来源
        # 主机时，不允许把其他来源主机的序列号回填到本主机（回归修复），
        # 保留 BUSID 展示。
        manager = _fake_usbip_manager(devices=[{"busid": "1-1", "serial": ""}])
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory,
            "_attached_usbip_serial_map",
            return_value={("100.82.1.32", "1-1"): "c3d9b8674f4b94f6"},
        ):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertEqual(result["devices"], ["1-1"])

    def test_same_host_busid_backfills_serial(self):
        # (来源主机, BUSID) 精确同主机命中时仍回填序列号。
        manager = _fake_usbip_manager(devices=[{"busid": "1-1", "serial": ""}])
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory,
            "_attached_usbip_serial_map",
            return_value={("10.0.0.5", "1-1"): "c3d9b8674f4b94f6"},
        ):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertEqual(result["devices"], ["c3d9b8674f4b94f6"])

    def test_ambiguous_busid_keeps_busid_display(self):
        # 来源主机不在映射里（如 attach 走 Tailscale 地址对不上），且
        # 不同来源主机同 BUSID 序列号不唯一：宁显 BUSID 不错配。
        manager = _fake_usbip_manager(devices=[{"busid": "1-1", "serial": ""}])
        manager.config_manager.load_config.return_value = {}
        with patch.object(host_inventory, "usbip_manager", manager), patch.object(
            host_inventory,
            "_attached_usbip_serial_map",
            return_value={
                ("100.82.1.32", "1-1"): "S1",
                ("10.0.0.6", "1-1"): "S2",
            },
        ):
            host_inventory._refresh("hcq@10.0.0.5")
            result = host_inventory.host_local_device_inventory("hcq@10.0.0.5")
        self.assertEqual(result["devices"], ["1-1"])


class AttachedUsbipSerialMapTests(unittest.TestCase):
    """(来源主机, 来源BUSID) -> 序列号 映射的构建。"""

    def _run(self, outputs):
        def fake_run(command, timeout=10):
            return outputs.pop(0)
        return fake_run

    def test_url_format_uses_local_busid_directly(self):
        outputs = [
            _shell_result(
                "Port 00: <Port in Use>\n"
                "  3-1 -> usbip://10.0.0.5:3240/1-1\n"
            ),
            _shell_result("3-1 c3d9b8674f4b94f6\n"),
        ]
        with patch.object(
            host_inventory, "_run_on_test_host", self._run(outputs),
        ):
            mapping = host_inventory._attached_usbip_serial_map()
        self.assertEqual(mapping, {("10.0.0.5", "1-1"): "c3d9b8674f4b94f6"})

    def test_pipe_format_joins_via_vhci_status_port(self):
        outputs = [
            _shell_result(
                "Port 00: <Port in Use>\n"
                "    1-1 | 2207:0006 | SSI 17 | Remote USB/IP host 10.0.0.5\n"
            ),
            _shell_result("3-1 c3d9b8674f4b94f6\n"),
            _shell_result(
                "hub port sta spd dev      sockfd local_busid\n"
                "hs  0000 008 480 00000002 7      3-1\n"
            ),
        ]
        with patch.object(
            host_inventory, "_run_on_test_host", self._run(outputs),
        ):
            mapping = host_inventory._attached_usbip_serial_map()
        self.assertEqual(mapping, {("10.0.0.5", "1-1"): "c3d9b8674f4b94f6"})

    def test_single_entry_fallback_when_join_fails(self):
        # 管道格式没有 local_busid 且 status 不可读时无法关联：不再做
        # “唯一导入+唯一序列号”兜底（可能把其他主机的 serial 套进来），
        # 返回部分成功关联的映射，未关联条目保持未知。
        outputs = [
            _shell_result(
                "Port 00: <Port in Use>\n"
                "    1-1 | 2207:0006 | SSI 17 | Remote USB/IP host 10.0.0.5\n"
            ),
            _shell_result("3-1 c3d9b8674f4b94f6\n"),
            _shell_result(""),  # status 不可读：无法由 port 关联 local_busid
        ]
        with patch.object(
            host_inventory, "_run_on_test_host", self._run(outputs),
        ):
            mapping = host_inventory._attached_usbip_serial_map()
        self.assertEqual(mapping, {})

    def test_port_command_failure_returns_empty(self):
        with patch.object(
            host_inventory, "_run_on_test_host",
            self._run([_shell_result(ok=False)]),
        ):
            self.assertEqual(host_inventory._attached_usbip_serial_map(), {})

    def test_exception_returns_empty(self):
        def boom(command, timeout=10):
            raise RuntimeError("ssh down")
        with patch.object(host_inventory, "_run_on_test_host", boom):
            self.assertEqual(host_inventory._attached_usbip_serial_map(), {})


if __name__ == "__main__":
    unittest.main()
