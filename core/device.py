"""
设备管理 - 核心业务逻辑
"""
import logging
import time
from typing import List, Dict, Any
from .ssh import ssh_manager
from .config import config_manager
from .device_utils import DeviceUtils

logger = logging.getLogger(__name__)


class DeviceManager:
    """
    设备管理器

    特性：
    - 设备列表查询
    - 设备信息获取
    - 设备锁定管理
    - 设备操作（重启、remount等）
    """

    def __init__(self):
        """初始化设备管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def get_connected_devices(
        self,
        force_refresh: bool = False,
        ssh=None
    ) -> List[str]:
        """
        获取已连接的Android设备列表

        Args:
            force_refresh: 是否强制刷新
            ssh: SSH连接（如果不提供则创建新连接）

        Returns:
            设备ID列表
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                logger.error("[Device] Failed to get SSH connection")
                return []
            created_ssh = True
        else:
            created_ssh = False

        try:
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                "adb devices",
                timeout=10
            )

            # 使用 DeviceUtils 解析设备列表
            return DeviceUtils.parse_adb_devices(output)

        except Exception as e:
            logger.error(f"[Device] Error getting devices: {e}")
            return []
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def get_device_info(
        self,
        device_id: str,
        ssh=None
    ) -> Dict[str, Any]:
        """
        获取设备详细信息

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            设备信息字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {}
            created_ssh = True
        else:
            created_ssh = False

        try:
            info = {}

            # 定义信息获取命令
            info_commands = {
                'serial_no': f"adb -s {device_id} shell getprop ro.serialno",
                'model': f"adb -s {device_id} shell getprop ro.product.model",
                'android_version': f"adb -s {device_id} shell getprop ro.build.version.release",
                'build_type': f"adb -s {device_id} shell getprop ro.build.type",
                'build_tags': f"adb -s {device_id} shell getprop ro.build.tags",
                'build_date': f"adb -s {device_id} shell getprop ro.build.date",
                'sdk_version': f"adb -s {device_id} shell getprop ro.build.version.sdk",
                'security_patch': f"adb -s {device_id} shell getprop ro.build.version.security_patch",
                'fingerprint': f"adb -s {device_id} shell getprop ro.build.fingerprint",
            }

            for key, cmd in info_commands.items():
                try:
                    output, _, _ = self.ssh_manager.execute_command(
                        ssh,
                        cmd,
                        timeout=10
                    )
                    value = output.strip()
                    if '\n' in value:
                        value = value.split('\n')[0].strip()
                    info[key] = value or "未知"
                except:
                    info[key] = "未知"

            return info

        except Exception as e:
            logger.error(f"[Device] Error getting device info: {e}")
            return {}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def reboot_device(
        self,
        device_id: str,
        ssh=None
    ) -> Dict[str, Any]:
        """
        重启设备

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            结果字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}
            created_ssh = True
        else:
            created_ssh = False

        try:
            # 执行重启
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} reboot",
                timeout=30
            )

            if code != 0:
                return {
                    'success': False,
                    'error': error or '重启命令执行失败'
                }

            # 等待设备重新上线（最多60秒）
            start_time = time.time()
            while time.time() - start_time < 60:
                check_output, _, _ = self.ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} get-state",
                    timeout=10
                )
                if 'device' in check_output.lower():
                    wait_time = time.time() - start_time
                    return {
                        'success': True,
                        'back_online': True,
                        'wait_time': round(wait_time, 1)
                    }
                time.sleep(2)

            return {
                'success': True,
                'back_online': False,
                'wait_time': 60.0
            }

        except Exception as e:
            logger.error(f"[Device] Error rebooting device: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def remount_device(
        self,
        device_id: str,
        ssh=None
    ) -> Dict[str, Any]:
        """
        Remount设备（root权限）

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            结果字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}
            created_ssh = True
        else:
            created_ssh = False

        try:
            # 执行 adb root
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} root",
                timeout=15
            )
            time.sleep(2)

            # 执行 remount
            remount_output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} remount",
                timeout=15
            )

            # 检查 veritymode
            verity_output, _, _ = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} shell getprop ro.boot.veritymode",
                timeout=10
            )
            verity_mode = verity_output.strip()

            # 判断是否需要重启 - 基于实际的 remount 输出
            # 关键指示：如果输出包含 "Now reboot your device" 则需要重启
            # 如果输出包含 "Overlayfs enabled" 或 "Remount succeeded" (无重启提示) 则已完成
            needs_reboot = 'Now reboot your device' in remount_output
            overlayfs_enabled = 'Overlayfs enabled' in remount_output or 'overlayfs' in remount_output.lower()

            # 如果启用了 overlayfs，说明已经完成 remount，不需要重启
            if overlayfs_enabled:
                needs_reboot = False
                verity_mode = 'disabled'  # 逻辑上设置为 disabled

            result = {
                'success': code == 0,
                'verity_mode': verity_mode,
                'needs_reboot': needs_reboot,
                'overlayfs_enabled': overlayfs_enabled,
                'output': remount_output[-500:] if remount_output else error
            }

            if needs_reboot:
                result['warning'] = '设备需要重启才能使 remount 生效'
            elif overlayfs_enabled:
                result['info'] = '设备已启用 overlayfs，处于读写模式'

            return result

        except Exception as e:
            logger.error(f"[Device] Error remounting device: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)


# 全局设备管理器实例
device_manager = DeviceManager()
