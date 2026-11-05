"""Convert recorder video pixels to current Android logical display pixels."""
from __future__ import annotations

import re

from foundation.aoa_display import input_displays


def _primary_viewport(result):
    if result.returncode != 0:
        return None
    return next((item for item in input_displays(result.stdout or "") if item["display_id"] == 0), None)


def resolve_frame_input(serial, action, display, adb_run):
    """Legacy inputs use native pixels; Web inputs explicitly carry frame size.

    scrcpy max_size changes the encoded resolution, independently of wm size
    and CSS scaling. Input viewports already include Android display rotation.
    """
    frame = action.get("frame_size")
    if frame is None:
        return action, display
    result = adb_run(serial, "shell", "dumpsys", "input", timeout=10)
    dump = result.stdout or ""
    selected = _primary_viewport(result)
    if selected is None:
        # Some Android builds omit a usable InputReader viewport. The display
        # service exposes the same current logical viewport independently.
        manager = adb_run(serial, "shell", "dumpsys", "display", timeout=10)
        selected = _primary_viewport(manager)
    if selected:
        display = selected["display"]
    else:
        if result.returncode != 0:
            raise ValueError("无法核对投屏坐标：读取设备显示信息失败，请检查 ADB 连接后重试")
        rotations = set(re.findall(r"SurfaceOrientation:\s*([0-3])\b", dump))
        if "Viewport" in dump or len(rotations) != 1:
            raise ValueError("无法核对投屏坐标：未取得唯一有效的主显示器视口，请检查设备显示配置")
        if int(next(iter(rotations))) % 2:
            display = display[::-1]
    width, height = display
    if not all(type(n) is int and 1 <= n <= 10000 for n in display):
        raise ValueError("无法核对投屏坐标：设备显示尺寸无效")
    # Encoders round dimensions to block boundaries; tolerate that small error,
    # but never stretch a stale portrait frame onto a landscape display.
    products = (frame[0] * height, frame[1] * width)
    if abs(products[0] - products[1]) > max(products) * 0.02:
        raise ValueError("投屏画面与设备方向或尺寸不一致，请等待新画面后重试")
    converted = dict(action)
    pairs = (("x1", "y1"), ("x2", "y2")) if action["action"] == "swipe" else (("x", "y"),)
    for x, y in pairs:
        converted[x] = min(width - 1, round(action[x] * width / frame[0]))
        converted[y] = min(height - 1, round(action[y] * height / frame[1]))
    return converted, (width, height)
