from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import api as devices_api
from features.devices.manager import DeviceManager
from features.devices.rockusb import (
    parse_rockusb_probe_output,
    rockusb_loader_serials,
    rockusb_loader_vid_pids,
)


def _request(username: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/devices/list",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    request.state.current_user = CurrentUser(
        id=f"id-{username}",
        username=username,
        role="user",
    )
    return request


PROBE_OUTPUT = (
    "0006\tRK3562GMS7\tSSI 17 on ARM64\n"
    "350d\tLOADER-ONLY\tUSB download gadget\n"
    "011d\tIN-ADB\tAndroid Device\n"
    "\tNO-SERIAL\tUSB download gadget\n"
    "garbage-line\n"
)


class RockusbProbeParsingTests(unittest.TestCase):
    def test_parse_skips_malformed_and_serial_less_entries(self):
        entries = parse_rockusb_probe_output(PROBE_OUTPUT)
        self.assertEqual(
            [entry["serial"] for entry in entries],
            ["RK3562GMS7", "LOADER-ONLY", "IN-ADB"],
        )
        self.assertEqual(entries[0]["pid"], "0006")

    def test_loader_serials_exclude_any_adb_or_fastboot_state(self):
        serials = rockusb_loader_serials(
            PROBE_OUTPUT,
            exclude_serials={"IN-ADB", "LOADER-ONLY"},
        )
        self.assertEqual(serials, [])

    def test_loader_serials_deduplicate(self):
        probe = (
            "350f\tRK3562GMS1\tUSB download gadget\n"
            "350f\tRK3562GMS1\tUSB download gadget\n"
        )
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["RK3562GMS1"],
        )

    def test_non_burn_mode_pid_is_never_a_loader(self):
        # 现场案例（2026-09-07）：c3d9b8674f4b94f6 以 2207:0007 健康枚举，
        # 永远不出现在 adb 枚举中；只按 VID 判定会把它永久误报成 Loader。
        probe = (
            "0006\tRK3562GMS7\tSSI 17 on ARM64\n"
            "0007\tc3d9b8674f4b94f6\tSSI 17 Go on ARM64\n"
            "0006\tPNERK35720001\tP10_R\n"
            "351a\tLOADER-DEV\tUSB download gadget\n"
        )
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["LOADER-DEV"],
        )

    def test_rk3576_loader_pid_is_recognized(self):
        # 现场案例（2026-09-15）：RK3576GMS1 烧写时进入 Loader 枚举为
        # 2207:350e / "USB download gadget"；默认 PID 清单此前只有
        # RK3572 的 351a，导致 wait_for_single_rockusb_loader 超时误报
        # "未能确认目标设备是唯一 Loader"。
        probe = "350e\tRK3576GMS1\tUSB download gadget\n"
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["RK3576GMS1"],
        )
        self.assertIn("350e", rockusb_loader_vid_pids({}))

    def test_unknown_soc_loader_pid_recognized_by_product_marker(self):
        # 现场案例（2026-09-16）：RK3562GMS1 Loader 枚举为未知 PID +
        # "USB download gadget"，默认 PID 清单（351a/350e）不覆盖，
        # wait_for_single_rockusb_loader 120s 超时并误报
        # "未能确认目标设备是唯一 Loader"。BootROM 产品名标记跨 SoC
        # 稳定，作为未知 PID 的兜底判定，新 SoC 无需补 PID 即可烧写。
        probe = (
            "0006\tHEALTHY-ADB\tSSI 17 on ARM64\n"
            "0007\tHEALTHY-OTHER\tSSI 17 Go on ARM64\n"
            "350f\tRK3562GMS1\tUSB download gadget\n"
        )
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["RK3562GMS1"],
        )

    def test_maskrom_product_marker_recognized(self):
        probe = "320a\tMASKROM-DEV\tMaskROM\n"
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["MASKROM-DEV"],
        )

    def test_healthy_function_pid_is_never_a_loader_even_with_marker(self):
        # H1 钉住：健康运行态功能枚举（0006 ADB / 0007 其他功能）即使
        # iProduct 恰好命中 BootROM 标记子串，也绝不能进入烧写目标列表
        # （现场存在 0007 健康枚举且永不出现于 adb 的设备）。
        probe = (
            "0006\tDEV-A\tRockusb Device Test Build\n"
            "0007\tDEV-B\tUSB download gadget\n"
        )
        self.assertEqual(rockusb_loader_serials(probe), [])

    def test_healthy_function_pid_not_rescued_by_explicit_loader_pids(self):
        # PID 是硬约束：把 0007 误配进 loader_pids（或平台误配置）也不
        # 得让健康枚举变成可烧写目标。
        probe = "0007\tDEV-B\tSSI 17 Go on ARM64\n"
        self.assertEqual(
            rockusb_loader_serials(probe, loader_pids=["0007", "351a"]),
            [],
        )

    def test_marker_hit_excluded_by_fastboot_enumeration(self):
        # 标记层命中 × fastboot 枚举的组合：健康 fastboot 设备的产品名
        # 若含标记，必须被排除层拦下。
        probe = "350f\tFB-MARKED\tUSB download gadget\n"
        self.assertEqual(
            rockusb_loader_serials(probe, exclude_serials={"FB-MARKED"}),
            [],
        )

    def test_marker_only_recognition_with_empty_pid_set(self):
        # 配置只含健康 PID（loader_pids 为空）时，未知 PID 的 Loader
        # 仍靠标记层识别。
        probe = "350f\tDEV\tUSB download gadget\n"
        self.assertEqual(
            rockusb_loader_serials(probe, loader_pids=set()),
            ["DEV"],
        )

    def test_product_marker_match_is_case_insensitive(self):
        probe = (
            "350f\tDEV1\tUSB Download Gadget\n"
            "350f\tDEV2\tRockusb Device\n"
        )
        self.assertEqual(
            rockusb_loader_serials(probe),
            ["DEV1", "DEV2"],
        )

    def test_explicit_loader_pids_override_default(self):
        probe = "320a\tMASKROM-DEV\tMaskROM\n351a\tLOADER-DEV\tLoader Dev\n"
        self.assertEqual(
            rockusb_loader_serials(probe, loader_pids=["320a"]),
            ["MASKROM-DEV"],
        )

    def test_loader_vid_pids_exclude_adb_and_fastboot_modes(self):
        pids = rockusb_loader_vid_pids({})
        self.assertIn("351a", pids)
        self.assertNotIn("0006", pids)  # ADB mode
        self.assertNotIn("4d00", pids)  # Fastboot mode

    def test_loader_vid_pids_read_from_platform_config(self):
        pids = rockusb_loader_vid_pids({
            "usbip_vid_pids": ["2207:0006", "18d1:4d00", "2207:320b"],
        })
        self.assertEqual(pids, {"320b"})


class _StaticConfig:
    def load_config(self):
        return {"ubuntu_host": ""}


class RockusbManagerScanTests(unittest.TestCase):
    def _local_manager(self):
        manager = DeviceManager(config_manager=_StaticConfig())
        enter = patch("features.devices.manager.is_local_host", return_value=True)
        enter.start()
        self.addCleanup(enter.stop)
        return manager

    def test_local_scan_excludes_adb_visible_serials(self):
        manager = self._local_manager()
        adb_output = "List of devices attached\nIN-ADB\tunauthorized\n"

        def fake_run(cmd, **_kwargs):
            if cmd[:1] == ["adb"]:
                return SimpleNamespace(stdout=adb_output, stderr="", returncode=0)
            return SimpleNamespace(stdout=PROBE_OUTPUT, stderr="", returncode=0)

        with (
            patch("features.devices.manager.has_blocked_adb_process", return_value=False),
            patch("features.devices.manager.subprocess.run", side_effect=fake_run),
        ):
            serials = manager.get_rockusb_loader_devices()

        # IN-ADB 处于 unauthorized 状态也必须被排除；健康枚举
        # RK3562GMS7（0006，无烧写标记）同样不得进入可烧写列表。
        # LOADER-ONLY（350d，未知 PID）通过 BootROM 产品名标记
        # （"USB download gadget"）被识别为 Loader。
        self.assertEqual(serials, ["LOADER-ONLY"])

    def test_local_scan_blocked_adb_returns_empty(self):
        manager = self._local_manager()
        with patch("features.devices.manager.has_blocked_adb_process", return_value=True):
            self.assertEqual(manager.get_rockusb_loader_devices(), [])


class DeviceListLoaderMergeTests(unittest.IsolatedAsyncioTestCase):
    async def _list_devices(self, *, adb, loader):
        global_state = SimpleNamespace(
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            user_states={},
            user_states_lock=threading.RLock(),
        )
        with (
            patch.object(
                devices_api.runtime,
                "get_client_id_from_request",
                return_value="alice",
            ),
            patch.object(devices_api.runtime, "global_state", global_state),
            patch.object(
                devices_api.device_manager,
                "get_connected_devices",
                return_value=adb,
            ),
            patch.object(
                devices_api.device_manager,
                "get_fastboot_devices",
                return_value=[],
            ),
            patch.object(
                devices_api.device_manager,
                "get_rockusb_loader_devices",
                return_value=loader,
            ),
            patch("features.users.load_device_groups", return_value=[]),
            patch("features.users.build_device_group_map", return_value={}),
            patch("features.users.current_username_for_request", return_value="alice"),
            patch.object(devices_api.reconnect, "reconcile_observed_usbip_devices"),
            patch.object(
                devices_api.reconnect,
                "filter_suppressed_usbip_devices",
                side_effect=lambda devices: list(devices),
            ),
            patch.object(devices_api, "known_usbip_sources", return_value={}),
            patch.object(devices_api, "_prune_inactive_usbip_sources", return_value={}),
        ):
            response = await devices_api.get_connected_devices(
                _request("alice"),
                force_refresh=True,
            )
        return json.loads(response.body), global_state

    async def test_loader_device_listed_with_loader_protocol(self):
        devices, _state = await self._list_devices(adb=[], loader=["RK3562GMS7"])
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["device_id"], "RK3562GMS7")
        self.assertEqual(devices[0]["protocol"], "rockusb-loader")
        self.assertEqual(devices[0]["status"], "loader")
        self.assertEqual(devices[0]["transport"], "local_usb")

    async def test_loader_entry_does_not_shadow_adb_entry(self):
        devices, state = await self._list_devices(
            adb=["RK3562GMS7"], loader=["RK3562GMS7"],
        )
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["protocol"], "adb")
        cached = state.device_cache["devices"]
        self.assertEqual(len(cached), 1)
        self.assertEqual(cached[0]["protocol"], "adb")

    async def test_loader_entry_survives_cached_reads(self):
        _devices, state = await self._list_devices(adb=[], loader=["RK3562GMS7"])
        cached = state.device_cache["devices"]
        self.assertEqual(cached[0]["protocol"], "rockusb-loader")
        self.assertEqual(cached[0]["status"], "loader")


if __name__ == "__main__":
    unittest.main()
