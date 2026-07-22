"""
ADB转发 - 核心业务逻辑

特性：
- ADB端口转发
- SSH隧道
- 设备连接管理
"""

import logging
import shlex
import time
from typing import Any

from .utils import DeviceUtils


logger = logging.getLogger(__name__)


ADB_FORWARD_PORT = 5037


def _adb_tunnel_kill_command(port: int = ADB_FORWARD_PORT) -> str:
    """Kill only SSH tunnels owned by this feature, not every adb process."""
    return f"pkill -f 'ssh .* -L {port}:localhost:{port}|ssh.*-L {port}:localhost:{port}'"


class ADBForwardManager:
    """
    ADB转发管理器

    特性：
    - ADB端口转发启动/停止
    - SSH隧道管理
    - 设备连接监控
    """

    def __init__(self, ssh_manager=None, config_manager=None):
        """初始化ADB转发管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_tunnels: dict[str, Any] = {}  # {client_id: tunnel_info}

    def start_forward(
        self,
        device_host: str,
    ) -> dict[str, Any]:
        """
        启动ADB端口转发

        Args:
            device_host: 设备主机地址（格式: user@ip）
        Returns:
            结果字典
        """
        try:
            config = self.config_manager.load_config()

            if not device_host:
                device_host = config.get('device_host', '')

            if not device_host or '@' not in device_host:
                return {'success': False, 'error': '无效的设备主机地址'}

            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}

            try:
                self.ssh_manager.execute_command(ssh, _adb_tunnel_kill_command())
                time.sleep(1)

                is_windows = 'windows' in device_host.lower()

                if is_windows:
                    # Windows ADB 转发尚未实现。
                    result = {
                        'success': True,
                        'warning': 'Windows设备主机ADB支持待完善',
                        'devices': []
                    }
                else:
                    # Linux设备主机
                    safe_device_host = shlex.quote(device_host)
                    start_adb_cmd = f"ssh {safe_device_host} 'adb kill-server; adb -a nodaemon server start &'"
                    self.ssh_manager.execute_command(ssh, start_adb_cmd)
                    time.sleep(2)

                    # 设置SSH隧道
                    forward_target = f"localhost:{ADB_FORWARD_PORT}"

                    forward_cmd = (
                        f"ssh -f -N -o BatchMode=yes -o StrictHostKeyChecking=yes "
                        f"-o ExitOnForwardFailure=yes "
                        f"-L {ADB_FORWARD_PORT}:{forward_target} {safe_device_host}"
                    )

                    self.ssh_manager.execute_command(ssh, forward_cmd, timeout=10)
                    time.sleep(3)

                    # 测试连接
                    test_output, _test_error, _test_code = self.ssh_manager.execute_command(
                        ssh,
                        "adb devices",
                        timeout=10
                    )

                    # 使用 DeviceUtils 解析设备列表
                    devices = DeviceUtils.parse_adb_devices(test_output)

                    result = {
                        'success': True,
                        'devices': devices,
                        'device_count': len(devices),
                        'adb_output': test_output[:500],
                        'message': f'✅ ADB端口转发成功! 设备: {", ".join(devices) if devices else "无"}'
                    }

                return result

            except Exception as e:
                logger.error(f"Error starting ADB forward: {e}")
                return {'success': False, 'error': str(e)}
            finally:
                self.ssh_manager.return_connection(ssh)

        except Exception as e:
            logger.error(f"Error in start_forward: {e}")
            return {'success': False, 'error': str(e)}

    def stop_forward(self, client_id: str | None = None) -> dict[str, Any]:
        """
        停止ADB端口转发

        Args:
            client_id: 客户端ID（可选）

        Returns:
            结果字典
        """
        try:
            config = self.config_manager.load_config()
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}

            try:
                self.ssh_manager.execute_command(ssh, _adb_tunnel_kill_command())
                self.ssh_manager.execute_command(ssh, "adb disconnect")

                # 清除活动隧道记录
                if client_id and client_id in self.active_tunnels:
                    del self.active_tunnels[client_id]

                return {'success': True, 'message': '✅ ADB端口转发已停止'}

            except Exception as e:
                logger.error(f"Error stopping ADB forward: {e}")
                return {'success': False, 'error': str(e)}
            finally:
                self.ssh_manager.return_connection(ssh)

        except Exception as e:
            logger.error(f"Error in stop_forward: {e}")
            return {'success': False, 'error': str(e)}


# 全局ADB转发管理器实例
adb_forward_manager = ADBForwardManager()
