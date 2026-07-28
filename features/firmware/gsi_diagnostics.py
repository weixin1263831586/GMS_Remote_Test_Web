from __future__ import annotations

import re


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def diagnose_gsi_burn_failure(output: str) -> str:
    """Convert common fastboot failures into an actionable user-facing message."""
    clean_output = _ANSI_ESCAPE_RE.sub("", output or "").strip()
    lowered = clean_output.lower()

    if (
        "command not available on locked devices" in lowered
        or "download is not allowed on locked devices" in lowered
        or "flashing is not allowed in lock state" in lowered
    ):
        return (
            "设备 Bootloader 自动解锁未成功，仍禁止删除、调整或写入分区。"
            "请确认设备允许 OEM 解锁并检查解锁命令输出。"
        )

    if "partition should be flashed in fastbootd" in lowered:
        return "设备未处于 Fastbootd，无法写入动态分区；请重新进入 Fastbootd 后重试。"

    if "no such file or directory" in lowered or "cannot load" in lowered:
        return "GSI 镜像文件不存在或无法读取，请检查 System/Vendor Boot 镜像路径和权限。"

    if "device " in lowered and " not found" in lowered:
        return "Fastboot 未找到目标设备，请检查 USB 连接、设备序列号和当前 Fastboot/Fastbootd 状态。"

    meaningful_lines = [
        line.strip()
        for line in clean_output.splitlines()
        if line.strip() and not line.strip().startswith("< waiting for")
    ]
    if meaningful_lines:
        return " ".join(meaningful_lines[-3:])[-600:]
    return "GSI 烧写命令执行失败，未返回有效错误信息；请检查设备连接和 Fastboot 状态。"
