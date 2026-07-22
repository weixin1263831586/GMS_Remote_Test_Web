"""设备截图、结构化 UI 布局和坐标点按接口。

Android CLI 错误时可能仍返回 0，因此需同时检查错误输出。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shlex
import time
import xml.etree.ElementTree as ET

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from foundation.networking import is_local_host
from foundation.responses import error_response

from . import runtime
from .support import (
    SSHConnection,
    device_claim_conflict_response,
    device_mutation_guard,
)


logger = logging.getLogger(__name__)
router = APIRouter()

# android CLI 报错前缀（exit code 恒为 0，靠文本判错）。
_ERROR_MARKERS = ("Error:", "ERROR:", "Failed to", "Multiple devices", "Unknown option", "Usage:")


class UiControlRequest(BaseModel):
    serial: str


class UiTapRequest(BaseModel):
    serial: str
    x: int = Field(ge=0, le=10000)
    y: int = Field(ge=0, le=10000)


def _android_cli_path(config: dict) -> str:
    """返回 android CLI 绝对路径。

    非交互式 SSH（exec_command）不加载 ~/.bashrc，PATH 里没有 ~/.local/bin，
    故必须用绝对路径调用。优先读 config.android_cli_path，否则按测试主机
    ubuntu_user 推导默认位置 ~/.local/bin/android。
    """
    configured = (config or {}).get("android_cli_path")
    if configured:
        return configured
    ubuntu_user = runtime.config_manager.get_ubuntu_user(config)
    return f"/home/{ubuntu_user}/.local/bin/android"


def _looks_like_error(text: str) -> str | None:
    """CLI 恒返回 exit 0，靠输出文本判错。返回错误消息或 None。"""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if any(stripped.startswith(m) for m in _ERROR_MARKERS):
            return stripped
    return None


def _extract_json_object(text: str):
    """从 android layout 输出中提取首个 JSON 数组/对象。

    layout -p 输出可能混入 java 日志行（如 ``Jul 10 ... java.util.prefs``），
    需定位首个 ``[`` 或 ``{`` 到末尾配对片段再解析。
    """
    start = -1
    for i, ch in enumerate(text):
        if ch in "[{":
            start = i
            break
    if start < 0:
        return None
    candidate = text[start:]
    # 从末尾裁掉 JSON 之后的日志尾随。
    for end_char in ("]", "}"):
        idx = candidate.rfind(end_char)
        if idx > 0:
            candidate = candidate[: idx + 1]
            break
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _run_remote(ssh, command: str, timeout: int = 30) -> tuple[str, str, int]:
    return runtime.ssh_manager.execute_command(ssh, command, timeout=timeout)


def _run_remote_with_connection(config: dict, command: str, timeout: int) -> tuple[str, str, int]:
    with SSHConnection(config) as ssh:
        if not ssh:
            raise ConnectionError("SSH connection failed")
        return _run_remote(ssh, command, timeout=timeout)


def _layout_with_uiautomator2_sync(serial: str) -> list[dict]:
    """用 uiautomator2 获取语义 UI 树。

    uiautomator2 默认关闭 waitForIdleTimeout，能处理持续动画或系统界面始终不进入
    idle 状态的设备；这正是 Android CLI layout 在 RK3576GMS1 上失败的场景。
    """
    import uiautomator2 as u2

    device = u2.connect_usb(serial)
    device.jsonrpc.setConfigurator({"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0})
    xml = device.dump_hierarchy(compressed=False, pretty=False, max_depth=50)
    return _simplify_uiautomator_xml(xml)


def _simplify_uiautomator_xml(xml: str) -> list[dict]:
    """把 uiautomator2 XML 层级拍平成前端需要的元素列表。"""
    root = ET.fromstring(xml)
    items: list[dict] = []
    interaction_attrs = (
        ("clickable", "clickable"),
        ("long-clickable", "long_clickable"),
        ("scrollable", "scrollable"),
        ("checkable", "checkable"),
        ("focusable", "focusable"),
        ("editable", "editable"),
    )
    for node in root.iter("node"):
        attrs = node.attrib
        bounds = attrs.get("bounds", "")
        center = _center_from_bounds(bounds)
        interactions = [name for attr, name in interaction_attrs if attrs.get(attr) == "true"]
        text = attrs.get("text") or attrs.get("content-desc") or ""
        resource_id = attrs.get("resource-id") or ""
        if not (text or resource_id or center or interactions):
            continue
        items.append({
            "text": text,
            "content_desc": attrs.get("content-desc") or "",
            "resource_id": resource_id,
            "class_name": attrs.get("class") or "",
            "package": attrs.get("package") or "",
            "center": center,
            "bounds": bounds,
            "interactions": interactions,
            "enabled": attrs.get("enabled", "true") == "true",
        })
    return items


def _center_from_bounds(bounds: str):
    nums = re.findall(r"-?\d+", bounds or "")
    if len(nums) < 4:
        return None
    left, top, right, bottom = map(int, nums[:4])
    if right <= left or bottom <= top:
        return None
    return [(left + right) // 2, (top + bottom) // 2]


def _capture_screenshot_sync(config: dict, capture_cmd: str, remote_png: str) -> bytes:
    if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
        try:
            out, err, code = runtime.run_local_shell_command(capture_cmd, 30)
            if code != 0 or not os.path.exists(remote_png):
                raise RuntimeError(f"screencap failed: {(err or out).strip()}")
            with open(remote_png, "rb") as handle:
                return handle.read()
        finally:
            with contextlib.suppress(OSError):
                os.remove(remote_png)

    with SSHConnection(config) as ssh:
        if not ssh:
            raise ConnectionError("SSH connection failed")
        try:
            _run_remote(ssh, capture_cmd, timeout=30)
            check = f"test -s {shlex.quote(remote_png)} && echo OK || echo EMPTY"
            out, _, _ = _run_remote(ssh, check, timeout=5)
            if "OK" not in out.split():
                raise RuntimeError("screencap produced no image (device offline or unauthorized?)")
            return _scp_read(ssh, remote_png)
        finally:
            with contextlib.suppress(Exception):
                _run_remote(ssh, f"rm -f {shlex.quote(remote_png)}", timeout=5)


@router.post("/api/devices/ui/screenshot")
async def ui_screenshot(req: UiControlRequest, request: Request):
    """截取指定设备当前屏幕，返回 base64 PNG。

    走 ``adb -s <serial> exec-out screencap -p``，输出重定向到测试主机临时文件，
    再 SCP 拉回 web 服务器读字节并 base64 内联返回前端。
    """
    serial = (req.serial or "").strip()
    if not serial:
        return error_response("serial is required", 400)
    conflict = device_claim_conflict_response(
        [serial],
        runtime.get_client_id_from_request(request),
        allow_owner=True,
    )
    if conflict:
        return conflict

    config = runtime.config_manager.load_config()
    safe_serial = re.sub(r"[^A-Za-z0-9_.-]+", "_", serial)[:120] or "device"
    remote_png = f"/tmp/gms_ui_shot_{safe_serial}_{int(time.time())}.png"
    # exec-out 把 PNG 二进制写到 stdout；重定向到文件。stderr 丢弃。
    capture_cmd = f"adb -s {shlex.quote(serial)} exec-out screencap -p > {shlex.quote(remote_png)} 2>/dev/null"

    try:
        data = await asyncio.to_thread(_capture_screenshot_sync, config, capture_cmd, remote_png)
        if not data:
            return error_response("empty screenshot", 502)
        return JSONResponse(content={
            "success": True,
            "serial": serial,
            "image": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        })
    except Exception as exc:
        logger.warning("[UI Control] screenshot failed for %s: %s", serial, exc)
        return error_response(str(exc), 500)


def _scp_read(ssh, remote_path: str) -> bytes:
    """通过 SFTP 读取远程文件内容到内存。"""
    with contextlib.suppress(Exception):
        sftp = ssh.open_sftp()
        try:
            with sftp.open(remote_path, "rb") as handle:
                return handle.read()
        finally:
            sftp.close()
    return b""


@router.post("/api/devices/ui/layout")
async def ui_layout(req: UiControlRequest, request: Request):
    """返回指定设备当前 App 的 UI 布局树（含每个元素的坐标/文本/可交互性）。"""
    serial = (req.serial or "").strip()
    if not serial:
        return error_response("serial is required", 400)
    conflict = device_claim_conflict_response(
        [serial],
        runtime.get_client_id_from_request(request),
        allow_owner=True,
    )
    if conflict:
        return conflict

    config = runtime.config_manager.load_config()
    android = _android_cli_path(config)
    # --device 支持多设备；-p 输出 pretty JSON。
    cmd = f"{shlex.quote(android)} layout --device={shlex.quote(serial)} -p"

    try:
        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            try:
                elements = await asyncio.wait_for(
                    asyncio.to_thread(_layout_with_uiautomator2_sync, serial),
                    timeout=30,
                )
                return JSONResponse(content={
                    "success": True,
                    "serial": serial,
                    "source": "uiautomator2",
                    "elements": elements,
                })
            except Exception as u2_exc:
                logger.warning("[UI Control] uiautomator2 layout failed for %s: %s; falling back", serial, u2_exc)
                out, err, _ = await asyncio.to_thread(runtime.run_local_shell_command, cmd, 30)
        else:
            out, err, _ = await asyncio.to_thread(_run_remote_with_connection, config, cmd, 30)

        err_msg = _looks_like_error(out) or _looks_like_error(err)
        if err_msg:
            return error_response(f"android layout failed: {err_msg}", 502)

        layout = _extract_json_object(out)
        if layout is None:
            return error_response("failed to parse layout JSON", 502)
        return JSONResponse(content={
            "success": True,
            "serial": serial,
            "source": "android-cli",
            "elements": _simplify_layout(layout),
        })
    except Exception as exc:
        logger.warning("[UI Control] layout failed for %s: %s", serial, exc)
        return error_response(str(exc), 500)


def _simplify_layout(layout) -> list[dict]:
    """把 layout 原始结构拍平成前端可直接渲染的元素列表。

    layout 输出已是元素数组（或包了一层），每个元素带 center/bounds/text 等。
    只保留前端点按需要的字段，降低传输体积。
    """
    if isinstance(layout, dict):
        # 某些版本可能包成 {"elements": [...]}。
        layout = layout.get("elements") or layout.get("tree") or []
    items: list[dict] = []
    if not isinstance(layout, list):
        return items
    for el in layout:
        if not isinstance(el, dict):
            continue
        center = el.get("center")
        items.append({
            "text": el.get("text") or el.get("content-desc") or "",
            "resource_id": el.get("resource-id") or "",
            "center": _parse_center(center),
            "bounds": el.get("bounds") or "",
            "interactions": el.get("interactions") or [],
        })
    # 过滤掉没有可用文本也无坐标的噪声节点，保留可点按/有文本的优先。
    return [it for it in items if it["text"] or it["center"] or it["interactions"]]


def _parse_center(center):
    """``"[600,960]"`` → ``[600, 960]``。"""
    if not center:
        return None
    nums = re.findall(r"-?\d+", str(center))
    if len(nums) >= 2:
        return [int(nums[0]), int(nums[1])]
    return None


@router.post("/api/devices/ui/tap")
@device_mutation_guard("ui-tap", device_field="serial")
async def ui_tap(req: UiTapRequest, request: Request):
    """点按指定坐标。坐标通常取自 /ui/layout 返回的 element.center。"""
    serial = (req.serial or "").strip()
    if not serial:
        return error_response("serial is required", 400)
    if req.x is None or req.y is None:
        return error_response("x and y are required", 400)

    client_id = runtime.get_client_id_from_request(request)
    conflict = device_claim_conflict_response(
        [serial], client_id, allow_owner=True
    )
    if conflict:
        return conflict

    config = runtime.config_manager.load_config()
    cmd = f"adb -s {shlex.quote(serial)} shell input tap {int(req.x)} {int(req.y)}"

    try:
        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            out, err, code = await asyncio.to_thread(runtime.run_local_shell_command, cmd, 10)
        else:
            out, err, code = await asyncio.to_thread(_run_remote_with_connection, config, cmd, 10)
        if code != 0:
            return error_response(f"tap failed: {(err or out).strip()}", 502)
        return JSONResponse(content={"success": True, "serial": serial, "x": req.x, "y": req.y})
    except Exception as exc:
        logger.warning("[UI Control] tap failed for %s: %s", serial, exc)
        return error_response(str(exc), 500)
