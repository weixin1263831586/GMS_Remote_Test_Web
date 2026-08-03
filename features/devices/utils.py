"""设备工具类 - 提供设备解析、窗口计算、进程管理等通用工具函数。"""
import logging
import shlex
from typing import Any

from foundation.security import sanitize_device_ids
from foundation.window_layout import (
    calculate_device_window_position,
    calculate_window_positions,
)


logger = logging.getLogger(__name__)


def _require_safe_device(device_id: str) -> str:
    """Validate a single device id and return the sanitized serial."""
    safe_devices = sanitize_device_ids([device_id])
    if not safe_devices:
        raise ValueError(f"Invalid device id: {device_id!r}")
    return safe_devices[0]


class DeviceUtils:
    """设备工具类"""

    @staticmethod
    def parse_adb_devices(output: str) -> list[str]:
        """解析 `adb devices` 命令输出，返回处于 device 状态的设备ID列表。

        过滤掉 localhost:<port> 形式的 Microdroid/vsock 虚拟机设备，
        它们由 GTS/VTS 虚拟化测试临时创建，不属于真实物理设备。
        adbproxy-rs 接入的设备使用独立的 inventory 探测路径（带 proxy_source
        标记），不经过此函数。
        """
        return [
            line.split('\t')[0]
            for line in output.split('\n')[1:]
            if line.strip()
            and '\tdevice' in line
            and not line.split('\t')[0].startswith('localhost:')
        ]

    @staticmethod
    def parse_fastboot_devices(output: str) -> list[str]:
        """解析 `fastboot devices` 输出，兼容 bootloader Fastboot 和 Fastbootd。"""
        devices = []
        for line in (output or "").splitlines():
            parts = line.split()
            if not parts:
                continue
            if len(parts) >= 2 and parts[1].lower() in {"fastboot", "fastbootd"}:
                devices.append(parts[0])
        return devices

    @staticmethod
    def calculate_window_positions(
        devices: list[str],
        screen_width: int = 1920,
        screen_height: int = 1080,
        max_window_width: int = 350,
    ) -> dict[str, Any]:
        """计算投屏窗口的位置和大小。返回 window_width/height, start_x/y, horizontal_gap。"""
        return calculate_window_positions(
            devices,
            screen_width=screen_width,
            screen_height=screen_height,
            max_window_width=max_window_width,
        )

    @staticmethod
    def calculate_device_window_position(
        device_index: int,
        window_width: int,
        window_height: int,
        start_x: int,
        start_y: int,
        horizontal_gap: int,
        screen_width: int = 1920,
        screen_height: int = 1080,
        vertical_margin: int = 50,
    ) -> dict[str, int]:
        """计算单个设备的窗口位置，含边界检查。返回 {'x_offset', 'y_offset'}。"""
        return calculate_device_window_position(
            device_index,
            window_width,
            window_height,
            start_x,
            start_y,
            horizontal_gap,
            screen_width=screen_width,
            screen_height=screen_height,
            vertical_margin=vertical_margin,
        )

    @staticmethod
    def kill_process(ssh, process_pattern: str) -> bool:
        """终止匹配 process_pattern 的远程进程。"""
        try:
            ssh.exec_command(f"pkill -f -- {shlex.quote(process_pattern)}")
            return True
        except Exception as e:
            logger.error(f"Error killing process: {e}")
            return False

    @staticmethod
    def scrcpy_log_path(device_id: str) -> str:
        """Return a safe per-device scrcpy log path."""
        return f"/tmp/scrcpy_{_require_safe_device(device_id)}.log"

    @staticmethod
    def scrcpy_process_pattern(device_id: str) -> str:
        return f"scrcpy.*-s {_require_safe_device(device_id)}"

    @staticmethod
    def build_scrcpy_command(
        *,
        scrcpy_path: str,
        device_id: str,
        ubuntu_user: str,
        x_offset: int,
        y_offset: int,
        window_width: int,
        window_height: int,
        use_gdm_xauthority_fallback: bool = False,
        background: bool = True,
    ) -> str:
        safe_device = _require_safe_device(device_id)
        log_path = f"/tmp/scrcpy_{safe_device}.log"
        quoted_scrcpy = scrcpy_path if scrcpy_path == "scrcpy" else shlex.quote(scrcpy_path)
        quoted_user = shlex.quote(ubuntu_user)
        quoted_device = shlex.quote(safe_device)
        quoted_log = shlex.quote(log_path)
        quoted_title = shlex.quote(safe_device)
        launcher = "nohup " if background else ""
        suffix = " &" if background else ""

        if use_gdm_xauthority_fallback:
            xauthority = (
                "if [ -f /run/user/1000/gdm/Xauthority ]; then "
                "export XAUTHORITY=/run/user/1000/gdm/Xauthority; "
                "else "
                f"export XAUTHORITY=/home/{quoted_user}/.Xauthority; "
                "fi"
            )
        else:
            xauthority = f"export XAUTHORITY=/home/{quoted_user}/.Xauthority"

        return (
            "export DISPLAY=:0 && "
            f"{xauthority} && "
            f"{launcher}{quoted_scrcpy} -s {quoted_device} "
            "--max-size 800 "
            "--no-control "
            f"--window-title {quoted_title} "
            f"--window-x {int(x_offset)} "
            f"--window-y {int(y_offset)} "
            f"--window-width {int(window_width)} "
            f"--window-height {int(window_height)} "
            f"> {quoted_log} 2>&1{suffix}"
        )

    @staticmethod
    def check_scrcpy_healthy(ssh, device_id: str) -> tuple[bool, str | None]:
        """检查 scrcpy 是否健康运行。单命令检查进程 + 状态 + 日志 Connected。返回 (is_healthy, pid_or_error)。"""
        try:
            pattern = DeviceUtils.scrcpy_process_pattern(device_id)
            log_path = DeviceUtils.scrcpy_log_path(device_id)
            cmd = (
                f"pid=$(pgrep -f -- {shlex.quote(pattern)} | head -n 1) && "
                '[ -n "$pid" ] && '
                'state=$(ps -p $pid -o state= 2>/dev/null | tr -d \' \') && '
                '[[ "$state" =~ ^[RSD]$ ]] && '
                f"tail -c 2048 {shlex.quote(log_path)} 2>/dev/null | grep -q 'Connected' && "
                'echo $pid || echo ""'
            )
            stdout, _, _ = ssh.exec_command(cmd)
            pid = stdout.read().decode('utf-8', errors='ignore').strip()
            return (bool(pid), pid or None)
        except Exception as e:
            logger.error(f"Error checking scrcpy health for {device_id}: {e}")
            return (False, str(e))
