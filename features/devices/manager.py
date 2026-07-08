"""
设备管理 - 核心业务逻辑
"""
import logging
import re
import subprocess
import threading
from typing import Any

from foundation.networking import is_local_host

from .adb_ops import reboot_with_runner, root_and_remount
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
_local_adb_devices_lock = threading.Lock()


def has_blocked_adb_process() -> bool:
    try:
        result = subprocess.run(
            ["ps", "-eo", "stat,args"],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return False

    adb_waiting_count = 0
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        stat, args = fields
        argv0 = args.split(maxsplit=1)[0]
        is_adb = argv0 == "adb" or argv0.endswith("/adb")
        if not is_adb:
            continue
        if stat.startswith("D"):
            return True
        if "devices" in args or "fork-server server" in args:
            adb_waiting_count += 1
    if adb_waiting_count >= 5:
        logger.warning("[Device] Found %s pending adb processes; treating local adb as unhealthy", adb_waiting_count)
        return True
    return False


class DeviceManager:
    """
    设备管理器

    特性：
    - 设备列表查询
    - 设备信息获取
    - 设备锁定管理
    - 设备操作（重启、remount等）
    """

    def __init__(self, ssh_manager=None, config_manager=None):
        """初始化设备管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def get_connected_devices(
        self,
        force_refresh: bool = False,
        ssh=None
    ) -> list[str]:
        """
        获取已连接的Android设备列表

        Args:
            force_refresh: 是否强制刷新
            ssh: SSH连接（如果不提供则创建新连接）

        Returns:
            设备ID列表
        """
        config = self.config_manager.load_config()

        if ssh is None and is_local_host(config.get('ubuntu_host', '')):
            acquired = False
            try:
                if has_blocked_adb_process():
                    logger.warning("[Device] Local adb server is blocked in kernel state; skipping adb devices scan")
                    return []
                acquired = _local_adb_devices_lock.acquire(blocking=False)
                if not acquired:
                    logger.warning("[Device] Local adb devices query skipped because a previous query is still running")
                    return []
                result = subprocess.run(
                    ['adb', 'devices'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode != 0:
                    logger.warning(f"[Device] Local adb devices failed: {result.stderr.strip()}")
                    return []
                return DeviceUtils.parse_adb_devices(result.stdout)
            except FileNotFoundError:
                logger.warning("[Device] adb command not found on local host")
                return []
            except Exception as e:
                logger.error(f"[Device] Error getting local devices: {e}")
                return []
            finally:
                if acquired:
                    _local_adb_devices_lock.release()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                logger.error("[Device] Failed to get SSH connection")
                return []
            created_ssh = True
        else:
            created_ssh = False

        try:
            output, _error, _code = self.ssh_manager.execute_command(
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
    ) -> dict[str, Any]:
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

            # 属性名到输出键名的映射
            prop_map = {
                'ro.serialno': 'serial_no',
                'ro.product.model': 'model',
                'ro.build.version.release': 'android_version',
                'ro.build.type': 'build_type',
                'ro.build.tags': 'build_tags',
                'ro.build.date': 'build_date',
                'ro.build.version.sdk': 'sdk_version',
                'ro.build.version.security_patch': 'security_patch',
                'ro.build.fingerprint': 'fingerprint',
                'ro.soc.model': 'soc_model',
            }

            # 使用 getprop 不带参数输出所有属性（key=value 格式），按 key 解析更健壮
            getprop_cmd = f"adb -s {device_id} shell getprop"

            try:
                output, _, _ = self.ssh_manager.execute_command(
                    ssh,
                    getprop_cmd,
                    timeout=15
                )
                # 解析 getprop 输出: [ro.serialno]: [value]
                for line in output.strip().split('\n'):
                    m = re.match(r'\[([\w.]+)\]:\s*\[(.*)\]', line)
                    if m:
                        prop_name, value = m.group(1), m.group(2)
                        if prop_name in prop_map:
                            info[prop_map[prop_name]] = value or "未知"
                # 补充未获取到的属性
                for key in prop_map.values():
                    if key not in info:
                        info[key] = "未知"
            except Exception:
                for key in prop_map.values():
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
        ssh=None,
        wait_for_online: bool = True,
    ) -> dict[str, Any]:
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
            def run_adb(_device_id: str | None, args: str, timeout: int) -> tuple[str, int]:
                output, error, code = self.ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} {args}",
                    timeout=timeout,
                )
                return (output or error or ''), code

            reboot = reboot_with_runner(
                run_adb,
                device_id,
                wait_for_online=wait_for_online,
                wait_timeout=60,
                poll_interval=2,
            )
            if not reboot.success:
                return {'success': False, 'error': reboot.output or '重启命令执行失败'}
            if not wait_for_online:
                return {
                    'success': True,
                    'back_online': False,
                    'wait_time': 0.0,
                    'message': '重启命令已发送，设备恢复由后台监控确认'
                }
            return {
                'success': True,
                'back_online': reboot.back_online,
                'wait_time': reboot.wait_time,
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
    ) -> dict[str, Any]:
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
            def run_adb(_device_id: str | None, args: str, timeout: int) -> tuple[str, int]:
                output, error, code = self.ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} {args}",
                    timeout=timeout,
                )
                return (output or error or ''), code

            remount = root_and_remount(
                run_adb,
                device_id,
                root_timeout=15,
                remount_timeout=15,
            )
            remount_output = remount.remount_output

            # 检查 veritymode
            verity_output, _, _ = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} shell getprop ro.boot.veritymode",
                timeout=10
            )
            verity_mode = verity_output.strip()

            needs_reboot = remount.needs_reboot
            overlayfs_enabled = remount.overlayfs_enabled
            if overlayfs_enabled:
                verity_mode = 'disabled'  # 逻辑上设置为 disabled

            result = {
                'success': remount.success,
                'verity_mode': verity_mode,
                'needs_reboot': needs_reboot,
                'overlayfs_enabled': overlayfs_enabled,
                'output': remount_output[-500:] if remount_output else remount.root_output[-500:],
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
