"""Stable user-facing messages for serial transport failures."""

from __future__ import annotations

import errno


def friendly_serial_error(exc: Exception) -> str:
    number = getattr(exc, "errno", None)
    text = str(exc)
    lowered = text.lower()
    if number in {errno.EACCES, errno.EPERM} or "permission denied" in lowered:
        return "串口权限不足：服务用户需加入 dialout 组并重新登录"
    if number == errno.EBUSY or "resource busy" in lowered:
        return "串口被占用，请关闭 picocom/minicom 等程序"
    if "no such file" in lowered or number == errno.ENOENT:
        return "串口已拔出或设备节点不存在"
    if number in {errno.EIO, getattr(errno, "EPROTO", 71)} or any(
        marker in lowered for marker in ("input/output error", "protocol error")
    ):
        return "USB 串口通信异常：请重新插拔 FTDI 或更换 USB 端口，系统将自动重连"
    return f"串口错误：{text}"
