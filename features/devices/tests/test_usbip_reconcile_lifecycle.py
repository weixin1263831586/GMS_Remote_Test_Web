import asyncio
import json
import os
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from features.auth import CurrentUser
from features.devices.models import DeviceActionRequest, USBIPDisconnectRequest, USBIPStartRequest
from features.devices.tests.usbip_runtime_fixture import (
    UsbipRuntimeTestCase,
    _authenticated_request,
    global_state,
)
from foundation.command_result import CommandResult


class UsbipReconciliationTests(UsbipRuntimeTestCase):
    def test_usbip_status_defaults_to_configured_device_host(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        with global_state.usbip_states_lock:
            old_states = dict(global_state.usbip_states)
            global_state.usbip_states.clear()
            global_state.usbip_states["hcq@172.16.14.66"] = {"connected": True, "timestamp": 1}
        try:
            with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="alice-user-id"), \
                    patch.object(integrations.runtime.get_runtime(), "resolve_tailscale_device_host", None):
                response = asyncio.run(integrations.get_usbip_status(request=request))
        finally:
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["connected"])
        self.assertEqual(body["device_host"], "hcq@172.16.14.66")
        self.assertFalse(body["transport_connected"])

    def test_usbip_assignments_aggregates_all_hosts(self):
        """显示全部：一次返回所有来源主机的接入，按主机+Worker分组。"""
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "wlq@172.16.14.246|2-2": {
                            "device_host": "wlq@172.16.14.246",
                            "source_host": "172.16.14.246",
                            "worker_id": "ats-worker-controller",
                            "busid": "2-2",
                            "device_serials": ["ATS357629"],
                            "status": "attached",
                            "generation": 3,
                        },
                        "wlq@172.16.14.246|2-1": {
                            "device_host": "wlq@172.16.14.246",
                            "source_host": "172.16.14.246",
                            "worker_id": "ats-worker-controller",
                            "busid": "2-1",
                            "device_serials": ["RK3576GMS1"],
                            "status": "attached",
                            "generation": 4,
                        },
                        "hjf@172.16.14.188|1-5": {
                            "device_host": "hjf@172.16.14.188",
                            "source_host": "172.16.14.188",
                            "worker_id": "ats-worker-246",
                            "busid": "1-5",
                            "device_serials": [],
                            "status": "unknown",
                        },
                        "detached-host|9-9": {
                            "device_host": "",
                            "busid": "9-9",
                            "status": "attached",
                        },
                    },
                }

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ):
            response = asyncio.run(integrations.list_usbip_assignments())

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(body["total"], 2)
        by_host = {
            group["device_host"]: group
            for group in body["cluster_selections"]
        }
        wlq = by_host["wlq@172.16.14.246"]
        # 同一来源+Worker 的多个 BUSID 聚合为一组，busid 升序。
        self.assertEqual(wlq["busids"], ["2-1", "2-2"])
        self.assertEqual(wlq["device_serials"], ["RK3576GMS1", "ATS357629"])
        self.assertEqual(wlq["statuses_by_busid"], {"2-1": "attached", "2-2": "attached"})
        self.assertEqual(wlq["generations_by_busid"], {"2-1": 4, "2-2": 3})
        hjf = by_host["hjf@172.16.14.188"]
        self.assertEqual(hjf["busids"], ["1-5"])
        self.assertEqual(hjf["worker_id"], "ats-worker-246")
        self.assertEqual(hjf["statuses_by_busid"], {"1-5": "unknown"})

    def test_usbip_assignments_empty(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def get_runtime_config(self):
                return {}

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ):
            response = asyncio.run(integrations.list_usbip_assignments())

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(body["cluster_selections"], [])
        self.assertEqual(body["total"], 0)

    def test_usbip_assignments_verify_reports_live_transport_state(self):
        """verify=true：本地分配按 usbip port 实时核对，区分已记录/已连接。"""
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "wlq@172.16.14.246|2-2": {
                            "device_host": "wlq@172.16.14.246",
                            "worker_id": "ats-worker-controller",
                            "busid": "2-2",
                            "device_serials": ["ATS357629"],
                            "status": "attached",
                        },
                        "wlq@172.16.14.246|2-1": {
                            "device_host": "wlq@172.16.14.246",
                            "worker_id": "ats-worker-controller",
                            "busid": "2-1",
                            "device_serials": ["RK3576GMS1"],
                            "status": "attached",
                        },
                        "hjf@172.16.14.188|1-5": {
                            "device_host": "hjf@172.16.14.188",
                            "worker_id": "ats-worker-246",
                            "busid": "1-5",
                            "status": "unknown",
                        },
                    },
                }

            def load_config(self):
                return {}

        fake_ssh = MagicMock()
        fake_ssh_manager = MagicMock()
        fake_ssh_manager.get_connection.return_value = fake_ssh
        # usbip port: 2-1 仍在，2-2 已消失（如手工 detach / worker 重启）。
        fake_ssh_manager.execute_command.return_value = CommandResult(
            stdout=(
                "Port 00:\n"
                "Port 01:\n"
                "       2-1 | 2207:0006 | RK3576GMS1 | Remote USB/IP host 172.16.14.246\n"
                "Port 02:\n"
                "       2-2 | 2207:0006 | ATS357629 | Remote USB/IP host 172.16.14.99\n"
            ),
            stderr="",
            code=0,
        )

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(), "ssh_manager", fake_ssh_manager,
        ):
            response = asyncio.run(
                integrations.list_usbip_assignments(verify=True)
            )

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertTrue(body["verified"])
        by_host = {
            group["device_host"]: group
            for group in body["cluster_selections"]
        }
        wlq = by_host["wlq@172.16.14.246"]
        self.assertEqual(
            wlq["transport_state_by_busid"],
            {"2-1": "attached", "2-2": "detached"},
        )
        # 远端 Worker 分配不本地核对，状态为 unknown。
        hjf = by_host["hjf@172.16.14.188"]
        self.assertEqual(hjf["transport_state_by_busid"], {"1-5": "unknown"})

    def test_usbip_assignments_verify_matches_source_host(self):
        """attach 用 Tailscale source_host 时按它核对。

        device_host 是 SSH 地址（hcq@172.16.14.66），source_host 是
        usbip attach 的地址（100.x.x.x）；usbip port 里看到的是
        source_host。用 device_host 核对会误报 detached。
        """
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "hcq@172.16.14.66|1-12-3": {
                            "device_host": "hcq@172.16.14.66",
                            "source_host": "100.97.1.2",
                            "worker_id": "ats-worker-controller",
                            "busid": "1-12-3",
                            "device_serials": ["RK3576GMS1"],
                            "status": "attached",
                        },
                    },
                }

            def load_config(self):
                return {}

        fake_ssh = MagicMock()
        fake_ssh_manager = MagicMock()
        fake_ssh_manager.get_connection.return_value = fake_ssh
        # usbip port 显示的是 attach 用的 Tailscale 地址。
        fake_ssh_manager.execute_command.return_value = CommandResult(
            stdout=(
                "Port 00:\n"
                "Port 01:\n"
                "       1-12-3 | 2207:0006 | RK3576GMS1 | Remote USB/IP host 100.97.1.2\n"
            ),
            stderr="",
            code=0,
        )

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(), "ssh_manager", fake_ssh_manager,
        ):
            response = asyncio.run(
                integrations.list_usbip_assignments(verify=True)
            )

        body = json.loads(response.body.decode("utf-8"))
        group = body["cluster_selections"][0]
        # source_host 命中 → attached（按 device_host 核对会误报 detached）。
        self.assertEqual(
            group["transport_state_by_busid"], {"1-12-3": "attached"},
        )

    def test_usbip_assignments_verify_falls_back_to_unknown_on_ssh_error(self):
        """SSH 核对失败：不阻塞响应，实时状态降级为 unknown。"""
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "wlq@172.16.14.246|2-2": {
                            "device_host": "wlq@172.16.14.246",
                            "worker_id": "ats-worker-controller",
                            "busid": "2-2",
                            "status": "attached",
                        },
                    },
                }

            def load_config(self):
                return {}

        fake_ssh_manager = MagicMock()
        fake_ssh_manager.get_connection.return_value = MagicMock()
        fake_ssh_manager.execute_command.return_value = CommandResult(
            stdout="", stderr="boom", code=1,
        )

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(), "ssh_manager", fake_ssh_manager,
        ):
            response = asyncio.run(
                integrations.list_usbip_assignments(verify=True)
            )

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertTrue(body["verified"])
        group = body["cluster_selections"][0]
        self.assertEqual(
            group["transport_state_by_busid"], {"2-2": "unknown"},
        )

    def test_usbip_status_includes_serials_for_each_disconnect_item(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "hcq@172.16.14.66|1-8": {
                            "device_host": "hcq@172.16.14.66",
                            "source_host": "172.16.14.66",
                            "worker_id": "ats-worker-controller",
                            "busid": "1-8",
                            "device_serials": ["RK3576GMS6"],
                            "status": "attached",
                        },
                    },
                }

        request = SimpleNamespace(
            headers={}, client=SimpleNamespace(host="127.0.0.1")
        )
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            integrations.runtime.get_runtime(), "resolve_tailscale_device_host", None
        ), patch("features.devices.usbip.status_api.probe_existing_local_usbip_transport",
            return_value={
                "transport_connected": True,
                "device_list": ["RK3576GMS6"],
                "protocol_status": {
                    "mode": "adb",
                    "adb": {"RK3576GMS6": "device"},
                    "adb_ready": ["RK3576GMS6"],
                },
            },
        ):
            response = asyncio.run(
                integrations.get_usbip_status(request=request)
            )

        body = json.loads(response.body.decode("utf-8"))
        selection = body["cluster_selections"][0]
        self.assertEqual(selection["busids"], ["1-8"])
        self.assertEqual(selection["device_serials"], ["RK3576GMS6"])
        self.assertEqual(
            selection["device_serials_by_busid"],
            {"1-8": ["RK3576GMS6"]},
        )
        self.assertEqual(
            selection["statuses_by_busid"],
            {"1-8": "attached"},
        )

    def test_usbip_status_repairs_stale_local_attached_state(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["rk3572test"],
                    "status": "attached",
                },
            },
        }

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

            def get_runtime_config(self):
                return runtime_config

        request = SimpleNamespace(
            headers={}, client=SimpleNamespace(host="127.0.0.1")
        )
        with global_state.usbip_states_lock:
            old_states = dict(global_state.usbip_states)
            global_state.usbip_states.clear()
            global_state.usbip_states["hcq@172.16.14.66"] = {
                "connected": True,
                "timestamp": 1,
                "transport_connected": True,
                "adb_ready": True,
                "reconnecting": False,
                "protocol_status": {
                    "mode": "adb",
                    "adb_ready": ["rk3572test"],
                },
            }
        with global_state.device_cache_lock:
            old_cache = dict(global_state.device_cache)
            global_state.device_cache = {
                "devices": [{"device_id": "rk3572test"}],
                "timestamp": 1,
            }
        try:
            with patch.object(
                integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
            ), patch.object(
                integrations.runtime.get_runtime(),
                "get_client_id_from_request",
                return_value="hcq@172.16.14.66",
            ), patch.object(
                integrations.runtime.get_runtime(), "resolve_tailscale_device_host", None
            ), patch("features.devices.usbip.status_api.probe_existing_local_usbip_transport",
                return_value=None,
            ), patch("features.devices.usbip.status_api._local_worker_id",
                return_value="ats-worker-controller",
            ), patch.object(
                integrations.device_manager,
                "get_connected_devices",
                return_value=[],
            ), patch.object(
                integrations.reconnect,
                "schedule_usbip_reconnect",
                return_value=True,
            ) as schedule:
                response = asyncio.run(
                    integrations.get_usbip_status(request=request)
                )
        finally:
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["connected"])
        self.assertFalse(body["transport_connected"])
        self.assertFalse(body["adb_ready"])
        self.assertTrue(body["reconnecting"])
        self.assertTrue(body["transport_mismatch"])
        self.assertEqual(body["transport_state"], "degraded")
        self.assertEqual(body["readiness"], "not_ready")
        self.assertEqual(
            body["cluster_selections"][0]["statuses_by_busid"],
            {"1-1": "unknown"},
        )
        schedule.assert_called_once_with(
            "hcq@172.16.14.66",
            reason="USB/IP status detected stale local attached assignment",
            expected_devices=["rk3572test"],
        )

    def test_usbip_status_source_record_does_not_imply_transport_restored(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        with global_state.usbip_states_lock:
            old_states = dict(global_state.usbip_states)
            global_state.usbip_states.clear()
        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }
        try:
            with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="alice-user-id"), \
                    patch.object(integrations.runtime.get_runtime(), "resolve_tailscale_device_host", None):
                response = asyncio.run(integrations.get_usbip_status(request=request))
        finally:
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["connected"])
        self.assertFalse(body["transport_connected"])
        self.assertFalse(body["adb_ready"])

    def test_suppressed_usbip_auto_connect_is_blocked_until_manual_connect(self):
        import features.devices.integrations_api as integrations
        import features.devices.reconnect as reconnect

        calls = []

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None, **kwargs):
                calls.append((device_host, device_password, usbip_attach_host))
                return {"success": True, "message": "ok", "device_list": ["USBIP001"]}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
        try:
            with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch("features.devices.usbip.connection_api.usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@172.16.14.66"):
                auto_response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66"),
                    request=request,
                    help=False,
                ))
                manual_response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66", manual_connect=True),
                    request=request,
                    help=False,
                ))
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])

        auto_body = json.loads(auto_response.body.decode("utf-8"))
        manual_body = json.loads(manual_response.body.decode("utf-8"))
        self.assertFalse(auto_body["success"])
        self.assertTrue(auto_body["manual_disconnect_suppressed"])
        self.assertTrue(manual_body["success"])
        self.assertEqual(calls, [("hcq@172.16.14.66", "secret", None)])
        self.assertFalse(reconnect.is_usbip_reconnect_suppressed("hcq@172.16.14.66", "USBIP001"))

    def test_failed_manual_usbip_connect_keeps_auto_reconnect_suppressed(self):
        import features.devices.integrations_api as integrations
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None, **kwargs):
                return {"success": False, "error": "未找到Android设备"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
        try:
            with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch("features.devices.usbip.connection_api.usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@172.16.14.66"):
                response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66", manual_connect=True),
                    request=request,
                    help=False,
                ))
            body = json.loads(response.body.decode("utf-8"))
            self.assertFalse(body["success"])
            self.assertTrue(reconnect.is_usbip_reconnect_suppressed("hcq@172.16.14.66", "USBIP001"))
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])

    def test_removed_usbip_device_schedules_server_side_reconnect(self):
        import features.devices.reconnect as reconnect

        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }

        scheduled = []
        try:
            with patch.object(
                reconnect,
                "schedule_usbip_reconnect",
                side_effect=lambda host, reason="", expected_devices=(): scheduled.append(
                    (host, reason, tuple(expected_devices))
                ) or True,
            ):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="test")
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, ["hcq@172.16.14.66"])
        self.assertEqual(scheduled, [("hcq@172.16.14.66", "test: USBIP001", ("USBIP001",))])

    def test_manual_usbip_disconnect_suppresses_server_side_reconnect(self):
        import features.devices.reconnect as reconnect

        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }

        scheduled = []
        try:
            reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
            with patch.object(
                reconnect,
                "schedule_usbip_reconnect",
                side_effect=lambda host, reason="", expected_devices=(): scheduled.append(
                    (host, reason, tuple(expected_devices))
                ) or True,
            ):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="manual disconnect")
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, [])
        self.assertEqual(scheduled, [])

    def test_firmware_pause_blocks_reconnect_without_hiding_device(self):
        import features.devices.reconnect as reconnect

        reconnect.pause_usbip_reconnect(device_ids=["USBIP001"])
        try:
            self.assertTrue(
                reconnect.is_usbip_reconnect_paused(device_id="USBIP001")
            )
            self.assertFalse(reconnect.schedule_usbip_reconnect(
                "hcq@172.16.14.66",
                expected_devices=["USBIP001"],
            ))
            # Firmware ownership is not a user-requested disconnect; normal
            # inventory must continue to show the selected serial.
            self.assertEqual(
                reconnect.filter_suppressed_usbip_devices(["USBIP001"]),
                ["USBIP001"],
            )
        finally:
            reconnect.resume_usbip_reconnect(device_ids=["USBIP001"])

        self.assertFalse(
            reconnect.is_usbip_reconnect_paused(device_id="USBIP001")
        )

    def test_remote_worker_assignment_blocks_local_reconnect(self):
        import features.devices.reconnect as reconnect

        fake_runtime = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "status": "attached",
                    "timestamp": 1,
                },
                "hcq@172.16.14.66|1-2": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-2",
                    "status": "attached",
                    "timestamp": 1,
                },
            },
        }
        with patch.object(reconnect, "_local_worker_id", return_value="ats-worker-controller"), patch.object(
            reconnect.runtime.config_manager, "get_runtime_config", return_value=fake_runtime
        ):
            # A busid on this host belongs to a remote Worker.
            self.assertTrue(reconnect._device_host_has_remote_assignment("hcq@172.16.14.66"))
            # Hostname-only form (no user@) still matches via source_host.
            self.assertTrue(reconnect._device_host_has_remote_assignment("172.16.14.66"))
            # An unrelated host is unaffected.
            self.assertFalse(reconnect._device_host_has_remote_assignment("10.0.0.9"))
            # Local reconnect scheduling must be refused for the contended host.
            self.assertFalse(reconnect.schedule_usbip_reconnect("hcq@172.16.14.66"))

    def test_usbip_disconnect_finds_devices_from_runtime_sources(self):
        import features.devices.integrations_api as integrations

        old_sources = dict(global_state.usbip_devices_source)
        old_manager_sources = dict(integrations.usbip_manager.device_sources)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            integrations.usbip_manager.device_sources.clear()

            with patch.object(integrations.runtime.config_manager, "get_runtime_config", return_value={
                "usbip_devices_source": {
                    "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    "OTHER001": {"source": "hcq@172.16.14.67", "timestamp": 1},
                }
            }):
                self.assertEqual(
                    integrations._usbip_devices_for_host("hcq@172.16.14.66"),
                    ["USBIP001"],
                )
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            integrations.usbip_manager.device_sources.clear()
            integrations.usbip_manager.device_sources.update(old_manager_sources)

    def test_usbip_disconnect_detaches_ubuntu_ports_in_normal_mode(self):
        import features.devices.integrations_api as integrations

        calls = []

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_host": "hcq@172.16.14.66",
                    "device_pswd": "secret",
                    "client_ssh_credentials": [],
                }

            def get_runtime_config(self):
                return {
                    "usbip_devices_source": {
                        "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    }
                }

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        class FakeSshManager:
            def get_connection(self, config):
                return "ubuntu-ssh"

            def return_connection(self, ssh):
                calls.append(("return", ssh))

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                calls.append(("exec", ssh, cmd))
                return CommandResult(stdout="", stderr="", code=0)

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                self.config = config

            def __enter__(self):
                return "windows-ssh"

            def __exit__(self, exc_type, exc, tb):
                return False

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

        with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch.object(integrations.runtime.get_runtime(), "ssh_manager", FakeSshManager()), \
                patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@172.16.14.66"), \
                patch.object(integrations.runtime.get_runtime(), "resolve_tailscale_device_host", return_value=(None, None)), \
                patch("features.devices.usbip.connection_api.DeviceSSHConnection", FakeDeviceSSHConnection), \
                patch("features.devices.usbip.connection_api.notify_device_change", AsyncMock()), \
                patch("features.devices.usbip.connection_api.acquire_device_operation_claim", return_value=("operation:usbip:test", [{"id": "claim-1", "device_key": "ats-worker-controller:USBIP001", "generation": 1, "owner_id": "user-id"}], None)), \
                patch("features.devices.usbip.connection_api.release_device_operation_claim"), \
                patch("features.devices.usbip.connection_api.audit_device_operation"), \
                patch("features.devices.reconnect.stop_usbip_reconnect_for_host") as stop_reconnect, \
                patch("features.devices.usbip.support.detach_ubuntu_usbip_ports", return_value=["00"] ) as detach:
            response = asyncio.run(integrations.stop_usbip(request=request, req=None))

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        detach.assert_called_once_with("ubuntu-ssh", "172.16.14.66", detach_all=False)
        stop_reconnect.assert_called_once_with("hcq@172.16.14.66", timeout=2)
        self.assertIn(("exec", "windows-ssh", "usbipd unbind --all"), calls)

    def test_selected_local_disconnect_keeps_usbipd_binding_for_fast_reconnect(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        calls = []
        runtime_config = {
            "usbip_devices_source": {
                "RK3576GMS1": {
                    "source": "hcq@172.16.14.66",
                    "timestamp": 1,
                },
            },
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["RK3576GMS1"],
                    "status": "attached",
                },
            },
        }

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_host": "hcq@172.16.14.66",
                    "device_pswd": "secret",
                    "client_ssh_credentials": [],
                }

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

            def save_runtime_config(self, data):
                runtime_config.clear()
                runtime_config.update(data)
                return True

        class FakeSshManager:
            def get_connection(self, config):
                return "ubuntu-ssh"

            def return_connection(self, ssh):
                calls.append(("return", ssh))

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                calls.append(("exec", ssh, cmd))
                return CommandResult(stdout="", stderr="", code=0)

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                self.config = config

            def __enter__(self):
                return "windows-ssh"

            def __exit__(self, exc_type, exc, tb):
                return False

        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller")
        )
        detach_result = {
            "detached_ports": ["00"],
            "remaining_devices": [],
        }
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(), "ssh_manager", FakeSshManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=fake_cluster
        ), patch("features.devices.usbip.connection_api.DeviceSSHConnection", FakeDeviceSSHConnection
        ), patch("features.devices.usbip.connection_api._detach_ubuntu_usbip_for_devices",
            return_value=detach_result,
        ), patch("features.devices.usbip.connection_api.notify_device_change", AsyncMock()
        ), patch("features.devices.usbip.connection_api.acquire_device_operation_claim",
            return_value=("", [], None),
        ), patch("features.devices.usbip.connection_api.release_device_operation_claim"
        ), patch("features.devices.usbip.connection_api.audit_device_operation"
        ), patch(
            "features.devices.reconnect.stop_usbip_reconnect_for_host"
        ):
            response = asyncio.run(integrations.stop_usbip(
                request=SimpleNamespace(headers={}, client=None),
                req=USBIPDisconnectRequest(
                    device_host="hcq@172.16.14.66",
                    source_host="172.16.14.66",
                    worker_id="ats-worker-controller",
                    busids=["1-1"],
                ),
                _elevated=CurrentUser(
                    id="admin-id", username="admin", role="admin"
                ),
            ))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            ("exec", "windows-ssh", "usbipd detach --busid 1-1"),
            calls,
        )
        self.assertFalse(any("usbipd unbind" in call[-1] for call in calls if call[0] == "exec"))

    def test_usbip_disconnect_defaults_to_resolved_device_host_not_platform_user_id(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {
                    "client_hosts": {"172.16.14.66": "hcq"},
                    "client_ssh_credentials": [
                        {
                            "device_host": "hcq@172.16.14.66",
                            "username": "hcq",
                            "host": "172.16.14.66",
                            "password": "secret",
                        }
                    ],
                }

            def get_runtime_config(self):
                return {
                    "usbip_devices_source": {
                        "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    }
                }

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        class FakeSshManager:
            def get_connection(self, config):
                return "ubuntu-ssh"

            def return_connection(self, ssh):
                pass

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                return CommandResult(stdout="", stderr="", code=0)

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                self.config = config

            def __enter__(self):
                return "windows-ssh"

            def __exit__(self, exc_type, exc, tb):
                return False

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.16.14.66"))

        with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch.object(integrations.runtime.get_runtime(), "ssh_manager", FakeSshManager()), \
                patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="NqWo58sh1jr5c6ZiyxxPtQ"), \
                patch.object(integrations.runtime.get_runtime(), "resolve_tailscale_device_host", return_value=(None, None)), \
                patch("features.devices.usbip.support.get_client_display_id_from_request", return_value="hcq@172.16.14.66"), \
                patch("features.devices.usbip.connection_api.DeviceSSHConnection", FakeDeviceSSHConnection), \
                patch("features.devices.usbip.connection_api.notify_device_change", AsyncMock()), \
                patch("features.devices.usbip.connection_api.acquire_device_operation_claim", return_value=("operation:usbip:test", [{"id": "claim-1", "device_key": "ats-worker-controller:USBIP001", "generation": 1, "owner_id": "user-id"}], None)), \
                patch("features.devices.usbip.connection_api.release_device_operation_claim"), \
                patch("features.devices.usbip.connection_api.audit_device_operation"), \
                patch("features.devices.reconnect.stop_usbip_reconnect_for_host"), \
                patch("features.devices.usbip.support.detach_ubuntu_usbip_ports", return_value=["00"]) as detach:
            response = asyncio.run(integrations.stop_usbip(request=request, req=None))

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        detach.assert_called_once_with("ubuntu-ssh", "172.16.14.66", detach_all=False)

    def test_verified_detach_falls_back_when_target_device_remains(self):
        import features.devices.integrations_api as integrations

        adb_calls = 0

        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                nonlocal adb_calls
                if cmd == "adb devices":
                    adb_calls += 1
                    if adb_calls == 1:
                        return CommandResult(stdout="List of devices attached\nUSBIP001\tdevice\n", stderr="", code=0)
                    return CommandResult(stdout="List of devices attached\n", stderr="", code=0)
                return CommandResult(stdout="", stderr="", code=0)

        detach_calls = []

        def fake_detach(ssh, remote_host=None, detach_all=False):
            detach_calls.append((remote_host, detach_all))
            return ["00"] if not detach_all else ["01"]

        with patch.object(integrations.runtime.get_runtime(), "ssh_manager", FakeSshManager()), \
                patch("features.devices.usbip.support.detach_ubuntu_usbip_ports", side_effect=fake_detach):
            result = integrations._detach_ubuntu_usbip_for_devices(
                "ubuntu-ssh",
                device_host="hcq@172.16.14.66",
                usbip_attach_host=None,
                devices_to_remove=["USBIP001"],
                detach_all=False,
            )

        self.assertEqual(detach_calls, [("172.16.14.66", False), (None, True)])
        self.assertEqual(result["detached_ports"], ["00", "01"])
        self.assertEqual(result["remaining_devices"], [])

    def test_verified_detach_can_defer_settle_until_source_unbind(self):
        import features.devices.integrations_api as integrations

        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return CommandResult(
        stdout="List of devices attached\nUSBIP001\tdevice\n",
        stderr="",
        code=0,
    )
                return CommandResult(stdout="", stderr="", code=0)

        with patch.object(
            integrations.runtime.get_runtime(), "ssh_manager", FakeSshManager()
        ), patch("features.devices.usbip.support.detach_ubuntu_usbip_ports",
            return_value=["00"],
        ), patch.object(integrations.time, "sleep") as sleep:
            result = integrations._detach_ubuntu_usbip_for_devices(
                "ubuntu-ssh",
                device_host="hcq@172.16.14.66",
                usbip_attach_host=None,
                devices_to_remove=["USBIP001"],
                busids=["1-1"],
                settle=False,
            )

        self.assertEqual(result["detached_ports"], ["00"])
        self.assertEqual(result["remaining_devices"], ["USBIP001"])
        sleep.assert_not_called()

    def test_device_list_refresh_keeps_usbip_source_for_reconnect(self):
        import features.devices.api as devices_router

        old_sources = dict(global_state.usbip_devices_source)
        old_cache = dict(global_state.device_cache)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source["USBIP001"] = {
                    "source": "hcq@172.16.14.66",
                    "timestamp": 1,
                }
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}

            request = _authenticated_request()
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001"]), \
                    patch.object(devices_router.device_manager, "get_fastboot_devices", return_value=[]), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            self.assertIn("USBIP001", global_state.usbip_devices_source)
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_device_list_hides_manually_disconnected_usbip_device(self):
        import features.devices.api as devices_router
        import features.devices.reconnect as reconnect

        old_cache = dict(global_state.device_cache)
        try:
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}
            reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
            request = _authenticated_request()
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001", "USBIP001"]), \
                    patch.object(devices_router.device_manager, "get_fastboot_devices", return_value=[]), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                response = asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            body = json.loads(response.body.decode("utf-8"))
            self.assertEqual([item["device_id"] for item in body], ["LOCAL001"])
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_device_list_marks_persisted_usbip_source_when_memory_empty(self):
        import features.devices.api as devices_router

        old_sources = dict(global_state.usbip_devices_source)
        old_cache = dict(global_state.device_cache)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}

            request = _authenticated_request()
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001", "USBIP001"]), \
                    patch.object(devices_router.device_manager, "get_fastboot_devices", return_value=[]), \
                    patch.object(devices_router.runtime.config_manager, "get_runtime_config", return_value={
                        "usbip_devices_source": {
                            "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1}
                        }
                    }), \
                    patch.object(devices_router, "_prune_inactive_usbip_sources", side_effect=lambda _devices, sources, _config: sources), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime.get_runtime(), "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                response = asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            body = json.loads(response.body.decode("utf-8"))
            devices = {item["device_id"]: item for item in body}
            self.assertNotIn("is_usbip", devices["LOCAL001"])
            self.assertTrue(devices["USBIP001"]["is_usbip"])
            self.assertEqual(devices["USBIP001"]["source"], "hcq@172.16.14.66")
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_usbip_reboot_returns_without_waiting_for_adb_online(self):
        import features.devices.operations.api as operations

        old_sources = dict(global_state.usbip_devices_source)
        calls = []
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()

            def fake_reboot_device(device_id, ssh=None, wait_for_online=True):
                calls.append((device_id, wait_for_online))
                return {"success": True, "back_online": False, "wait_time": 0.0}

            with patch.object(operations.runtime.get_runtime(), "config_manager", SimpleNamespace(get_runtime_config=lambda: {
                "usbip_devices_source": {
                    "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1}
                }
            })), patch.object(operations.device_manager, "reboot_device", side_effect=fake_reboot_device), \
                    patch.object(operations.reconnect, "schedule_usbip_reconnect", return_value=True):
                response = asyncio.run(operations.reboot_devices(
                    DeviceActionRequest(devices=["USBIP001"]),
                    _authenticated_request(),
                ))

            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["success"])
            self.assertEqual(calls, [("USBIP001", False)])
            self.assertTrue(body["data"]["results"][0]["usbip_reconnect_expected"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_usbip_reboot_schedules_backend_reconnect_for_device_host(self):
        import features.devices.operations.api as operations

        old_sources = dict(global_state.usbip_devices_source)
        scheduled = []
        calls = []
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source["USBIP001"] = {
                    "source": "hcq@172.16.14.66",
                    "timestamp": 1,
                }

            def fake_reboot_device(device_id, ssh=None, wait_for_online=True):
                calls.append((device_id, wait_for_online))
                return {"success": True}

            with patch.object(operations.device_manager, "reboot_device", side_effect=fake_reboot_device), \
                    patch.object(operations.reconnect, "schedule_usbip_reconnect", side_effect=lambda host, reason="", expected_devices=(): scheduled.append((host, reason, tuple(expected_devices))) or True):
                response = asyncio.run(operations.reboot_devices(
                    DeviceActionRequest(devices=["USBIP001"]),
                    _authenticated_request(),
                ))

            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["success"])
            self.assertEqual(calls, [("USBIP001", False)])
            self.assertEqual(scheduled, [("hcq@172.16.14.66", "USB/IP device reboot requested", ("USBIP001",))])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_reconnect_waits_for_stable_expected_device(self):
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        class FakeUsbipManager:
            def __init__(self):
                self.calls = 0

            def start_usbip(self, device_host, device_password, **kwargs):
                self.calls += 1
                return {"success": True, "device_list": ["USBIP001"]}

        fake_usbip = FakeUsbipManager()
        device_sequences = iter([
            [],
            ["USBIP001"],
            ["USBIP001"],
            ["USBIP001"],
        ])

        with patch.object(reconnect.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch.object(reconnect, "usbip_manager", fake_usbip), \
                patch.object(
                    reconnect, "_resolved_busids_for_devices",
                    return_value=["1-1"],
                ), \
                patch.object(reconnect, "has_blocked_adb_process", return_value=False), \
                patch.object(reconnect.device_manager, "get_connected_devices", side_effect=lambda force_refresh=True: next(device_sequences)), \
                patch.object(reconnect, "USBIP_RECONNECT_INTERVAL_SECONDS", 0), \
                patch.object(reconnect, "USBIP_RECONNECT_STABLE_INTERVAL_SECONDS", 0):
            reconnect._reconnect_worker(
                "hcq@172.16.14.66",
                "test",
                threading.Event(),
                ("USBIP001",),
            )

        self.assertEqual(fake_usbip.calls, 2)
        self.assertIn("USBIP001", global_state.usbip_devices_source)

    def test_reconnect_result_is_ignored_after_manual_disconnect_suppression(self):
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, **kwargs):
                reconnect.suppress_usbip_reconnect(device_host, ["USBIP001"])
                return {
                    "success": True,
                    "transport_connected": True,
                    "device_list": ["USBIP001"],
                }

        old_sources = dict(global_state.usbip_devices_source)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with patch.object(reconnect.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch.object(reconnect, "usbip_manager", FakeUsbipManager()), \
                    patch.object(
                        reconnect, "_resolved_busids_for_devices",
                        return_value=["1-1"],
                    ), \
                    patch.object(reconnect.device_manager, "get_connected_devices", return_value=["USBIP001"]):
                reconnect._reconnect_worker(
                    "hcq@172.16.14.66",
                    "test",
                    threading.Event(),
                    ("USBIP001",),
                )
            self.assertNotIn("USBIP001", global_state.usbip_devices_source)
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_reconcile_observed_usbip_device_restores_source_mapping(self):
        import features.devices.reconnect as reconnect

        old_sources = dict(global_state.usbip_devices_source)
        old_states = dict(global_state.usbip_states)
        class FakeConfigManager:
            def get_runtime_config(self):
                return {}

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states["hcq@172.16.14.66"] = {
                    "connected": True,
                    "transport_connected": False,
                    "adb_ready": False,
                    "reconnecting": True,
                    "expected_devices": ["USBIP001"],
                    "protocol_status": {"mode": "reconnecting"},
                }
            with patch.object(reconnect.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                 patch.object(
                     reconnect, "classify_local_usb_attachment",
                     return_value={},
                 ):
                restored = reconnect.reconcile_observed_usbip_devices(["LOCAL001", "USBIP001"])

            self.assertEqual(restored, {"hcq@172.16.14.66": ["USBIP001"]})
            self.assertEqual(
                global_state.usbip_devices_source["USBIP001"]["source"],
                "hcq@172.16.14.66",
            )
            self.assertFalse(global_state.usbip_states["hcq@172.16.14.66"]["reconnecting"])
            self.assertTrue(global_state.usbip_states["hcq@172.16.14.66"]["adb_ready"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)

    def test_reconcile_does_not_promote_physically_local_device(self):
        """物理直连串号不得被提升为 USB/IP 来源。"""
        import features.devices.reconnect as reconnect

        old_sources = dict(global_state.usbip_devices_source)
        old_states = dict(global_state.usbip_states)
        class FakeConfigManager:
            def get_runtime_config(self):
                return {}

            def update_runtime_config(self, updates):
                return True

            def save_runtime_config(self, data):
                return True

        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states["hcq@172.16.14.66"] = {
                    "connected": True,
                    "transport_connected": False,
                    "adb_ready": False,
                    "reconnecting": True,
                    "expected_devices": ["RK3562GMS7"],
                    "protocol_status": {"mode": "reconnecting"},
                }
            with patch.object(reconnect.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                 patch.object(
                     reconnect, "classify_local_usb_attachment",
                     return_value={"RK3562GMS7": "physical"},
                 ):
                restored = reconnect.reconcile_observed_usbip_devices(["RK3562GMS7"])

            self.assertEqual(restored, {})
            self.assertNotIn("RK3562GMS7", global_state.usbip_devices_source)
            state = global_state.usbip_states["hcq@172.16.14.66"]
            self.assertFalse(state["reconnecting"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)

    def test_classify_local_usb_attachment_distinguishes_vhci_from_physical(self):
        import features.devices.reconnect as reconnect

        # realpath 模拟：physical 落在 xHCI 控制器，vhci 落在 vhci_hcd。
        real_paths = {
            os.path.join("/sys/bus/usb/devices", "1-13"):
                "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-13",
            os.path.join("/sys/bus/usb/devices", "4-1"):
                "/sys/devices/platform/vhci_hcd.0/usb4/4-1",
        }

        def fake_realpath(path):
            return real_paths.get(path, path)

        with patch.object(reconnect.os.path, "realpath", side_effect=fake_realpath):
            result = reconnect.classify_local_usb_attachment(
                ["PHYS001", "VHCI001", "UNKNOWN001"],
                bus_paths={"PHYS001": "1-13", "VHCI001": "4-1"},
            )

        self.assertEqual(result, {"PHYS001": "physical", "VHCI001": "vhci"})
        # 判定不了的串号不出现在结果里（fail-open）。
        self.assertNotIn("UNKNOWN001", result)

    def test_frontend_waits_for_backend_autoreconnects_usbip_disconnects(self):
        # 2026-08 shell 拆分后，USB/IP 前端重连逻辑分布在 pages/ 与 shell/websocket-manager.js。
        text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in [Path("web/static/js/navigation.js"), *sorted(Path("web/static/js/pages").glob("*.js")), *sorted(Path("web/static/js/shell").glob("*.js"))])

        self.assertIn("device_host: deviceHost", text)
        self.assertIn("scheduleUsbipReconnect", text)
        self.assertIn("USBIP_RECONNECT_MAX_ATTEMPTS", text)
        self.assertIn("USBIP_RECONNECT_INITIAL_DELAY_MS", text)
        self.assertIn("USB/IP 设备正在重启", text)
        self.assertIn("usbipManualDisconnectUntil", text)
        self.assertIn("data.source !== 'usbip_disconnect'", text)
        self.assertIn("manual_connect: true", text)
        self.assertIn("isUsbipAdbReady", text)
        self.assertIn("isUsbipProtocolVisible", text)
        self.assertIn("usbipReconnectWaiting || usbipReconnectTimer", text)
        self.assertNotIn("status.transport_connected || usbipDevices.length > 0", text)
        self.assertIn("等待后端自动重连", text)
        self.assertIn("/api/usbip/status?device_host=", text)
        self.assertNotIn("const result = await apiCall('/api/usbip/connect', 'POST', payload);", text)
        self.assertNotIn("result.success || result.devices", text)
        self.assertNotIn("Button reset due to device disconnect", text)


class StaleAssignmentRetirementTests(unittest.TestCase):
    """物理挪插到本机的设备，其过期 USB/IP 分配
    应被 retire（持久化+内存双清），固件烧写路由立即回落本地链路。"""

    def setUp(self):
        import features.devices.integrations_api as integrations

        self.integrations = integrations
        self.old_sources = dict(global_state.usbip_devices_source)
        self.old_states = dict(global_state.usbip_states)
        self.old_manager_sources = dict(
            getattr(integrations.usbip_manager, "device_sources", {}) or {}
        )
        with global_state.usbip_devices_source_lock:
            global_state.usbip_devices_source.clear()
        with global_state.usbip_states_lock:
            global_state.usbip_states.clear()

    def tearDown(self):
        integrations = self.integrations
        with global_state.usbip_devices_source_lock:
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source.update(self.old_sources)
        with global_state.usbip_states_lock:
            global_state.usbip_states.clear()
            global_state.usbip_states.update(self.old_states)
        integrations.usbip_manager.device_sources.clear()
        integrations.usbip_manager.device_sources.update(self.old_manager_sources)

    def _make_config_manager(self, runtime_config: dict):
        class FakeConfigManager:
            def get_runtime_config(self):
                return dict(runtime_config)

            def load_config(self):
                return {}

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

            def save_runtime_config(self, data):
                runtime_config.clear()
                runtime_config.update(data)
                return True

        return FakeConfigManager()

    def test_stale_assignment_retired_when_device_is_physical_local(self):
        integrations = self.integrations
        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["RK3562GMS7"],
                    "status": "attached",
                },
                # 同主机其他分配不受影响。
                "hcq@172.16.14.66|1-2": {
                    "device_host": "hcq@172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-2",
                    "device_serials": ["USBIP-KEEP"],
                    "status": "attached",
                },
            },
        }
        local_assignments = list(
            runtime_config["usbip_cluster_assignments"].values()
        )
        global_state.usbip_devices_source["RK3562GMS7"] = {
            "source": "hcq@172.16.14.66",
            "timestamp": 1,
        }
        integrations.usbip_manager.device_sources["RK3562GMS7"] = {
            "source": "hcq@172.16.14.66",
        }

        with patch.object(
            integrations.runtime.get_runtime(),
            "config_manager",
            self._make_config_manager(runtime_config),
        ):
            state, _changed = asyncio.run(
                integrations._retire_assignments_for_physically_local_devices(
                    "hcq@172.16.14.66",
                    local_assignments,
                    {"RK3562GMS7"},
                    {"RK3562GMS7": "physical"},
                )
            )

        # 持久化分配被清除，其他分配保留。
        self.assertNotIn("hcq@172.16.14.66|1-1", runtime_config["usbip_cluster_assignments"])
        self.assertIn("hcq@172.16.14.66|1-2", runtime_config["usbip_cluster_assignments"])
        # 内存来源记录（全局 + manager 缓存）被清理。
        self.assertNotIn("RK3562GMS7", global_state.usbip_devices_source)
        self.assertNotIn("RK3562GMS7", integrations.usbip_manager.device_sources)
        # 状态置为最终 disconnected，不调度重连。
        self.assertFalse(state["reconnecting"])
        self.assertFalse(state["connected"])
        self.assertIn("retired", state["reason"])

    def test_stale_assignment_kept_when_device_classification_unknown(self):
        """fail-open：分类不出来（如 sysfs 不可用）时不删除任何分配。"""
        integrations = self.integrations
        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["USBIP001"],
                    "status": "attached",
                },
            },
        }
        local_assignments = list(
            runtime_config["usbip_cluster_assignments"].values()
        )

        with patch.object(
            integrations.runtime.get_runtime(),
            "config_manager",
            self._make_config_manager(runtime_config),
        ):
            state, _changed = asyncio.run(
                integrations._retire_assignments_for_physically_local_devices(
                    "hcq@172.16.14.66",
                    local_assignments,
                    {"USBIP001"},
                    {},  # 分类失败：无任何 physical 判定
                )
            )

        # 分类为空时不满足 all(physical)，但直接调用 retire 时按 assignment
        # 自身序列号判定——空分类下判定不成立，stale_busids 为空，
        # 分配原样保留（fail-open）。
        self.assertIn("hcq@172.16.14.66|1-1", runtime_config["usbip_cluster_assignments"])
        self.assertFalse(state["reconnecting"])
