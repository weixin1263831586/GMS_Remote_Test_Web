"""AOA USB 调试闭环恢复：无 ADB 完成 OFF→ON 并自动验收。

实机（RK3576 SSI 17 user build）沉淀的固件行为知识，换设备适配时按此
流程重新测定，机制层代码无需改动：

- 开关行为不对称：ON→OFF 点击开关立即提交（无弹窗）；OFF→ON 才弹
  "Allow USB debugging?" 确认框，且只在 adb 已断开的暗态出现——无法
  用 ``uiautomator dump`` 测量，OK 坐标需 adb 存活期间截图（像素级）
  或现场拍照换算获得，经 ``--ok-px``/``--scale`` 传入本工具。
- gadget quirk：AOA 会话收尾复位后部分平台会自动重枚举回配件 PID；
  ``_ensure_native_mode`` 可靠拉回原生 PID，且 USB 复位不影响应用层
  弹窗（对话框是应用窗口）。
- 标定换算：AOA 触摸为 360×640 逻辑坐标；未标定时用 AOSP 按轴映射
  （分母 max-min+1，见 ``real_to_aoa_point``），实测标定后用单一 uniform
  scale（real ÷ scale = logical）精度更高（RK3576: 3.3241），
  多点仿射标定见 foundation/aoa_calibration（ADR 0026）。

Profile 复用按固件分组（model + Android 版本 + 分辨率/fingerprint），
命名约定 ``<model>-<android>-<width>x<height>-usb-debug-on``，不按
serial 绑定。

三阶段闭环（每阶段间设备全程无 adb）：
1. OFF：点击开关提交 OFF，断言 adb 断开并回到原生 PID；
2. 弹窗：全新配件会话再点开关，OFF→ON 弹窗出现（adb 仍断开）；
3. ON：全新配件会话点击弹窗 OK，断言 adb 恢复 device 状态。

CLI::

    python -m worker_agent.aoa_recovery --serial RK3576GMS1 \
        --ok-px 1049 1063 [--scale 3.3241] [--switch-px X Y] [--wait-adb 45]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from collections.abc import Callable
from typing import Any

from foundation.aoa_profile import AOA_TOUCH_MAX_X, AOA_TOUCH_MAX_Y, real_to_aoa_point
from worker_agent.aoa_actions import (
    AoaError,
    _adb_run,
    _display_size,
    _dump_elements,
    _ensure_native_mode,
    run_aoa_sequence,
)


logger = logging.getLogger(__name__)


def _usb_core():
    # 延迟导入：与 aoa_actions._usb 一致，测试环境无需 pyusb。
    import usb.core
    return usb.core

USB_DEBUG_TITLE = "USB debugging"
SWITCH_CLASS = "android.widget.Switch"# 同一行开关与标题中心的垂直偏差上限（行高约 96-192px）。
ROW_Y_TOLERANCE_PX = 120
SCROLL_MAX_ATTEMPTS = 8
# OFF 提交后 adb 断开的观察窗口（一般 2-6s，含 gadget 重枚举抖动）。
DARK_OBSERVE_SECONDS = 12


def real_to_logical(
        x: int, y: int, display: tuple[int, int],
        scale: float | None = None) -> tuple[int, int]:
    """真实显示坐标 → AOA 逻辑坐标；标定过用 uniform scale，否则线性映射。"""
    if scale is not None:
        if not 0.05 <= float(scale) <= 100:
            raise AoaError(f"scale out of range: {scale}")
        logical_x = round(int(x) / float(scale))
        logical_y = round(int(y) / float(scale))
        return (
            min(max(logical_x, 0), AOA_TOUCH_MAX_X),
            min(max(logical_y, 0), AOA_TOUCH_MAX_Y),
        )
    return real_to_aoa_point(x, y, display[0], display[1])


def _adb_state(serial: str) -> str:
    """读取 adb devices 里该 serial 的状态（absent/offline/unauthorized/device）。"""
    completed = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True, timeout=15, check=False)
    for line in (completed.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial:
            return parts[1]
    return "absent"


def _wait_dark(serial: str, timeout_seconds: int = DARK_OBSERVE_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """等待设备脱离 adb device 状态（OFF 已提交），返回最终状态。"""
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    state = _adb_state(serial)
    while state == "device" and time.monotonic() < deadline:
        sleep(1.0)
        state = _adb_state(serial)
    return state


def measure_usb_debug_switch(
        serial: str, sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """adb 存活时打开开发者选项并定位 USB debugging 行开关。

    返回 ``{"real": (x, y), "checked": bool}``；找不到行/开关抛
    ``AoaError``（最多向下滚 SCROLL_MAX_ATTEMPTS 次）。
    """
    _adb_run(serial, "shell", "svc", "power", "stayon", "true", timeout=10)
    _adb_run(serial, "shell", "wm", "dismiss-keyguard", timeout=10)
    # 开机向导会重置开发模式（development_settings_enabled=null 时深链
    # Intent 被静默弹回桌面，实测复现）：进入前先经 adb 置位。
    _adb_run(serial, "shell", "settings", "put", "global",
             "development_settings_enabled", "1", timeout=10)
    _adb_run(serial, "shell", "am", "force-stop",
             "com.android.settings", timeout=10)
    _adb_run(serial, "shell", "am", "start", "-a",
             "android.settings.APPLICATION_DEVELOPMENT_SETTINGS", timeout=15)
    sleep(2.0)
    for _attempt in range(SCROLL_MAX_ATTEMPTS):
        nodes, _parent_of = _dump_elements(serial, sleep=sleep)
        # 行标题按语言本地化（匈牙利语 "USB hibakeresés"）：精确匹配优先，
        # 退回任意 "USB" 前缀标题（该页同前缀行唯一）。
        titles = [node for node in nodes
                  if node.get("center") and isinstance(node.get("text"), str)
                  and node["text"].startswith("USB")]
        title = next((node for node in titles
                      if node["text"] == USB_DEBUG_TITLE), None) or (
            titles[0] if titles else None)
        if title is not None:
            switches = [node for node in nodes
                        if node.get("class") == SWITCH_CLASS and node.get("center")]
            row_switch = min(
                (node for node in switches
                 if abs(node["center"][1] - title["center"][1]) <= ROW_Y_TOLERANCE_PX),
                key=lambda node: abs(node["center"][1] - title["center"][1]),
                default=None,
            )
            if row_switch is None:
                raise AoaError(
                    f"USB debugging row switch not found on {serial}: "
                    f"no {SWITCH_CLASS} within {ROW_Y_TOLERANCE_PX}px of row title")
            return {"real": tuple(row_switch["center"]),
                    "checked": bool(row_switch.get("checked"))}
        _adb_run(serial, "shell", "input", "swipe",
                 "600", "900", "600", "400", "300", timeout=10)
        sleep(0.8)
    raise AoaError(f"USB debugging row not found on {serial} "
                   f"after {SCROLL_MAX_ATTEMPTS} scroll attempts")


def verify_usb_debug_loop(
        serial: str,
        ok_point: tuple[int, int] | None,
        *,
        ok_logical: tuple[int, int] | None = None,
        switch_point: tuple[int, int] | None = None,
        scale: float | None = None,
        wait_adb_seconds: int = 45,
        sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """三阶段闭环：AOA 关断 → 暗态点开关弹窗 → 点 OK 恢复 adb。

    ``ok_point``/``switch_point`` 为真实显示像素坐标（截图测量）；
    ``ok_logical`` 已是 AOA 逻辑坐标时直接使用（二选一，必须给一个）。
    内部统一经 ``real_to_logical`` 换算。任一阶段断言失败时返回
    ``{"success": False, "steps": [...]}``（不抛异常，便于汇总上报）。
    """
    if ok_logical is None and ok_point is None:
        raise AoaError("ok_point 或 ok_logical 必须提供一个")
    steps: list[dict[str, Any]] = []

    def step(phase: str, detail: str, ok: bool, **extra: Any) -> None:
        entry = {"phase": phase, "detail": detail, "ok": ok} | extra
        steps.append(entry)
        if ok:
            logger.info("aoa-recovery %s %s [%s] %s", serial, phase, "PASS", detail)
        else:
            logger.warning("aoa-recovery %s %s [FAIL] %s", serial, phase, detail)

    display = _display_size(serial)
    if switch_point is None:
        measured = measure_usb_debug_switch(serial, sleep=sleep)
        switch_point = measured["real"]
        if not measured["checked"]:
            return {"serial": serial, "success": False, "steps": steps,
                    "error": "USB debugging 已经是 OFF，闭环需要从 ON 起步"}
    switch_real = tuple(switch_point)
    switch_logical = real_to_logical(*switch_real, display, scale)
    if ok_logical is None:
        ok_logical = real_to_logical(*ok_point, display, scale)
    ok_command = f"click {ok_logical[0]} {ok_logical[1]}"
    switch_command = f"click {switch_logical[0]} {switch_logical[1]}"
    summary = {
        "serial": serial, "display": list(display), "scale": scale,
        "switch": {"real": list(switch_real), "logical": list(switch_logical)},
        "ok": {"real": list(ok_point) if ok_point else None,
               "logical": list(ok_logical)},
    }

    # ---- 阶段 1：OFF 提交并断言纯暗态 ----
    off_result = run_aoa_sequence(serial, [switch_command], wait_adb_seconds=0)
    step("off", f"AOA click switch {switch_logical}", bool(off_result.get("success")),
         commands_executed=off_result.get("commands_executed"))
    if not off_result.get("success"):
        return summary | {"success": False, "steps": steps}
    dark_state = _wait_dark(serial, sleep=sleep)
    step("off", f"adb left device state (state={dark_state})", dark_state != "device")
    native = _ensure_native_mode(_usb_core(), serial)
    step("off", "native PID restored (quirk re-enumeration reset)", bool(native))
    if dark_state == "device" or not native:
        return summary | {"success": False, "steps": steps}

    # ---- 阶段 2：全新配件会话点开关，OFF→ON 确认弹窗出现 ----
    dlg_result = run_aoa_sequence(serial, [switch_command], wait_adb_seconds=0)
    step("dialog", f"AOA click switch again {switch_logical}",
         bool(dlg_result.get("success")),
         commands_executed=dlg_result.get("commands_executed"))
    still_dark = _adb_state(serial) != "device"
    step("dialog", "adb still absent (dialog is app-layer, USB reset is safe)",
         still_dark)
    if not dlg_result.get("success") or not still_dark:
        return summary | {"success": False, "steps": steps}
    # quirk：会话复位可能重枚举回配件 PID；复位到原生，弹窗不受影响。
    _ensure_native_mode(_usb_core(), serial)

    # ---- 阶段 3：全新配件会话点弹窗 OK，等待 adb 恢复 ----
    on_result = run_aoa_sequence(serial, [ok_command],
                                 wait_adb_seconds=max(0, int(wait_adb_seconds)))
    step("on", f"AOA click dialog OK {ok_logical}, wait adb {wait_adb_seconds}s",
         bool(on_result.get("adb_recovered")),
         adb_state=on_result.get("adb_state"), adb_hint=on_result.get("adb_hint"))
    return summary | {
        "success": bool(on_result.get("adb_recovered")),
        "steps": steps,
        "adb_recovered": bool(on_result.get("adb_recovered")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m worker_agent.aoa_recovery",
        description="AOA 无 ADB 完成 USB 调试 OFF→ON 三阶段闭环验收")
    parser.add_argument("--serial", required=True, help="设备 adb serial")
    ok_group = parser.add_mutually_exclusive_group(required=True)
    ok_group.add_argument("--ok-px", nargs=2, type=int, metavar=("X", "Y"),
                          help="弹窗 OK 按钮真实像素坐标（截图测量）")
    ok_group.add_argument("--ok-logical", nargs=2, type=int, metavar=("X", "Y"),
                          help="弹窗 OK 的 AOA 逻辑坐标（已换算时直接给）")
    parser.add_argument("--switch-px", nargs=2, type=int, metavar=("X", "Y"),
                        help="跳过自动测量，直接给开关真实像素坐标")
    parser.add_argument("--scale", type=float, default=None,
                        help="实测 uniform 标定 scale（real÷scale=logical），"
                             "不给则按分辨率线性映射")
    parser.add_argument("--wait-adb", type=int, default=45,
                        help="阶段 3 等待 adb 恢复的秒数（默认 45）")
    args = parser.parse_args(argv)

    ok_point = tuple(args.ok_px) if args.ok_px else None
    ok_logical = tuple(args.ok_logical) if args.ok_logical else None
    result = verify_usb_debug_loop(
        args.serial,
        ok_point,
        ok_logical=ok_logical,
        switch_point=tuple(args.switch_px) if args.switch_px else None,
        scale=args.scale,
        wait_adb_seconds=args.wait_adb,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
