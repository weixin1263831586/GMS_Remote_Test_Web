import asyncio
import os
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.firmware import api, firmware_api, runtime, shares_api


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_user": "tester", "client_username": "codex"}

    def get_ubuntu_user(self, config):
        return config.get("ubuntu_user", "tester")


class FakeSshManager:
    def __init__(self, file_check_output):
        self.file_check_output = file_check_output
        self.commands = []

    def optional_connection(self, _config):
        return self

    @asynccontextmanager
    async def async_optional_connection(self, config):
        with self.optional_connection(config) as ssh:
            yield ssh

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_transport(self):
        return object()

    def execute_command(self, _ssh, cmd, timeout=None):
        self.commands.append((cmd, timeout))
        if "test -f" in cmd:
            return self.file_check_output, "", 0
        return "", "", 0


async def fake_lock_firmware_devices(**_kwargs):
    return ["D1"], None


async def fake_release_firmware_devices(*_args, **_kwargs):
    return None


class FirmwareApiTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = TemporaryDirectory()
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n"),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=threading.RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=threading.RLock(),
                websocket_connections={},
            ),
            generate_help_or_continue=lambda help, *_args: (
                JSONResponse({"help": True}) if help else None
            ),
            get_client_id_from_request=lambda _request: "codex@127.0.0.1",
            lock_firmware_devices=fake_lock_firmware_devices,
            release_firmware_devices=fake_release_firmware_devices,
            project_root=".",
            firmware_share_store=None,
            apk_max_tasks=20,
            apk_max_file_size=500 * 1024 * 1024,
            apk_max_source_file_size=2 * 1024 * 1024,
            apk_upload_dir=self.runtime_directory.name,
        )
        app = FastAPI()

        @app.middleware("http")
        async def authenticate_test_request(request, call_next):
            request.state.current_user = CurrentUser(
                id="id-codex", username="codex", role="user"
            )
            return await call_next(request)

        app.include_router(api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.runtime_directory.cleanup()

    def test_firmware_help_does_not_execute_host_command(self):
        response = self.client.post("/api/burn/firmware?help=true")

        self.assertEqual(response.status_code, 200)

    def test_apk_missing_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/missing-task")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_apk_unknown_uuid_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/00000000-0000-0000-0000-000000000000")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_apk_delete_rejects_non_uuid_task_id(self):
        response = self.client.delete("/api/apk/task/not-a-uuid")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_missing_remote_firmware_path_does_not_enter_loader(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        response = self.client.post(
            "/api/burn/firmware?devices=D1",
            data={"firmware_path": "/tmp/not-found.img"},
        )

        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )
        self.assertFalse(any("reboot loader" in command for command, _ in fake_ssh.commands))

    def test_usbip_explicit_uf_is_rejected_before_loader_transition(self):
        notifications = []
        watcher_started = {"count": 0}
        reconnect_lifecycle = []
        runtime.configure_runtime(
            store_notification=lambda *args, **kwargs: notifications.append(args)
        )

        class FakeBurnChannel:
            """单次 upgrade_tool 运行：先吐出 chunks 再退出。"""

            def __init__(self, exit_status, chunks=()):
                self._exit_status = exit_status
                self._chunks = [chunk.encode("utf-8") for chunk in chunks]

            def exit_status_ready(self):
                return not self._chunks

            def recv_ready(self):
                return bool(self._chunks)

            def recv(self, _size):
                return self._chunks.pop(0)

            def recv_exit_status(self):
                return self._exit_status

        class BurnFlowSsh(FakeSshManager):
            def __init__(self):
                super().__init__("__GMS_REMOTE_FILE_FOUND__\n")
                self.burn_runs = []
                self.watcher_armed_before_burn = []
                self.pause_seen_at_loader_reboot = []

            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if cmd == "adb -s D1 reboot loader":
                    self.pause_seen_at_loader_reboot.append(
                        reconnect_lifecycle == ["pause"]
                    )
                if "test -f" in cmd:
                    return self.file_check_output, "", 0
                if " SFI " in cmd:
                    return "loading firmware\n", "", 0
                if "adb devices" in cmd:
                    return "List of devices attached\nD1\tdevice\n", "", 0
                if cmd.endswith(" ld"):
                    return "List of rockusb connected(1)\n", "", 0
                return "", "", 0

            def exec_command(self, cmd, get_pty=False, timeout=None):
                self.watcher_armed_before_burn.append(
                    watcher_started["count"]
                )
                self.burn_runs.append(cmd)
                if len(self.burn_runs) == 1:
                    channel = FakeBurnChannel(255, [(
                        "Loading firmware...\nStart to upgrade firmware...\n"
                        "Download Boot Start\nDownload Boot Success\n"
                        "Wait For Maskrom Start\nWait For Maskrom Fail\n"
                    )])
                else:
                    channel = FakeBurnChannel(0)
                stdout = SimpleNamespace(channel=channel)
                stderr = SimpleNamespace(read=lambda: b"")
                return object(), stdout, stderr

        fake_ssh = BurnFlowSsh()
        runtime.configure_runtime(ssh_manager=fake_ssh)
        from features.firmware import usbip_transport

        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["D1"],
        }]

        async def prepare_routes(_devices):
            return routes, ""

        async def capture_baseline(_routes):
            return {
                ("172.16.14.66", "1-1"): {
                    "instance_id": "USB\\VID_2207&PID_351A\\D1",
                    "vid_pid": "2207:351a",
                }
            }, ""

        async def reattach_succeeds(_ssh, _routes, **_kwargs):
            watcher_started["count"] += 1
            ready_event = _kwargs.get("ready_event")
            if ready_event is not None:
                ready_event.set()
            return {"success": True, "attached": [{"busid": "1-1"}]}

        async def pause_reconnects(_routes):
            reconnect_lifecycle.append("pause")
            return ["D1"], ""

        def resume_reconnects(devices):
            reconnect_lifecycle.append(("resume", tuple(devices)))

        with patch("scp.SCPClient"), patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="",
        ), patch.object(
            firmware_api, "_prepare_usbip_firmware_routes",
            side_effect=prepare_routes,
        ), patch.object(
            firmware_api, "_capture_rockusb_route_baseline",
            side_effect=capture_baseline,
        ), patch.object(
            firmware_api, "_pause_usbip_reconnects",
            side_effect=pause_reconnects,
        ), patch.object(
            firmware_api, "_resume_usbip_reconnects",
            side_effect=resume_reconnects,
        ), patch.object(
            firmware_api, "_reattach_usbip_after_rockusb_reset",
            side_effect=reattach_succeeds,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/srv/update.img", "burn_mode": "uf"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("禁止进入upgrade_tool", response.json()["error"])
        self.assertEqual(fake_ssh.burn_runs, [])
        self.assertEqual(fake_ssh.pause_seen_at_loader_reboot, [])
        self.assertEqual(reconnect_lifecycle, [])
        self.assertFalse(notifications)

    def test_usbip_routes_default_to_fastboot_firmware_backend(self):
        fastboot_calls = []

        class RecordingSsh(FakeSshManager):
            def __init__(self):
                super().__init__("__GMS_REMOTE_FILE_FOUND__\n")
            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if "test -f" in cmd:
                    return self.file_check_output, "", 0
                if " SFI " in cmd:
                    return "loading firmware\n", "", 0
                if "adb devices" in cmd:
                    return "List of devices attached\nD1\tdevice\n", "", 0
                return "", "", 0

        fake_ssh = RecordingSsh()
        runtime.configure_runtime(
            ssh_manager=fake_ssh,
            store_notification=lambda *_args, **_kwargs: None,
        )
        from features.firmware import usbip_transport

        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["D1"],
        }]

        async def prepare_routes(_devices):
            return routes, ""

        async def fastboot_burn(_ssh, **kwargs):
            fastboot_calls.append(kwargs)
            return {
                "backend": "usbip-fastboot",
                "results": [{
                    "device": "D1",
                    "success": True,
                    "partitions": ["super", "boot_a", "vbmeta_a"],
                }],
                "skipped": ["uboot.img(uboot_a 仅支持本地upgrade_tool烧写)"],
            }

        with patch("scp.SCPClient"), patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="",
        ), patch.object(
            firmware_api, "_prepare_usbip_firmware_routes",
            side_effect=prepare_routes,
        ), patch.object(
            firmware_api, "_run_usbip_fastboot_firmware",
            side_effect=fastboot_burn,
        ), patch.object(
            firmware_api, "_run_partition_burn",
            side_effect=AssertionError("USB/IP auto mode must not enter Loader"),
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/srv/update.img"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["data"]["backend"], "usbip-fastboot")
        self.assertEqual(len(fastboot_calls), 1)
        self.assertEqual(fastboot_calls[0]["remote_firmware"], "/srv/update.img")
        self.assertEqual(fastboot_calls[0]["devices"], ["D1"])
        commands = [command for command, _timeout in fake_ssh.commands]
        self.assertFalse(any("reboot loader" in command for command in commands))
        self.assertFalse(any(" uf " in command for command in commands))

    def test_usbip_partition_mode_runs_loader_di_partition_burn(self):
        partition_calls = []

        class RecordingSsh(FakeSshManager):
            def __init__(self):
                super().__init__("__GMS_REMOTE_FILE_FOUND__\n")

            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if "test -f" in cmd:
                    return self.file_check_output, "", 0
                if " SFI " in cmd:
                    return "loading firmware\n", "", 0
                if "adb devices" in cmd:
                    return "List of devices attached\nD1\tdevice\n", "", 0
                return "", "", 0

        fake_ssh = RecordingSsh()
        runtime.configure_runtime(
            ssh_manager=fake_ssh,
            store_notification=lambda *_args, **_kwargs: None,
        )
        from features.firmware import usbip_transport

        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["D1"],
        }]

        async def prepare_routes(_devices):
            return routes, ""

        async def partition_burn(_ssh, **kwargs):
            partition_calls.append(kwargs)
            return {
                "written": [{"partition": "super", "image": "super.img"}],
                "skipped": [],
                "total_bytes": 4096,
            }

        async def capture_baseline(_routes):
            return {
                ("172.16.14.66", "1-1"): {
                    "instance_id": "USB\\VID_2207&PID_0006\\D1",
                    "vid_pid": "2207:0006",
                }
            }, ""

        async def reattach_succeeds(_ssh, _routes, **_kwargs):
            ready_event = _kwargs.get("ready_event")
            if ready_event is not None:
                ready_event.set()
            return {"success": True, "attached": [{"busid": "1-1"}]}

        async def pause_reconnects(_routes):
            return ["D1"], ""

        async def loader_ready(_ssh, _cmd, _count, **_kwargs):
            return True, "List of rockusb connected(1)"

        with patch("scp.SCPClient"), patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="",
        ), patch.object(
            firmware_api, "_prepare_usbip_firmware_routes",
            side_effect=prepare_routes,
        ), patch.object(
            firmware_api, "_capture_rockusb_route_baseline",
            side_effect=capture_baseline,
        ), patch.object(
            firmware_api, "_pause_usbip_reconnects",
            side_effect=pause_reconnects,
        ), patch.object(
            firmware_api, "_reattach_usbip_after_rockusb_reset",
            side_effect=reattach_succeeds,
        ), patch.object(
            firmware_api, "_wait_for_rockusb_loaders",
            side_effect=loader_ready,
        ), patch.object(
            firmware_api, "_resume_usbip_reconnects", lambda _devices: None,
        ), patch.object(
            firmware_api, "_run_usbip_fastboot_firmware",
            side_effect=AssertionError("partition mode must not use Fastboot"),
        ), patch.object(
            firmware_api, "_run_partition_burn",
            side_effect=partition_burn,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/srv/update.img", "burn_mode": "partition"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(partition_calls), 1)
        self.assertFalse(partition_calls[0]["transport_probe"])
        self.assertEqual(partition_calls[0]["remote_firmware"], "/srv/update.img")
        commands = [command for command, _timeout in fake_ssh.commands]
        # partition 模式显式进入 Loader，但绝不执行 uf 整包路径。
        self.assertTrue(any("reboot loader" in command for command in commands))
        self.assertFalse(any(" uf " in command for command in commands))

    def test_local_device_partition_mode_is_rejected(self):
        class RecordingSsh(FakeSshManager):
            def __init__(self):
                super().__init__("__GMS_REMOTE_FILE_FOUND__\n")

            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if "test -f" in cmd:
                    return self.file_check_output, "", 0
                if " SFI " in cmd:
                    return "loading firmware\n", "", 0
                if "adb devices" in cmd:
                    return "List of devices attached\nD1\tdevice\n", "", 0
                return "", "", 0

        fake_ssh = RecordingSsh()
        runtime.configure_runtime(
            ssh_manager=fake_ssh,
            store_notification=lambda *_args, **_kwargs: None,
        )
        from features.firmware import usbip_transport

        async def prepare_routes(_devices):
            return [], ""

        with patch("scp.SCPClient"), patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="",
        ), patch.object(
            firmware_api, "_prepare_usbip_firmware_routes",
            side_effect=prepare_routes,
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/srv/update.img", "burn_mode": "partition"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("partition固件模式仅用于", response.json()["error"])
        commands = [command for command, _timeout in fake_ssh.commands]
        self.assertFalse(any("reboot loader" in command for command in commands))

    def test_usbip_uf_rejection_does_not_start_descriptor_watcher(self):
        reconnect_lifecycle = []

        class FailedBurnChannel:
            def __init__(self):
                self._chunks = [(
                    b"Download Boot Start\nDownload Boot Success\n"
                    b"Wait For Maskrom Start\nWait For Maskrom Fail\n"
                )]

            def exit_status_ready(self):
                return not self._chunks

            def recv_ready(self):
                return bool(self._chunks)

            def recv(self, _size):
                return self._chunks.pop(0)

            def recv_exit_status(self):
                return 255

        class DescriptorFailureSsh(FakeSshManager):
            def __init__(self):
                super().__init__("__GMS_REMOTE_FILE_FOUND__\n")

            def execute_command(self, _ssh, cmd, timeout=None):
                self.commands.append((cmd, timeout))
                if "test -f" in cmd:
                    return self.file_check_output, "", 0
                if " SFI " in cmd:
                    return "loading firmware\n", "", 0
                if "adb devices" in cmd:
                    return "List of devices attached\nD1\tdevice\n", "", 0
                if cmd.endswith(" ld"):
                    return "List of rockusb connected(1)\n", "", 0
                return "", "", 0

            def exec_command(self, _cmd, get_pty=False, timeout=None):
                stdout = SimpleNamespace(channel=FailedBurnChannel())
                stderr = SimpleNamespace(read=lambda: b"")
                return object(), stdout, stderr

        fake_ssh = DescriptorFailureSsh()
        runtime.configure_runtime(
            ssh_manager=fake_ssh,
            store_notification=lambda *_args, **_kwargs: None,
        )
        from features.firmware import usbip_transport

        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["D1"],
        }]

        async def prepare_routes(_devices):
            return routes, ""

        async def capture_baseline(_routes):
            return {
                ("172.16.14.66", "1-1"): {
                    "instance_id": "USB\\VID_2207&PID_351A\\D1",
                    "vid_pid": "2207:351a",
                }
            }, ""

        async def pause_reconnects(_routes):
            reconnect_lifecycle.append("pause")
            return ["D1"], ""

        watcher_calls = {"count": 0}

        async def descriptor_failure(_ssh, _routes, **_kwargs):
            watcher_calls["count"] += 1
            ready_event = _kwargs.get("ready_event")
            if ready_event is not None:
                ready_event.set()
            if watcher_calls["count"] == 1:
                return {"success": True, "attached": [{"busid": "1-1"}]}
            return {
                "success": False,
                "attached": [],
                "pending": [{"source_host": "172.16.14.66", "busid": "1-1"}],
                "errors": {
                    "172.16.14.66/1-1": (
                        "Windows USB descriptor enumeration failed (0000:0002)"
                    )
                },
                "source_list": (
                    "Connected:\nBUSID  VID:PID    DEVICE              STATE\n"
                    "1-1    0000:0002  Unknown USB Device "
                    "(Device Descriptor Request Failed)    Allowed\n"
                ),
            }

        loader_checks = {"count": 0}

        async def loader_state(_ssh, _cmd, _count, **_kwargs):
            loader_checks["count"] += 1
            if loader_checks["count"] == 1:
                return True, "List of rockusb connected(1)"
            return False, "List of rockusb connected(0)"

        with patch("scp.SCPClient"), patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="",
        ), patch.object(
            firmware_api, "_prepare_usbip_firmware_routes",
            side_effect=prepare_routes,
        ), patch.object(
            firmware_api, "_capture_rockusb_route_baseline",
            side_effect=capture_baseline,
        ), patch.object(
            firmware_api, "_pause_usbip_reconnects",
            side_effect=pause_reconnects,
        ), patch.object(
            firmware_api, "_reattach_usbip_after_rockusb_reset",
            side_effect=descriptor_failure,
        ), patch.object(
            firmware_api, "_wait_for_rockusb_loaders",
            side_effect=loader_state,
        ), patch.object(
            firmware_api, "_defer_usbip_reconnects",
            side_effect=lambda devices: reconnect_lifecycle.append(
                ("defer", tuple(devices))
            ),
        ), patch.object(
            firmware_api, "_resume_usbip_reconnects",
            side_effect=lambda devices: reconnect_lifecycle.append(
                ("resume", tuple(devices))
            ),
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/srv/update.img", "burn_mode": "uf"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("禁止进入upgrade_tool", response.json()["error"])
        self.assertEqual(watcher_calls["count"], 0)
        self.assertEqual(reconnect_lifecycle, [])

    def test_unexpected_error_after_lock_releases_devices(self):
        released = []

        async def record_release(client_id, devices):
            released.append((client_id, devices))

        runtime.configure_runtime(release_firmware_devices=record_release)
        with patch(
            "features.firmware.firmware_api.os.path.exists",
            side_effect=RuntimeError("stat failed"),
        ):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"firmware_path": "/tmp/update.img"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(released, [("codex@127.0.0.1", ["D1"])])

    def test_invalid_staged_firmware_is_rejected_before_loader(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        with TemporaryDirectory() as tmp, patch(
            "features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT",
            tmp,
        ), patch(
            "features.firmware.firmware_api.validate_local_update_image",
            return_value=firmware_api.FirmwareValidationResult(
                valid=False,
                message="固件预检失败，设备尚未重启：Loader 哈希校验失败。",
            ),
        ):
            session = Path(
                firmware_api._firmware_upload_session_dir(
                    "codex@127.0.0.1",
                    "invalid-loader",
                )
            )
            session.mkdir(parents=True)
            (session / "upload_metadata.json").write_text(
                '{"file_name":"update.img","total_chunks":1,"file_size":8}',
                encoding="utf-8",
            )
            (session / "staged-update.img").write_bytes(b"firmware")

            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={"finalize_upload": "1", "upload_id": "invalid-loader"},
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("设备尚未重启", response.json()["error"])
        self.assertFalse(any("reboot loader" in command for command, _ in fake_ssh.commands))

    def test_gsi_relative_vendor_image_resolves_under_suite_dir(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_FOUND__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        resolved, error = firmware_api._resolve_gsi_remote_image(
            fake_ssh,
            "/home/hcq/GMS-Suite",
            "vendor_boot-debug.img",
            "Vendor boot image",
        )

        self.assertIsNone(error)
        self.assertEqual(resolved, "/home/hcq/GMS-Suite/vendor_boot-debug.img")
        self.assertIn(
            "test -f /home/hcq/GMS-Suite/vendor_boot-debug.img",
            fake_ssh.commands[0][0],
        )

    def test_gsi_missing_relative_vendor_image_returns_error(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        resolved, error = firmware_api._resolve_gsi_remote_image(
            fake_ssh,
            "/home/hcq/GMS-Suite",
            "vendor_boot-debug.img",
            "Vendor boot image",
        )

        self.assertIsNone(resolved)
        self.assertEqual(
            error,
            "Vendor boot image not found: /home/hcq/GMS-Suite/vendor_boot-debug.img",
        )

    def test_firmware_chunk_upload_can_resume_without_locking_devices(self):
        lock_calls = []

        async def record_lock(**kwargs):
            lock_calls.append(kwargs)
            return ["D1"], None

        runtime.configure_runtime(lock_firmware_devices=record_lock)

        with TemporaryDirectory() as tmp, patch("features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT", tmp):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "chunk_index": "0",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8", "chunk_size": "4",
                },
                files={"file": ("update.img", b"1234")},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertFalse(payload["upload_complete"])
            self.assertEqual(payload["chunks_uploaded"], 1)
            self.assertEqual(lock_calls, [])

            resume = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "check_chunks": "1",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8", "chunk_size": "4",
                },
            )
            resume_payload = resume.json()
            self.assertEqual(resume_payload["uploaded_chunks"], [0])
            self.assertEqual(resume_payload["chunks_uploaded"], 1)
            self.assertEqual(resume_payload["total_chunks"], 2)
            self.assertEqual(resume_payload["progress"], 50.0)
            self.assertEqual(resume_payload["uploaded_size"], 4)
            self.assertEqual(resume_payload["total_size"], 8)

    def test_failed_chunk_merge_releases_merge_lock(self):
        async def save_chunk(_upload, path, _max_size=None):
            Path(path).write_bytes(b'1234')
            return 4

        with (
            TemporaryDirectory() as tmp,
            patch('features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT', tmp),
            patch(
                'features.firmware.chunk_uploads.save_upload_to_path',
                side_effect=save_chunk,
            ),
            patch(
                'features.firmware.chunk_uploads.merge_files_to_path',
                side_effect=OSError('disk full'),
            ),
        ):
            with self.assertRaisesRegex(OSError, 'disk full'):
                asyncio.run(
                    firmware_api._handle_firmware_chunk_upload(
                        {
                            'upload_id': 'failed-merge', 'file_name': 'update.img',
                            'chunk_index': '0',
                            'total_chunks': '1', 'file_size': '4',
                            'chunk_size': '4',
                            'file': object(),
                        },
                        'client',
                    )
                )

            lock_path = firmware_api._firmware_upload_session_dir(
                'client', 'failed-merge'
            )
            self.assertFalse(os.path.exists(os.path.join(lock_path, '.merge.lock')))

    def test_firmware_chunk_upload_rejects_changed_session_metadata(self):
        with TemporaryDirectory() as tmp, patch(
            'features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT',
            tmp,
        ):
            first = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'chunk_index': '0', 'total_chunks': '2',
                    'upload_id': 'upload-1', 'file_name': 'update.img',
                    'file_size': '8', 'chunk_size': '4',
                },
                files={'file': ('update.img', b'1234')},
            )
            changed = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'chunk_index': '1', 'total_chunks': '3',
                    'upload_id': 'upload-1', 'file_name': 'other.img',
                    'file_size': '12', 'chunk_size': '4',
                },
                files={'file': ('other.img', b'5678')},
            )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(changed.status_code, 400)
            self.assertIn('metadata', changed.json()['error'])

    def test_firmware_chunk_check_rejects_excessive_chunk_count(self):
        with TemporaryDirectory() as tmp, patch(
            'features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT',
            tmp,
        ):
            response = self.client.post(
                '/api/burn/firmware?devices=D1',
                data={
                    'check_chunks': '1',
                    'total_chunks': str(firmware_api.MAX_FIRMWARE_CHUNKS + 1),
                    'upload_id': 'upload-1',
                    'file_name': 'update.img',
                    'file_size': '8',
                    'chunk_size': '4',
                },
            )

            self.assertEqual(response.status_code, 400)

    def test_create_firmware_share_persists_remote_record(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            with (
                patch(
                    "features.firmware.shares_api._stat_remote",
                    return_value={
                        "host": "10.10.10.206",
                        "user": "hcq",
                        "path": "/home/hcq/build/Image-rk3576s_u-user",
                        "filename": "Image-rk3576s_u-user",
                        "size": 1234,
                        "mtime": 100,
                    },
                ),
                patch(
                    "features.firmware.shares_api.get_client_username_from_request",
                    return_value="tester",
                ),
            ):
                response = self.client.post(
                    "/api/firmware-shares",
                    json={
                        "remote": "hcq@10.10.10.206:/home/hcq/build/Image-rk3576s_u-user",
                        "name": "daily build",
                    },
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["data"]["record"]["name"], "daily build")
            self.assertEqual(payload["data"]["record"]["size"], 1234)
            self.assertRegex(
                payload["data"]["record"]["id"],
                r"^[0-9a-f]{32}$",
            )
            self.assertNotIn("password", payload["data"]["record"])
            stored = shares_api._load_records()
            self.assertIsNone(stored[0].get("password"))

    def test_create_firmware_share_stores_supplied_password_for_download(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            with (
                patch(
                    "features.firmware.shares_api._stat_remote",
                    return_value={
                        "host": "10.10.10.206",
                        "user": "hcq",
                        "path": "/home/hcq/build/update.img",
                        "filename": "update.img",
                        "size": 1234,
                        "mtime": 100,
                    },
                ),
                patch(
                    "features.firmware.shares_api.get_client_username_from_request",
                    return_value="tester",
                ),
            ):
                self.client.post(
                    "/api/firmware-shares",
                    json={
                        "remote": "hcq@10.10.10.206:/home/hcq/build/update.img",
                        "password": "s3cret",
                    },
                )

            stored = shares_api._load_records()
            self.assertIsNone(stored[0].get("password"))
            self.assertTrue(stored[0].get("password_encrypted"))
            self.assertEqual(
                shares_api._record_password(stored[0]), "s3cret"
            )

    def test_update_firmware_share_credentials_stores_password(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            shares_api._save_records([{
                "id": "share1",
                "name": "update.img",
                "host": "10.10.10.206",
                "user": "hcq",
                "path": "/home/hcq/build/update.img",
                "filename": "update.img",
                "size": 1234,
                "mtime": 100,
            }])
            with patch(
                "features.firmware.shares_api._stat_remote",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build/update.img",
                    "filename": "update.img",
                    "size": 1234,
                    "mtime": 100,
                },
            ) as stat_remote:
                response = self.client.post(
                    "/api/firmware-shares/share1/credentials",
                    json={"password": "fixed"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertIsNone(shares_api._load_records()[0].get("password"))
            self.assertTrue(shares_api._load_records()[0].get("password_encrypted"))
            self.assertEqual(
                shares_api._record_password(shares_api._load_records()[0]), "fixed"
            )
            self.assertEqual(stat_remote.call_args.kwargs["password"], "fixed")

    def test_firmware_share_check_uses_stored_password(self):
        with TemporaryDirectory() as tmp:
            runtime.configure_runtime(firmware_share_store=f"{tmp}/shares.json")
            shares_api._save_records([{
                "id": "share1",
                "name": "update.img",
                "host": "10.10.10.206",
                "user": "hcq",
                "path": "/home/hcq/build/update.img",
                "filename": "update.img",
                "size": 1234,
                "mtime": 100,
                "password": "stored",
            }])
            with patch(
                "features.firmware.shares_api._stat_remote",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build/update.img",
                    "filename": "update.img",
                    "size": 1234,
                    "mtime": 100,
                },
            ) as stat_remote:
                response = self.client.get("/api/firmware-shares/share1/check")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(stat_remote.call_args.kwargs["password"], "stored")

    def test_firmware_share_browse_uses_remote_directory_listing(self):
        with (
            patch(
                "features.firmware.shares_api._list_remote_dir",
                return_value={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build",
                    "files": [{"name": "Image-rk3576s_u-userdebug", "type": "file", "size": 10}],
                },
            ),
        ):
            response = self.client.post(
                "/api/firmware-shares/browse",
                json={"host": "10.10.10.206", "user": "hcq", "path": "/home/hcq/build"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["files"][0]["name"], "Image-rk3576s_u-userdebug")

    def test_firmware_share_browse_auth_failure_returns_401(self):
        with patch(
            "features.firmware.shares_api._list_remote_dir",
            side_effect=shares_api._AuthError("远端认证失败（用户名/密码/密钥不匹配）"),
        ):
            response = self.client.post(
                "/api/firmware-shares/browse",
                json={"host": "10.10.10.206", "user": "hcq", "path": "/home/hcq/build"},
            )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["success"])

    def test_firmware_share_browse_forwards_password(self):
        captured = {}

        def _spy(host, user, path, config, password=None):
            captured["password"] = password
            return {"host": host, "user": user, "path": path, "files": []}

        with patch("features.firmware.shares_api._list_remote_dir", side_effect=_spy):
            self.client.post(
                "/api/firmware-shares/browse",
                json={
                    "host": "10.10.10.206",
                    "user": "hcq",
                    "path": "/home/hcq/build",
                    "password": "s3cret",
                },
            )

        self.assertEqual(captured["password"], "s3cret")

    def test_sftp_client_prefers_supplied_password(self):
        captured = {}

        class _FakeSftp:
            def stat(self, path):
                captured["called"] = True

            def close(self):
                pass

        class _FakeClient:
            def __init__(self):
                self._connect_kwargs = None

            def set_missing_host_key_policy(self, policy):
                pass

            def load_system_host_keys(self):
                pass

            def load_host_keys(self, path):
                pass

            def connect(self, **kwargs):
                self._connect_kwargs = kwargs
                captured["password"] = kwargs.get("password")

            def open_sftp(self):
                return _FakeSftp()

            def close(self):
                pass

        with (
            patch("features.firmware.shares_api.paramiko.SSHClient", return_value=_FakeClient()),
            patch("features.firmware.shares_api._host_credentials", return_value={
                "hostname": "10.10.10.206", "username": "hcq", "password": None,
                "key_filename": None, "port": 22,
            }),shares_api._sftp_client("10.10.10.206", "hcq", {}, password="override")
        ):
            pass

        self.assertEqual(captured["password"], "override")
