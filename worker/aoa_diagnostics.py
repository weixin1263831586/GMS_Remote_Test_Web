"""Retain failed-step evidence without guessing which Android window was hit."""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)


def confirm_usb_restore(result, observe_native):
    """A completed replay can outlive the USB handle used by cleanup.

    Only a fresh native USB identity plus authorized ADB can resolve these
    cleanup errors. Incomplete input and other cleanup failures stay failures.
    """
    errors = result.get("cleanup_errors") or []
    total = result.get("commands_total")
    if (result.get("adb_recovered") is not True or type(total) is not int or total <= 0
            or result.get("commands_executed") != total or result.get("failed_index")
            or not errors or any(item.get("step") not in {"reset_usb", "restore_native_mode"} for item in errors)):
        return
    result["native_mode_restored"] = bool(observe_native())
    if not result["native_mode_restored"]:
        result["reconcile_required"] = True
        return
    result.update(native_mode_restored=True, cleanup_errors=[], reconcile_required=False)
    result["cleanup_warnings"] = [*(result.get("cleanup_warnings") or []), *errors]


def record_failure(error, *, index, total, command, completed,
                   reports_sent=0, reports_attempted=0, last_input=None):
    last_index, last_command = last_input or (0, "")
    result = {
        "success": False, "reconcile_required": True,
        "commands_total": total, "commands_executed": completed,
        "failed_index": index, "failed_command": str(command),
        "failed_input_state": "uncertain" if reports_attempted else "not_sent",
        "failed_reports_sent": reports_sent,
        "last_input_index": last_index, "last_input_command": last_command,
    }
    error.aoa_result = result
    return result


def inspect_failed_adb(error, *, observe, suppressed, validate):
    """Observe only after USB cleanup and daemon suppression have ended.

    ADB availability never turns an interrupted gesture into successful replay.
    The observer must enforce the original serial and physical USB port.
    """
    result = getattr(error, "aoa_result", None)
    if result is None or getattr(error, "cancelled", False):
        return
    try:
        validate()
        if suppressed():
            return
        state = observe()
        validate()
        result.update(state)
        if state.get("adb_recovered") is True:
            result["adb_hint"] = "ADB 已连接，但回放中断；请核对当前页面，无需重新烧写或重复点击 USB 调试开关"
        elif state.get("adb_state") == "unauthorized":
            result["adb_hint"] = "检测到 ADB unauthorized，请在设备上核对并确认电脑 RSA 授权弹框；不要再次点击 USB 调试开关"
        result["success"] = False
    except Exception:
        # Fencing loss or a failed read must not replace the initial USB error.
        logger.warning("Unable to inspect ADB after interrupted AOA replay", exc_info=True)


def failure_detail(result):
    if not result:
        return ""
    detail = ""
    if result.get("failed_index"):
        state = ("本条尚未发送 HID" if result.get("failed_input_state") == "not_sent"
                 else "本条 HID 已尝试发送，输入结果不确定，未自动重放")
        detail = (f"AOA 第 {result['failed_index']}/{result['commands_total']} 条 "
                  f"{result['failed_command']} 失败（{state}）")
        if result.get("last_input_index"):
            detail += (f"；上一条完成的输入：第 {result['last_input_index']} 条 "
                       f"{result['last_input_command']}（仅确认报文发送）")
    if result.get("adb_state"):
        detail += f"；ADB 状态：{result['adb_state']}"
    if result.get("adb_hint"):
        detail += f"；{result['adb_hint']}"
    return detail.lstrip("；")
