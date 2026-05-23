"""
ADB转发 - 核心业务逻辑

特性：
- ADB端口转发
- SSH隧道
- 设备连接管理
"""

import logging
import time
import shlex
from typing import Dict, Any

from .ssh import ssh_manager
from .config import config_manager
from .device_utils import DeviceUtils

logger = logging.getLogger(__name__)


class ADBForwardManager:
    """
    ADB转发管理器

    特性：
    - ADB端口转发启动/停止
    - SSH隧道管理
    - 设备连接监控
    """

    def __init__(self):
        """初始化ADB转发管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_tunnels: Dict[str, Any] = {}  # {client_id: tunnel_info}

    def start_forward(
        self,
        device_host: str,
        device_password: str = None
    ) -> Dict[str, Any]:
        """
        启动ADB端口转发

        Args:
            device_host: 设备主机地址（格式: user@ip）
            device_password: 设备主机密码

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
                # 清理旧的SSH隧道
                self.ssh_manager.execute_command(ssh, "pkill -f adb; pkill -f 'ssh.*-L 5037'")
                time.sleep(1)

                # 检测设备主机类型
                is_windows = 'windows' in device_host.lower()

                # 启动设备主机上的ADB server
                if is_windows:
                    # Windows主机：需要通过SSH连接到Windows
                    # 这里简化处理，实际需要更复杂的SSH转发
                    result = {
                        'success': True,
                        'warning': 'Windows设备主机ADB支持待完善',
                        'devices': []
                    }
                else:
                    # Linux设备主机
                    start_adb_cmd = f"ssh {device_host} 'adb kill-server; adb -a nodaemon server start &'"
                    self.ssh_manager.execute_command(ssh, start_adb_cmd)
                    time.sleep(2)

                    # 设置SSH隧道
                    forward_target = "localhost:5037"

                    if device_password:
                        # 使用sshpass（需要安装）
                        safe_password = shlex.quote(device_password)
                        forward_cmd = f"SSHPASS={safe_password} sshpass -e ssh -f -N -L 5037:{forward_target} {device_host}"
                    else:
                        # 使用密钥认证
                        forward_cmd = f"ssh -f -N -L 5037:{forward_target} {device_host}"

                    self.ssh_manager.execute_command(ssh, forward_cmd, timeout=10)
                    time.sleep(3)

                    # 测试连接
                    test_output, test_error, test_code = self.ssh_manager.execute_command(
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

                self.ssh_manager.return_connection(ssh)
                return result

            except Exception as e:
                self.ssh_manager.return_connection(ssh)
                logger.error(f"Error starting ADB forward: {e}")
                return {'success': False, 'error': str(e)}

        except Exception as e:
            logger.error(f"Error in start_forward: {e}")
            return {'success': False, 'error': str(e)}

    def stop_forward(self, client_id: str = None) -> Dict[str, Any]:
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
                # 停止SSH隧道和ADB进程
                self.ssh_manager.execute_command(ssh, "pkill -f 'ssh.*5037'")
                self.ssh_manager.execute_command(ssh, "pkill -f 'adb.*forward'")
                self.ssh_manager.execute_command(ssh, "adb disconnect")

                # 清除活动隧道记录
                if client_id and client_id in self.active_tunnels:
                    del self.active_tunnels[client_id]

                self.ssh_manager.return_connection(ssh)

                return {'success': True, 'message': '✅ ADB端口转发已停止'}

            except Exception as e:
                self.ssh_manager.return_connection(ssh)
                logger.error(f"Error stopping ADB forward: {e}")
                return {'success': False, 'error': str(e)}

        except Exception as e:
            logger.error(f"Error in stop_forward: {e}")
            return {'success': False, 'error': str(e)}


# 全局ADB转发管理器实例
adb_forward_manager = ADBForwardManager()
