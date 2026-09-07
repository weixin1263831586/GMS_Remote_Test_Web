"""Device-facing worker actions: probing, USB/IP, allow-listed ops, flashing, metrics.

从 inventory.py 拆出（2026-08 审核第七节）：设备探测/USB/IP/设备操作/固件与
GSI 烧写/主机指标。套件执行逻辑见 suite_actions.py。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from foundation.native_tools import resolve_native_tool
from foundation.transport_contract import execute_external_transport

from .config import WorkerConfig
from .fastboot_workflow import FastbootPreparer, subprocess_runner, vendor_partition


logger = logging.getLogger(__name__)


def _run(
    argv: list[str],
    timeout: int = 10,
    env: dict[str, str] | None = None,
) -> str:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _adb_environment(adb_server_socket: str | None) -> dict[str, str] | None:
    if not adb_server_socket:
        return None
    env = dict(os.environ)
    env["ADB_SERVER_SOCKET"] = adb_server_socket
    return env


# 设备详情缓存：heartbeat 高频调用 probe_devices，但每台设备的详情探测
# 是一次串行 ADB 往返（超时 3s）。多设备 + ADB 卡顿时会拖过 Controller 的
# offline 阈值造成假离线。详情（model/Android 版本/电量/SoC）变化缓慢，
# 因此 heartbeat 与详情探测彻底解耦：
# - 后台 enrichment 线程按有界并发（4）逐台刷新，每台设备有独立的
#   next_refresh_at（错峰，避免 cache stampede 同时过期）；
# - heartbeat/probe_devices 只读缓存快照，永远不做同步 ADB 往返；
#   缓存未就绪时对应字段为空，最坏情况是详情晚到一轮 heartbeat。
_DEVICE_DETAILS_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_DEVICE_DETAILS_TTL_SECONDS = 60.0
_DEVICE_DETAILS_ENRICH_CONCURRENCY = 4
# 设备从清单消失后超过该时长即从缓存/刷新计划中淘汰：
# 不淘汰会让后台线程对大量历史 serial 持续做无效 ADB 探测
# （每台最坏 3s timeout），逐渐占满 enrichment 线程。
_DEVICE_DETAILS_EVICTION_SECONDS = 180.0
_ENRICH_MIN_INTERVAL_SECONDS = 15.0
_details_enrich_lock = threading.Lock()
_details_refresh_at: dict[str, float] = {}
_details_last_seen_at: dict[str, float] = {}
_details_enrich_thread: threading.Thread | None = None
_details_enrich_stop = threading.Event()
_adb_socket_for_enrich: list[str | None] = [None]


def _probe_device_details_uncached(
    serial: str, adb_server_socket: str | None = None
) -> dict[str, str]:
    output = _run([
        "adb", "-s", serial, "shell",
        "echo __MODEL__; getprop ro.product.model; "
        "echo __ANDROID__; getprop ro.build.version.release; "
        "echo __BATTERY__; dumpsys battery | grep '^  level:' | head -n 1; "
        "echo __SOC__; getprop ro.soc.model",
    ], timeout=3, env=_adb_environment(adb_server_socket))
    markers = {
        "__MODEL__": "model",
        "__ANDROID__": "android_version",
        "__BATTERY__": "battery_level",
        "__SOC__": "soc_model",
    }
    details: dict[str, str] = {}
    current = ""
    for line in output.splitlines():
        value = line.strip()
        if value in markers:
            current = markers[value]
            continue
        if not current or not value or current in details:
            continue
        if current == "battery_level":
            value = value.partition(":")[2].strip() if ":" in value else value
        details[current] = value
    return details


def _enrich_details_worker() -> None:
    """后台刷新详情：有界并发（4）+ 每台独立错峰刷新周期 + stale 淘汰。"""
    from concurrent.futures import ThreadPoolExecutor

    while not _details_enrich_stop.is_set():
        now = time.monotonic()
        # 淘汰长期未出现在 probe_devices 清单中的设备。
        for serial in [
            serial for serial, last_seen in list(_details_last_seen_at.items())
            if now - last_seen > _DEVICE_DETAILS_EVICTION_SECONDS
        ]:
            _DEVICE_DETAILS_CACHE.pop(serial, None)
            _details_refresh_at.pop(serial, None)
            _details_last_seen_at.pop(serial, None)
        due = [
            serial
            for serial, (_, _) in list(_DEVICE_DETAILS_CACHE.items())
            if _details_refresh_at.get(serial, 0.0) <= now
        ]
        if not due:
            _details_enrich_stop.wait(2.0)
            continue
        # 错峰：把 next_refresh_at 展开到 TTL 区间内，避免所有设备
        # 同一时刻集体过期（cache stampede）。探测本身以并发 4 执行。
        stagger = _DEVICE_DETAILS_TTL_SECONDS / max(len(due), 1)

        def _refresh(serial: str, index: int, stagger_step: float = stagger) -> None:
            details = _probe_device_details_uncached(
                serial, _adb_socket_for_enrich[0])
            _DEVICE_DETAILS_CACHE[serial] = (time.monotonic(), details)
            _details_refresh_at[serial] = (
                time.monotonic()
                + min(_DEVICE_DETAILS_TTL_SECONDS,
                      _ENRICH_MIN_INTERVAL_SECONDS + index * stagger_step)
            )

        with ThreadPoolExecutor(
            max_workers=_DEVICE_DETAILS_ENRICH_CONCURRENCY,
            thread_name_prefix="device-details",
        ) as executor:
            futures = [
                executor.submit(_refresh, serial, index)
                for index, serial in enumerate(due)
            ]
            # 不设整体死线：设备多时排在其后的 future 等待时间 =
            # (N/并发-1) × 3s，固定 timeout 会让最后一台必然超时并
            # 杀死 enrichment 线程（且设备集不变时不会重启）。
            # 单次探测自身有 3s timeout，不会永久卡住。
            for future in futures:
                try:
                    future.result()
                except Exception:
                    logger.warning(
                        "device detail enrichment failed for a batch",
                        exc_info=True,
                    )
        _details_enrich_stop.wait(0.5)


def ensure_details_enrichment_started(adb_server_socket: str | None = None) -> None:
    """启动（幂等）后台详情探测线程。"""
    global _details_enrich_thread
    with _details_enrich_lock:
        _adb_socket_for_enrich[0] = adb_server_socket
        if _details_enrich_thread is None or not _details_enrich_thread.is_alive():
            _details_enrich_stop.clear()
            _details_enrich_thread = threading.Thread(
                target=_enrich_details_worker,
                name="device-details-enrich", daemon=True)
            _details_enrich_thread.start()


def seed_device_details(serial: str) -> None:
    """设备出现时注册/续期：纳入刷新计划并记录 last_seen（供淘汰）。"""
    _details_last_seen_at[serial] = time.monotonic()
    if serial not in _DEVICE_DETAILS_CACHE:
        _DEVICE_DETAILS_CACHE[serial] = (0.0, {})
        _details_refresh_at[serial] = 0.0
    ensure_details_enrichment_started(_adb_socket_for_enrich[0])


def _probe_device_details(
    serial: str,
    adb_server_socket: str | None = None,
) -> dict[str, str]:
    """只读缓存快照（探测由后台线程完成，绝不同步阻塞调用方）。"""
    _adb_socket_for_enrich[0] = adb_server_socket or _adb_socket_for_enrich[0]
    cached = _DEVICE_DETAILS_CACHE.get(serial)
    if cached and cached[1]:
        return cached[1]
    seed_device_details(serial)
    return {}


def probe_devices(
    include_details: bool = False,
    adb_server_socket: str | None = None,
) -> list[dict[str, Any]]:
    proxy_imports = {}
    if not adb_server_socket:
        try:
            from .adb_proxy import imported_devices, sync_source_policy

            sync_source_policy()
            proxy_imports = imported_devices()
        except (OSError, RuntimeError, ValueError):
            proxy_imports = {}
    devices = []
    adb_env = _adb_environment(adb_server_socket)
    for line in _run(
        ["adb", "devices", "-l"],
        env=adb_env,
    ).splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] not in {"device", "offline", "unauthorized"}:
            continue
        serial, adb_state = parts[0], parts[1]
        proxy_source = next(
            (
                metadata
                for imported, metadata in proxy_imports.items()
                if serial == imported or serial.endswith(f":{imported}")
            ),
            None,
        )
        # Skip localhost:<port> devices that are NOT ADB Proxy imports.
        # These are Microdroid/vsock virtual machines created by GTS/VTS
        # virtualization tests.  ADB Proxy imported devices also use
        # localhost:<port> serials and must be preserved.
        if serial.startswith("localhost:") and not proxy_source:
            continue
        properties = {}
        for item in parts[2:]:
            key, separator, value = item.partition(":")
            if separator:
                properties[key] = value
        if proxy_source:
            properties.update({
                "adb_proxy_source_worker_id": proxy_source["source_worker_id"],
                "adb_proxy_source_address": proxy_source["source_address"],
                "adb_proxy_source_serial": proxy_source["source_serial"],
            })
        if include_details and adb_state == "device":
            seed_device_details(serial)
            properties.update(
                _probe_device_details(serial, adb_server_socket)
            )
        devices.append({
            "serial": serial,
            "transport": "adb_proxy" if proxy_source else "local_usb",
            "state": "available" if adb_state == "device" else adb_state,
            "properties": properties,
        })
    # Fastboot-only devices must remain visible to the controller.
    known = {item["serial"] for item in devices}
    for line in _run(["fastboot", "devices"]).splitlines():
        serial = line.split()[0] if line.split() else ""
        if serial and serial not in known:
            devices.append({"serial": serial, "transport": "local_usb",
                            "state": "fastboot", "properties": {}})
    return devices


def execute_usbip_action(
    action: str,
    source_host: str,
    busids: list[str],
    adb_server_socket: str | None = None,
    generation: int = 0,
) -> dict[str, Any]:
    """Execute USB/IP exclusively through the required Rust executor."""
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,255}", source_host or ""):
        raise ValueError("invalid USB/IP source host")
    selected = []
    for raw in busids or []:
        busid = str(raw).strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", busid):
            raise ValueError(f"invalid USB/IP busid: {busid}")
        selected.append(busid)
    if not selected:
        raise ValueError("at least one USB/IP busid is required")
    command = resolve_native_tool("GMS_USBIP_CONTROL_BIN", "gms-usbip-control")
    return execute_external_transport(
        command,
        transport="usbip",
        action=action,
        payload={
            "source_host": source_host,
            "busids": selected,
            "adb_server_socket": adb_server_socket or "",
            "generation": max(0, int(generation or 0)),
        },
        timeout=180 if action == "attach" else 60,
    )

def execute_device_action(action: str, device_ids: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one strictly allow-listed Android device operation."""
    from features.cluster.device_action_spec import (
        adb_proxy_forbidden_device_actions as _proxy_forbidden_builder,
    )
    from features.cluster.device_action_spec import (
        inspection_device_actions as _inspection_builder,
    )
    _proxy_forbidden = _proxy_forbidden_builder()
    _inspection_actions = _inspection_builder()

    attached = {item["serial"]: item for item in probe_devices()}
    serials = []
    for device_id in device_ids:
        serial = str(device_id).split(":", 1)[-1]
        if serial not in attached:
            raise ValueError(f"device is not attached to this worker: {serial}")
        if (
            attached[serial].get("transport") == "adb_proxy"
            and action in _proxy_forbidden
        ):
            raise ValueError(
                f"ADB Proxy remote device has no local USB/Fastboot channel: {serial}"
            )
        serials.append(serial)
    options = options or {}
    if action in _inspection_actions:
        if len(serials) != 1:
            raise ValueError(f"{action} requires exactly one device")
        from .android_inspection import execute_inspection_action

        return execute_inspection_action(action, serials[0], options)
    if action == "scrcpy_start":
        bundled_scrcpy = Path(__file__).resolve().parent.parent / "tools" / "scrcpy-linux-x86_64-v3.3.4" / "scrcpy"
        executable = os.getenv("GMS_WORKER_SCRCPY_PATH") or shutil.which("scrcpy")
        if not executable and bundled_scrcpy.is_file() and os.access(bundled_scrcpy, os.X_OK):
            executable = str(bundled_scrcpy)
        if not executable:
            raise RuntimeError("scrcpy is not installed on this Worker")
        display = str(options.get("display") or os.getenv("DISPLAY") or ":0")
        # 与 Controller 使用相同的 scrcpy 窗口布局。
        screen_width = 1920
        screen_height = 1080
        max_window_width = 350
        gap = 20
        total = len(serials)
        window_width = (
            min(max_window_width, (screen_width - gap * (total + 1)) // total)
            if total
            else max_window_width
        )
        window_height = int(window_width * 16 / 9)
        max_height = int(screen_height * 0.7)
        if window_height > max_height:
            window_height = max_height
            window_width = int(window_height * 9 / 16)
        total_width = total * window_width + max(total - 1, 0) * gap
        start_x = max(gap, (screen_width - total_width) // 2)
        start_y = max(50, (screen_height - window_height) // 2)
        results = []
        for index, serial in enumerate(serials):
            # 精确匹配参数，避免影响其他设备的镜像进程。
            already_running = False
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    argv = (proc / "cmdline").read_bytes().split(b"\0")
                except (OSError, PermissionError):
                    continue
                decoded = [item.decode(errors="replace") for item in argv if item]
                if decoded and Path(decoded[0]).name == "scrcpy" and "-s" in decoded:
                    position = decoded.index("-s")
                    if position + 1 < len(decoded) and decoded[position + 1] == serial:
                        already_running = True
                        break
            if already_running:
                results.append({"device": serial, "success": True, "already_running": True})
                continue
            env = dict(os.environ)
            env["DISPLAY"] = display
            x_offset = start_x + index * (window_width + gap)
            process = subprocess.Popen(
                [executable, "-s", serial, "--window-title", f"GMS {serial}",
                 "--window-x", str(x_offset), "--window-y", str(start_y),
                 "--window-width", str(window_width), "--window-height", str(window_height),
                 "--max-size", "800", "--no-audio"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=env,
            )
            results.append({"device": serial, "success": True, "pid": process.pid,
                            "already_running": False, "display": display})
        return {"results": results, "summary": {"total": len(results),
                "success": sum(bool(item["success"]) for item in results), "failed": 0}}
    if action == "screenshot":
        if len(serials) != 1:
            raise ValueError("screenshot requires exactly one device")
        completed = subprocess.run(["adb", "-s", serials[0], "exec-out", "screencap", "-p"],
                                   capture_output=True, timeout=30, check=False)
        if completed.returncode != 0 or not completed.stdout:
            raise RuntimeError((completed.stderr or b"screenshot failed").decode(errors="replace"))
        return {"serial": serials[0], "image": "data:image/png;base64," +
                base64.b64encode(completed.stdout).decode("ascii")}
    if action == "layout":
        if len(serials) != 1:
            raise ValueError("layout requires exactly one device")
        serial = serials[0]
        remote = f"/data/local/tmp/gms-layout-{os.getpid()}.xml"
        dump = subprocess.run(["adb", "-s", serial, "shell", "uiautomator", "dump", remote],
                              capture_output=True, text=True, timeout=30, check=False)
        read = subprocess.run(["adb", "-s", serial, "exec-out", "cat", remote],
                              capture_output=True, text=True, timeout=30, check=False)
        subprocess.run(["adb", "-s", serial, "shell", "rm", "-f", remote],
                       capture_output=True, timeout=10, check=False)
        if dump.returncode != 0 or read.returncode != 0 or not read.stdout.strip():
            raise RuntimeError((dump.stderr or read.stderr or "layout failed").strip())
        elements = []
        for node in ET.fromstring(read.stdout).iter("node"):
            attrs = node.attrib
            nums = [int(value) for value in re.findall(r"-?\d+", attrs.get("bounds", ""))[:4]]
            center = [(nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2] if len(nums) == 4 else None
            interactions = [name for key, name in (("clickable", "clickable"),
                ("long-clickable", "long_clickable"), ("scrollable", "scrollable"),
                ("checkable", "checkable"), ("focusable", "focusable")) if attrs.get(key) == "true"]
            if attrs.get("text") or attrs.get("content-desc") or attrs.get("resource-id") or interactions:
                elements.append({"text": attrs.get("text", ""),
                    "content_desc": attrs.get("content-desc", ""),
                    "resource_id": attrs.get("resource-id", ""), "class_name": attrs.get("class", ""),
                    "package": attrs.get("package", ""), "center": center,
                    "bounds": attrs.get("bounds", ""), "interactions": interactions,
                    "enabled": attrs.get("enabled", "true") == "true"})
        return {"serial": serial, "source": "worker-uiautomator", "elements": elements}
    if action == "tap":
        if len(serials) != 1 or options.get("x") is None or options.get("y") is None:
            raise ValueError("tap requires one device and coordinates")
        x, y = int(options["x"]), int(options["y"])
        if not 0 <= x <= 10000 or not 0 <= y <= 10000:
            raise ValueError("tap coordinates out of range")
        completed = subprocess.run(["adb", "-s", serials[0], "shell", "input", "tap", str(x), str(y)],
                                   capture_output=True, text=True, timeout=15, check=False)
        return {"serial": serials[0], "x": x, "y": y, "success": completed.returncode == 0,
                "output": (completed.stdout or completed.stderr).strip()}
    if action == "wifi":
        ssid = str(options.get("ssid") or "")
        password = str(options.get("password") or "")
        if not ssid:
            raise ValueError("wifi action requires an SSID")
        results = []
        for serial in serials:
            enabled = subprocess.run(
                ["adb", "-s", serial, "shell", "cmd", "wifi", "set-wifi-enabled", "enabled"],
                capture_output=True, text=True, timeout=20, check=False,
            )
            argv = ["adb", "-s", serial, "shell", "cmd", "wifi", "connect-network", ssid]
            if password:
                argv.extend(["wpa2", password])
            else:
                argv.append("open")
            connected = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            results.append({"device": serial,
                "success": enabled.returncode == 0 and connected.returncode == 0,
                "output": "\n".join(filter(None, [enabled.stdout, enabled.stderr,
                                                    connected.stdout, connected.stderr])).strip()})
        return {"results": results, "summary": {"total": len(results),
                "success": sum(bool(item["success"]) for item in results),
                "failed": sum(not item["success"] for item in results)}}
    results = []
    for serial in serials:
        commands: list[list[str]]
        if action == "reboot":
            commands = [["adb", "-s", serial, "reboot"]]
        elif action == "reboot_bootloader":
            commands = [["adb", "-s", serial, "reboot", "bootloader"]]
        elif action == "remount":
            commands = [["adb", "-s", serial, "root"], ["adb", "-s", serial, "remount"]]
        elif action == "get_properties":
            commands = [["adb", "-s", serial, "shell", "getprop"]]
        elif action == "bootloader_status":
            commands = [["adb", "-s", serial, "shell", "getprop", "ro.boot.verifiedbootstate"]]
        elif action in {"bootloader_lock", "bootloader_unlock"}:
            # ADB 到 Fastboot 的整个过程始终锁定指定序列号。
            subprocess.run(["adb", "-s", serial, "reboot", "bootloader"],
                           capture_output=True, text=True, timeout=30, check=False)
            verb = "lock" if action == "bootloader_lock" else "unlock"
            commands = [["fastboot", "-s", serial, "flashing", verb]]
        else:
            raise ValueError(f"unsupported device action: {action}")
        output = []
        success = True
        for argv in commands:
            try:
                completed = subprocess.run(argv, capture_output=True, text=True,
                                           timeout=30, check=False)
                output.append((completed.stdout or completed.stderr).strip())
                if completed.returncode != 0:
                    success = False
                    break
            except (OSError, subprocess.TimeoutExpired) as exc:
                output.append(str(exc))
                success = False
                break
        results.append({"device": serial, "success": success, "output": "\n".join(output)})
    return {"results": results, "summary": {"total": len(results),
            "success": sum(bool(item["success"]) for item in results),
            "failed": sum(not item["success"] for item in results)}}


def _run_with_output_stream(
    argv: list[str],
    timeout: int = 1800,
    on_output=None,
) -> dict[str, Any]:
    """运行长命令并逐行流式回调输出（实时日志通道）。

    ``on_output(line, is_stderr)`` 由调用方提供（可为 None，退化为
    subprocess.run 行为）。返回 stdout/stderr/returncode 与既有
    subprocess.run 兼容的结构，便于 flash_* 继续复用解析逻辑。
    """
    if on_output is None:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout, check=False)
        return {"stdout": completed.stdout or "", "stderr": completed.stderr or "",
                "returncode": completed.returncode}
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    lines: list[str] = []
    try:
        deadline = time.monotonic() + timeout
        import select
        fd = process.stdout.fileno()
        # 行缓冲在用户态维护：select 就绪后用 os.read 取"当前可用"字节，
        # 绝不使用文本流 readline()——缓冲里只有不完整行（无换行符）时
        # readline 会阻塞等待剩余数据，select 的超时保护被击穿，烧写
        # 卡死时超时杀进程不再生效。
        pending = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise subprocess.TimeoutExpired(argv, timeout)
            ready, _f, _e = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                if process.poll() is not None:
                    break
                continue
            pending += chunk
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                raw, pending = pending[:newline], pending[newline + 1:]
                line = raw.decode("utf-8", errors="replace")
                lines.append(line)
                try:
                    on_output(line.rstrip("\r"), False)
                except Exception:
                    pass
        if pending:
            line = pending.decode("utf-8", errors="replace")
            lines.append(line)
            try:
                on_output(line.rstrip("\r"), False)
            except Exception:
                pass
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    return {"stdout": "\n".join(lines), "stderr": "",
            "returncode": process.returncode}


def flash_firmware(config: WorkerConfig, firmware: Path, device_ids: list[str],
                   on_output=None) -> dict[str, Any]:
    """Flash exactly one locally attached device using a Worker-local image."""
    if len(device_ids) != 1:
        raise ValueError("firmware flashing requires exactly one device")
    serial = str(device_ids[0]).split(":", 1)[-1]
    attached = {item["serial"]: item for item in probe_devices()}
    if serial not in attached:
        raise ValueError("device is not attached to this Worker")
    if attached[serial].get("transport") == "adb_proxy":
        raise ValueError("firmware flashing requires a device attached by local USB")
    firmware = firmware.resolve()
    allowed_root = (config.data_root / "firmware").resolve()
    if not firmware.is_file() or not firmware.is_relative_to(allowed_root):
        raise ValueError("firmware is outside the Worker staging root")
    bundled = Path(__file__).resolve().parent.parent / "tools" / "upgrade_tool"
    tool = Path(os.getenv("GMS_WORKER_UPGRADE_TOOL", str(bundled))).resolve()
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise RuntimeError("upgrade_tool is not installed on this Worker")
    if attached[serial].get("state") == "fastboot":
        subprocess.run(
            ["fastboot", "-s", serial, "reboot"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        deadline = time.monotonic() + 120
        while True:
            adb_devices = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            states = {
                parts[0]: parts[1]
                for line in (adb_devices.stdout or "").splitlines()[1:]
                if len(parts := line.split()) >= 2
            }
            if states.get(serial) == "device":
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Fastboot device did not return to ADB; enter Loader/MaskROM manually"
                )
            time.sleep(2)
    reboot = subprocess.run(["adb", "-s", serial, "reboot", "loader"], capture_output=True,
                            text=True, timeout=30, check=False)
    if reboot.returncode != 0:
        raise RuntimeError((reboot.stderr or reboot.stdout or "failed to enter loader").strip())
    time.sleep(8)
    listed = subprocess.run([str(tool), "ld"], capture_output=True, text=True, timeout=20, check=False)
    match = re.search(r"List of rockusb connected\((\d+)\)", listed.stdout or "")
    if listed.returncode != 0 or not match or int(match.group(1)) != 1:
        raise RuntimeError("expected exactly one RockUSB loader device on Worker")
    completed = _run_with_output_stream([str(tool), "uf", str(firmware)], timeout=1800,
                                        on_output=on_output)
    output = "\n".join(filter(None, [completed["stdout"], completed["stderr"]])).strip()
    return {"device": serial, "success": completed["returncode"] == 0,
            "exit_code": completed["returncode"], "output": output[-20000:]}


def flash_gsi(config: WorkerConfig, system_img: Path | None, vendor_img: Path | None,
              device_ids: list[str], on_output=None) -> dict[str, Any]:
    if len(device_ids) != 1:
        raise ValueError("GSI flashing requires exactly one device")
    if system_img is None and vendor_img is None:
        raise ValueError("GSI flashing requires a system or vendor image")
    serial = str(device_ids[0]).split(":", 1)[-1]
    attached = {item["serial"]: item for item in probe_devices()}
    if serial not in attached:
        raise ValueError("device is not attached to this Worker")
    if attached[serial].get("transport") == "adb_proxy":
        raise ValueError("GSI flashing requires a device attached by local USB")
    allowed = (config.data_root / "firmware").resolve()
    for image in (system_img, vendor_img):
        if image and (not image.resolve().is_relative_to(allowed) or not image.is_file()):
            raise ValueError("GSI image is outside Worker staging root")
    script = Path(__file__).resolve().parent.parent / "scripts" / "run_GSI_Burn.sh"
    if not script.is_file():
        raise RuntimeError("GSI burn script is not installed on Worker")
    misc_img = Path(__file__).resolve().parent.parent / "tools" / "misc.img"
    if not misc_img.is_file():
        raise RuntimeError("misc.img is not installed on Worker")
    prepared = FastbootPreparer(subprocess_runner).prepare_gsi_fastbootd(serial)
    argv = [
        str(script),
        serial,
        prepared.oem_argument("unlock"),
        str(system_img) if system_img else "",
        str(misc_img),
    ]
    if vendor_img:
        argv.extend([vendor_partition(str(vendor_img)), str(vendor_img)])
    completed = _run_with_output_stream(argv, timeout=1800, on_output=on_output)
    output = "\n".join(filter(None, [completed["stdout"], completed["stderr"]])).strip()
    return {"device": serial, "success": completed["returncode"] == 0,
            "exit_code": completed["returncode"], "output": output[-20000:]}


def host_metrics(config: WorkerConfig) -> dict[str, float]:
    usage = shutil.disk_usage(config.data_root.parent if config.data_root.parent.exists() else Path.home())
    memory_percent = 0.0
    memory_total_gb = 0.0
    memory_available_gb = 0.0
    # load 先赋默认值：/proc/meminfo 解析异常跳过时局部变量仍存在，
    # 不需要靠 'load' in locals() 这类脆弱判断。
    load = 0.0
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        memory_percent = 100 * (1 - values["MemAvailable"] / values["MemTotal"])
        memory_total_gb = values["MemTotal"] / 1024 ** 2
        memory_available_gb = values["MemAvailable"] / 1024 ** 2
    except Exception:
        pass
    try:
        load = os.getloadavg()[0]
        cpu_percent = min(100.0, load * 100 / max(1, os.cpu_count() or 1))
    except OSError:
        cpu_percent = 0.0
    return {"cpu_percent": round(cpu_percent, 2),
            "memory_percent": round(memory_percent, 2),
            "memory_total_gb": round(memory_total_gb, 2),
            "memory_available_gb": round(memory_available_gb, 2),
            "load_1m": round(load, 2),
            "disk_free_gb": round(usage.free / 1024 ** 3, 2),
            "disk_total_gb": round(usage.total / 1024 ** 3, 2)}


def browse_directory(path: str = "", default_path: Path | None = None) -> dict[str, Any]:
    """List one directory on the Worker host for the remote file browser.

    空路径时打开 default_path（通常为套件根目录 GMS-Suite），未配置则回落主目录。
    """
    if str(path or "").strip():
        target = Path(path).expanduser()
    else:
        target = Path(default_path).expanduser() if default_path else Path.home()
    target = target.resolve()
    if not target.is_dir():
        raise ValueError("path is not a directory on Worker")
    files = []
    with os.scandir(target) as entries:
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            is_dir = entry.is_dir(follow_symlinks=True)
            files.append({
                "name": entry.name,
                "type": "directory" if is_dir else "file",
                "size": 0 if is_dir else entry_stat.st_size,
                "permissions": stat.filemode(entry_stat.st_mode),
                "modified": int(entry_stat.st_mtime),
            })
    files.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
    return {"path": str(target), "files": files}


def copy_image_into(source: Path, target: Path, max_bytes: int) -> dict[str, Any]:
    """Copy one GSI image into Worker staging, returning size and SHA-256."""
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise ValueError("GSI image source file not found on Worker")
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader, Path(target).open("wb") as writer:
        while block := reader.read(4 * 1024 * 1024):
            total += len(block)
            if total > max_bytes:
                raise ValueError("GSI image exceeds Worker staging limit")
            digest.update(block)
            writer.write(block)
    if not total:
        raise ValueError("GSI image is empty")
    return {"size_bytes": total, "sha256": digest.hexdigest()}
