"""设备工具类 - 提供设备解析、窗口计算、进程管理等通用工具函数。"""
import logging
from typing import Any

from foundation.window_layout import (
    calculate_device_window_position,
    calculate_window_positions,
)


logger = logging.getLogger(__name__)


class DeviceUtils:
    """设备工具类"""

    @staticmethod
    def parse_adb_devices(output: str) -> list[str]:
        """解析 `adb devices` 命令输出，返回设备ID列表。"""
        return [line.split('\t')[0] for line in output.split('\n')[1:] if line.strip() and '\tdevice' in line]

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
            ssh.exec_command(f"pkill -f '{process_pattern}'")
            return True
        except Exception as e:
            logger.error(f"Error killing process: {e}")
            return False

    @staticmethod
    def check_scrcpy_healthy(ssh, device_id: str) -> tuple[bool, str | None]:
        """检查 scrcpy 是否健康运行。单命令检查进程 + 状态 + 日志 Connected。返回 (is_healthy, pid_or_error)。"""
        try:
            cmd = (
                f"pid=$(pgrep -f 'scrcpy.*-s {device_id}') && "
                '[ -n "$pid" ] && '
                'state=$(ps -p $pid -o state= 2>/dev/null | tr -d \' \') && '
                '[[ "$state" =~ ^[RSD]$ ]] && '
                f"tail -c 2048 /tmp/scrcpy_{device_id}.log 2>/dev/null | grep -q 'Connected' && "
                'echo $pid || echo ""'
            )
            stdout, _, _ = ssh.exec_command(cmd)
            pid = stdout.read().decode('utf-8', errors='ignore').strip()
            return (bool(pid), pid or None)
        except Exception as e:
            logger.error(f"Error checking scrcpy health for {device_id}: {e}")
            return (False, str(e))
