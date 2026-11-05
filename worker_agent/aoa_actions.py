"""Worker 侧 AOA（Android Open Accessory 2.0）执行器。

不依赖 ADB/root：pyusb 走 libusb 控制传输，把 Worker 主机模拟成 AOAv2
配件（HID 触摸屏/键盘/系统键），向设备发送 UI 输入——用于 user build
烧写后自动重新打开 USB 调试（ADR 0022；对齐 AOSP aoa_helper 的
``AoaDevice``/``UsbHelper`` 行为）。

关键行为（与官方一致）：
- 非配件模式时发送 accessory 信息串 + ACCESSORY_START，等待重枚举，
  最多重试 5 次；
- 按整段命令需要注册触摸屏/键盘/系统键，执行期间保持设备集合稳定；
- 在完整命令之间检查 USB 重枚举并重建 HID；手势中断不自动重发；
- 全程控制传输 bmRequestType=0x40（vendor OUT），串号跨重枚举保持。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from foundation import aoa_profile
from foundation.aoa_calibration import TouchMapping

from .adb_suppression import begin_suppression as _begin_adb_suppression
from .aoa_session import AoaSession, device_gone, register_hids
from .aoa_usb import bound_port, cleanup_diagnostics, cleanup_steps, find_bound_device, validate_binding


logger = logging.getLogger(__name__)

ACCESSORY_START_MAX_RETRIES = 5
USB_TRANSFER_TIMEOUT_MS = 10_000
DEVICE_NOT_FOUND = -4

# accessory 信息串（官方 MANUFACTURER/MODEL/VERSION，id: 0/1/3）
_ACCESSORY_STRINGS: tuple[tuple[int, bytes], ...] = (
    (0, b"Android\0"),
    (1, b"GMS Remote Test AOA\0"),
    (3, b"1.0\0"),
)

# 动作间隔：官方 ACTION_DELAY=3s 过慢，默认 1s，可按机型经环境变量调。
ACTION_DELAY_SECONDS = 1.0
STEP_DELAY_SECONDS = 0.01
CONFIGURE_DELAY_SECONDS = 1.0


class AoaError(RuntimeError):
    """AOA 执行失败（设备不支持/传输错误/超时）。"""


def _usb():
    try:
        import usb.core
        import usb.util

        return usb.core, usb.util
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise AoaError("Worker 缺少 pyusb 依赖，无法执行 AOA 操作") from exc


def auto_touch_allowed_by_policy(policy: str) -> bool:
    """该 mapping_policy 是否允许自动触摸（Worker 侧判定，失败关闭）。"""
    from foundation.aoa_calibration import auto_touch_allowed

    try:
        return auto_touch_allowed(policy)
    except ValueError:
        return False


def _accessory_mode(usb_core, device) -> bool:
    vid, pid = device.idVendor, device.idProduct
    low, high = aoa_profile.AOA_ACCESSORY_PID_RANGE
    return vid == aoa_profile.AOA_GOOGLE_VID and low <= pid <= high


def _open_device(usb_core, serial: str):
    """按串号查找 USB 设备（accessory 模式下串号保持）。"""
    def match(candidate) -> bool:
        try:
            return candidate.serial_number == serial
        except (usb_core.USBError, ValueError):
            # 未授权/无法读描述符的设备跳过；accessory 模式下可读。
            return False

    def find_serial():
        matches = iter(usb_core.find(custom_match=match, find_all=True))
        device = next(matches, None)
        if next(matches, None) is not None:
            raise AoaError(f"ambiguous USB serial on this worker: {serial}")
        return device

    return find_bound_device(usb_core, serial, find_serial)



def _reset_usb(device) -> None:
    validate_binding()
    device.reset()


def _detach_kernel_drivers(device) -> None:
    validate_binding()
    try:
        configuration = device.get_active_configuration()
    except Exception:
        return
    for interface in range(configuration.bNumInterfaces):
        try:
            if device.is_kernel_driver_active(interface):
                device.detach_kernel_driver(interface)
        except Exception:
            continue


def probe_aoa(serial: str) -> dict[str, Any]:
    """检测设备 AOAv2 能力：GET_PROTOCOL 返回版本 ≥2 才支持。"""
    usb_core, usb_util = _usb()
    device = _open_device(usb_core, serial)
    if device is None:
        raise AoaError(f"device is not attached to this worker: {serial}")
    info: dict[str, Any] = {
        "serial": serial,
        "vendor_id": device.idVendor,
        "product_id": device.idProduct,
        "accessory_mode": _accessory_mode(usb_core, device),
    }
    try:
        try:
            protocol = device.ctrl_transfer(
                aoa_profile.AOA_REQUEST_INPUT,
                aoa_profile.AOA_ACCESSORY_GET_PROTOCOL, 0, 0, 2,
                timeout=USB_TRANSFER_TIMEOUT_MS,
            )
            info["aoa_protocol"] = int(protocol[0]) | (int(protocol[1]) << 8)
        except usb_core.USBError as exc:
            info["aoa_protocol"] = None
            info["probe_error"] = f"GET_PROTOCOL failed: {exc}"
        info["aoa_supported"] = bool(info.get("aoa_protocol")) and info["aoa_protocol"] >= 2
    finally:
        usb_util.dispose_resources(device)
    return info


def _wait_adb_state(serial: str, timeout_seconds: int) -> dict[str, Any]:
    """轮询 adb，等待设备进入 device 状态（AOA 执行成功 ≠ ADB 已恢复）。"""
    deadline = time.monotonic() + max(0, int(timeout_seconds))
    last_state = "absent"
    while True:
        completed = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=15, check=False)
        last_state = "absent"
        matches = 0
        for line in (completed.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == serial:
                matches += 1
                last_state = parts[1]
                # 端口字段格式随 platform-tools 版本不同：老版本输出
                # ``usb:1-6``，本机 1.0.41 输出裸 ``1-6``。两种都认，
                # 否则正确的物理端口被误判为 identity_mismatch。
                bound = bound_port()
                if bound and not any(item in {f"usb:{bound}", bound}
                                     for item in parts[2:]):
                    last_state = "identity_mismatch"
        if matches > 1:
            last_state = "ambiguous"
        if getattr(completed, "returncode", 0) != 0:
            last_state = "probe_failed"
        validate_binding()
        if last_state == "device":
            return {"adb_state": "device", "adb_recovered": True,
                    "usb_port_path": bound_port()}
        if time.monotonic() >= deadline:
            return {
                "adb_state": last_state,
                "adb_recovered": False,
                "adb_hint": {
                    "unauthorized": "请在设备上确认 USB 调试授权弹窗",
                    "offline": "ADB 处于 offline，尝试重新插拔或重启 adbd",
                    "absent": "设备未出现，确认已开启 USB 调试并连接",
                }.get(last_state, "ADB 未进入 device 状态"),
            }
        time.sleep(2)


def _transfer(device, request: int, value: int, index: int, data: bytes = b"") -> int:
    validate_binding()
    try:
        return int(device.ctrl_transfer(
            aoa_profile.AOA_REQUEST_OUTPUT, request, value, index, data,
            timeout=USB_TRANSFER_TIMEOUT_MS,
        ))
    except Exception as exc:  # pyusb 把 libusb 错误包装成 USBError
        if "not found" in str(exc).lower() or device_gone(exc):
            return DEVICE_NOT_FOUND
        raise AoaError(f"AOA control transfer failed (request {request}): {exc}") from exc


def _configure_accessory(usb_core, serial: str, timeout_seconds: int = 20):
    """进入配件模式并返回重枚举后的设备句柄（官方 initialize 等价）。"""
    device = _open_device(usb_core, serial)
    deadline = time.monotonic() + timeout_seconds
    # gadget 重枚举/复位窗口内设备会短暂离线（实测 RK3576 恢复出厂后
    # 可超过 3.5s）：起始 absent 时轮询等待同串号设备出现，而不是立即
    # 放弃——上层调用（重枚举重建、闭环阶段衔接）都依赖这一容错。
    while device is None and time.monotonic() <= deadline:
        time.sleep(1.0)
        device = _open_device(usb_core, serial)
    attempts = 0
    while device is not None:
        if _accessory_mode(usb_core, device):
            return device
        if attempts >= ACCESSORY_START_MAX_RETRIES:
            raise AoaError(
                f"failed to start accessory mode on {serial} after {attempts} attempts")
        if time.monotonic() > deadline:
            break
        attempts += 1
        try:
            protocol = device.ctrl_transfer(
                aoa_profile.AOA_REQUEST_INPUT, aoa_profile.AOA_ACCESSORY_GET_PROTOCOL,
                0, 0, 2, timeout=USB_TRANSFER_TIMEOUT_MS)
        except Exception as exc:
            raise AoaError(f"cannot negotiate AOAv2 on {serial}: {exc}") from exc
        if len(protocol) != 2 or (int(protocol[0]) | (int(protocol[1]) << 8)) < 2:
            raise AoaError(f"device does not support AOAv2: {serial}")
        for index, payload in _ACCESSORY_STRINGS:
            _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_STRING, 0, index, payload)
        _transfer(device, aoa_profile.AOA_ACCESSORY_START, 0, 0)
        time.sleep(CONFIGURE_DELAY_SECONDS)
        # 配件模式触发重枚举：轮询等待同串号设备重新出现。
        while time.monotonic() <= deadline:
            device = _open_device(usb_core, serial)
            if device is not None:
                break
            time.sleep(1)
    if device is None:
        raise AoaError(f"AOAv2-compatible device not found: {serial}")
    raise AoaError(f"timed out entering accessory mode: {serial}")


def _adb_run(serial: str, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    validate_binding()
    return subprocess.run(
        ["adb", "-s", serial, *args], capture_output=True, text=True,
        timeout=timeout, check=False)


def _display_size(serial: str) -> tuple[int, int]:
    completed = _adb_run(serial, "shell", "wm", "size", timeout=10)
    sizes: dict[str, tuple[int, int]] = {}
    for line in (completed.stdout or "").splitlines():
        value = line.split(":", 1)[-1].strip()
        if "x" in value and line.strip().startswith(("Physical", "Override")):
            width, _, height = value.partition("x")
            key = "override" if line.strip().startswith("Override") else "physical"
            sizes[key] = (int(width.strip()), int(height.strip()))
    # ``wm size`` 通常先输出 Physical、后输出 Override；必须完整解析后
    # 再选 Override，不能在第一行提前返回。
    if sizes:
        return sizes.get("override") or sizes["physical"]
    raise AoaError(f"cannot determine display size of {serial}: {completed.stdout!r}")


def _dump_elements(
        serial: str, sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[dict], dict]:
    """uiautomator dump 并解析为节点列表（含父索引，用于向上找可点击祖先）。

    uiautomator dump 在动画/窗口切换期间会间歇失败（无 stderr、空输出或
    非零退出），有限次重试并间隔等待界面沉淀（wake/home 前缀后必现窗口）。
    sleep 由调用方注入（录制入口传入可替换实现，测试不真实等待）。
    """
    remote = f"/data/local/tmp/gms-aoa-dump-{os.getpid()}.xml"
    last_error = "uiautomator dump failed"
    # 部分启动器（实机 RK3576 QuickStepGo）动画期间长期拿不到 idle：
    # 5 次 × 2s 窗口仍失败才放弃。重试链内置环境自愈：RK3576 出厂的
    # com.cghs.stresstest（system 常驻）注册全局无障碍客户端，持续监听
    # 窗口变化会让 uiautomator 的 idle 判定永不满足（实机复现）；force-stop
    # 它可立即恢复（应用会自启重新注册，每次换来一段干净窗口）。
    for attempt in range(5):
        dump = _adb_run(serial, "shell", "uiautomator", "dump", remote, timeout=25)
        read = _adb_run(serial, "exec-out", "cat", remote, timeout=15)
        _adb_run(serial, "shell", "rm", "-f", remote, timeout=10)
        # exec-out 会把 cat 的 stderr 并进 stdout：读回内容必须是 XML
        # （cat 报错文本/空文件都按失败重试）。
        if dump.returncode == 0 and read.returncode == 0 \
                and read.stdout.lstrip().startswith("<?xml"):
            break
        last_error = (dump.stderr or read.stderr or read.stdout[:120]
                      or last_error).strip()
        if attempt < 4:
            if "idle" in last_error.lower():
                _adb_run(serial, "shell", "am", "force-stop",
                         "com.cghs.stresstest", timeout=10)
            sleep(2.0)
    else:
        raise AoaError(last_error)
    nodes: list[dict] = []
    parent_of: dict = {}

    def visit(element, parent_index: int | None) -> None:
        index = len(nodes)
        parent_of[index] = parent_index
        attrs = element.attrib
        nums = [int(v) for v in re.findall(r"-?\d+", attrs.get("bounds", ""))[:4]]
        center = (
            (nums[0] + nums[2]) // 2,
            (nums[1] + nums[3]) // 2,
        ) if len(nums) == 4 else None
        nodes.append({
            "text": attrs.get("text", ""),
            "resource_id": attrs.get("resource-id", ""),
            "content_desc": attrs.get("content-desc", ""),
            "class": attrs.get("class", ""),
            "clickable": attrs.get("clickable") == "true",
            "checked": attrs.get("checked") == "true",
            "center": center,
        })
        for child in element:
            visit(child, index)

    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(read.stdout)
    except ET.ParseError as exc:
        raise AoaError(f"uiautomator dump is not valid XML: {exc}") from exc
    visit(root, None)
    return nodes, parent_of


def _node_matches(node: dict, matcher: dict) -> bool:
    text = (node.get("text") or "").strip()
    desc = (node.get("content_desc") or "").strip()
    resource_id = node.get("resource_id") or ""
    wanted_text = matcher.get("text")
    if wanted_text and wanted_text.lower() not in text.lower():
        return False
    any_text = matcher.get("any_text") or []
    if any_text and not any(
            str(option).lower() in text.lower() or str(option).lower() in desc.lower()
            for option in any_text):
        return False
    wanted_desc = matcher.get("content_desc")
    if wanted_desc:
        # 录制脚本允许字符串或多语言候选列表（与 any_text 一致）。
        wanted_descs = (wanted_desc if isinstance(wanted_desc, (list, tuple))
                        else [wanted_desc])
        if not any(str(option).lower() in desc.lower() for option in wanted_descs):
            return False
    wanted_id = matcher.get("resource_id")
    if wanted_id and wanted_id != resource_id:
        return False
    wanted_class = matcher.get("class")
    if wanted_class and (node.get("class") or "") != wanted_class:
        return False
    return True


def _node_matches_exact(node: dict, matcher: dict) -> bool:
    """精确匹配（大小写不敏感）：文本须完全相等，而非子串包含。"""
    text = (node.get("text") or "").strip().lower()
    desc = (node.get("content_desc") or "").strip().lower()
    wanted_text = matcher.get("text")
    if wanted_text and str(wanted_text).strip().lower() != text:
        return False
    any_text = matcher.get("any_text") or []
    if any_text and not any(
            str(option).strip().lower() == text or str(option).strip().lower() == desc
            for option in any_text):
        return False
    wanted_desc = matcher.get("content_desc")
    if wanted_desc:
        wanted_descs = (wanted_desc if isinstance(wanted_desc, (list, tuple))
                        else [wanted_desc])
        if not any(str(option).strip().lower() == desc for option in wanted_descs):
            return False
    wanted_id = matcher.get("resource_id")
    if wanted_id and wanted_id != node.get("resource_id"):
        return False
    wanted_class = matcher.get("class")
    if wanted_class and node.get("class", "") != wanted_class:
        return False
    return True


def _find_tap_point(nodes: list[dict], parent_of: dict, matcher: dict) -> tuple[int, int] | None:
    """返回匹配元素的点击点：优先可点击节点本身，否则向上找可点击祖先。

    两轮扫描：先找文本精确相等的节点（“System”不会误命中
    “System update”），无果再退回子串匹配。
    """
    for exact in (True, False):
        check = _node_matches_exact if exact else _node_matches
        for index, node in enumerate(nodes):
            if not check(node, matcher):
                continue
            current = index
            for _ in range(6):
                candidate = nodes[current]
                if candidate.get("clickable") and candidate.get("center"):
                    return candidate["center"]
                current = parent_of.get(current)
                if current is None:
                    break
            if node.get("center"):
                return node["center"]
    return None


def _visible_texts_summary(nodes: list[dict], limit: int = 12) -> list[str]:
    texts = []
    for node in nodes:
        label = node.get("text") or node.get("content_desc")
        if label and label.strip():
            texts.append(label.strip())
        if len(texts) >= limit:
            break
    return texts


def _register_all_hid(device, touch_mode: str = "pointer") -> None:
    """注册全部 AOA HID 通道（标定与回放共用的注册序列）。"""
    from foundation.aoa_hid_mode import hid_registrations

    for hid_id, descriptor in hid_registrations(touch_mode):
        result = _transfer(
            device, aoa_profile.AOA_ACCESSORY_REGISTER_HID, hid_id, len(descriptor))
        if result == DEVICE_NOT_FOUND:
            raise AoaError(f"device disappeared while registering HID {hid_id}")
        result = _transfer(
            device, aoa_profile.AOA_ACCESSORY_SET_HID_REPORT_DESC,
            hid_id, 0, descriptor)
        if result == DEVICE_NOT_FOUND:
            raise AoaError(f"device disappeared while loading HID {hid_id} descriptor")


def _ensure_native_mode(usb_core, serial: str) -> bool:
    """确认设备已退出 accessory 模式；残留时以驱动卸载+复位强推。

    实机 8e4d 验证：host 侧 ``_reset_usb(device)`` 后设备可能仍以 accessory
    PID（18d1:2D0x）枚举，需先 detach 全部内核驱动再复位才可靠恢复原生
    PID；accessory 模式残留会干扰后续 adb 手势与设备管理。
    """
    for _attempt in range(2):
        device = _open_device(usb_core, serial)
        if device is not None and not _accessory_mode(usb_core, device):
            return True
        if device is None:
            time.sleep(1)
            continue
        try:
            _detach_kernel_drivers(device)
            _reset_usb(device)
        except Exception:
            logger.warning("AOA native-mode reset failed for %s", serial)
        # 复位后重枚举需要时间：过短会在旧枚举句柄上误判 accessory 残留
        # （实机 RK3576：lsusb 已显示原生 PID，检查仍撞上缓存句柄）。
        time.sleep(3.5)
    device = _open_device(usb_core, serial)
    if device is None or _accessory_mode(usb_core, device):
        logger.warning("AOA device %s still in accessory mode after reset", serial)
        return False
    return True


def _calibrate_touch_mapping(
    serial: str,
    display_w: int,
    display_h: int,
    sleep: Callable[[float], None],
    touch_mode: str = "pointer",
) -> tuple[float, float] | None:
    """实测设备的触摸坐标映射（raw→cooked 比例）。

    在 accessory+ADB 模式下注入一次探针触摸（设备中心），从
    ``dumpsys input`` 读取 InputReader 实际分发（cooked）的坐标，
    反解每轴比例。部分设备两轴共用统一比例，按轴假设会系统性偏移。
    任何失败都返回 None（调用方退回按轴换算），设备状态尽力恢复。
    """
    probe_x = aoa_profile.AOA_TOUCH_MAX_X // 2
    probe_y = aoa_profile.AOA_TOUCH_MAX_Y // 2
    usb_core, usb_util = _usb()
    device = None
    probe_down_sent = False
    try:
        # 熄屏时 InputReader 会丢弃外接触摸（实测：HID 发送成功但
        # dumpsys 无探针坐标），标定前必须先亮屏解锁。
        _adb_run(serial, "shell", "input", "keyevent", "224")
        _adb_run(serial, "shell", "wm", "dismiss-keyguard")
        sleep(1.0)
        device = _configure_accessory(usb_core, serial)
        _detach_kernel_drivers(device)
        _register_all_hid(device, touch_mode)
        sleep(CONFIGURE_DELAY_SECONDS)
        result = _transfer(
            device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
            aoa_profile.AOA_HID_TOUCH_SCREEN, 0,
            aoa_profile.touch_report(True, probe_x, probe_y))
        if result == DEVICE_NOT_FOUND:
            raise AoaError("probe touch: device vanished")
        probe_down_sent = True
        # 等 InputReader 处理探针事件后再抓状态。
        sleep(1.5)
        dump = _adb_run(serial, "shell", "dumpsys", "input",
                        timeout=30).stdout or ""
        cooked = _parse_probe_cooked_point(dump, probe_x, probe_y)
        result = _transfer(
            device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
            aoa_profile.AOA_HID_TOUCH_SCREEN, 0,
            aoa_profile.touch_report(False, probe_x, probe_y))
        if result == DEVICE_NOT_FOUND:
            raise AoaError("probe release: device vanished")
        if cooked is None:
            logger.warning("AOA touch calibration: cooked point not found in dumpsys for %s",
                           serial)
            return None
        scale_x = cooked[0] / probe_x
        scale_y = cooked[1] / probe_y
        if scale_x <= 0 or scale_y <= 0:
            return None
        return (scale_x, scale_y)
    except Exception as exc:
        logger.warning("AOA touch calibration failed on %s: %s", serial, exc)
        return None
    finally:
        errors = cleanup_diagnostics()
        steps = []
        if probe_down_sent and device is not None:
            steps += [
                ("release_touch", lambda: _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                    aoa_profile.AOA_HID_TOUCH_SCREEN, 0, aoa_profile.touch_report(False, probe_x, probe_y))),
                ("dismiss_probe_menu", lambda: _adb_run(serial, "shell", "input", "keyevent", "4")),
            ]
        if device is not None:
            steps += [(f"unregister_hid_{hid}", lambda hid=hid: _transfer(
                device, aoa_profile.AOA_ACCESSORY_UNREGISTER_HID, hid, 0))
                for hid, _ in reversed(aoa_profile.AOA_HID_REGISTRATIONS)]
            steps += [("reset_usb", lambda: _reset_usb(device)),
                      ("dispose_usb", lambda: usb_util.dispose_resources(device))]
        steps += [("restore_native_mode", lambda: _ensure_native_mode(usb_core, serial)),
                  ("verify_adb_state", lambda: _wait_adb_state(serial, 20).get("adb_recovered") is True)]
        cleanup_steps(steps, errors)
        if errors:
            logger.warning("AOA calibration cleanup diagnostics on %s: %s", serial, errors)


def _parse_probe_cooked_point(dump: str, probe_x: int, probe_y: int) -> tuple[float, float] | None:
    """从 dumpsys input 中找到探针所在 mapper 的 cooked 坐标。

    按 ``Last Raw Touch:`` 头切分 mapper 块（块的其余字段缩进更深，不会
    出现新的 ``Last Raw Touch`` 头）；含探针坐标 ``x=<probe_x>, y=<probe_y>``
    的块内，紧随的 ``Last Cooked Touch`` 首个指针坐标即分发结果。
    """
    headers = [match.start() for match in re.finditer(r"Last Raw Touch:", dump)]
    for index, start in enumerate(headers):
        end = headers[index + 1] if index + 1 < len(headers) else len(dump)
        block = dump[start:end]
        if f"x={probe_x}, y={probe_y}," not in block:
            continue
        cooked_match = re.search(
            r"Last Cooked Touch: pointerCount=1\s*\[0\]:[^\n]*?x=([0-9.]+), y=([0-9.]+)",
            block)
        if cooked_match:
            return (float(cooked_match.group(1)), float(cooked_match.group(2)))
    return None


# 多点仿射标定的采样点数（ADR 0026：五点布局，四角内缩 + 中心）。
CALIBRATION_PROBE_COUNT = 5


def calibration_probe_points() -> list[tuple[int, int]]:
    """五点 HID 探针布局：四角 25%/75% 内缩 + 中心（aoa_calibration 单一真值）。"""
    from foundation.aoa_calibration import five_point_calibration_points

    return five_point_calibration_points(
        aoa_profile.AOA_TOUCH_MAX_X, aoa_profile.AOA_TOUCH_MAX_Y)


def _calibrate_touch_affine(
    serial: str,
    display_w: int,
    display_h: int,
    sleep: Callable[[float], None],
    touch_mode: str = "pointer",
    context: dict | None = None,
) -> TouchMapping | None:
    """多点仿射标定：五个 HID 探针实测 cooked 坐标，拟合 HID→屏幕仿射。

    升级单点比例标定（ADR 0026）：≥3 个不共线采样点覆盖缩放/偏移/
    轴间耦合（旋转类），保留每点残差并输出 RMS/最大误差；最大误差超过
    ``MAX_CALIBRATION_ERROR_PX``（8px）时 policy 自动降为 unverified。
    任何失败返回 None（调用方退回单点/按轴），设备状态尽力恢复。

    探针只在专用安全 Activity 中执行；五点拟合后额外测量两个独立点。
    Activity 不可用时不进行 USB/HID 注入，也不在普通页面降级探测。
    """
    from foundation.aoa_calibration import mapping_from_samples

    from .aoa_calibration import open_calibration_surface

    validation_probes = [(120, 213), (240, 427)]
    probes = calibration_probe_points() + validation_probes
    usb_core, usb_util = _usb()
    device = None
    probe_down_sent = False
    try:
        # 熄屏时 InputReader 会丢弃外接触摸：先亮屏解锁（与单点标定一致）。
        _adb_run(serial, "shell", "input", "keyevent", "224")
        _adb_run(serial, "shell", "wm", "dismiss-keyguard")
        open_calibration_surface(serial, _adb_run)
        sleep(1.0)
        device = _configure_accessory(usb_core, serial)
        _detach_kernel_drivers(device)
        _register_all_hid(device, touch_mode)
        sleep(CONFIGURE_DELAY_SECONDS)
        samples: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for probe_x, probe_y in probes:
            result = _transfer(
                device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                aoa_profile.AOA_HID_TOUCH_SCREEN, 0,
                aoa_profile.touch_report(True, probe_x, probe_y))
            if result == DEVICE_NOT_FOUND:
                raise AoaError("probe touch: device vanished")
            probe_down_sent = True
            sleep(1.2)
            dump = _adb_run(serial, "shell", "dumpsys", "input",
                            timeout=30).stdout or ""
            cooked = _parse_probe_cooked_point(dump, probe_x, probe_y)
            result = _transfer(
                device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                aoa_profile.AOA_HID_TOUCH_SCREEN, 0,
                aoa_profile.touch_report(False, probe_x, probe_y))
            if result == DEVICE_NOT_FOUND:
                raise AoaError("probe release: device vanished")
            if cooked is None:
                logger.warning(
                    "AOA affine calibration: cooked point not found for probe "
                    "(%d, %d) on %s", probe_x, probe_y, serial)
                return None
            samples.append(((float(probe_x), float(probe_y)), cooked))
            sleep(0.4)
        mapping = mapping_from_samples(
            touch_mode, samples[:CALIBRATION_PROBE_COUNT], context=context,
            validation_samples=samples[CALIBRATION_PROBE_COUNT:],
            display_pixels=(int(display_w), int(display_h)))
        logger.info(
            "AOA affine calibration on %s: policy=%s rms=%.2fpx max=%.2fpx",
            serial, mapping.effective_policy, mapping.rms_error_px,
            mapping.max_error_px)
        return mapping
    except Exception as exc:
        logger.warning("AOA affine calibration failed on %s: %s", serial, exc)
        return None
    finally:
        errors = cleanup_diagnostics()
        steps = []
        if probe_down_sent and device is not None:
            last_x, last_y = probes[-1]
            steps += [
                ("release_touch", lambda: _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                    aoa_profile.AOA_HID_TOUCH_SCREEN, 0, aoa_profile.touch_report(False, last_x, last_y))),
                ("dismiss_probe_menu", lambda: _adb_run(serial, "shell", "input", "keyevent", "4")),
            ]
        if device is not None:
            steps += [(f"unregister_hid_{hid}", lambda hid=hid: _transfer(
                device, aoa_profile.AOA_ACCESSORY_UNREGISTER_HID, hid, 0))
                for hid, _ in reversed(aoa_profile.AOA_HID_REGISTRATIONS)]
            steps += [("reset_usb", lambda: _reset_usb(device)),
                      ("dispose_usb", lambda: usb_util.dispose_resources(device))]
        steps += [("restore_native_mode", lambda: _ensure_native_mode(usb_core, serial)),
                  ("verify_adb_state", lambda: _wait_adb_state(serial, 20).get("adb_recovered") is True)]
        cleanup_steps(steps, errors)
        if errors:
            logger.warning("AOA affine calibration cleanup on %s: %s", serial, errors)


def record_aoa_profile(
    serial: str,
    steps: list,
    *,
    setup_wizard: bool = False,
    tv: bool = False,
    calibrate_touch: bool | str = False,
    touch_mode: str = "pointer",
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """在 userdebug 设备（ADB 可用）上录制 AOA 恢复旅程。

    每个步骤先经 ADB 用 uiautomator 找到真实元素（多语言匹配），把真实
    像素坐标线性换算为 AOA 逻辑坐标（360×640），再执行 ``input tap`` 并
    产出等价 AOA 命令——录制的 Profile 在无 ADB 的 user 固件上可原样
    回放。``locate_only`` 步骤只定位记录、不点击（录制期间真点 USB 调试
    开关会关掉 ADB）。
    """
    normalized = aoa_profile.parse_record_steps(steps)
    display_w, display_h = _display_size(serial)
    model = (_adb_run(serial, "shell", "getprop", "ro.product.model").stdout or "").strip()
    release = (_adb_run(serial, "shell", "getprop", "ro.build.version.release").stdout or "").strip()

    wizard: dict[str, Any] = {}
    if setup_wizard:
        # userdebug 免向导：settings 写入后系统视为已完成开机设置。
        puts = [("global", "device_provisioned", "1"),
                ("secure", "user_setup_complete", "1")]
        if tv:
            puts.append(("secure", "tv_setup_complete", "1"))
        for namespace, key, value in puts:
            completed = _adb_run(serial, "shell", "settings", "put", namespace, key, value)
            wizard[f"{namespace}.{key}"] = completed.returncode == 0

    # 触摸标定：部分设备的 InputReader 对 POINTER 触摸用统一比例缩放
    # （X 轴比例同时作用于 Y，实机 8e4d：800x1280 屏两轴都乘 ~2.216），
    # 与 real_to_aoa_point 的按轴独立换算不一致，直接换算的 Y 坐标会
    # 系统性偏大。用探针触摸实测真实映射后反解，验证失败时停止录制。
    # 标定会把设备切入 accessory 模式并在专用安全界面注入触摸，只能由
    # 明确的用户动作触发。脚本录制默认使用按轴映射，避免单元测试、页面
    # 初始化或普通预设录制意外碰触真机。
    #
    # calibrate_touch="after"：标定延后到旅程录制完成之后（实机 RK3576
    # 验证——accessory 切换会让 WM insets 动画 leash 卡死，之后
    # uiautomator 永远拿不到 idle 状态，录制必败；先录后标可完全绕开）。
    # 标定成功后按实测比例重缩放已录命令（执行时用 uiautomator 真实
    # 屏幕坐标，导航本身无误差，只影响存进 Profile 的 AOA 逻辑坐标）。
    calibrate_after = calibrate_touch == "after"
    calibration_context = None
    touch_mapping: TouchMapping | None = None
    if calibrate_touch:
        try:
            from .aoa_calibration import display_context

            calibration_context = display_context(
                serial, display_w, display_h, _adb_run, touch_mode)
        except Exception as exc:
            logger.warning("AOA record calibration context unavailable on %s: %s", serial, exc)
    if calibrate_touch and not calibrate_after:
        touch_mapping = _calibrate_touch_affine(
            serial, display_w, display_h, sleep, touch_mode,
            context=calibration_context)
        if touch_mapping is None or not touch_mapping.verified:
            raise AoaError("AOA 标定未通过独立验证，请确认安全标定应用和 USB 状态")
    if touch_mapping is not None:
        # The affine mapping is the source of truth; scale values are retained
        # only for compatibility with old clients displaying calibration status.
        scale_x = display_w / (aoa_profile.AOA_TOUCH_MAX_X + (touch_mode == "direct"))
        scale_y = display_h / (aoa_profile.AOA_TOUCH_MAX_Y + (touch_mode == "direct"))
        logger.info("AOA record affine calibration on %s: policy=%s rms=%.2fpx max=%.2fpx",
                    serial, touch_mapping.effective_policy,
                    touch_mapping.rms_error_px, touch_mapping.max_error_px)
    else:
        # 回退：与 real_to_aoa_point 等价的按轴换算（scale 为 logical→pixel）。
        scale_x = display_w / (aoa_profile.AOA_TOUCH_MAX_X + (touch_mode == "direct"))
        scale_y = display_h / (aoa_profile.AOA_TOUCH_MAX_Y + (touch_mode == "direct"))

    def logical(point: tuple[int, int]) -> tuple[int, int]:
        if touch_mapping is not None:
            return touch_mapping.screen_to_hid(*point)
        return (max(0, min(aoa_profile.AOA_TOUCH_MAX_X, round(point[0] / scale_x))),
                max(0, min(aoa_profile.AOA_TOUCH_MAX_Y, round(point[1] / scale_y))))

    commands: list[str] = []
    executed: list[dict] = []

    def append_pace(wait_ms: int) -> None:
        """把录制时的动画等待写成回放命令（clamp 到单步 sleep 上限）。"""
        if wait_ms > 0:
            commands.append(f"sleep {min(wait_ms, aoa_profile.MAX_SLEEP_MS)}")

    # 先唤醒屏幕并解除 keyguard（userdebug 无密码，swipe 锁屏会挡住
    # 所有元素查找）：录制与回放都从亮屏+桌面开始。
    _adb_run(serial, "shell", "input", "keyevent", "224")
    _adb_run(serial, "shell", "wm", "dismiss-keyguard")
    sleep(0.5)
    # 设置应用可能带后台残留状态（上次录制停在的子页面）：点图标会恢复
    # 而非重新进入顶层列表，导航必败（实机 8e4d 验证）。录制前置强制
    # 重置设置进程并拉起根页面（大屏双栏设置自身持久化"上次页面"，
    # force-stop 后仍会恢复详情页，必须显式回到根）。
    # 缩放（RK3576 启动器动画导致 uiautomator 长期拿不到 idle 状态，
    # 录制机专属前置；回放侧 user 固件不受影响，坐标为最终布局位置）。
    _adb_run(serial, "shell", "am", "force-stop", "com.android.settings")
    _adb_run(serial, "shell", "am", "start", "-a", "android.settings.SETTINGS")
    sleep(1.5)
    _adb_run(serial, "shell", "input", "keyevent", "3")
    sleep(0.8)
    commands.append("wake")

    for index, step in enumerate(normalized):
        action = step["action"]
        if action in ("back", "home", "wake"):
            keyevent = {"back": "4", "home": "3", "wake": "224"}[action]
            _adb_run(serial, "shell", "input", "keyevent", keyevent)
            commands.append(action)
            append_pace(step["wait_ms"])
            executed.append({"index": index, "action": action})
            sleep(step["wait_ms"] / 1000)
            continue
        if action in ("scroll_down", "scroll_up"):
            start_y = 0.7 if action == "scroll_down" else 0.3
            end_y = 0.3 if action == "scroll_down" else 0.7
            start = logical((display_w // 2, int(display_h * start_y)))
            end = logical((display_w // 2, int(display_h * end_y)))
            _adb_run(serial, "shell", "input", "swipe",
                     str(display_w // 2), str(int(display_h * start_y)),
                     str(display_w // 2), str(int(display_h * end_y)),
                     str(step["swipe_ms"]))
            commands.append(
                f"swipe {start[0]} {start[1]} {step['swipe_ms']} {end[0]} {end[1]}")
            append_pace(step["wait_ms"])
            executed.append({"index": index, "action": action})
            sleep(step["wait_ms"] / 1000)
            continue

        # click / longClick：找元素（可先滚动重试）→ 换算 → 执行 → 记录。
        # 底部遮蔽区防护（实机 RK3576）：列表底部行的 tap 会被系统任务栏/
        # 手势条拦截（uiautomator 仍报其 bounds，点击无效果）；行落在此区
        # 时先滚动半屏把行挪到中部再找，绝不盲点遮蔽区坐标。
        bottom_covered_y = display_h - 170
        point: tuple[int, int] | None = None
        nodes: list[dict] = []
        parent_of: dict = {}
        for attempt in range(step["scroll_attempts"] + 1):
            nodes, parent_of = _dump_elements(serial, sleep)
            point = _find_tap_point(nodes, parent_of, step["find"])
            if point is not None and point[1] > bottom_covered_y:
                if attempt < step["scroll_attempts"]:
                    point = None          # 先滚动把行挪出遮蔽区
                else:
                    # 无滚动余量（固定布局页，如实机 RK3576 向导 Start 按钮
                    # center 落在 1750 临界线上）：钳制到遮蔽线上缘。大按钮
                    # 上缘仍在按钮内；矮行保持原点（dump bounds 未含手势条
                    # 遮挡时）。
                    clamped_y = bottom_covered_y - 20
                    if abs(clamped_y - point[1]) <= 80:
                        point = (point[0], clamped_y)
            if point is not None:
                break
            if attempt < step["scroll_attempts"]:
                # 长慢滑：实机 8e4d 验证——0.7H→0.3H/300ms 打不开应用抽屉
                # （拖动距离不足被启动器忽略），0.75H→0.15H/500ms 第 1 次
                # 开抽屉、抽屉内继续滚动列表。scroll_x 允许指定滚动横轴：
                # 双栏设置（实机平板）里左栏列表要滚 x≈0.25，右栏详情用 x≈0.5。
                swipe_x = int(display_w * step.get("scroll_x", 0.5))
                top = (swipe_x, int(display_h * 0.75))
                bottom = (swipe_x, int(display_h * 0.15))
                _adb_run(serial, "shell", "input", "swipe",
                         str(top[0]), str(top[1]),
                         str(bottom[0]), str(bottom[1]), "500")
                # 重试滑动属于回放路径：点击坐标以滚动后的界面为前提
                # （抽屉里找设置），不记录则回放会在未滚动界面上点错。
                start, end = logical(top), logical(bottom)
                commands.append(
                    f"swipe {start[0]} {start[1]} 500 {end[0]} {end[1]}")
                commands.append("sleep 800")
                sleep(0.8)
        if point is None:
            if step.get("optional"):
                executed.append({"index": index, "action": action, "skipped": True})
                continue
            raise AoaError(
                f"record step {index} ({action}): element not found for "
                f"{step['find']}; visible texts: "
                f"{_visible_texts_summary(nodes)}")
        tap_x, tap_y = point
        logical_point = logical(point)
        if not step.get("locate_only"):
            if action == "longClick":
                _adb_run(serial, "shell", "input", "swipe",
                         str(tap_x), str(tap_y), str(tap_x), str(tap_y), "1000")
                commands.append(f"longClick {logical_point[0]} {logical_point[1]}")
            else:
                for _ in range(step.get("clicks", 1)):
                    _adb_run(serial, "shell", "input", "tap", str(tap_x), str(tap_y))
                    commands.append(f"click {logical_point[0]} {logical_point[1]}")
                    sleep(0.3)
        else:
            commands.append(f"click {logical_point[0]} {logical_point[1]}")
        # 回放 pacing：录制时的 wait_ms 是动画等待，必须一并写进命令，
        # 否则回放全速连发会让点击落在界面动画中间（实机验证的教训）。
        append_pace(step["wait_ms"])
        executed.append({
            "index": index, "action": action, "center": list(point),
            "aoa": list(logical_point), "locate_only": step.get("locate_only", False),
        })
        sleep(step["wait_ms"] / 1000)

    if calibrate_after:
        # 标定放最后（accessory 切换会让 uiautomator idle 永久失效，
        # 见函数头注释）；成功则把已录命令按实测比例重缩放。
        post_mapping = _calibrate_touch_affine(
            serial, display_w, display_h, sleep, touch_mode,
            context=calibration_context)
        if post_mapping is not None:
            from foundation.aoa_calibration import convert_commands

            commands = convert_commands(
                commands,
                TouchMapping(touch_mode, (scale_x, 0.0, 0.0, scale_y), (0.0, 0.0),
                             display_pixels=(display_w, display_h)),
                post_mapping,
            )
            touch_mapping = post_mapping
            scale_x = display_w / aoa_profile.AOA_TOUCH_MAX_X
            scale_y = display_h / aoa_profile.AOA_TOUCH_MAX_Y
            logger.info("AOA record post affine calibration on %s: policy=%s",
                        serial, post_mapping.effective_policy)
        if post_mapping is None or not post_mapping.verified:
            raise AoaError("AOA 标定未通过独立验证；已录命令尚未标定")

    aoa_profile.parse_profile_commands(commands)
    return {
        "serial": serial,
        "success": not cleanup_diagnostics(),
        "cleanup_errors": list(cleanup_diagnostics()),
        "display": [display_w, display_h],
        "model": model,
        "release": release,
        "touch_calibration": {
            "measured": touch_mapping is not None,
            "context": touch_mapping.context if touch_mapping is not None else calibration_context,
            "hid_touch_mode": touch_mode,
            "scale_x": round(scale_x, 4),
            "scale_y": round(scale_y, 4),
            **({"mapping": touch_mapping.to_dict()} if touch_mapping is not None else {}),
        },
        "commands": commands,
        "steps": executed,
        **({"wizard": wizard} if wizard else {}),
    }


def _rescale_aoa_command(command: str, old_x: float, old_y: float,
                         new_x: float, new_y: float) -> str:
    """把按轴映射生成的 AOA 命令重缩放到实测映射（click/longClick/swipe）。

    旧逻辑坐标 → 原始像素（按轴比例）→ 新逻辑坐标（实测比例）。
    key/sleep/wake/home/back 等无坐标命令原样返回。
    """
    head, _, rest = command.partition(" ")
    if head in ("click", "longClick"):
        parts = rest.split()
        if len(parts) == 2:
            x, y = int(parts[0]), int(parts[1])
            nx = min(360, max(0, round(x * old_x / new_x)))
            ny = min(640, max(0, round(y * old_y / new_y)))
            return f"{head} {nx} {ny}"
    elif head == "swipe":
        parts = rest.split()
        if len(parts) == 5:
            x1, y1, ms, x2, y2 = (int(parts[0]), int(parts[1]),
                                  parts[2], int(parts[3]), int(parts[4]))
            nx1 = min(360, max(0, round(x1 * old_x / new_x)))
            ny1 = min(640, max(0, round(y1 * old_y / new_y)))
            nx2 = min(360, max(0, round(x2 * old_x / new_x)))
            ny2 = min(640, max(0, round(y2 * old_y / new_y)))
            return f"swipe {nx1} {ny1} {ms} {nx2} {ny2}"
    return command


def record_aoa_input(serial: str, raw_action: dict) -> dict[str, Any]:
    """Execute one allow-listed ADB input and return its AOAv2 command."""
    action = aoa_profile.parse_record_input_action(raw_action)
    touch_mode = action.get("hid_touch_mode", "pointer")
    display_w, display_h = _display_size(serial)
    touch_mapping = None
    if action.get("touch_mapping") is not None:
        try:
            touch_mapping = TouchMapping.from_dict(action["touch_mapping"])
        except ValueError as exc:
            raise AoaError(f"invalid AOA touch mapping: {exc}") from exc
        if touch_mapping.hid_touch_mode != touch_mode:
            raise AoaError(
                "AOA touch mapping belongs to another HID mode "
                f"({touch_mapping.hid_touch_mode} != {touch_mode})")
    scale_x = float(action.get("scale_x") or (
        display_w / (aoa_profile.AOA_TOUCH_MAX_X + (touch_mode == "direct"))))
    scale_y = float(action.get("scale_y") or (
        display_h / (aoa_profile.AOA_TOUCH_MAX_Y + (touch_mode == "direct"))))

    context = None
    if action["action"] == "calibrate" or action.get("calibration_context"):
        from .aoa_calibration import display_context

        context = display_context(serial, display_w, display_h, _adb_run, touch_mode)
        from foundation.aoa_calibration import context_matches

        if action.get("calibration_context") and not context_matches(action["calibration_context"], context):
            raise ValueError("display configuration changed; recalibrate before recording input")

    if action["action"] == "calibrate":
        # ADR 0027: only independent verification can publish a measured map.
        # Estimates are returned as unsuccessful, unmeasured diagnostics.
        from foundation import aoa_calibration

        mapping = _calibrate_touch_affine(
            serial, display_w, display_h, time.sleep, touch_mode, context=context)
        errors = list(cleanup_diagnostics())
        if errors:
            mapping = None
        measured = mapping is not None
        if mapping is None:
            from dataclasses import replace

            mapping = replace(aoa_calibration.per_axis_mapping(
                display_w, display_h, touch_mode,
                mapping_policy="direct_unverified" if touch_mode == "direct" else "estimated"), context=context)
        scale_x, scale_y = mapping.matrix[0], mapping.matrix[3]
        return {
            "serial": serial,
            "success": measured and mapping.verified and not errors,
            "action": "calibrate",
            "cleanup_errors": errors,
            "display": [display_w, display_h],
            "touch_calibration": {
                "measured": measured,
                "verified": mapping.verified,
                "context": context,
                "mapping": mapping.to_dict(),
                "scale_x": round(scale_x, 4),
                "scale_y": round(scale_y, 4),
            },
            **({"error": "AOA 标定失败：请检查安全标定应用、独立验证点及 USB 状态"}
               if not measured or not mapping.verified or errors else {}),
        }

    def logical(x: int, y: int) -> tuple[int, int]:
        if touch_mapping is not None:
            return touch_mapping.screen_to_hid(x, y)
        return (
            max(0, min(aoa_profile.AOA_TOUCH_MAX_X, round(x / scale_x))),
            max(0, min(aoa_profile.AOA_TOUCH_MAX_Y, round(y / scale_y))),
        )

    kind = action["action"]
    command = ""
    completed = None
    if kind == "tap":
        completed = _adb_run(
            serial, "shell", "input", "tap", str(action["x"]), str(action["y"]))
        x, y = logical(action["x"], action["y"])
        command = f"click {x} {y}"
    elif kind == "long_click":
        completed = _adb_run(
            serial, "shell", "input", "swipe", str(action["x"]), str(action["y"]),
            str(action["x"]), str(action["y"]), str(action["duration_ms"]))
        x, y = logical(action["x"], action["y"])
        command = f"longClick {x} {y}"
    elif kind == "swipe":
        completed = _adb_run(
            serial, "shell", "input", "swipe", str(action["x1"]),
            str(action["y1"]), str(action["x2"]), str(action["y2"]),
            str(action["duration_ms"]))
        x1, y1 = logical(action["x1"], action["y1"])
        x2, y2 = logical(action["x2"], action["y2"])
        command = f"swipe {x1} {y1} {action['duration_ms']} {x2} {y2}"
    elif kind in {"back", "home", "wake"}:
        keyevent = {"back": "4", "home": "3", "wake": "224"}[kind]
        completed = _adb_run(serial, "shell", "input", "keyevent", keyevent)
        command = kind
    elif kind == "write":
        # subprocess argv keeps host-shell metacharacters inert. Android's
        # input tool represents spaces as %s.
        completed = _adb_run(
            serial, "shell", "input", "text", action["text"].replace(" ", "%s"))
        command = f"write {action['text']}"
    elif kind == "sleep":
        command = f"sleep {action['duration_ms']}"

    if completed is not None and completed.returncode != 0:
        raise AoaError(
            (completed.stderr or completed.stdout or f"ADB {kind} failed").strip())
    # Validate the generated command at the same boundary used by profile save.
    aoa_profile.parse_profile_commands([command])
    result: dict[str, Any] = {
        "serial": serial,
        "success": True,
        "action": kind,
        "command": command,
        "display": [display_w, display_h],
        "touch_calibration": {
            "measured": "scale_x" in action and "scale_y" in action,
            "context": context,
            "scale_x": round(scale_x, 4),
            "scale_y": round(scale_y, 4),
        },
    }
    if touch_mapping is not None:
        result["touch_calibration"].update({
            "measured": True,
            "mapping": touch_mapping.to_dict(),
        })
    return result


def run_aoa_sequence(
    serial: str,
    commands: list[str],
    wait_adb_seconds: int = 0,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[int, int, str], None] | None = None,
    control_id: str = "",
    on_control_event: Callable[[dict[str, Any]], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    control_source: Callable[[], dict] | None = None,
    debug_command_source: Callable | None = None,
    suppress_adb_daemon: bool = False,
    suppress_adb_daemon_gate: Callable[[], bool] | None = None,
    touch_mode: str = "pointer",
    hid_settle_seconds: float = 2,
    session_mode: str = "continuous",
    touch_mapping: dict | None = None,
    source_touch_mapping: dict | None = None,
    mapping_policy: str = "",
    _deadline: float | None = None,
) -> dict[str, Any]:
    """执行一条 AOA 命令序列，随后可选等待 ADB 恢复。

    ``sleep`` 参数供测试注入，避免真实等待。
    ``on_progress(index, total, command_text)`` 在每条命令执行完后回调
    （index 从 1 起，含 sleep），供 Worker 上报命令过程事件；回调异常
    只告警不中断执行。
    ``control_id`` 非空时启用暂停/继续/终止控制（worker_agent.aoa_control）：
    每条命令执行前过控制门；暂停时长自动加回时间预算；用户终止抛
    AoaPlaybackCancelledError，走异常路径完成 USB 清理并收敛命令为 failed。
    ``suppress_adb_daemon`` 由派发方在无 Tradefed 任务/ADB Proxy 导入时
    置真：AOA 会话期抑制主机 adb 守护进程（复合 2D0x accessory 的 adb
    接口会让 daemon 与 pyusb 抢设备，实机 RK3576 复现 reset 风暴），
    会话在 finally 的 USB 清理完成后结束，随后才恢复 daemon 供
    ``_wait_adb_state`` 观察恢复状态。``suppress_adb_daemon_gate`` 提供时
    在武装前一刻求值并覆盖静态布尔：烧写恢复路径在 flash 后还有最长
    数分钟的 boot 等待，期间的并发 Tradefed 任务/ADB Proxy 导入必须
    在武装瞬间重新检查，否则会误杀无关任务的 adb server。门控自身
    异常按“不抑制”处理（fail-open：误杀代价高于放弃抑制）。
    """
    commands = [str(item).strip() for item in commands if str(item).strip()]
    from foundation import aoa_calibration
    from foundation.aoa_hid_mode import profile_hid_options

    profile_hid_options({"hid_touch_mode": touch_mode, "hid_settle_seconds": hid_settle_seconds,
                         "hid_session_mode": session_mode})
    try:
        aoa_calibration.mapping_options({"aoa_hid_touch_mode": touch_mode,
            "aoa_touch_mapping": touch_mapping, "aoa_source_touch_mapping": source_touch_mapping,
            "aoa_mapping_policy": mapping_policy})
    except ValueError as exc:
        raise AoaError(str(exc)) from exc
    # 触摸映射（ADR 0026 执行时动态转换）：Controller 下发的已验证标定
    # 记录经 parse 校验；无法提供时按轴估算兜底并标记 unverified——两种
    # 情况都把 policy 写进结果，禁止把估算当成功验证。
    target_mapping = None
    if touch_mapping is not None:
        try:
            target_mapping = aoa_calibration.TouchMapping.from_dict(touch_mapping)
        except ValueError as exc:
            raise AoaError(f"invalid AOA touch mapping from Controller: {exc}") from exc
        if target_mapping.hid_touch_mode != touch_mode:
            raise AoaError(
                "AOA touch mapping belongs to another HID mode "
                f"({target_mapping.hid_touch_mode} != {touch_mode})")
    source_mapping = None
    if source_touch_mapping is not None:
        try:
            source_mapping = aoa_calibration.TouchMapping.from_dict(source_touch_mapping)
        except ValueError as exc:
            raise AoaError(f"invalid source AOA touch mapping: {exc}") from exc
        if source_mapping.hid_touch_mode != touch_mode:
            raise AoaError(
                "source AOA touch mapping belongs to another HID mode "
                f"({source_mapping.hid_touch_mode} != {touch_mode})")
    effective_policy = (
        str(mapping_policy or (target_mapping.effective_policy if target_mapping else ""))
        or ("direct_unverified" if touch_mode == "direct" else "estimated"))
    if any(c.kind in {"click", "longClick", "swipe"} for c in aoa_profile.parse_profile_commands(commands)) and not auto_touch_allowed_by_policy(effective_policy):
        raise AoaError(
            f"AOA mapping policy {effective_policy} forbids automatic touch input; "
            "calibrate this device first")
    if target_mapping is not None:
        commands = aoa_calibration.convert_commands(
            commands, source_mapping or target_mapping, target_mapping)
    parsed = aoa_profile.parse_profile_commands(commands)
    if debug_command_source and (session_mode != "continuous" or wait_adb_seconds):
        raise AoaError("persistent debug requires continuous HID and no final ADB wait")
    if session_mode == "per_command":
        from .aoa_step_sessions import run_step_sessions

        def run_one(text, deadline, report):
            return run_aoa_sequence(serial, [text], sleep=sleep, on_event=report,
                suppress_adb_daemon=suppress_adb_daemon, suppress_adb_daemon_gate=suppress_adb_daemon_gate,
                touch_mode=touch_mode, hid_settle_seconds=hid_settle_seconds,
                touch_mapping=touch_mapping, source_touch_mapping=touch_mapping,
                mapping_policy=mapping_policy, _deadline=deadline)

        return run_step_sessions(serial, commands, parsed, run_one=run_one, wait_adb=_wait_adb_state,
            validate=validate_binding, error_type=AoaError, sleep=sleep, wait_adb_seconds=wait_adb_seconds,
            on_event=on_event, on_progress=on_progress, control_id=control_id, control_source=control_source,
            on_control_event=on_control_event, touch_mode=touch_mode, hid_settle_seconds=hid_settle_seconds)
    usb_core, usb_util = _usb()
    # clamp 环境变量上界：放大会话时长会越过抑制 flag 的 TTL 推导
    # （FLAG_TTL_SECONDS 假设最坏会话 ≤ MAX_AOA_OPERATION_SECONDS），
    # 让其他进程把活会话误判为 stale、抑制中途失效。
    action_delay = min(max(float(
        __import__("os").environ.get("GMS_WORKER_AOA_ACTION_DELAY", str(ACTION_DELAY_SECONDS))), 0.0), 10.0)
    deadline = time.monotonic() + aoa_profile.MAX_TOTAL_RUNTIME_MS / 1000 + 30
    hard_deadline = time.monotonic() + 900
    if debug_command_source:
        deadline = hard_deadline
    if _deadline is not None:
        deadline = min(deadline, _deadline)
    executed = 0
    device = None
    registered: set[int] = set()

    register_all = partial(register_hids, transfer=_transfer, serial=serial, sleep=sleep,
                           error_type=AoaError, required_channels=aoa_profile.required_hid_channels(parsed),
                           touch_mode=touch_mode, settle_seconds=hid_settle_seconds)
    session = None
    reports_sent = 0

    position = 0
    original = ""

    def report_event(phase, index, text=""):
        if on_event:
            try:
                event = {"phase": phase, "index": index, "total": max(index, len(parsed)), "command": text}
                if debug_command_source:
                    event["debug_sequence"] = index
                on_event(event)
            except Exception:
                logger.warning("AOA progress event failed", exc_info=True)

    def report_control(event):
        report_event(event["event_type"], event["index"])
        if on_control_event:
            on_control_event(event)

    execution_error = None
    # 在第一次 accessory 协商之前打开 adb 抑制会话：USB 开着调试的设备
    # 进 accessory 后枚举为 18d1:2D0x 复合设备（含 adb 接口），主机 adb
    # daemon 会与 pyusb 抢 interface（实机 dmesg：usbfs claim 报错 +
    # xhci reset 风暴 + device vanished）。会话在 finally 的 USB 清理
    # 之后结束，此后才允许 daemon 重启供 _wait_adb_state 观察。
    # 门控在此刻（而非派发时刻）求值：等待/清理期间环境可能变化，
    # 有并发任务时必须放弃抑制而不是误杀 adb server。
    suppress_now = suppress_adb_daemon
    if suppress_adb_daemon and suppress_adb_daemon_gate is not None:
        try:
            suppress_now = bool(suppress_adb_daemon_gate())
        except Exception:
            logger.warning("adb suppression gate raised; suppression disabled", exc_info=True)
            suppress_now = False
    adb_session = (
        _begin_adb_suppression() if suppress_now else None
    )
    try:
        if control_id:
            from .aoa_control import register

            register(control_id)
        device = _configure_accessory(usb_core, serial)
        _detach_kernel_drivers(device)
        sleep(CONFIGURE_DELAY_SECONDS)
        registered = register_all(device)
        sleep(CONFIGURE_DELAY_SECONDS)
        session = AoaSession(device, registered,
            find=lambda: _open_device(usb_core, serial),
            configure=lambda **kwargs: _configure_accessory(usb_core, serial, **kwargs),
            detach=_detach_kernel_drivers, register=register_all,
            dispose=lambda old: usb_util.dispose_resources(old), sleep=sleep, error_type=AoaError,
            clock=time.monotonic)

        def send_hid(hid_id: int, data: bytes, pause: float) -> None:
            nonlocal reports_sent
            if time.monotonic() > deadline:
                raise AoaError("AOA playback time budget exceeded; reconcile required")
            if hid_id not in registered:
                raise AoaError(
                    f"AOA HID channel {hid_id} is not supported by device {serial} "
                    "(registration was rejected; use another device or non-HID steps)")
            try:
                result = _transfer(
                    device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT, hid_id, 0, data)
            except AoaError as exc:
                raise AoaError(
                    f"AOA HID event on channel {hid_id} rejected by device {serial}: {exc}"
                ) from exc
            if result == DEVICE_NOT_FOUND:
                raise AoaError(
                    f"device vanished while sending HID to {serial}; step {position}/{len(parsed)}, "
                    f"report {reports_sent + 1}, {reports_sent} reports sent; "
                    "reconcile required, input was not replayed")
            if result < 0:
                raise AoaError(f"AOA HID event failed: {result}")
            reports_sent += 1
            sleep(pause)

        def debug_validate():
            validate_binding()
            if time.monotonic() >= hard_deadline:
                raise AoaError("AOA debug session reached its 15 minute limit; end debug before reconnecting")

        def debug_tick():
            debug_validate()
            if control_id:
                from .aoa_control import wait_if_paused

                wait_if_paused(control_id, index=position, total=max(position, len(parsed)),
                    sleep=sleep, on_event=report_control, on_tick=debug_validate, read_source=control_source)
            debug_validate()

        steps = ((index, command, commands[index - 1]) for index, command in enumerate(parsed, 1))
        if debug_command_source:
            from .aoa_debug_session import debug_steps

            def transform_debug(text):
                if any(c.kind in {"click", "longClick", "swipe"} for c in aoa_profile.parse_profile_commands([text])) and not auto_touch_allowed_by_policy(effective_policy):
                    raise AoaError("unsupported mapping forbids automatic touch input")
                return (aoa_calibration.convert_commands([text], source_mapping or target_mapping, target_mapping)[0]
                        if target_mapping else text)

            steps = debug_steps(debug_command_source, parsed, tick=debug_tick, sleep=sleep,
                                clock=time.monotonic, error_type=AoaError, transform=transform_debug)
        for position, command, original in steps:
            if control_id:
                from .aoa_control import wait_if_paused as _gate

                pause_duration = _gate(
                    control_id, index=position, total=len(parsed),
                    sleep=sleep, on_event=report_control,
                    on_tick=debug_validate if debug_command_source else validate_binding, read_source=control_source)
                if not debug_command_source:
                    deadline += pause_duration
            validate_binding()
            if time.monotonic() > deadline:
                raise AoaError("AOA playback time budget exceeded; reconcile required")
            reports_sent = 0
            report_event("running", position, str(original))
            try:
                if command.kind == "sleep":
                    sleep(min(command.sleep_ms, aoa_profile.MAX_SLEEP_MS) / 1000)
                    executed += 1
                    continue
                while True:
                    rebuilt = session.prepare(deadline,
                        lambda index=position, text=original: report_event("reconnecting", index, str(text)))
                    device, registered = session.device, session.registered
                    if not rebuilt:
                        break
                    if control_id:
                        pause_duration = _gate(
                            control_id, index=position, total=len(parsed), sleep=sleep,
                            on_event=report_control, on_tick=debug_validate if debug_command_source else validate_binding,
                            read_source=control_source)
                        if not debug_command_source:
                            deadline += pause_duration
                    report_event("running", position, str(original))
                if command.kind in ("wake", "home", "back"):
                    flag = {"wake": aoa_profile.AOA_SYSTEM_WAKE,
                            "home": aoa_profile.AOA_SYSTEM_HOME,
                            "back": aoa_profile.AOA_SYSTEM_BACK}[command.kind]
                    send_hid(aoa_profile.AOA_HID_SYSTEM,
                             aoa_profile.system_report(flag), action_delay)
                    executed += 1
                    continue
                if command.kind in ("click", "longClick"):
                    hold = 1.0 if command.kind == "longClick" else 0.0
                    if hold:
                        send_hid(aoa_profile.AOA_HID_TOUCH_SCREEN,
                                 aoa_profile.touch_report(True, command.x1, command.y1), hold)
                    else:
                        send_hid(aoa_profile.AOA_HID_TOUCH_SCREEN,
                                 aoa_profile.touch_report(True, command.x1, command.y1),
                                 STEP_DELAY_SECONDS)
                    send_hid(aoa_profile.AOA_HID_TOUCH_SCREEN,
                             aoa_profile.touch_report(False, command.x1, command.y1), action_delay)
                    executed += 1
                    continue
                if command.kind == "swipe":
                    for touch_down, x, y in aoa_profile.swipe_points(
                            command.x1, command.y1, command.x2, command.y2, command.duration_ms):
                        send_hid(aoa_profile.AOA_HID_TOUCH_SCREEN,
                                 aoa_profile.touch_report(touch_down, x, y),
                                 STEP_DELAY_SECONDS if touch_down else action_delay)
                    executed += 1
                    continue
                if command.kind == "write":
                    for report in aoa_profile.text_to_key_reports(command.text):
                        send_hid(aoa_profile.AOA_HID_KEYBOARD, report, STEP_DELAY_SECONDS)
                        send_hid(aoa_profile.AOA_HID_KEYBOARD, b"\x00\x00", STEP_DELAY_SECONDS)
                    sleep(action_delay)
                    executed += 1
                    continue
                if command.kind == "key":
                    for modifier, usage in command.keys:
                        send_hid(aoa_profile.AOA_HID_KEYBOARD,
                                 aoa_profile.key_report(modifier, usage), STEP_DELAY_SECONDS)
                        send_hid(aoa_profile.AOA_HID_KEYBOARD, b"\x00\x00", STEP_DELAY_SECONDS)
                    sleep(action_delay)
                    executed += 1
            finally:
                if executed >= position:
                    report_event("completed", position, str(original))
                if on_progress is not None:
                    try:
                        on_progress(position, len(parsed), str(original))
                    except Exception:
                        logger.warning("AOA progress callback failed at %s on %s",
                                       position, serial, exc_info=True)
    except Exception as exc:
        execution_error = exc
        report_event("cancelled" if getattr(exc, "cancelled", False) else "failed", position, str(original))
        raise
    finally:
        cleanup_errors = cleanup_diagnostics()
        if session is not None:
            device, registered = session.device, session.registered
        if device is not None:
            steps = [
                ("release_touch", lambda: _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                    aoa_profile.AOA_HID_TOUCH_SCREEN, 0, aoa_profile.touch_report(False, 0, 0))),
                ("release_keyboard", lambda: _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                    aoa_profile.AOA_HID_KEYBOARD, 0, b"\x00\x00")),
                ("release_system", lambda: _transfer(device, aoa_profile.AOA_ACCESSORY_SEND_HID_EVENT,
                    aoa_profile.AOA_HID_SYSTEM, 0, b"\x00")),
            ]
            channel_steps = {"release_touch": aoa_profile.AOA_HID_TOUCH_SCREEN,
                             "release_keyboard": aoa_profile.AOA_HID_KEYBOARD,
                             "release_system": aoa_profile.AOA_HID_SYSTEM}
            steps = [(name, cleanup) for name, cleanup in steps if channel_steps[name] in registered]
            steps += [(f"unregister_hid_{hid}", lambda hid=hid: _transfer(
                device, aoa_profile.AOA_ACCESSORY_UNREGISTER_HID, hid, 0))
                for hid in registered]
            steps += [("reset_usb", lambda: _reset_usb(device)),
                      ("dispose_usb", lambda: usb_util.dispose_resources(device)),
                      ("restore_native_mode", lambda: _ensure_native_mode(usb_core, serial))]
            cleanup_steps(steps, cleanup_errors)
        if control_id:
            from .aoa_control import unregister as _unregister_control

            _unregister_control(control_id)
        if cleanup_errors:
            logger.warning("AOA cleanup diagnostics for %s: %s", serial, cleanup_errors)
            if execution_error is not None:
                execution_error.cleanup_errors = cleanup_errors
        if adb_session is not None:
            # USB 清理（含 restore_native_mode）完成后才恢复 adb daemon：
            # _wait_adb_state 在本函数尾部运行，正好观察到重启后的 adb。
            adb_session.end()

    result: dict[str, Any] = {
        "serial": serial,
        "success": True,
        "commands_total": len(parsed),
        "hid_touch_mode": touch_mode,
        "hid_settle_seconds": hid_settle_seconds,
        "hid_session_mode": "continuous",
        "mapping_policy": effective_policy,
        "touch_mapping_verified": bool(
            target_mapping is not None and target_mapping.verified),
        "commands_executed": executed,
        "cleanup_errors": cleanup_errors,
        "reconcile_required": bool(cleanup_errors),
        "native_mode_restored": not any(e["step"] == "restore_native_mode" for e in cleanup_errors),
        "usb_reconnections": session.rebuilds if session else 0,
    }
    if wait_adb_seconds:
        result.update(_wait_adb_state(serial, int(wait_adb_seconds)))
        # AOA 执行成功但 ADB 未恢复：对编排器仍算"序列成功"，
        # 但把恢复状态如实带回，由上层决定是否进入测试。
        result["success"] = result.get("adb_recovered", False) and result["native_mode_restored"]
    return result
