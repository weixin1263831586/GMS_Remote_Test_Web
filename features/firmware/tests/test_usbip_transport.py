import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime, usbip_transport


class FakeSshManager:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.commands = []

    def execute_command(self, _ssh, cmd, timeout=None):
        # Legacy watcher fixtures below drive human-readable list snapshots.
        # Do not consume their Linux attach responses for the JSON-state probe.
        if cmd == "usbipd state":
            return ("", "structured state not configured", 1)
        self.commands.append((cmd, timeout))
        try:
            return next(self.responses)
        except StopIteration:
            # StopIteration 无法穿透 asyncio Future（to_thread 中会永久挂起），
            # 响应耗尽时返回确定性失败。
            return ("", "fake ssh responses exhausted", 1)


class UsbipFirmwareTransportTests(unittest.TestCase):
    def test_mode_switch_schedules_source_reconnect(self):
        with patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            usbip_transport.usbip_reconnect,
            "schedule_usbip_reconnect",
            return_value=True,
        ) as schedule:
            scheduled = usbip_transport.schedule_usbip_mode_reconnect(
                "rk3572test", "fastboot"
            )
        self.assertTrue(scheduled)
        schedule.assert_called_once_with(
            "hcq@172.16.14.66",
            reason="USB/IP rk3572test switching to fastboot",
            expected_devices=["rk3572test"],
            accept_transport_only=True,
        )

    def test_waits_for_loader_and_adb_reenumeration(self):
        manager = FakeSshManager([
            ("List of rockusb connected(0)\n", "", 0),
            ("List of rockusb connected(1)\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        ready, output = asyncio.run(usbip_transport.wait_for_rockusb_loaders(
            object(), "upgrade_tool ld", 1, timeout=1, interval=0.01
        ))
        self.assertTrue(ready)
        self.assertIn("connected(1)", output)

        manager = FakeSshManager([
            ("List of devices attached\n", "", 0),
            ("List of devices attached\nrk3572test\tdevice\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        ready, observed = asyncio.run(usbip_transport.wait_for_adb_devices(
            object(), ["rk3572test"], timeout=1, interval=0.01
        ))
        self.assertTrue(ready)
        self.assertEqual(observed, ["rk3572test"])

    def test_preflight_installs_autobind_for_assigned_port(self):
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }]
        with patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            usbip_transport, "resolve_usbip_flash_routes", return_value=routes
        ), patch.object(
            usbip_transport,
            "ensure_usbip_auto_bind_policies",
            return_value={"success": True},
        ) as ensure:
            resolved, error = asyncio.run(
                usbip_transport.prepare_usbip_firmware_routes(["rk3572test"])
            )
        self.assertEqual((resolved, error), (routes, ""))
        ensure.assert_called_once_with("hcq@172.16.14.66", ["1-1"])

    def test_firmware_pause_stops_existing_reconnect_before_burn(self):
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }]
        with patch.object(
            usbip_transport.usbip_reconnect,
            "pause_usbip_reconnect",
        ) as pause, patch.object(
            usbip_transport.usbip_reconnect,
            "stop_usbip_reconnect_for_host",
        ) as stop, patch.object(
            usbip_transport.usbip_reconnect,
            "active_usbip_reconnect_hosts",
            return_value=[],
        ):
            devices, error = asyncio.run(
                usbip_transport.pause_usbip_firmware_reconnects(routes)
            )

        self.assertEqual(devices, ["rk3572test"])
        self.assertEqual(error, "")
        pause.assert_called_once_with(device_ids=["rk3572test"])
        stop.assert_called_once_with("hcq@172.16.14.66", 20.0)

    def test_firmware_pause_rejects_burn_if_reconnect_does_not_stop(self):
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }]
        with patch.object(
            usbip_transport.usbip_reconnect,
            "pause_usbip_reconnect",
        ), patch.object(
            usbip_transport.usbip_reconnect,
            "stop_usbip_reconnect_for_host",
        ), patch.object(
            usbip_transport.usbip_reconnect,
            "active_usbip_reconnect_hosts",
            return_value=["hcq@172.16.14.66"],
        ):
            devices, error = asyncio.run(
                usbip_transport.pause_usbip_firmware_reconnects(routes)
            )

        self.assertEqual(devices, ["rk3572test"])
        self.assertIn("仍在占用", error)

    def test_maskrom_watch_retries_until_new_instance_is_ready(self):
        manager = FakeSshManager([
            ("", "device not found", 1),
            ("", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        with patch.object(
            usbip_transport, "ROCKUSB_ATTACH_RETRY_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), [{
                        "source_host": "172.16.14.66",
                        "busids": ["1-1"],
                    }],
                    timeout=1, interval=0.01,
                )
            )
        self.assertTrue(result["success"])
        # 单次 attach 超时必须覆盖 Windows 端重新枚举后的慢速挂载，
        # 不能用 1s 截断即将成功的请求。
        self.assertEqual(manager.commands[0], (
            "sudo usbip attach -r 172.16.14.66 -b 1-1", 4
        ))
        self.assertGreaterEqual(result["attempts"], 2)

    def test_maskrom_watch_reports_pending_and_last_error(self):
        manager = FakeSshManager([
            ("", "usbip: error: Exported Device not found", 1),
        ] * 300)
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=0.5, interval=0.01,
        ))
        self.assertFalse(result["success"])
        self.assertEqual(
            result["pending"],
            [{"source_host": "172.16.14.66", "busid": "1-1"}],
        )
        self.assertEqual(
            result["errors"]["172.16.14.66/1-1"],
            "usbip: error: Exported Device not found",
        )
        self.assertGreaterEqual(result["attempts"], 1)
        # 确定性失败（设备尚未导出）不应触发 usbip port 二次核验。
        self.assertTrue(all(cmd.startswith("sudo usbip attach") for cmd, _ in manager.commands))

    def test_maskrom_watch_verifies_timeout_attach_via_usbip_port(self):
        manager = FakeSshManager([
            ("", "usbip: error: could not connect to 172.16.14.66:3240 (timed out)", 1),
            (
                "Imported USB devices\n"
                "====================\n"
                "Port 00: <Port in Use> at High Speed(480 Mbps)\n"
                "       Rockchip Inc. Rockusb Device\n"
                "       1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=1, interval=0.01,
        ))
        self.assertTrue(result["success"])
        self.assertEqual(result["attached"], [
            {"source_host": "172.16.14.66", "busid": "1-1"},
        ])
        self.assertEqual(result["pending"], [])
        self.assertEqual(manager.commands[1], (
            usbip_transport.USBIP_PORT_COMMAND, 10,
        ))

    def test_maskrom_watch_verifies_exception_attach_via_usbip_port(self):
        class TimeoutSshManager:
            def __init__(self):
                self.commands = []

            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if cmd.startswith("sudo usbip attach"):
                    raise TimeoutError("timed out")
                return (
                    "Imported USB devices\n"
                    "====================\n"
                    "Port 00: <Port in Use> at High Speed(480 Mbps)\n"
                    "       Rockchip Inc. Rockusb Device\n"
                    "       1-1 -> usbip://172.16.14.66:3240/1-1\n",
                    "",
                    0,
                )

        manager = TimeoutSshManager()
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=1, interval=0.01,
        ))
        self.assertTrue(result["success"])
        self.assertEqual(result["attached"], [
            {"source_host": "172.16.14.66", "busid": "1-1"},
        ])
        self.assertEqual(manager.commands[1], (
            usbip_transport.USBIP_PORT_COMMAND, 10,
        ))

    def test_maskrom_watch_busy_attach_with_stale_port_stays_pending(self):
        # 端口占用但 usbip port 显示的是其它 BUSID：不能误判为已挂载。
        manager = FakeSshManager([
            ("", "usbip: error: port busy", 1),
            (
                "Imported USB devices\n"
                "====================\n"
                "Port 01: <Port in Use> at High Speed(480 Mbps)\n"
                "       Rockchip Inc. Rockusb Device\n"
                "       2-1 -> usbip://172.16.14.66:3240/2-1\n",
                "",
                0,
            ),
        ] * 40)
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=0.3, interval=0.05,
        ))
        self.assertFalse(result["success"])
        self.assertEqual(
            result["pending"],
            [{"source_host": "172.16.14.66", "busid": "1-1"}],
        )
        self.assertEqual(result["errors"]["172.16.14.66/1-1"], "usbip: error: port busy")

    def test_protocols_accept_adb_fastbootd_and_vendor_fastboot_label(self):
        manager = FakeSshManager([
            ("List of devices attached\nADB001\tdevice\n", "", 0),
            ("FB001\tfastbootd\nrk3572test\t Android Fastboot\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        protocols = usbip_transport.device_flash_protocols(
            object(), ["ADB001", "FB001", "rk3572test", "MISSING"]
        )
        self.assertEqual(protocols, {
            "ADB001": "adb", "FB001": "fastboot",
            "rk3572test": "fastboot", "MISSING": "",
        })

    def test_only_routed_loader_is_accepted_for_direct_retry(self):
        route = [{"device_ids": ["rk3572test"], "busids": ["1-1"]}]
        self.assertEqual(
            usbip_transport.accept_direct_rockusb_loaders(
                {"rk3572test": ""}, route,
                "List of rockusb connected(1)\n",
            ),
            {"rk3572test": "rockusb-loader"},
        )
        self.assertEqual(
            usbip_transport.accept_direct_rockusb_loaders(
                {"MISSING": ""}, route, "List of rockusb connected(1)\n"
            ),
            {"MISSING": ""},
        )

    def test_maskrom_diagnosis_detects_descriptor_enumeration_failure(self):
        # 来自实机抓取的 Windows usbipd list 输出。
        snapshot = (
            "[hcq@172.16.14.66]\n"
            "Connected:\n"
            "BUSID  VID:PID    DEVICE"
            "                                                        STATE\n"
            "1-1    0000:0002  Unknown USB Device (Device Descriptor"
            " Request Failed)         Shared (forced)\n"
            "1-2    03f0:344a  USB 输入设备"
            "                                                  Not shared\n"
            "1-13   0403:6001  USB Serial Converter"
            "                                          Not shared\n"
            "\n"
            "Persisted:\n"
            "GUID                                  DEVICE\n"
            "41a923d0-84cc-4ab6-882f-59f71a2af359  Rockusb Device\n"
        )
        rows = usbip_transport.parse_usbipd_connected_rows(snapshot)
        self.assertEqual([row["busid"] for row in rows], ["1-1", "1-2", "1-13"])
        self.assertEqual(rows[0]["vid_pid"], "0000:0002")

        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(
            snapshot, ["1-1"],
        )
        self.assertIn("USB 设备描述符", diagnosis)
        self.assertIn("驱动切换/端口复位", diagnosis)
        self.assertIn("A/B 验证", diagnosis)
        self.assertIn("Shared (forced)", diagnosis)

    def test_maskrom_diagnosis_detects_new_busid_and_unshared(self):
        moved = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-4    2207:350b  Rockusb Device      Not shared\n"
        )
        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(moved, ["1-1"])
        self.assertIn("新 BUSID", diagnosis)
        self.assertIn("1-4", diagnosis)

        unshared = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:350b  Rockusb Device      Not shared\n"
        )
        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(
            unshared, ["1-1"],
        )
        self.assertIn("Not shared", diagnosis)
        self.assertIn("AutoBind", diagnosis)

        # 无法判定时返回空串，由调用方回退到通用提示。
        self.assertEqual(
            usbip_transport.diagnose_maskrom_reattach_failure(
                "Connected:\nBUSID  VID:PID    DEVICE    STATE\n", ["9-9"],
            ),
            "",
        )

    def test_maskrom_diagnosis_reports_missing_busid_enumeration(self):
        # 生产日志实录：快照时刻目标 1-1 尚未回到 Connected 列表，只有
        # 无关设备。不能回退到 AutoBind/TCP 3240 通用提示。
        snapshot = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-13   0403:6001  USB Serial Converter    Not shared\n"
        )
        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(
            snapshot, ["1-1"],
        )
        self.assertIn("未在 Windows 上重新出现", diagnosis)
        self.assertIn("1-1", diagnosis)
        self.assertIn("VID_2207", diagnosis)

    def test_maskrom_diagnosis_detects_attached_elsewhere_state(self):
        snapshot = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:350b  Rockusb Device      Attached\n"
        )
        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(
            snapshot, ["1-1"],
        )
        self.assertIn("vhci", diagnosis)
        self.assertIn("usbip detach -p", diagnosis)

    def test_usbipd_allowed_policy_state_is_parsed_as_state(self):
        rows = usbip_transport.parse_usbipd_connected_rows(
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:0006  Rockusb Device      Allowed\n"
        )
        self.assertEqual(rows[0]["state"], "Allowed")
        self.assertEqual(rows[0]["device"], "Rockusb Device")

    def test_maskrom_watch_binds_unshared_instance_on_source(self):
        manager = FakeSshManager([
            ("", "", 0),
            (
                "Exportable USB devices\n"
                " - 172.16.14.66\n"
                "      1-1: Rockchip Maskrom Device\n",
                "",
                0,
            ),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(closed=False)

        def close():
            windows_ssh.closed = True

        windows_ssh.close = close
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        listings = [
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-13   0403:6001  USB Serial Converter    Not shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Not shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Not shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Shared\n",
                "",
            ),
        ]

        def next_listing(_ssh):
            if len(listings) > 1:
                return listings.pop(0)
            return listings[0]

        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "bind_usbip_busid_via_ssh",
            return_value={"success": True, "detail": "shared"},
        ) as bind, patch.object(
            usbip_transport, "query_usbipd_device_states", return_value={},
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            side_effect=next_listing,
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
                object(), route, timeout=2, interval=0.01,
                baseline={
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "",
                        "vid_pid": "2207:351a",
                    }
                },
            ))
        self.assertTrue(result["success"])
        # 仅对已经重新枚举且明确为 Not shared 的实例执行普通 bind；
        # watcher 不得执行会再次 reset 物理端口的 Windows detach。
        bind.assert_called_once()
        self.assertEqual(bind.call_args[0][1], "1-1")
        self.assertFalse(any(
            command.startswith("usbipd detach")
            for command, _timeout in manager.commands
        ))
        self.assertTrue(windows_ssh.closed)

    def test_force_probe_rebinds_before_attach_and_restarts_stability(self):
        manager = FakeSshManager([
            ("", "", 0),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(closed=False)
        windows_ssh.close = lambda: setattr(windows_ssh, "closed", True)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        other = {
            "1-13": {
                "busid": "1-13", "vid_pid": "0403:6001",
                "instance_id": "USB\\VID_0403&PID_6001\\A",
                "device": "USB Serial Converter", "state": "Not shared",
                "is_forced": False,
            },
        }
        normal = {
            "1-1": {
                "busid": "1-1", "vid_pid": "2207:351a",
                "instance_id": "USB\\VID_2207&PID_351A\\NEW",
                "device": "Rockusb Device", "state": "Shared",
                "is_forced": False,
            },
        }
        forced = {
            "1-1": {
                **normal["1-1"], "state": "Shared (forced)",
                "is_forced": True,
            },
        }
        states = [other, normal, normal, forced, forced]

        def next_state(_manager, _ssh):
            if len(states) > 1:
                return states.pop(0)
            return states[0]

        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_device_states",
            side_effect=next_state,
        ), patch.object(
            usbip_transport, "bind_usbip_busid_via_ssh",
            return_value={"success": True, "detail": "forced"},
        ) as bind, patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=2, interval=0.01,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                            "vid_pid": "2207:351a",
                        }
                    },
                    force_bind=True,
                )
            )

        self.assertTrue(result["success"])
        bind.assert_called_once_with(windows_ssh, "1-1", force=True)
        self.assertTrue(windows_ssh.closed)

    def test_require_forced_probe_never_binds_or_attaches_unforced_instance(self):
        manager = FakeSshManager([])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(closed=False)
        windows_ssh.close = lambda: setattr(windows_ssh, "closed", True)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        state = {
            "1-1": {
                "busid": "1-1", "vid_pid": "2207:351a",
                "instance_id": "USB\\VID_2207&PID_351A\\NEW",
                "device": "Rockusb Device", "state": "Shared",
                "is_forced": False,
            },
        }
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_device_states",
            return_value=state,
        ), patch.object(
            usbip_transport, "bind_usbip_busid_via_ssh",
        ) as bind, patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(
                "Connected:\nBUSID VID:PID DEVICE STATE\n"
                "1-1 2207:351a Rockusb Device Shared",
                "",
            ),
        ), patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh", return_value="",
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_SNAPSHOT_WAIT_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=0.1, interval=0.01,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                            "vid_pid": "2207:351a",
                        }
                    },
                    allow_identity_transition=True,
                    require_forced=True,
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("尚未预绑定", result["errors"]["172.16.14.66/1-1"])
        bind.assert_not_called()
        self.assertFalse(any("usbip attach" in cmd for cmd, _ in manager.commands))
        self.assertTrue(windows_ssh.closed)

    def test_maskrom_watch_does_not_accept_old_loader_attachment(self):
        manager = FakeSshManager([
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
            ("", "", 0),
            (
                "Exportable USB devices\n"
                " - 172.16.14.66\n"
                "      1-1: Rockchip Maskrom Device\n",
                "",
                0,
            ),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listings = iter([
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:351a  Rockusb Device      Attached\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:351a  Rockusb Device      Shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-13   0403:6001  USB Serial Converter    Not shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Shared\n",
                "",
            ),
            (
                "Connected:\n"
                "BUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:350b  Maskrom Device      Shared\n",
                "",
            ),
        ])
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            side_effect=lambda _ssh: next(listings),
        ) as list_via_ssh, patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=1, interval=0.01,
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(list_via_ssh.call_count, 5)
        commands = [command for command, _timeout in manager.commands]
        # The transient old Loader Shared row must not trigger attach.  The
        # first target-side command is stale vhci cleanup after BUSID absence.
        self.assertEqual(commands[0], usbip_transport.USBIP_PORT_COMMAND)
        self.assertEqual(commands[1], "sudo -n usbip detach -p 00")
        self.assertEqual(
            commands.count("sudo usbip attach -r 172.16.14.66 -b 1-1"),
            1,
        )

    def test_maskrom_watch_rejects_changed_pnp_without_physical_gap(self):
        manager = FakeSshManager([])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:351a  Maskrom Device      Shared\n"
        )
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={"1-1": "USB\\VID_2207&PID_351A\\MASKROM"},
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_SNAPSHOT_WAIT_SECONDS", 0,
        ), patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh", return_value="",
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=0.05, interval=0.01,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": (
                                "USB\\VID_2207&PID_351A\\LOADER"
                            ),
                            "vid_pid": "2207:351a",
                        }
                    },
                )
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 0)
        commands = [command for command, _timeout in manager.commands]
        self.assertNotIn("sudo -n usbip detach -p 00", commands)
        self.assertFalse(any(
            command.startswith("sudo usbip attach") for command in commands
        ))

    def test_download_boot_event_arms_same_busid_without_absence_sample(self):
        manager = FakeSshManager([
            ("", "", 0),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        state = {
            "1-1": {
                "busid": "1-1",
                "instance_id": "USB\\VID_2207&PID_351A\\SAME",
                "vid_pid": "2207:351a",
                "device": "Rockusb Device",
                "state": "Shared",
                "is_attached": False,
            }
        }

        async def scenario():
            transition_event = asyncio.Event()
            ready_event = asyncio.Event()
            task = asyncio.create_task(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=1, interval=0.01,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\SAME",
                            "vid_pid": "2207:351a",
                        }
                    },
                    transition_event=transition_event,
                    ready_event=ready_event,
                )
            )
            await asyncio.wait_for(ready_event.wait(), timeout=1)
            self.assertFalse(any(
                command.startswith("sudo usbip attach")
                for command, _timeout in manager.commands
            ))
            transition_event.set()
            return await task

        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_device_states",
            return_value=state,
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_RETRY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(scenario())

        self.assertTrue(result["success"])
        self.assertTrue(any(
            command.startswith("sudo usbip attach")
            for command, _timeout in manager.commands
        ))

    def test_structured_state_absence_never_attaches_missing_busid(self):
        """结构化 usbipd state 模式下目标行缺失 = 物理缺席，不得 attach。

        旧实现仅在人类可读 list（listing 非空）时识别缺席；结构化模式下
        listing 恒为空，缺席分支被跳过并对已消失的 BUSID 连发 claim。实机
        上每次 claim 都撞击正在重新枚举的 PnP 节点，触发 VBoxUsb 崩溃并
        把端口锁死为 0000:0002（Code 43）。
        """
        manager = FakeSshManager([])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        state = {
            "1-9": {
                "busid": "1-9",
                "instance_id": "USB\\VID_03F0&PID_134A\\5&1",
                "vid_pid": "03f0:134a",
                "device": "USB Input Device",
                "state": "Not shared",
                "is_attached": False,
            },
        }

        async def scenario():
            transition_event = asyncio.Event()
            ready_event = asyncio.Event()
            task = asyncio.create_task(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=0.5, interval=0.02,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                            "vid_pid": "2207:351a",
                        }
                    },
                    transition_event=transition_event,
                    ready_event=ready_event,
                )
            )
            await asyncio.wait_for(ready_event.wait(), timeout=1)
            transition_event.set()
            return await task

        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_device_states",
            return_value=state,
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=("", ""),
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ):
            result = asyncio.run(scenario())

        self.assertFalse(result["success"])
        self.assertIn("尚未重新枚举", str(result["errors"]))
        attach_commands = [
            command for command, _timeout in manager.commands
            if command.startswith("sudo usbip attach")
        ]
        self.assertEqual(attach_commands, [])

    def test_transitional_attach_failure_reapplies_settle_dwell(self):
        """被拒的 attach（Device not found）必须重置稳定跟踪并重新落定。

        秒级间隔连发 claim 会持续撞击过渡态 PnP 节点（正在到达或离开），
        实机上每次撞击都可能把端口锁死为 0000:0002；重置后落定等待重新
        生效，第二次 attach 只在等待期满后发生。
        """
        manager = FakeSshManager([
            ("", "", 0),
            (
                "",
                "usbip: error: Attach Request for 1-1 failed - Device not found",
                1,
            ),
            ("", "", 0),
            (
                "Imported USB devices\nPort 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        state = {
            "1-1": {
                "busid": "1-1",
                "instance_id": "USB\\VID_2207&PID_351A\\NEW",
                "vid_pid": "2207:351a",
                "device": "Rockusb Device",
                "state": "Shared",
                "is_attached": False,
            },
        }

        async def scenario():
            transition_event = asyncio.Event()
            ready_event = asyncio.Event()
            task = asyncio.create_task(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=4, interval=0.02,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\OLD",
                            "vid_pid": "2207:351a",
                        }
                    },
                    transition_event=transition_event,
                    ready_event=ready_event,
                )
            )
            await asyncio.wait_for(ready_event.wait(), timeout=1)
            transition_event.set()
            return await task

        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_device_states",
            return_value=state,
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_RETRY_SECONDS", 0.05,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0.5,
        ):
            result = asyncio.run(scenario())

        self.assertTrue(result["success"], result)
        attach_commands = [
            command for command, _timeout in manager.commands
            if command.startswith("sudo usbip attach")
        ]
        self.assertEqual(len(attach_commands), 2)
        self.assertGreaterEqual(result["elapsed_seconds"], 0.5)

    def test_adb_to_loader_watch_accepts_identity_change_without_gap(self):
        """ADB→Loader 行替换可能发生在两次轮询之间，缺席采样不到。

        allow_identity_transition=True 时，PnP 实例/VID 相对基线变化且
        稳定后即视为转换，不必等 BUSID 完整消失；同实例同 VID 的旧行
        残留仍不构成转换。
        """
        manager = FakeSshManager([
            (
                "Exportable USB devices\n"
                " - 172.16.14.66\n"
                "      1-1: Rockchip Rockusb Device\n",
                "",
                0,
            ),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:351a  Rockusb Device      Shared\n"
        )
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={"1-1": "USB\\VID_2207&PID_351A\\LOADER"},
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_RETRY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
                object(), route, timeout=2, interval=0.01,
                baseline={
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "USB\\VID_2207&PID_0006\\D1",
                        "vid_pid": "2207:0006",
                    }
                },
                allow_identity_transition=True,
            ))
        self.assertTrue(result["success"])
        self.assertTrue(any(
            command.startswith("sudo usbip attach") for command, _ in manager.commands
        ))

    def test_identity_change_requires_flag_even_with_different_vid(self):
        """默认（uf/MaskROM 路径）不接受仅凭 VID 变化的转换。"""
        manager = FakeSshManager([])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:351a  Rockusb Device      Shared\n"
        )
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={"1-1": "USB\\VID_2207&PID_351A\\LOADER"},
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_SNAPSHOT_WAIT_SECONDS", 0,
        ), patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh", return_value="",
        ):
            result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
                object(), route, timeout=0.05, interval=0.01,
                baseline={
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "USB\\VID_2207&PID_0006\\D1",
                        "vid_pid": "2207:0006",
                    }
                },
            ))
        self.assertFalse(result["success"])
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(any(
            command.startswith("sudo usbip attach") for command, _ in manager.commands
        ))

    def test_maskrom_watch_ignores_intermediate_pid_until_full_gap(self):
        manager = FakeSshManager([
            ("", "", 0),
            (
                "Exportable USB devices\n"
                " - 172.16.14.66\n"
                "      1-1: Rockchip Maskrom Device\n",
                "",
                0,
            ),
            ("", "", 0),
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                "",
                0,
            ),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listings = iter([
            (
                "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:0006  Android ADB Interface    Shared\n",
                "",
            ),
            (
                "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:351a  Rockusb Device      Shared\n",
                "",
            ),
            (
                "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                "1-13   0403:6001  USB Serial Converter    Not shared\n",
                "",
            ),
            (
                "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:351a  Rockusb Device      Shared\n",
                "",
            ),
            (
                "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                "1-1    2207:351a  Rockusb Device      Shared\n",
                "",
            ),
        ])
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            side_effect=lambda _ssh: next(listings),
        ) as list_via_ssh, patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={"1-1": "USB\\VID_2207&PID_351A\\ROCKCHIP"},
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_VERIFY_SECONDS", 0,
        ), patch.object(
            usbip_transport, "ROCKUSB_ATTACH_SETTLE_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=1, interval=0.01,
                    baseline={
                        ("172.16.14.66", "1-1"): {
                            "instance_id": "USB\\VID_2207&PID_351A\\LOADER",
                            "vid_pid": "2207:351a",
                        }
                    },
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(list_via_ssh.call_count, 5)
        commands = [command for command, _timeout in manager.commands]
        self.assertEqual(
            commands.count("sudo usbip attach -r 172.16.14.66 -b 1-1"),
            1,
        )

    def test_maskrom_watch_cleans_only_exact_local_stale_route(self):
        manager = FakeSshManager([
            (
                "Imported USB devices\n"
                "Port 00: <Port in Use>\n"
                "  1-1 -> usbip://172.16.14.66:3240/1-1\n"
                "Port 01: <Port in Use>\n"
                "  1-11 -> usbip://172.16.14.66:3240/1-11\n",
                "",
                0,
            ),
            ("", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-13   0403:6001  USB Serial Converter    Not shared\n"
        )
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh", return_value="",
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 10,
        ), patch.object(
            usbip_transport, "ROCKUSB_SNAPSHOT_WAIT_SECONDS", 0,
        ):
            result = asyncio.run(
                usbip_transport.reattach_usbip_after_rockusb_reset(
                    object(), route, timeout=0.05, interval=0.01,
                )
            )
        self.assertFalse(result["success"])
        commands = [command for command, _timeout in manager.commands]
        self.assertIn("sudo -n usbip detach -p 00", commands)
        self.assertNotIn("sudo -n usbip detach -p 01", commands)
        self.assertFalse(any(command.startswith("sudo usbip attach") for command in commands))

    def test_maskrom_watch_captures_usbipd_snapshot_on_expiry(self):
        manager = FakeSshManager([
            ("", "usbip: error: Attach Request for 1-1 failed - Device not found", 1),
        ] * 300)
        runtime.configure_runtime(ssh_manager=manager)
        opened = []

        def fake_open(device_host, device_password=None):
            ssh_win = SimpleNamespace(closed=False)
            ssh_win.close = lambda: setattr(ssh_win, "closed", True)
            opened.append(ssh_win)
            return ssh_win, ""

        listing = (
            "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:350b  Maskrom Device      Not shared\n"
        )
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh", side_effect=fake_open,
        ), patch.object(
            usbip_transport, "bind_usbip_busid_via_ssh",
            return_value={"success": False, "detail": "no device"},
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh", return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh",
            return_value="GUID EFFECT OPERATION BUSID\nabc Allow AutoBind 1-1",
        ), patch.object(
            usbip_transport, "ROCKUSB_STABLE_SAMPLE_COUNT", 2,
        ):
            result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
                object(), route, timeout=0.5, interval=0.01,
                baseline={
                    ("172.16.14.66", "1-1"): {
                        "instance_id": "",
                        "vid_pid": "2207:351a",
                    }
                },
            ))
        self.assertFalse(result["success"])
        self.assertIn("[hcq@172.16.14.66]", result["source_list"])
        self.assertIn("1-1    2207:350b", result["source_list"])
        # AutoBind 规则命中情况随快照一并返回。
        self.assertIn("[hcq@172.16.14.66 policy]", result["source_list"])
        self.assertIn("Allow AutoBind 1-1", result["source_list"])
        self.assertTrue(all(ssh_win.closed for ssh_win in opened))

    def test_maskrom_snapshot_polls_until_pending_busid_appears(self):
        # 生产日志实录：watcher 超时时刻 1-1 尚未出现在 Connected 列表，
        # 枚举失败（0000:0002）行在数秒后才出现。快照必须轮询等待，
        # 否则会把描述符枚举失败误报成通用 AutoBind/TCP 3240 提示。
        manager = FakeSshManager([
            ("", "usbip: error: Attach Request for 1-1 failed - Device not found", 1),
        ] * 300)
        runtime.configure_runtime(ssh_manager=manager)
        windows_ssh = SimpleNamespace(closed=False)
        windows_ssh.close = lambda: setattr(windows_ssh, "closed", True)
        route = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
        }]
        early = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-13   0403:6001  USB Serial Converter    Not shared\n"
        )
        late = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    0000:0002  Unknown USB Device (Device Descriptor"
            " Request Failed)    Shared (forced)\n"
        )
        listings = [(early, ""), (late, "")]
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "bind_usbip_busid_via_ssh",
            return_value={"success": False, "detail": "no device"},
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            side_effect=lambda _ssh: listings.pop(0),
        ) as list_via_ssh, patch.object(
            usbip_transport, "usbipd_policy_list_via_ssh", return_value="",
        ), patch.object(
            usbip_transport, "ROCKUSB_SNAPSHOT_INTERVAL_SECONDS", 0.01,
        ), patch.object(
            usbip_transport, "ROCKUSB_SOURCE_POLL_SECONDS", 10,
        ):
            result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
                object(), route, timeout=0.5, interval=0.01,
            ))
        self.assertFalse(result["success"])
        self.assertEqual(list_via_ssh.call_count, 2)
        self.assertIn("0000:0002", result["source_list"])
        self.assertTrue(windows_ssh.closed)
        # 抓到描述符失败行后，诊断函数能给出枚举层结论。
        diagnosis = usbip_transport.diagnose_maskrom_reattach_failure(
            result["source_list"], ["1-1"],
        )
        self.assertIn("USB 设备描述符", diagnosis)


class RockusbBaselineTests(unittest.TestCase):
    def _route(self):
        return [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }]

    def test_captures_loader_instance_before_burn(self):
        windows_ssh = SimpleNamespace(closed=False)
        windows_ssh.close = lambda: setattr(windows_ssh, "closed", True)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:351a  Rockusb Device      Attached\n"
        )
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={
                "1-1": "USB\\VID_2207&PID_351A\\RK3572TEST"
            },
        ):
            baseline, error = asyncio.run(
                usbip_transport.capture_rockusb_route_baseline(self._route())
            )
        self.assertEqual(error, "")
        self.assertEqual(
            baseline[("172.16.14.66", "1-1")],
            {
                "instance_id": "USB\\VID_2207&PID_351A\\RK3572TEST",
                "vid_pid": "2207:351a",
            },
        )
        self.assertTrue(windows_ssh.closed)

    def test_rejects_descriptor_failure_without_mutating_binding(self):
        windows_ssh = SimpleNamespace(closed=False)
        windows_ssh.close = lambda: setattr(windows_ssh, "closed", True)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    0000:0002  Unknown USB Device"
            " (Device Descriptor Request Failed)    Shared (forced)\n"
        )
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
        ) as instance_probe:
            baseline, error = asyncio.run(
                usbip_transport.capture_rockusb_route_baseline(self._route())
            )
        self.assertEqual(baseline, {})
        self.assertIn("0000:0002", error)
        self.assertIn("断电重上电", error)
        # The baseline probe is read-only; no unbind/rebind command is issued.
        instance_probe.assert_called_once()
        self.assertTrue(windows_ssh.closed)

    def test_rejects_non_rockusb_device_on_assigned_port(self):
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    18d1:4ee0  Android ADB Interface Attached\n"
        )
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={},
        ):
            baseline, error = asyncio.run(
                usbip_transport.capture_rockusb_route_baseline(self._route())
            )
        self.assertEqual(baseline, {})
        self.assertIn("不是 RockUSB Loader", error)

    def test_rejects_loader_route_that_is_no_longer_attached(self):
        windows_ssh = SimpleNamespace(close=lambda: None)
        listing = (
            "Connected:\n"
            "BUSID  VID:PID    DEVICE              STATE\n"
            "1-1    2207:351a  Rockusb Device      Shared\n"
        )
        with patch.object(
            usbip_transport, "open_usbip_source_ssh",
            return_value=(windows_ssh, ""),
        ), patch.object(
            usbip_transport, "usbipd_list_via_ssh",
            return_value=(listing, ""),
        ), patch.object(
            usbip_transport, "query_usbipd_busid_instance_ids",
            return_value={},
        ):
            baseline, error = asyncio.run(
                usbip_transport.capture_rockusb_route_baseline(self._route())
            )
        self.assertEqual(baseline, {})
        self.assertIn("已不在 Attached", error)


if __name__ == "__main__":
    unittest.main()
