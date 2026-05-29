"""
USB/IP - 核心业务逻辑

特性：
- USB/IP设备转发
- Windows主机支持
- 设备绑定/解绑
"""

import logging
import time
from typing import Dict, Any, List, Optional, Tuple

from .ssh import ssh_manager
from .config import config_manager
from .common_utils import CommonUtils
from .device_utils import DeviceUtils

logger = logging.getLogger(__name__)

# usbipd 安装命令常量
USBIPD_INSTALL_CMD = 'winget install dorssel.usbipd-win --source winget'

USBIPD_INSTALL_GUIDE = '''在Windows电脑上以【管理员身份】运行PowerShell执行：
{install_cmd}
验证安装：usbipd --version'''


def split_host_port(hostname: str, default_port: int = 22) -> Tuple[str, int]:
    """Parse host[:port] for IPv4/hostname targets."""
    if not hostname:
        return hostname, default_port
    if hostname.count(':') == 1:
        host, port_text = hostname.rsplit(':', 1)
        if port_text.isdigit():
            return host, int(port_text)
    return hostname, default_port

# Shared USB/IP parsing constants
DEFAULT_ANDROID_USBIP_VID_PIDS = ('2207:0006',)
ANDROID_USBIP_MARKERS = ('android', 'adb', 'rk356', 'rockchip')


def _iter_connected_lines(output: str):
    """Yield stripped lines from the 'Connected:' section of usbipd list output."""
    in_connected = False
    for line in (output or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('Connected:'):
            in_connected = True
            continue
        if stripped.startswith('Persisted:'):
            break
        if not in_connected:
            continue
        yield stripped


def parse_usbipd_android_busids(output: str, vid_pid: Optional[str] = None) -> List[str]:
    """从 usbipd list 输出中提取 Android 设备 BUSID。"""
    vid_pids = {pid.lower() for pid in DEFAULT_ANDROID_USBIP_VID_PIDS}
    if vid_pid:
        vid_pids.add(vid_pid.lower())

    busids: List[str] = []
    for stripped in _iter_connected_lines(output):
        lowered = stripped.lower()
        if any(pid in lowered for pid in vid_pids) or any(marker in lowered for marker in ANDROID_USBIP_MARKERS):
            parts = stripped.split()
            if parts and '-' in parts[0]:
                busids.append(parts[0])
    return busids


def parse_usbipd_busid_statuses(output: str) -> Dict[str, str]:
    """Parse usbipd list Connected section into BUSID -> status."""
    statuses: Dict[str, str] = {}
    for stripped in _iter_connected_lines(output):
        parts = stripped.split()
        if parts and '-' in parts[0]:
            lowered = stripped.lower()
            if 'not shared' in lowered:
                statuses[parts[0]] = 'not_shared'
            elif 'attached' in lowered:
                statuses[parts[0]] = 'attached'
            elif 'shared' in lowered:
                statuses[parts[0]] = 'shared'
            else:
                statuses[parts[0]] = 'unknown'
    return statuses


class USBIPManager:
    """
    USB/IP管理器

    特性：
    - USB/IP设备转发
    - Windows主机支持
    - 设备绑定/解绑/attach
    """

    def __init__(self):
        """初始化USB/IP管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager
        self.active_connections: Dict[str, Any] = {}  # {client_id: connection_info}
        self.device_sources: Dict[str, Dict[str, Any]] = {}  # {device_id: source_info}

    def start_usbip(
        self,
        device_host: str,
        device_password: str = None,
        usbip_attach_host: str = None
    ) -> Dict[str, Any]:
        """
        启动USB/IP转发

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

            # 自动查找密码
            if not device_password:
                device_password = self.config_manager.find_device_host_password(
                    device_host,
                    config
                )

            if not device_password:
                device_password = config.get('device_pswd', '')

            if not device_password:
                return {
                    'success': False,
                    'error': f'未找到 {device_host} 的SSH凭据',
                    'instructions': '请先在登录页面输入SSH密码'
                }

            # 连接Windows主机
            username, hostname = CommonUtils.parse_host_address(device_host)
            ssh_hostname, ssh_port = split_host_port(hostname)
            usbip_attach_host = usbip_attach_host or config.get('usbip_attach_host') or ssh_hostname
            win_ssh = self._create_windows_ssh(ssh_hostname, username, device_password, ssh_port)

            if not win_ssh:
                return {'success': False, 'error': f'SSH连接失败到 {device_host}'}

            try:
                # 检查系统类型
                is_windows = self._is_windows_host(win_ssh)
                if not is_windows:
                    win_ssh.close()
                    return {'success': False, 'error': 'USB/IP仅支持Windows主机'}

                # 检查usbipd是否已安装
                installed, version = self.check_usbipd_installed(win_ssh)
                if not installed:
                    win_ssh.close()
                    return {
                        'success': False,
                        'error': 'usbipd未安装',
                        'install_guide': USBIPD_INSTALL_GUIDE.format(install_cmd=USBIPD_INSTALL_CMD)
                    }

                # 终止ADB
                self._execute_ssh(win_ssh, 'taskkill /F /IM adb.exe /T')

                # 查找Android设备
                busids = self._find_android_devices(win_ssh, config)
                if not busids:
                    win_ssh.close()
                    return {'success': False, 'error': '未找到Android设备'}

                # 绑定设备
                bound = self._bind_devices(win_ssh, busids)
                win_ssh.close()

                if not bound:
                    return {'success': False, 'error': '设备绑定失败'}

                # 连接Ubuntu并attach设备
                ubuntu_ssh = self.ssh_manager.get_connection(config)
                if not ubuntu_ssh:
                    return {'success': False, 'error': '无法连接Ubuntu主机'}

                try:
                    # 确保vhci驱动已加载
                    self._ensure_vhci_driver(ubuntu_ssh)

                    # Attach设备
                    attached, device_list = self._attach_devices(
                        ubuntu_ssh,
                        usbip_attach_host,
                        busids
                    )


                    # 更新设备来源记录
                    for device_id in device_list:
                        self.device_sources[device_id] = {
                            'source': device_host,
                            'timestamp': time.time()
                        }

                    self.ssh_manager.return_connection(ubuntu_ssh)

                    return {
                        'success': True,
                        'message': f'✅ 成功连接{len(attached)}个设备: {", ".join(attached)}',
                        'devices': attached,
                        'device_list': device_list
                    }

                except Exception as e:
                    ubuntu_ssh.close()
                    logger.error(f"Error in Ubuntu attach: {e}")
                    return {'success': False, 'error': str(e)}

            except Exception as e:
                win_ssh.close()
                logger.error(f"Error in Windows side: {e}")
                return {'success': False, 'error': str(e)}

        except Exception as e:
            logger.error(f"Error in start_usbip: {e}")
            return {'success': False, 'error': str(e)}

    def stop_usbip(self, client_id: str = None) -> Dict[str, Any]:
        """
        停止USB/IP转发

        Args:
            client_id: 客户端ID（可选）

        Returns:
            结果字典
        """
        try:
            # 清除连接状态，但保留设备来源记录
            if client_id and client_id in self.active_connections:
                del self.active_connections[client_id]

            return {
                'success': True,
                'message': '✅ USB/IP连接已断开（设备来源保留）'
            }

        except Exception as e:
            logger.error(f"Error in stop_usbip: {e}")
            return {'success': True, 'message': '✅ USB/IP连接已断开'}

    def get_usbip_status(self, client_id: str = None) -> Dict[str, Any]:
        """
        获取USB/IP状态

        Args:
            client_id: 客户端ID（可选）

        Returns:
            状态字典
        """
        connected = False

        # 检查活动连接
        if client_id and client_id in self.active_connections:
            connected = True

        # 检查是否有设备来源记录
        if not connected and self.device_sources:
            connected = True

        return {
            'connected': connected,
            'device_count': len(self.device_sources)
        }

    # ============ 辅助方法 ============

    def _create_windows_ssh(self, hostname: str, username: str, password: str, port: int = 22):
        """创建Windows主机SSH连接"""
        try:
            import paramiko
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(
                hostname=hostname,
                port=port,
                username=username,
                password=password,
                timeout=10
            )
            return ssh
        except Exception as e:
            logger.error(f"Error creating Windows SSH: {e}")
            return None

    def _execute_ssh(self, ssh, command: str, timeout: int = 10, get_pty: bool = False) -> Tuple[str, str, int]:
        """执行SSH命令"""
        try:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout, get_pty=get_pty)
            stdout_text = stdout.read().decode('utf-8', errors='ignore')
            stderr_text = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()
            return stdout_text, stderr_text, exit_code
        except Exception as e:
            return '', str(e), -1

    def _is_windows_host(self, ssh) -> bool:
        """检查是否为Windows主机"""
        try:
            stdout, stderr, code = self._execute_ssh(ssh, 'ver 2>&1')
            return 'microsoft' in stdout.lower() or 'windows' in stdout.lower()
        except:
            return False

    def _find_android_devices(self, ssh, config: Dict[str, Any]) -> List[str]:
        """查找Android设备的BUSID"""
        try:
            # 使用 get_pty=True 获取完整的设备列表（需要交互式会话环境）
            stdout, stderr, code = self._execute_ssh(
                ssh,
                'usbipd list',
                timeout=15,
                get_pty=True
            )

            logger.info(f"USB/IP devices:\n{stdout}")

            devices = parse_usbipd_android_busids(stdout, config.get('usbip_vid_pid'))
            logger.info(f"Found USB/IP devices: {devices}")
            return devices

        except Exception as e:
            logger.error(f"Error finding Android devices: {e}")
            return []

    def _bind_devices(self, ssh, busids: List[str]) -> List[str]:
        """绑定设备到USB/IP"""
        bound = []
        for busid in busids:
            try:
                # 检查状态
                stdout, _, _ = self._execute_ssh(ssh, f'usbipd list | findstr {busid}')

                if 'Shared' in stdout:
                    logger.info(f"Device {busid} already shared")
                    bound.append(busid)
                    continue
                elif 'Attached' in stdout:
                    # Detach first
                    self._execute_ssh(ssh, f'usbipd detach --busid {busid}', timeout=15)
                    time.sleep(1)

                # Bind
                self._execute_ssh(ssh, f'usbipd bind --busid {busid}', timeout=15)
                time.sleep(2)
                logger.info(f"Device {busid} bound")
                bound.append(busid)

            except Exception as e:
                logger.error(f"Error binding device {busid}: {e}")

        return bound

    def _ensure_vhci_driver(self, ssh):
        """确保vhci_hcd驱动已加载"""
        try:
            stdout, _, _ = self.ssh_manager.execute_command(ssh, 'lsmod | grep vhci_hcd')
            if not stdout.strip():
                logger.info("Loading vhci_hcd driver...")
                self.ssh_manager.execute_command(ssh, 'sudo modprobe vhci_hcd')
                time.sleep(1)
        except Exception as e:
            logger.error(f"Error ensuring vhci driver: {e}")

    def _attach_devices(
        self,
        ssh,
        device_ip: str,
        busids: List[str]
    ) -> Tuple[List[str], List[str]]:
        """在Ubuntu上attach设备，返回已attach的BUSID和新设备ID列表"""
        try:
            # 获取attach前的设备列表
            stdout_before, _, _ = self.ssh_manager.execute_command(ssh, 'adb devices')
            devices_before = set(DeviceUtils.parse_adb_devices(stdout_before))
            logger.info(f"Devices before attach: {devices_before}")

            # Attach设备
            attached = []
            for busid in busids:
                cmd = f'sudo usbip attach -r {device_ip} -b {busid}'
                logger.info(f"Attaching {busid} from {device_ip}...")
                self.ssh_manager.execute_command(ssh, cmd, timeout=10)
                time.sleep(2)
                attached.append(busid)

            # 等待设备稳定
            time.sleep(3)
            self.ssh_manager.execute_command(ssh, 'sudo udevadm trigger')
            self.ssh_manager.execute_command(ssh, 'sudo udevadm settle')

            # 获取attach后的设备列表
            stdout_after, _, _ = self.ssh_manager.execute_command(ssh, 'adb devices')
            devices_after = set(DeviceUtils.parse_adb_devices(stdout_after))
            logger.info(f"Devices after attach: {devices_after}")

            # 计算新增设备
            new_devices = list(devices_after - devices_before)
            logger.info(f"New devices via USB/IP: {new_devices}")

            # 🔧 修复：如果没有新增设备，检查是否有设备来源已知的USB/IP设备
            # 如果设备已存在，我们仍然需要返回它，因为这是通过USB/IP连接的
            if not new_devices:
                # 检查是否有之前记录的USB/IP设备现在仍然在线
                for device_id in devices_after:
                    if device_id in self.device_sources:
                        # 这个设备之前是通过USB/IP连接的，现在还在
                        new_devices = [device_id]
                        logger.info(f"Found existing USB/IP device still online: {device_id}")
                        break

            return attached, new_devices

        except Exception as e:
            logger.error(f"Error attaching devices: {e}")
            return [], []

    def check_usbipd_installed(self, ssh) -> Tuple[bool, str]:
        """
        检查 usbipd 是否已安装

        Args:
            ssh: SSH 连接对象

        Returns:
            (是否安装, 版本信息)
        """
        try:
            stdout, stderr, code = self._execute_ssh(ssh, 'usbipd --version')
            if code == 0 and stdout.strip():
                return True, stdout.strip()
            return False, ''
        except Exception as e:
            logger.error(f"Error checking usbipd: {e}")
            return False, ''

    def install_usbipd(self, ssh, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        自动安装 usbipd 到 Windows 主机

        Args:
            ssh: SSH 连接对象
            config: 配置字典

        Returns:
            安装结果字典
        """
        try:
            # 检查是否已经是管理员权限
            check_admin_cmd = 'whoami /groups | findstr S-1-16-12288'
            stdout, stderr, code = self._execute_ssh(ssh, check_admin_cmd)

            if code != 0 or 'S-1-16-12288' not in stdout:
                return {
                    'success': False,
                    'error': f'需要管理员权限。请在 Windows 上以【管理员身份】运行 PowerShell，然后执行: {USBIPD_INSTALL_CMD}'
                }

            # 执行自动安装命令（添加自动接受参数）
            install_cmd = f'{USBIPD_INSTALL_CMD} --accept-package-agreements --accept-source-agreements'
            stdout, stderr, code = self._execute_ssh(ssh, install_cmd, timeout=120)

            if code == 0:
                # 验证安装
                installed, version = self.check_usbipd_installed(ssh)
                if installed:
                    return {
                        'success': True,
                        'message': f'usbipd 安装成功！版本: {version}',
                        'version': version
                    }
                else:
                    return {
                        'success': True,
                        'message': 'usbipd 安装完成，请验证版本'
                    }
            else:
                return {
                    'success': False,
                    'error': f'安装失败: {stderr or stdout}'
                }

        except Exception as e:
            logger.error(f"Error installing usbipd: {e}")
            return {
                'success': False,
                'error': str(e)
            }


# 全局USB/IP管理器实例
usbip_manager = USBIPManager()
