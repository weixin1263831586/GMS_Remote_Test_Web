from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from features.devices import serial_console_api
from features.devices.serial_console import SerialConsoleService
from features.devices.serial_console_access import device_serial_availability, visible_ports
from features.devices.serial_console_identity import serial_port_identity
from features.devices.serial_console_storage import BindingStore


class _FakeDevice:
    def __init__(self, device_node: str, *, action: str = "", driver: str = "ftdi_sio"):
        self.device_node = device_node
        self.action = action
        self.driver = driver
        self.parent = None
        self.sys_path = "/sys/devices/pci/usb1/1-1/ttyUSB0"
        self.properties = {
            "ID_VENDOR_ID": "0403",
            "ID_MODEL_ID": "6001",
            "ID_PATH": "pci-0000:00:14.0-usb-0:1:1.0",
        }


class _FakeContext:
    def __init__(self, devices):
        self.devices = devices

    def list_devices(self, *, subsystem: str):
        if subsystem != "tty":
            return []
        return list(self.devices)


class _FakeSerial:
    def __init__(self, chunks: list[bytes]):
        self.chunks = list(chunks)
        self.writes: list[bytes] = []
        self.closed = False
        self.data_read = threading.Event()

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, _size: int) -> bytes:
        if self.closed:
            raise OSError("port closed")
        if self.chunks:
            value = self.chunks.pop(0)
            self.data_read.set()
            return value
        time.sleep(0.01)
        return b""

    def write(self, value: bytes) -> int:
        self.writes.append(value)
        return len(value)

    def close(self):
        self.closed = True


class _CloseRaceSerial(_FakeSerial):
    """复现 pyserial close() 竞态：fd 已置 None、is_open 仍为 True 时 read() 抛 TypeError。"""

    def __init__(self, chunks: list[bytes]):
        super().__init__(chunks)
        self.read_blocked = threading.Event()
        self.closed_event = threading.Event()

    def read(self, size: int) -> bytes:
        if self.chunks:
            return super().read(size)
        # 模拟阻塞在 os.read() 的捕获线程：close() 并发发生后才抛出竞态错误。
        self.read_blocked.set()
        self.closed_event.wait(timeout=5)
        raise TypeError("'NoneType' object cannot be interpreted as an integer")

    def close(self):
        super().close()
        self.closed_event.set()


class SerialConsoleStoreTests(unittest.TestCase):
    def test_binding_round_trip_update_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            store = BindingStore(path)
            saved = store.upsert(
                "usb-FTDI_A-if00-port0",
                {
                    "label": "RK board",
                    "note": "rack 2",
                    "baudrate": 1_500_000,
                    "capture_enabled": True,
                    "newline": "cr",
                },
            )
            self.assertEqual(saved["baudrate"], 1_500_000)
            self.assertTrue(saved["capture_enabled"])
            self.assertEqual(saved["binding_version"], 2)
            self.assertFalse(saved["identity_verified"])
            self.assertEqual(
                BindingStore(path).get("usb-FTDI_A-if00-port0")["label"],
                "RK board",
            )
            self.assertTrue(store.delete("usb-FTDI_A-if00-port0"))
            self.assertIsNone(store.get("usb-FTDI_A-if00-port0"))

    def test_structured_binding_caches_device_and_worker_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / "bindings.json")
            saved = store.upsert(
                "usb-FTDI_A-if00-port0",
                {"device_id": "DEVICE-1", "worker_id": "worker-a"},
            )
            self.assertEqual(saved["device_id"], "DEVICE-1")
            self.assertEqual(saved["worker_id"], "worker-a")
            self.assertTrue(saved["identity_verified"])

    def test_invalid_baudrate_and_port_key_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BindingStore(Path(directory) / "bindings.json")
            with self.assertRaisesRegex(ValueError, "波特率"):
                store.upsert("ttyUSB0", {"baudrate": 20})
            with self.assertRaisesRegex(ValueError, "串口标识"):
                store.upsert("../ttyUSB0", {"baudrate": 115200})


class SerialConsoleServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.by_id = Path(self.temp.name) / "by-id"
        self.by_id.mkdir()
        self.stable_name = "usb-FTDI_FT232R_USB_UART_A6022883-if00-port0"
        os.symlink("/dev/ttyUSB0", self.by_id / self.stable_name)
        self.devices = [_FakeDevice("/dev/ttyUSB0")]

    def tearDown(self):
        self.temp.cleanup()

    def make_service(self, *, serial_factory=None, **kwargs):
        return SerialConsoleService(
            self.temp.name,
            serial_factory=serial_factory,
            udev_context_factory=lambda: _FakeContext(self.devices),
            by_id_root=self.by_id,
            retry_interval=0.01,
            **kwargs,
        )

    def test_enumeration_prefers_by_id_and_exposes_usb_metadata(self):
        service = self.make_service()
        ports = service.list_ports()
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0]["port_key"], self.stable_name)
        self.assertEqual(ports[0]["by_id"], str(self.by_id / self.stable_name))
        self.assertEqual(ports[0]["vendor_product"], "0403:6001")
        self.assertEqual(ports[0]["driver"], "ftdi_sio")

    def test_enumeration_uses_usb_path_instead_of_unstable_tty_number(self):
        (self.by_id / self.stable_name).unlink()
        service = self.make_service()
        port = service.list_ports()[0]
        expected = serial_port_identity(
            by_id="",
            devname="/dev/ttyUSB0",
            usb_path="pci-0000:00:14.0-usb-0:1:1.0",
        )
        self.assertEqual(port["port_key"], expected["port_key"])
        self.assertEqual(port["identity_source"], "usb-path")
        self.assertTrue(port["identity_stable"])

    def test_device_availability_requires_explicit_binding(self):
        local_worker = "ats-worker-controller"
        unbound = [{"port_key": "p1", "online": True, "binding": None}]
        result = device_serial_availability(
            unbound, device_id="D1", worker_id=local_worker
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["state"], "unbound")
        bound = [{
            "port_key": "p1", "devname": "/dev/ttyUSB0", "online": True,
            "capture_active": True, "last_output_at": "2026-09-23T00:00:00Z",
            "binding": {"device_id": "D1", "worker_id": local_worker},
        }]
        result = device_serial_availability(
            bound, device_id="D1", worker_id=local_worker
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["confidence"], "output_verified")

    def test_usb_io_error_has_actionable_message(self):
        error = OSError(5, "Input/output error")
        self.assertEqual(
            self.make_service()._friendly_serial_error(error),
            "USB 串口通信异常：请重新插拔 FTDI 或更换 USB 端口，系统将自动重连",
        )

    def test_capture_populates_backlog_log_and_accepts_console_input(self):
        handle = _FakeSerial([b"U-Boot 2024\n", b"Starting kernel\n"])
        service = self.make_service(serial_factory=lambda **_kwargs: handle)
        service.update_binding(
            self.stable_name,
            {
                "label": "RK3572",
                "baudrate": 1_500_000,
                "capture_enabled": True,
                "newline": "cr",
            },
        )
        try:
            self.assertTrue(handle.data_read.wait(timeout=1))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if "Starting kernel" in service.read_log(self.stable_name)["content"]:
                    break
                time.sleep(0.01)
            log = service.read_log(self.stable_name)
            self.assertIn("U-Boot 2024", log["content"])
            self.assertIn("Starting kernel", log["content"])
            runtime = service._runtime(self.stable_name)
            self.assertIn("U-Boot 2024", "".join(runtime.backlog))
            self.assertEqual(service.write(self.stable_name, "help", append_newline=True), 5)
            self.assertEqual(handle.writes, [b"help\r"])
        finally:
            service.stop()

    def test_console_subscription_reads_without_persistent_capture(self):
        handle = _FakeSerial([b"loader> "])
        service = self.make_service(serial_factory=lambda **_kwargs: handle)
        service.update_binding(
            self.stable_name,
            {"baudrate": 115200, "capture_enabled": False},
        )

        async def exercise():
            subscriber_id, queue, _backlog = await service.subscribe(self.stable_name)
            data = await asyncio.wait_for(queue.get(), timeout=1)
            service.unsubscribe(self.stable_name, subscriber_id)
            return data

        try:
            self.assertEqual(asyncio.run(exercise()), "loader> ")
            self.assertEqual(service.list_log_dates(self.stable_name), [])
        finally:
            service.stop()

    def test_delete_binding_disconnects_an_open_console(self):
        service = self.make_service(serial_factory=lambda **_kwargs: _FakeSerial([]))
        service.update_binding(
            self.stable_name,
            {"baudrate": 115200, "capture_enabled": False},
        )

        async def exercise():
            subscriber_id, queue, _backlog = await service.subscribe(self.stable_name)
            self.assertTrue(service.delete_binding(self.stable_name))
            self.assertIsNone(await asyncio.wait_for(queue.get(), timeout=1))
            service.unsubscribe(self.stable_name, subscriber_id)

        try:
            asyncio.run(exercise())
            self.assertIsNone(service.store.get(self.stable_name))
        finally:
            service.stop()

    def test_concurrent_close_race_does_not_record_spurious_error(self):
        handles: list[_CloseRaceSerial] = []

        def factory(**_kwargs) -> _CloseRaceSerial:
            handle = _CloseRaceSerial([b"U-Boot 2024\n"])
            handles.append(handle)
            return handle

        service = self.make_service(serial_factory=factory)
        service.update_binding(
            self.stable_name,
            {"baudrate": 115200, "capture_enabled": False},
        )

        async def exercise():
            subscriber_id, _queue, _backlog = await service.subscribe(self.stable_name)
            return subscriber_id

        try:
            subscriber_id = asyncio.run(exercise())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not handles:
                time.sleep(0.01)
            self.assertTrue(handles, "捕获线程未创建串口句柄")
            self.assertTrue(handles[0].data_read.wait(timeout=2))
            # 等捕获线程阻塞在 read() 内，再走生产路径：用户点击控制台
            # “关闭”→ 退订 → 无订阅且无采集 → _stop_worker 并发关句柄。
            self.assertTrue(handles[0].read_blocked.wait(timeout=2))
            service.unsubscribe(self.stable_name, subscriber_id)
            runtime = service._runtime(self.stable_name)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and runtime.thread and runtime.thread.is_alive():
                time.sleep(0.01)
            # pyserial close() 竞态窗口内 read() 抛出的 TypeError 不应
            # 被当作串口故障残留在 runtime.error 中展示给用户。
            self.assertEqual(runtime.error, "")
            self.assertIn("U-Boot 2024", "".join(runtime.backlog))
        finally:
            service.stop()

    def test_utf8_character_split_across_reads_is_not_corrupted(self):
        service = self.make_service()
        runtime = service._runtime(self.stable_name)
        encoded = "启动完成\n".encode()
        service._record_data(self.stable_name, runtime, encoded[:2])
        service._record_data(self.stable_name, runtime, encoded[2:])
        self.assertEqual("".join(runtime.backlog), "启动完成\n")

    def test_retention_removes_old_and_caps_total_bytes(self):
        service = self.make_service(max_log_bytes=10, retention_days=14)
        directory = service._port_log_dir(self.stable_name)
        directory.mkdir(parents=True)
        old = datetime.now().date() - timedelta(days=30)
        yesterday = datetime.now().date() - timedelta(days=1)
        today = datetime.now().date()
        (directory / f"{old:%Y%m%d}.log").write_bytes(b"old")
        (directory / f"{yesterday:%Y%m%d}.log").write_bytes(b"12345678")
        (directory / f"{today:%Y%m%d}.log").write_bytes(b"abcdefgh")
        service.cleanup_retention(self.stable_name)
        files = list(directory.glob("*.log"))
        self.assertFalse((directory / f"{old:%Y%m%d}.log").exists())
        self.assertLessEqual(sum(path.stat().st_size for path in files), 10)

    def test_hotplug_remove_and_add_updates_runtime_state(self):
        service = self.make_service()
        service.list_ports()
        runtime = service._runtime(self.stable_name)
        runtime.online = True
        self.devices.clear()
        service.handle_udev_event("remove", _FakeDevice("/dev/ttyUSB0", action="remove"))
        self.assertFalse(runtime.online)
        self.assertIn("拔出", runtime.error)
        self.devices.append(_FakeDevice("/dev/ttyUSB0", action="add"))
        service.handle_udev_event("add", self.devices[0])
        self.assertTrue(runtime.online)


class _ApiService:
    def __init__(self, log_path: Path):
        self.log_file = log_path
        self.binding = {
            "label": "board",
            "note": "",
            "baudrate": 1_500_000,
            "capture_enabled": False,
            "newline": "cr",
        }

    def list_ports(self):
        return [{"port_key": "ttyUSB0", "devname": "/dev/ttyUSB0", "binding": None}]

    def update_binding(self, _key, value):
        self.binding.update(value)
        return dict(self.binding)

    def delete_binding(self, _key):
        return True

    def set_capture(self, _key, enabled):
        self.binding["capture_enabled"] = enabled
        return dict(self.binding)

    def read_log(self, _key, *, date=None, tail=500):
        return {"date": date or "20260910", "content": "boot\n", "lines": 1, "available_dates": ["20260910"]}

    def log_path(self, _key, _date=None):
        return self.log_file

    def clear_logs(self, _key):
        return 1


class SerialConsoleApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp.name) / "20260910.log"
        self.log_path.write_text("boot\n", encoding="utf-8")
        self.service = _ApiService(self.log_path)
        app = FastAPI()
        app.include_router(serial_console_api.router)
        app.include_router(serial_console_api.page_router)
        self.patch = patch.object(
            serial_console_api, "serial_console_service", self.service
        )
        self.patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.patch.stop()
        self.temp.cleanup()

    def test_rest_endpoints(self):
        self.assertEqual(
            self.client.get("/api/devices/console/ports").json()["data"]["count"], 1
        )
        availability = self.client.get(
            "/api/devices/console/availability/DEVICE-1"
        ).json()["data"]
        self.assertEqual(availability["state"], "unbound")
        self.assertFalse(availability["available"])
        response = self.client.put(
            "/api/devices/console/bindings/ttyUSB0",
            json={
                "label": "DEVICE-1", "device_id": "DEVICE-1",
                "baudrate": 115200, "capture_enabled": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["binding"]["baudrate"], 115200)
        self.assertEqual(
            self.client.post("/api/devices/console/ports/ttyUSB0/capture/stop").status_code,
            200,
        )
        logs = self.client.get(
            "/api/devices/console/ports/ttyUSB0/logs?date=20260910&tail=200"
        )
        self.assertEqual(logs.json()["data"]["content"], "boot\n")
        self.assertEqual(
            self.client.get(
                "/api/devices/console/ports/ttyUSB0/logs/download?date=20260910"
            ).content,
            b"boot\n",
        )
        self.assertEqual(
            self.client.delete("/api/devices/console/ports/ttyUSB0/logs").status_code,
            200,
        )
        self.assertEqual(
            self.client.delete("/api/devices/console/bindings/ttyUSB0").status_code,
            200,
        )

    def test_empty_log_read_carries_capture_status_hint(self):
        # 空结果必须能区分「没接线 / 没绑定 / 没开采集」。
        self.service.read_log = lambda _key, **_kw: {
            "date": "20260910",
            "content": "",
            "lines": 0,
            "available_dates": [],
        }
        logs = self.client.get("/api/devices/console/ports/ttyUSB0/logs")
        self.assertEqual(logs.status_code, 200, logs.text)
        data = logs.json()["data"]
        self.assertEqual(data["lines"], 0)
        status = data["capture_status"]
        self.assertFalse(status["bound"])
        self.assertIn("绑定", status["hint"])

        self.service.list_ports = lambda: [
            {
                "port_key": "ttyUSB0",
                "devname": "/dev/ttyUSB0",
                "online": True,
                "binding": {"label": "board"},
                "capture_enabled": True,
                "capture_active": True,
                "error": None,
            }
        ]
        idle = self.client.get("/api/devices/console/ports/ttyUSB0/logs")
        status = idle.json()["data"]["capture_status"]
        self.assertTrue(status["bound"])
        self.assertTrue(status["device_online"])
        self.assertTrue(status["capture_enabled"])
        self.assertIn("波特率", status["hint"])

        self.service.list_ports = lambda: [
            {
                "port_key": "ttyUSB0", "devname": "/dev/ttyUSB0",
                "online": True, "binding": {"label": "board"},
                "capture_enabled": True, "capture_active": False,
                "error": "串口被占用",
            }
        ]
        busy = self.client.get("/api/devices/console/ports/ttyUSB0/logs")
        self.assertIn("被占用", busy.json()["data"]["capture_status"]["hint"])

        self.service.list_ports = lambda: [
            {
                "port_key": "ttyUSB0",
                "devname": "/dev/ttyUSB0",
                "online": False,
                "binding": {"label": "board"},
                "capture_enabled": True,
                "capture_active": False,
                "error": "拔出",
            }
        ]
        offline = self.client.get("/api/devices/console/ports/ttyUSB0/logs")
        status = offline.json()["data"]["capture_status"]
        self.assertFalse(status["device_online"])
        self.assertIn("不在线", status["hint"])

    def test_agent_console_read_requires_devices_read_scope(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_method="agent_token"))
        user = object()
        scope_dependency = Mock(return_value=user)
        with patch.object(
            serial_console_api,
            "require_authenticated_user_when_auth_required",
            return_value=user,
        ), patch.object(
            serial_console_api,
            "require_agent_scope",
            return_value=scope_dependency,
        ) as require_scope:
            result = serial_console_api._require_console_read(request)

        self.assertIs(result, user)
        require_scope.assert_called_once_with("devices.read")
        scope_dependency.assert_called_once_with(request)

    def test_agent_port_visibility_applies_worker_and_device_acl(self):
        request = SimpleNamespace(
            state=SimpleNamespace(
                auth_method="agent_token",
                agent_token_record={
                    "allowed_workers": "worker-a",
                    "allowed_devices": "D1",
                },
            )
        )
        ports = [
            {"port_key": "p1", "binding": {"worker_id": "worker-a", "device_id": "D1"}},
            {"port_key": "p2", "binding": {"worker_id": "worker-a", "device_id": "D2"}},
            {"port_key": "p3", "binding": None},
        ]
        self.assertEqual([item["port_key"] for item in visible_ports(request, ports)], ["p1"])

    def test_embedded_page_inlines_assets(self):
        response = self.client.get("/devices-console")
        self.assertEqual(response.status_code, 200)
        self.assertIn("设备串口", response.text)
        self.assertNotIn("{{CONSOLE_JS}}", response.text)

    def test_websocket_rejects_failed_handshake_authentication(self):
        with patch.object(
            serial_console_api,
            "validate_websocket_request",
            return_value=(None, 4401),
        ), self.assertRaises(WebSocketDisconnect) as raised, self.client.websocket_connect(
            "/api/devices/console/ws/ttyUSB0"
        ):
            pass
        self.assertEqual(raised.exception.code, 4401)
