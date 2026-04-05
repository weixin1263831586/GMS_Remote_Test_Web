"""
设备工具类 - 提供设备相关的通用工具函数

整合重复的设备解析、窗口计算等逻辑
"""
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class DeviceUtils:
    """设备工具类"""

    @staticmethod
    def parse_adb_devices(output: str) -> List[str]:
        """
        解析ADB设备列表输出

        Args:
            output: `adb devices` 命令的输出

        Returns:
            设备ID列表
        """
        devices = []
        for line in output.split('\n')[1:]:
            if line.strip() and '\tdevice' in line:
                device_id = line.split('\t')[0]
                devices.append(device_id)
        return devices

    @staticmethod
    def calculate_window_positions(
        devices: List[str],
        screen_width: int = 1920,
        screen_height: int = 1080,
        max_window_width: int = 350
    ) -> Dict[str, Any]:
        """
        计算投屏窗口的位置和大小

        Args:
            devices: 设备ID列表
            screen_width: 屏幕宽度，默认1920
            screen_height: 屏幕高度，默认1080
            max_window_width: 窗口最大宽度，默认350

        Returns:
            dict: 包含窗口大小和起始位置的字典
                {
                    'window_width': int,
                    'window_height': int,
                    'start_x': int,
                    'start_y': int,
                    'horizontal_gap': int
                }
        """
        devices_sorted = sorted(devices)
        total_devices = len(devices_sorted)

        horizontal_gap = 20
        vertical_margin = 50

        max_available_width = screen_width - (horizontal_gap * (total_devices + 1))
        window_width = min(max_window_width, max_available_width // total_devices)
        window_height = int(window_width * 16 / 9)  # 16:9 aspect ratio

        max_height = int(screen_height * 0.7)
        if window_height > max_height:
            window_height = max_height
            window_width = int(window_height * 9 / 16)

        # Center the windows
        total_width = total_devices * window_width + (total_devices - 1) * horizontal_gap
        start_x = max(horizontal_gap, (screen_width - total_width) // 2)
        start_y = max(vertical_margin, (screen_height - window_height) // 2)

        return {
            'window_width': window_width,
            'window_height': window_height,
            'start_x': start_x,
            'start_y': start_y,
            'horizontal_gap': horizontal_gap
        }

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
        vertical_margin: int = 50
    ) -> Dict[str, int]:
        """
        计算单个设备的窗口位置

        Args:
            device_index: 设备索引（从0开始）
            window_width: 窗口宽度
            window_height: 窗口高度
            start_x: 起始X坐标
            start_y: 起始Y坐标
            horizontal_gap: 水平间距
            screen_width: 屏幕宽度
            screen_height: 屏幕高度
            vertical_margin: 垂直边距

        Returns:
            {'x_offset': int, 'y_offset': int}
        """
        x_offset = start_x + device_index * (window_width + horizontal_gap)
        y_offset = start_y

        # 边界检查
        if x_offset + window_width > screen_width:
            x_offset = max(0, screen_width - window_width - horizontal_gap)
        if y_offset + window_height > screen_height:
            y_offset = max(0, screen_height - window_height - vertical_margin)

        return {'x_offset': x_offset, 'y_offset': y_offset}

    @staticmethod
    def check_process_running(ssh, process_pattern: str) -> bool:
        """
        检查进程是否运行

        Args:
            ssh: SSH连接对象
            process_pattern: 进程匹配模式

        Returns:
            进程是否在运行
        """
        try:
            stdout, _, code = ssh.exec_command(f"pgrep -f '{process_pattern}'")
            output = stdout.read().decode('utf-8', errors='ignore').strip()
            return bool(output) and code == 0
        except Exception as e:
            logger.error(f"Error checking process: {e}")
            return False

    @staticmethod
    def kill_process(ssh, process_pattern: str) -> bool:
        """
        终止进程

        Args:
            ssh: SSH连接对象
            process_pattern: 进程匹配模式

        Returns:
            是否成功
        """
        try:
            ssh.exec_command(f"pkill -f '{process_pattern}'")
            return True
        except Exception as e:
            logger.error(f"Error killing process: {e}")
            return False

    @staticmethod
    def check_scrcpy_healthy(ssh, device_id: str) -> tuple[bool, Optional[str]]:
        """
        检查 scrcpy 是否健康运行（单命令高效版本）

        使用单个 SSH 命令同时检查：进程存在 + 状态正常 + 日志有 Connected

        Args:
            ssh: SSH 连接对象
            device_id: 设备 ID

        Returns:
            (is_healthy, pid_or_error)
        """
        try:
            # 单命令检查：获取 PID，验证状态为 R/S/D，并检查日志最后 2KB 是否有 Connected
            cmd = (
                f"pid=$(pgrep -f 'scrcpy.*-s {device_id}') && "
                '[ -n "$pid" ] && '
                'state=$(ps -p $pid -o state= 2>/dev/null | tr -d ' ') && '
                '[[ "$state" =~ ^[RSD]$ ]] && '
                f"tail -c 2048 /tmp/scrcpy_{device_id}.log 2>/dev/null | grep -q 'Connected' && "
                'echo $pid || echo ""'
            )
            stdout, _, code = ssh.exec_command(cmd)
            pid = stdout.read().decode('utf-8', errors='ignore').strip()
            return (bool(pid), pid if pid else None)
        except Exception as e:
            logger.error(f"Error checking scrcpy health for {device_id}: {e}")
            return (False, str(e))