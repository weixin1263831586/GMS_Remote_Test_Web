from __future__ import annotations

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import api as devices_api
from features.devices import device_lock_manager


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


class DeviceListIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_scan_cache_rejoins_owner_and_groups_per_request(self):
        global_state = SimpleNamespace(
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            user_states={},
            user_states_lock=threading.RLock(),
        )
        device_lock_manager.force_unlock_device("CACHE-DEVICE")
        device_lock_manager.lock_device(
            "CACHE-DEVICE",
            "alice",
            "Alice",
            source_id="test:alice",
            source_type="test",
        )

        def groups_for(username):
            return [{"id": f"{username}-group", "devices": ["CACHE-DEVICE"]}]

        def group_map(groups):
            return {"CACHE-DEVICE": [groups[0]["id"]]}

        try:
            with (
                patch.object(
                    devices_api.runtime,
                    "get_client_id_from_request",
                    side_effect=lambda request: request.state.current_user.username,
                ),
                patch.object(devices_api.runtime, "global_state", global_state),
                patch.object(
                    devices_api.device_manager,
                    "get_connected_devices",
                    return_value=["CACHE-DEVICE"],
                ) as scan,
                patch.object(
                    devices_api.device_manager,
                    "get_fastboot_devices",
                    return_value=["FASTBOOT-DEVICE"],
                ) as fastboot_scan,
                patch("features.users.load_device_groups", side_effect=groups_for),
                patch("features.users.build_device_group_map", side_effect=group_map),
                patch("features.users.current_username_for_request", side_effect=lambda request: request.state.current_user.username),
                patch.object(devices_api.reconnect, "reconcile_observed_usbip_devices"),
                patch.object(devices_api.reconnect, "filter_suppressed_usbip_devices", side_effect=lambda devices: devices),
                patch.object(devices_api, "_known_usbip_sources", return_value={}),
                patch.object(devices_api, "_prune_inactive_usbip_sources", return_value={}),
            ):
                with global_state.device_cache_lock:
                    global_state.device_cache = {"devices": [], "timestamp": 0}
                alice_response = await devices_api.get_connected_devices(
                    _request("alice"),
                    force_refresh=True,
                )
                bob_response = await devices_api.get_connected_devices(
                    _request("bob"),
                    force_refresh=False,
                )

            alice = json.loads(alice_response.body)[0]
            bob = json.loads(bob_response.body)[0]
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(fastboot_scan.call_count, 1)
            self.assertEqual(alice["groups"], ["alice-group"])
            self.assertEqual(bob["groups"], ["bob-group"])
            self.assertTrue(alice["locked_by_self"])
            self.assertFalse(bob["locked_by_self"])
            fastboot = next(
                item
                for item in json.loads(alice_response.body)
                if item["device_id"] == "FASTBOOT-DEVICE"
            )
            self.assertEqual(fastboot["status"], "fastboot")
            self.assertEqual(fastboot["protocol"], "fastboot")
            self.assertGreaterEqual(global_state.device_cache["timestamp"], time.time() - 5)
            self.assertNotIn("groups", global_state.device_cache["devices"][0])
            self.assertNotIn("locked_by_self", global_state.device_cache["devices"][0])
        finally:
            device_lock_manager.force_unlock_device("CACHE-DEVICE")

    async def test_firmware_lock_survives_adb_fastboot_protocol_transition(self):
        serial = "FIRMWARE-TRANSITION"
        global_state = SimpleNamespace(
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            user_states={},
            user_states_lock=threading.RLock(),
        )
        device_lock_manager.force_unlock_device(serial)
        acquired, _message = device_lock_manager.lock_device(
            serial,
            "alice",
            "Alice",
            source_id="firmware:alice",
            source_type="firmware",
        )
        self.assertTrue(acquired)
        lease_id = device_lock_manager.get_lock_status(serial)["lease_id"]

        try:
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
                    side_effect=[[serial], []],
                ),
                patch.object(
                    devices_api.device_manager,
                    "get_fastboot_devices",
                    side_effect=[[], [serial]],
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
                adb_response = await devices_api.get_connected_devices(
                    _request("alice"),
                    force_refresh=True,
                )
                fastboot_response = await devices_api.get_connected_devices(
                    _request("alice"),
                    force_refresh=True,
                )

            adb_device = json.loads(adb_response.body)[0]
            fastboot_device = json.loads(fastboot_response.body)[0]
            self.assertEqual(adb_device["device_id"], serial)
            self.assertEqual(adb_device["protocol"], "adb")
            self.assertEqual(fastboot_device["device_id"], serial)
            self.assertEqual(fastboot_device["protocol"], "fastboot")
            self.assertTrue(adb_device["locked"])
            self.assertTrue(fastboot_device["locked"])
            self.assertEqual(
                device_lock_manager.get_lock_status(serial)["lease_id"],
                lease_id,
            )
            self.assertEqual(
                device_lock_manager.get_lock_status(serial)["source_id"],
                "firmware:alice",
            )
        finally:
            device_lock_manager.registry.release(
                "firmware:alice",
                status="cancelled",
            )


if __name__ == "__main__":
    unittest.main()
