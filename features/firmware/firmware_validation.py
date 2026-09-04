"""Rockchip update-image validation used before entering Loader mode."""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MAX_DIAGNOSTIC_CHARS = 1200


@dataclass(frozen=True)
class FirmwareValidationResult:
    valid: bool
    message: str = ""
    output: str = ""


def _clean_output(value: str) -> str:
    cleaned = _ANSI_ESCAPE_RE.sub("", value or "").replace("\r", "\n")
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines)[-_MAX_DIAGNOSTIC_CHARS:]


def _result(return_code: int, output: str) -> FirmwareValidationResult:
    diagnostic = _clean_output(output)
    if return_code == 0:
        return FirmwareValidationResult(valid=True, output=diagnostic)

    reason = "upgrade_tool 无法解析 update.img"
    action = (
        "请重新选择源固件上传，系统会按内容指纹建立新会话；"
        "若重新上传后仍失败，请在固件产出端用与目标芯片匹配的 "
        "MiniLoaderAll.bin 和同一套 Rockchip afptool/rkImageMaker 重新生成。"
    )
    lowered = diagnostic.lower()
    if "wrong hash of loader" in lowered:
        reason = "update.img 内部 Loader 哈希校验失败"
    elif "wrong hash of firmware" in lowered:
        reason = "update.img 固件哈希校验失败"
    elif "invalid tag of loader" in lowered:
        reason = "update.img 内部 Loader 标记无效"
    elif "invalid tag of firmware" in lowered:
        reason = "update.img 固件标记无效"
    elif "failed to create update object" in lowered:
        reason = "当前 upgrade_tool 无法创建该 update.img 的解析对象"

    message = (
        f"固件预检失败，设备尚未重启：{reason}。"
        f"{action}"
    )
    if diagnostic:
        message += f"\n诊断输出：{diagnostic}"
    return FirmwareValidationResult(valid=False, message=message, output=diagnostic)


def validate_local_update_image(
    tool_path: str,
    firmware_path: str,
    *,
    timeout: int = 120,
) -> FirmwareValidationResult:
    try:
        completed = subprocess.run(
            [tool_path, "SFI", firmware_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return FirmwareValidationResult(
            valid=False,
            message=f"固件预检超时（{timeout} 秒），设备尚未重启。",
        )
    except OSError as exc:
        return FirmwareValidationResult(
            valid=False,
            message=f"无法执行固件预检工具，设备尚未重启：{exc}",
        )
    return _result(completed.returncode, completed.stdout or "")


def validate_remote_update_image(
    ssh_manager,
    ssh,
    tool_path: str,
    firmware_path: str,
    *,
    timeout: int = 120,
) -> FirmwareValidationResult:
    command = f"{shlex.quote(tool_path)} SFI {shlex.quote(firmware_path)}"
    try:
        result = ssh_manager.execute_command(
            ssh,
            command,
            timeout=timeout,
        )
    except Exception as exc:
        return FirmwareValidationResult(
            valid=False,
            message=f"远端固件预检执行失败，设备尚未重启：{exc}",
        )
    return _result(result.code, "\n".join(
        part for part in (result.stdout, result.stderr) if part
    ))
