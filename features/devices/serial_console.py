"""Controller-local serial console discovery, capture, and persistence."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import errno
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pyudev

from foundation.config import settings

from .serial_console_storage import (
    SUPPORTED_NEWLINES,
    BindingStore,
    load_serial_module,
    utc_now,
    validate_newline,
    validate_port_key,
)


logger = logging.getLogger(__name__)

DEFAULT_MAX_LOG_BYTES = 50 * 1024 * 1024
DEFAULT_LOG_RETENTION_DAYS = 14
DATE_RE = re.compile(r"^\d{8}$")


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[str]


@dataclass
class _PortRuntime:
    backlog: deque[str] = field(default_factory=lambda: deque(maxlen=4000))
    decoder: Any = field(default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace"))
    subscribers: dict[str, _Subscriber] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    wake_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    handle: Any = None
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    log_lock: threading.RLock = field(default_factory=threading.RLock)
    online: bool = False
    capture_active: bool = False
    error: str = ""
    last_output_at: str = ""
    last_retention_check: float = 0.0


class SerialConsoleService:
    def __init__(
        self,
        data_root: str | Path,
        *,
        serial_factory=None,
        udev_context_factory=None,
        by_id_root: str | Path = "/dev/serial/by-id",
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
        retry_interval: float = 2.0,
    ) -> None:
        self.root = Path(data_root) / "devices_console"
        self.logs_root = self.root / "logs"
        self.store = BindingStore(self.root / "bindings.json")
        self.serial_factory = serial_factory
        self.udev_context_factory = udev_context_factory or pyudev.Context
        self.by_id_root = Path(by_id_root)
        self.max_log_bytes = max(1, int(max_log_bytes))
        self.retention_days = max(1, int(retention_days))
        self.retry_interval = max(0.01, float(retry_interval))
        self._lock = threading.RLock()
        self._runtimes: dict[str, _PortRuntime] = {}
        self._port_cache: dict[str, dict[str, Any]] = {}
        self._devname_to_key: dict[str, str] = {}
        self._observer = None
        self._started = False

    def configure_data_root(self, data_root: str | Path) -> None:
        """Point the singleton at the lifespan-owned runtime directory."""
        with self._lock:
            if self._started:
                raise RuntimeError("串口服务运行中，无法切换数据目录")
            self.root = Path(data_root) / "devices_console"
            self.logs_root = self.root / "logs"
            self.store = BindingStore(self.root / "bindings.json")
            self._runtimes.clear()
            self._port_cache.clear()
            self._devname_to_key.clear()

    def _runtime(self, port_key: str) -> _PortRuntime:
        key = validate_port_key(port_key)
        with self._lock:
            return self._runtimes.setdefault(key, _PortRuntime())

    def _by_id_paths(self) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            entries = list(self.by_id_root.iterdir())
        except OSError:
            return result
        for entry in entries:
            try:
                result[os.path.realpath(entry)] = str(entry)
            except OSError:
                continue
        return result

    @staticmethod
    def _device_value(device, name: str, default: str = "") -> str:
        properties = getattr(device, "properties", {}) or {}
        value = properties.get(name, default)
        return str(value or default)

    @staticmethod
    def _device_driver(device) -> str:
        current = device
        while current is not None:
            driver = getattr(current, "driver", None)
            if driver:
                return str(driver)
            current = getattr(current, "parent", None)
        return ""

    def _physical_ports(self) -> list[dict[str, Any]]:
        by_target = self._by_id_paths()
        ports: list[dict[str, Any]] = []
        try:
            devices = self.udev_context_factory().list_devices(subsystem="tty")
        except Exception as exc:
            logger.warning("Unable to enumerate serial ports: %s", exc)
            return ports
        for device in devices:
            devname = str(getattr(device, "device_node", "") or "")
            if not (devname.startswith("/dev/ttyUSB") or devname.startswith("/dev/ttyACM")):
                continue
            by_id = by_target.get(os.path.realpath(devname), "")
            port_key = validate_port_key(Path(by_id or devname).name)
            vid = self._device_value(device, "ID_VENDOR_ID").lower()
            pid = self._device_value(device, "ID_MODEL_ID").lower()
            ports.append(
                {
                    "port_key": port_key,
                    "devname": devname,
                    "by_id": by_id,
                    "vid": vid,
                    "pid": pid,
                    "vendor_product": f"{vid}:{pid}" if vid and pid else "",
                    "driver": self._device_driver(device),
                    "usb_path": self._device_value(
                        device, "ID_PATH", str(getattr(device, "sys_path", "") or "")
                    ),
                    "online": True,
                }
            )
        ports.sort(key=lambda item: (item["devname"], item["port_key"]))
        with self._lock:
            self._port_cache = {item["port_key"]: dict(item) for item in ports}
            self._devname_to_key.update(
                {item["devname"]: item["port_key"] for item in ports}
            )
            present = set(self._port_cache)
            for key, runtime in self._runtimes.items():
                runtime.online = key in present
        return ports

    def list_ports(self) -> list[dict[str, Any]]:
        physical = {item["port_key"]: item for item in self._physical_ports()}
        bindings = self.store.list()
        result = []
        for key in sorted(set(physical) | set(bindings)):
            port = dict(physical.get(key) or {
                "port_key": key,
                "devname": "",
                "by_id": "",
                "vid": "",
                "pid": "",
                "vendor_product": "",
                "driver": "",
                "usb_path": "",
                "online": False,
            })
            binding = bindings.get(key)
            runtime = self._runtime(key)
            with self._lock:
                port.update(
                    {
                        "binding": dict(binding) if binding else None,
                        "capture_enabled": bool(binding and binding.get("capture_enabled")),
                        "capture_active": runtime.capture_active,
                        "console_clients": len(runtime.subscribers),
                        "last_output_at": runtime.last_output_at,
                        "error": runtime.error,
                    }
                )
            result.append(port)
        return result

    def update_binding(self, port_key: str, value: dict[str, Any]) -> dict[str, Any]:
        key = validate_port_key(port_key)
        previous = self.store.get(key)
        binding = self.store.upsert(key, value)
        needs_restart = previous and (
            previous.get("baudrate") != binding["baudrate"]
            or previous.get("capture_enabled") != binding["capture_enabled"]
        )
        if needs_restart:
            self._stop_worker(key)
        if binding["capture_enabled"] or self._subscriber_count(key):
            self._ensure_worker(key)
        return binding

    def delete_binding(self, port_key: str) -> bool:
        key = validate_port_key(port_key)
        runtime = self._runtime(key)
        with self._lock:
            if runtime.subscribers:
                raise RuntimeError("控制台仍有连接，无法删除绑定")
        self._stop_worker(key)
        return self.store.delete(key)

    def set_capture(self, port_key: str, enabled: bool) -> dict[str, Any]:
        key = validate_port_key(port_key)
        binding = self.store.get(key)
        if not binding:
            raise KeyError("请先绑定串口")
        binding["capture_enabled"] = bool(enabled)
        updated = self.update_binding(key, binding)
        if not enabled and not self._subscriber_count(key):
            self._stop_worker(key)
        return updated

    def _subscriber_count(self, port_key: str) -> int:
        runtime = self._runtime(port_key)
        with self._lock:
            return len(runtime.subscribers)

    def _desired(self, port_key: str) -> bool:
        binding = self.store.get(port_key)
        return bool(binding and binding.get("capture_enabled")) or bool(
            self._subscriber_count(port_key)
        )

    def _serial_constructor(self):
        if self.serial_factory is not None:
            return self.serial_factory
        return load_serial_module().Serial

    @staticmethod
    def _friendly_serial_error(exc: Exception) -> str:
        number = getattr(exc, "errno", None)
        text = str(exc)
        lowered = text.lower()
        if number in {errno.EACCES, errno.EPERM} or "permission denied" in lowered:
            return "串口权限不足：服务用户需加入 dialout 组并重新登录"
        if number == errno.EBUSY or "resource busy" in lowered or "device or resource busy" in lowered:
            return "串口被占用，请关闭 picocom/minicom 等程序"
        if "no such file" in lowered or number == errno.ENOENT:
            return "串口已拔出或设备节点不存在"
        if number in {errno.EIO, getattr(errno, "EPROTO", 71)} or any(
            marker in lowered for marker in ("input/output error", "protocol error")
        ):
            return "USB 串口通信异常：请重新插拔 FTDI 或更换 USB 端口，系统将自动重连"
        return f"串口错误：{text}"

    def _resolve_port(self, port_key: str) -> dict[str, Any] | None:
        key = validate_port_key(port_key)
        return next((item for item in self._physical_ports() if item["port_key"] == key), None)

    def _ensure_worker(self, port_key: str) -> None:
        key = validate_port_key(port_key)
        if not self.store.get(key):
            raise KeyError("请先绑定串口")
        runtime = self._runtime(key)
        with self._lock:
            if runtime.thread and runtime.thread.is_alive():
                runtime.wake_event.set()
                return
            runtime.stop_event = threading.Event()
            runtime.wake_event = threading.Event()
            runtime.thread = threading.Thread(
                target=self._capture_loop,
                args=(key, runtime),
                name=f"SerialConsole-{key[:32]}",
                daemon=True,
            )
            runtime.thread.start()

    def _close_handle(self, runtime: _PortRuntime) -> None:
        with self._lock:
            handle = runtime.handle
            runtime.handle = None
            runtime.capture_active = False
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()

    def _stop_worker(self, port_key: str) -> None:
        runtime = self._runtime(port_key)
        runtime.stop_event.set()
        runtime.wake_event.set()
        self._close_handle(runtime)
        thread = runtime.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            if runtime.thread is thread and (not thread or not thread.is_alive()):
                runtime.thread = None

    def _wait_retry(self, runtime: _PortRuntime, delay: float) -> None:
        runtime.wake_event.wait(delay)
        runtime.wake_event.clear()

    def _capture_loop(self, port_key: str, runtime: _PortRuntime) -> None:
        retry_delay = self.retry_interval
        try:
            while not runtime.stop_event.is_set() and self._desired(port_key):
                binding = self.store.get(port_key)
                port = self._resolve_port(port_key)
                if not binding or not port:
                    with self._lock:
                        runtime.online = False
                        runtime.error = "串口离线，等待重新插入"
                    self._wait_retry(runtime, retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
                    continue
                handle = None
                try:
                    constructor = self._serial_constructor()
                    handle = constructor(
                        port=port["by_id"] or port["devname"],
                        baudrate=binding["baudrate"],
                        timeout=0.2,
                        write_timeout=1,
                    )
                    with self._lock:
                        runtime.handle = handle
                        runtime.online = True
                        runtime.capture_active = True
                        runtime.error = ""
                    retry_delay = self.retry_interval
                    while not runtime.stop_event.is_set() and self._desired(port_key):
                        # 句柄已被并发关闭（控制台断开/停止采集/热插拔）时
                        # 直接退出本轮，不把关闭后的读错误当作串口故障。
                        if runtime.handle is not handle:
                            break
                        size = max(1, min(int(getattr(handle, "in_waiting", 0) or 1), 65536))
                        data = handle.read(size)
                        if not data:
                            continue
                        self._record_data(port_key, runtime, bytes(data))
                except Exception as exc:
                    # pyserial close() 先置 fd=None 再置 is_open=False，竞态窗口内
                    # read()/in_waiting 会抛 "'NoneType' object cannot be
                    # interpreted as an integer"。主动停止或句柄已被外部接管的
                    # 关闭属预期行为，不写入 runtime.error。
                    intentional_close = (
                        runtime.stop_event.is_set()
                        or (handle is not None and runtime.handle is not handle)
                    )
                    if not intentional_close:
                        with self._lock:
                            runtime.error = self._friendly_serial_error(exc)
                    self._close_handle(runtime)
                    if runtime.stop_event.is_set() or not self._desired(port_key):
                        break
                    self._wait_retry(runtime, retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
                finally:
                    self._close_handle(runtime)
        finally:
            self._close_handle(runtime)
            with self._lock:
                if runtime.thread is threading.current_thread():
                    runtime.thread = None

    @staticmethod
    def _offer(queue: asyncio.Queue[str], text: str) -> None:
        if queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(text)

    def _record_data(self, port_key: str, runtime: _PortRuntime, data: bytes) -> None:
        text = runtime.decoder.decode(data)
        timestamp = utc_now()
        with self._lock:
            runtime.backlog.extend(text.splitlines(keepends=True) or [text])
            runtime.last_output_at = timestamp
            subscribers = list(runtime.subscribers.values())
        for subscriber in subscribers:
            with contextlib.suppress(RuntimeError):
                subscriber.loop.call_soon_threadsafe(self._offer, subscriber.queue, text)
        binding = self.store.get(port_key)
        if binding and binding.get("capture_enabled"):
            self._append_log(port_key, runtime, data)

    def _port_log_dir(self, port_key: str) -> Path:
        return self.logs_root / validate_port_key(port_key)

    def _append_log(self, port_key: str, runtime: _PortRuntime, data: bytes) -> None:
        with runtime.log_lock:
            directory = self._port_log_dir(port_key)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{datetime.now().strftime('%Y%m%d')}.log"
            with open(path, "ab") as stream:
                stream.write(data)
            now = time.monotonic()
            if now - runtime.last_retention_check >= 60:
                runtime.last_retention_check = now
                self.cleanup_retention(port_key)

    def cleanup_retention(self, port_key: str) -> None:
        runtime = self._runtime(port_key)
        with runtime.log_lock:
            self._cleanup_retention_unlocked(port_key)

    def _cleanup_retention_unlocked(self, port_key: str) -> None:
        directory = self._port_log_dir(port_key)
        if not directory.exists():
            return
        cutoff = datetime.now().date() - timedelta(days=self.retention_days)
        files = sorted(path for path in directory.glob("*.log") if DATE_RE.fullmatch(path.stem))
        for path in list(files):
            try:
                if datetime.strptime(path.stem, "%Y%m%d").date() < cutoff:
                    path.unlink()
                    files.remove(path)
            except (OSError, ValueError):
                continue
        total = sum(path.stat().st_size for path in files if path.exists())
        for path in files:
            if total <= self.max_log_bytes:
                break
            size = path.stat().st_size
            if size >= total and size > self.max_log_bytes:
                with open(path, "rb") as source:
                    source.seek(-self.max_log_bytes, os.SEEK_END)
                    tail = source.read()
                temporary = path.with_suffix(".log.tmp")
                temporary.write_bytes(tail)
                os.replace(temporary, path)
                total = len(tail)
            else:
                path.unlink()
                total -= size

    def list_log_dates(self, port_key: str) -> list[str]:
        directory = self._port_log_dir(port_key)
        if not directory.exists():
            return []
        return sorted(
            (path.stem for path in directory.glob("*.log") if DATE_RE.fullmatch(path.stem)),
            reverse=True,
        )

    def log_path(self, port_key: str, date: str | None = None) -> Path:
        dates = self.list_log_dates(port_key)
        selected = str(date or (dates[0] if dates else datetime.now().strftime("%Y%m%d")))
        if not DATE_RE.fullmatch(selected):
            raise ValueError("日志日期格式必须是 yyyymmdd")
        return self._port_log_dir(port_key) / f"{selected}.log"

    def read_log(self, port_key: str, *, date: str | None = None, tail: int = 500) -> dict[str, Any]:
        limit = max(1, min(int(tail), 10_000))
        path = self.log_path(port_key, date)
        if not path.is_file():
            return {"date": path.stem, "content": "", "lines": 0, "available_dates": self.list_log_dates(port_key)}
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 2 * 1024 * 1024))
            content = stream.read().decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)[-limit:]
        return {
            "date": path.stem,
            "content": "".join(lines),
            "lines": len(lines),
            "available_dates": self.list_log_dates(port_key),
        }

    def clear_logs(self, port_key: str) -> int:
        runtime = self._runtime(port_key)
        with runtime.log_lock:
            directory = self._port_log_dir(port_key)
            if not directory.exists():
                return 0
            count = sum(1 for path in directory.glob("*.log") if path.is_file())
            shutil.rmtree(directory)
            return count

    async def subscribe(self, port_key: str) -> tuple[str, asyncio.Queue[str], str]:
        key = validate_port_key(port_key)
        if not self.store.get(key):
            raise KeyError("请先绑定串口")
        runtime = self._runtime(key)
        subscriber_id = uuid.uuid4().hex
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        with self._lock:
            runtime.subscribers[subscriber_id] = _Subscriber(
                loop=asyncio.get_running_loop(), queue=queue
            )
            backlog = "".join(runtime.backlog)
        self._ensure_worker(key)
        return subscriber_id, queue, backlog

    def unsubscribe(self, port_key: str, subscriber_id: str) -> None:
        key = validate_port_key(port_key)
        runtime = self._runtime(key)
        with self._lock:
            runtime.subscribers.pop(subscriber_id, None)
        if not self._desired(key):
            self._stop_worker(key)

    def write(self, port_key: str, data: str, *, append_newline: bool = False) -> int:
        key = validate_port_key(port_key)
        binding = self.store.get(key)
        if not binding:
            raise KeyError("请先绑定串口")
        value = str(data)
        if append_newline:
            value += SUPPORTED_NEWLINES[validate_newline(binding.get("newline", "cr"))]
        encoded = value.encode("utf-8")
        if not encoded or len(encoded) > 4096:
            raise ValueError("单次串口输入必须为 1 到 4096 字节")
        runtime = self._runtime(key)
        with self._lock:
            handle = runtime.handle
        if handle is None:
            raise RuntimeError(runtime.error or "串口尚未连接")
        try:
            with runtime.io_lock:
                return int(handle.write(encoded))
        except Exception as exc:
            raise RuntimeError(self._friendly_serial_error(exc)) from exc

    def handle_udev_event(self, action: str, device) -> None:
        devname = str(getattr(device, "device_node", "") or "")
        if not (devname.startswith("/dev/ttyUSB") or devname.startswith("/dev/ttyACM")):
            return
        if action == "remove":
            with self._lock:
                key = self._devname_to_key.get(devname, Path(devname).name)
                runtime = self._runtimes.get(key)
                if runtime:
                    runtime.online = False
                    runtime.error = "串口已拔出，等待重新插入"
                    runtime.wake_event.set()
            if runtime:
                self._close_handle(runtime)
            return
        if action == "add":
            self._physical_ports()
            for key, binding in self.store.list().items():
                if binding.get("capture_enabled") or self._subscriber_count(key):
                    self._ensure_worker(key)

    def _udev_callback(self, device) -> None:
        self.handle_udev_event(str(getattr(device, "action", "") or ""), device)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._physical_ports()
        for key, binding in self.store.list().items():
            if binding.get("capture_enabled"):
                self._ensure_worker(key)
        try:
            context = self.udev_context_factory()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="tty")
            self._observer = pyudev.MonitorObserver(
                monitor, callback=self._udev_callback, name="SerialConsole-Udev"
            )
            self._observer.start()
        except Exception:
            logger.exception("Failed to start serial-console hotplug monitor")

    def stop(self) -> None:
        observer = self._observer
        self._observer = None
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.stop()
        with self._lock:
            keys = list(self._runtimes)
            self._started = False
        for key in keys:
            self._stop_worker(key)


serial_console_service = SerialConsoleService(settings.data_root)
