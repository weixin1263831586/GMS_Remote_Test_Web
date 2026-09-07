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
        doubled = PROBE_OUTPUT + "0006\tRK3562GMS7\tSSI 17 on ARM64\n"
        self.assertEqual(
            rockusb_loader_serials(doubled),
            [],
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

    def test_explicit_loader_pids_override_default(self):
        probe = "320a\tMASKROM-DEV\tMaskROM\n351a\tLOADER-DEV\tUSB download gadget\n"
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

        # IN-ADB 处于 unauthorized 状态也必须被排除；RK3562GMS7/LOADER-ONLY
        # 的 PID（0006/350d）不是烧写模式 PID，同样不得进入可烧写列表。
        self.assertEqual(serials, [])

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
            patch.object(devices_api, "_known_usbip_sources", return_value={}),
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
