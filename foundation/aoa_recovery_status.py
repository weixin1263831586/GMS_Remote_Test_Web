"""Explain the failed recovery stage without misreporting an already connected ADB."""
from __future__ import annotations


def recovery_failure_reason(result: dict, error: str = "") -> str:
    if error:
        return error
    if result.get("adb_recovered") is True:
        if result.get("native_mode_restored") is False:
            return "ADB 已连接，但原生 USB 模式恢复未确认；请检查 USB 状态"
        if result.get("cleanup_errors"):
            return "ADB 已连接，但 AOA USB 清理仍有异常；请检查清理诊断"
        return result.get("adb_hint") or "ADB 已连接，但 AOA 执行结果未通过验证；请检查回放结果"
    return result.get("adb_hint") or {
        "unauthorized": "ADB 尚未授权，请在设备上确认允许 USB 调试",
        "offline": "ADB 处于 offline，请检查 USB 连接",
        "absent": "尚未检测到 ADB，请检查 USB 调试开关及连接",
        "identity_mismatch": "ADB USB 物理端口与回放目标不一致，请核对目标设备",
        "ambiguous": "ADB 目标设备不唯一，请核对 USB 设备身份",
        "io_dead": "ADB 显示 device 但 I/O 无响应（adbd 半死）；请重启设备后再验证",
    }.get(result.get("adb_state"), "ADB 尚未恢复或尚未授权")
