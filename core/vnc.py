"""
VNC管理 - 核心业务逻辑

特性：
- VNC启动/停止
- 多主机VNC支持
- 设备屏幕显示（scrcpy）
"""

import logging
import os
import subprocess
import time
from typing import Dict, Any, List

from .ssh import ssh_manager
from .config import config_manager, get_ubuntu_user
from .common_utils import CommonUtils
from .device_utils import DeviceUtils

logger = logging.getLogger(__name__)

# 导出窗口计算函数供外部使用
calculate_window_positions = DeviceUtils.calculate_window_positions


class VNCManager:
    """
    VNC管理器

    特性：
- VNC服务启动/停止
- 多主机VNC支持
- 设备屏幕显示（scrcpy）
"""

    def __init__(self):
        """初始化VNC管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def start_vnc(
        self,
        host: str = None,
        password: str = None,
        vnc_password: str = None
    ) -> Dict[str, Any]:
        """
        启动VNC服务

        Args:
            host: 主机地址（如果不提供则使用配置）
            password: SSH密码
            vnc_password: VNC密码

        Returns:
            结果字典
        """
        try:
            config = self.config_manager.load_config()

            # 解析主机信息
            if not host:
                host = config.get('ubuntu_host', '')

            if not host:
                return {'success': False, 'error': '未配置主机地址'}

            # 提取IP部分并检查是否本地
            host_ip = CommonUtils.extract_ip_from_host(host)
            is_local = CommonUtils.is_local_host(host_ip)

            if is_local:
                # 本地主机的VNC启动
                return self._start_local_vnc()

            # 远程主机的VNC启动
            return self._start_remote_vnc(host, password, vnc_password, config)

        except Exception as e:
            logger.error(f"Error starting VNC: {e}")
            return {'success': False, 'error': str(e)}

    def _start_local_vnc(self) -> Dict[str, Any]:
        """启动本地VNC服务"""
        try:
            logger.info("[VNC] Starting local VNC services...")

            # 检查x11vnc是否运行
            x11vnc_running = subprocess.run(
                ['pgrep', '-f', 'x11vnc.*:0'],
                capture_output=True,
                text=True
            ).returncode == 0

            # 检查websockify是否运行
            websockify_running = subprocess.run(
                ['pgrep', '-f', 'websockify.*6080'],
                capture_output=True,
                text=True
            ).returncode == 0

            # 如果x11vnc正在运行，检查是否使用了密码模式
            if x11vnc_running:
                # 检查是否有使用-rfbauth（密码模式）的x11vnc进程
                check_password_mode = subprocess.run(
                    ['pgrep', '-f', 'x11vnc.*-rfbauth'],
                    capture_output=True,
                    text=True
                ).returncode == 0

                if check_password_mode:
                    # 如果使用密码模式，需要重启为免密码模式
                    logger.info("[VNC] Found x11vnc running with password, restarting without password...")
                    subprocess.run(['pkill', '-f', 'x11vnc.*:0'])
                    time.sleep(1)
                    x11vnc_running = False

            local_ip = CommonUtils.get_local_ip() or 'localhost'

            # 如果已经运行且是免密码模式，返回成功
            if x11vnc_running and websockify_running:
                return {
                    'success': True,
                    'message': '✅ VNC服务已在运行(本地)',
                    'x11vnc_running': True,
                    'websockify_running': True,
                    'vnc_port': 5900,
                    'web_port': 6080,
                    'url': f"http://{local_ip}:6080/vnc.html?autoconnect=true",
                    'local': True
                }

            # 启动x11vnc
            if not x11vnc_running:
                x11vnc_cmd = [
                    'x11vnc',
                    '-display', ':0',
                    '-forever',
                    '-shared',
                    '-rfbport', '5900',
                    '-nopw',
                    '-bg'
                ]
                subprocess.Popen(x11vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("[VNC] Started x11vnc")
                time.sleep(0.5)

            # 启动websockify
            if not websockify_running:
                websockify_cmd = [
                    'python3', '-m', 'websockify',
                    '--web=/opt/noVNC',
                    '6080',
                    'localhost:5900'
                ]
                subprocess.Popen(websockify_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                logger.info("[VNC] Started websockify")
                time.sleep(0.5)

            # 验证服务是否运行
            x11vnc_running = subprocess.run(
                ['pgrep', '-f', 'x11vnc.*:0'],
                capture_output=True,
                text=True
            ).returncode == 0

            websockify_running = subprocess.run(
                ['pgrep', '-f', 'websockify.*6080'],
                capture_output=True,
                text=True
            ).returncode == 0

            if x11vnc_running and websockify_running:
                return {
                    'success': True,
                    'message': '✅ VNC服务已启动(本地)',
                    'x11vnc_running': True,
                    'websockify_running': True,
                    'vnc_port': 5900,
                    'web_port': 6080,
                    'url': f"http://{local_ip}:6080/vnc.html?autoconnect=true",
                    'local': True
                }
            else:
                return {
                    'success': False,
                    'error': 'VNC服务启动失败'
                }

        except Exception as e:
            logger.error(f"Error starting local VNC: {e}")
            return {'success': False, 'error': str(e)}

    def _start_remote_vnc(
        self,
        host: str,
        password: str,
        vnc_password: str,
        config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """启动远程VNC服务"""
        try:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}

            ubuntu_user = config.get('ubuntu_user') or get_ubuntu_user()

            # 如果提供了VNC密码，需要创建密码文件；否则使用免密模式
            if vnc_password:
                # 创建VNC密码文件（使用SFTP写入避免shell注入）
                try:
                    passwd_content = f"{vnc_password}\n{vnc_password}\n"
                    sftp = ssh.open_sftp()
                    with sftp.file('/tmp/.vnc_passwd_input', 'w') as f:
                        f.write(passwd_content)
                    sftp.close()
                    create_passwd_cmd = "x11vnc -display :0 -storepasswd $(head -1 /tmp/.vnc_passwd_input) ~/.vnc/passwd && rm -f /tmp/.vnc_passwd_input"
                    self.ssh_manager.execute_command(ssh, create_passwd_cmd, timeout=10)
                except Exception as e:
                    logger.warning(f"[VNC] Failed to create password file via SFTP: {e}")
                    # Fallback: escape single quotes in password
                    safe_password = vnc_password.replace("'", "'\\''")
                    create_passwd_cmd = f"echo '{safe_password}' | x11vnc -display :0 -storepasswd ~/.vnc/passwd"
                    self.ssh_manager.execute_command(ssh, create_passwd_cmd, timeout=10)
                time.sleep(0.5)  # 等待文件创建完成

            # 检查noVNC安装
            check_novnc_cmd = "[ -d /opt/noVNC ] && echo 'exists' || echo 'missing'"
            stdout, stderr, code = self.ssh_manager.execute_command(ssh, check_novnc_cmd)

            if "missing" in stdout:
                self.ssh_manager.return_connection(ssh)
                return {
                    'success': False,
                    'error': 'noVNC未安装',
                    'instructions': '''sudo apt-get install -y git
cd /opt
sudo git clone https://github.com/novnc/noVNC.git
sudo git clone https://github.com/novnc/websockify.git noVNC/utils/websockify'''
                }

            display_ready = False
            for _ in range(30):
                display_cmd = "export DISPLAY=:0 && xprop -root &>/dev/null && echo 'ready'"
                stdout, _, _ = self.ssh_manager.execute_command(ssh, display_cmd)
                if "ready" in stdout:
                    display_ready = True
                    break
                time.sleep(0.5)

            if not display_ready:
                self.ssh_manager.return_connection(ssh)
                return {
                    'success': False,
                    'error': 'DISPLAY未就绪',
                    'warning': '需要在主机桌面环境中运行'
                }

            # 检查并启动x11vnc
            check_x11_cmd = "pgrep -f 'x11vnc.*:0' && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdout, _, _ = self.ssh_manager.execute_command(ssh, check_x11_cmd)
            x11vnc_running = 'RUNNING' in stdout

            # 如果x11vnc正在运行，检查是否使用了密码模式
            if x11vnc_running and not vnc_password:
                # 免密模式，检查是否需要从密码模式重启
                check_password_mode = "pgrep -f 'x11vnc.*-rfbauth' && echo 'PASSWORD' || echo 'NOPASSWORD'"
                stdout, _, _ = self.ssh_manager.execute_command(ssh, check_password_mode)

                if 'PASSWORD' in stdout:
                    # 当前是密码模式，需要重启为免密模式
                    logger.info("[VNC] Found x11vnc running with password, restarting without password...")
                    self.ssh_manager.execute_command(ssh, "pkill -f 'x11vnc.*:0'", timeout=5)
                    time.sleep(0.5)
                    x11vnc_running = False

            if not x11vnc_running:
                auth_param = "-rfbauth ~/.vnc/passwd" if vnc_password else ""
                x11vnc_cmd = (
                    f"export DISPLAY=:0 && "
                    f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
                    f"x11vnc -display :0 -forever -shared {auth_param} -bg -o ~/logs/x11vnc.log"
                )
                self.ssh_manager.execute_command(ssh, x11vnc_cmd, timeout=15)
                time.sleep(1)

            # 检查并启动websockify
            check_ws_cmd = "pgrep -f 'websockify.*6080' && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdout, _, _ = self.ssh_manager.execute_command(ssh, check_ws_cmd)
            websockify_running = 'RUNNING' in stdout

            if not websockify_running:
                novnc_cmd = (
                    "cd /opt/noVNC && "
                    "nohup ./utils/websockify/run --web /opt/noVNC 6080 localhost:5900 "
                    "> ~/logs/novnc.log 2>&1 &"
                )
                self.ssh_manager.execute_command(ssh, novnc_cmd, timeout=10)
                time.sleep(1)

            target_ip = CommonUtils.extract_ip_from_host(host)

            self.ssh_manager.return_connection(ssh)

            return {
                'success': True,
                'message': '✅ VNC服务已启动',
                'x11vnc_running': x11vnc_running,
                'websockify_running': websockify_running,
                'vnc_port': 5900,
                'web_port': 6080,
                'url': f"http://{target_ip}:6080/vnc.html?autoconnect=true"
            }

        except Exception as e:
            if 'ssh' in locals():
                self.ssh_manager.return_connection(ssh)
            logger.error(f"Error starting remote VNC: {e}")
            return {'success': False, 'error': str(e)}

    def stop_vnc(self, host: str = None) -> Dict[str, Any]:
        """
        停止VNC服务

        Args:
            host: 主机地址

        Returns:
            结果字典
        """
        try:
            config = self.config_manager.load_config()

            if not host:
                host = config.get('ubuntu_host', '')

            is_local = CommonUtils.is_local_host(host)

            if is_local:
                # 停止本地VNC
                subprocess.run(['pkill', '-f', 'x11vnc'], capture_output=True)
                subprocess.run(['pkill', '-f', 'websockify'], capture_output=True)
                return {'success': True, 'message': '✅ 本地VNC已停止'}
            else:
                # 停止远程VNC
                ssh = self.ssh_manager.get_connection(config)
                if not ssh:
                    return {'success': False, 'error': 'SSH连接失败'}

                self.ssh_manager.execute_command(ssh, "pkill -f 'x11vnc.*:0'")
                self.ssh_manager.execute_command(ssh, "pkill -f 'websockify.*6080'")

                self.ssh_manager.return_connection(ssh)

                return {'success': True, 'message': '✅ 远程VNC已停止'}

        except Exception as e:
            logger.error(f"Error stopping VNC: {e}")
            return {'success': False, 'error': str(e)}

    def show_device_screens(
        self,
        devices: List[str],
        host: str = None
    ) -> Dict[str, Any]:
        """
        显示设备屏幕（使用scrcpy）

        Args:
            devices: 设备列表
            host: 主机地址

        Returns:
            结果字典
        """
        try:
            config = self.config_manager.load_config()

            if not host:
                host = config.get('ubuntu_host', '')
                ubuntu_user = config.get('ubuntu_user') or get_ubuntu_user()
            else:
                # 从host中解析user
                username, _ = CommonUtils.parse_host_address(host)
                if username:
                    ubuntu_user = username
                    host = CommonUtils.extract_ip_from_host(host)
                else:
                    ubuntu_user = config.get('ubuntu_user') or get_ubuntu_user()

            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}

            # 检查scrcpy
            scrcpy_path = config.get('scrcpy_path', '')
            if scrcpy_path:
                scrcpy_path = scrcpy_path.replace('${ubuntu_user}', ubuntu_user)
                check_cmd = f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
                stdout, _, code = self.ssh_manager.execute_command(ssh, check_cmd)
                if "not_found" in stdout:
                    self.ssh_manager.return_connection(ssh)
                    return {
                        'success': False,
                        'error': f'scrcpy未找到: {scrcpy_path}',
                        'instructions': '请检查配置文件中的 scrcpy_path'
                    }
            else:
                # 检查PATH
                check_cmd = "which scrcpy"
                stdout, _, code = self.ssh_manager.execute_command(ssh, check_cmd)
                if code != 0:
                    self.ssh_manager.return_connection(ssh)
                    return {
                        'success': False,
                        'error': 'scrcpy未安装',
                        'instructions': 'sudo apt-get install -y scrcpy'
                    }
                scrcpy_path = "scrcpy"

            # 计算窗口布局（使用统一函数）
            layout = calculate_window_positions(devices, max_window_width=500)
            window_width = layout['window_width']
            window_height = layout['window_height']
            start_x = layout['start_x']
            start_y = layout['start_y']
            horizontal_gap = layout['horizontal_gap']
            screen_width = 1920
            screen_height = 1080
            vertical_margin = 50

            results = []

            # 启动scrcpy
            for idx, device_id in enumerate(devices):
                # 计算窗口位置
                x_offset = start_x + idx * (window_width + horizontal_gap)
                y_offset = start_y

                # 边界检查
                if x_offset + window_width > screen_width:
                    x_offset = max(0, screen_width - window_width - horizontal_gap)
                if y_offset + window_height > screen_height:
                    y_offset = max(0, screen_height - window_height - vertical_margin)

                # 清理旧日志
                self.ssh_manager.execute_command(ssh, f"rm -f /tmp/scrcpy_{device_id}.log")

                # 构建scrcpy命令
                scrcpy_cmd = (
                    f"export DISPLAY=:0 && "
                    f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority && "
                    f"{scrcpy_path} -s {device_id} "
                    f"--max-size 800 "
                    f"--stay-awake "
                    f"--window-title '{device_id}' "
                    f"--window-x {x_offset} "
                    f"--window-y {y_offset} "
                    f"--window-width {window_width} "
                    f"--window-height {window_height} "
                    f"> /tmp/scrcpy_{device_id}.log 2>&1 &"
                )

                self.ssh_manager.execute_command(ssh, scrcpy_cmd)
                time.sleep(0.2)

                # 检查是否启动成功
                check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}'"
                stdout, _, _ = self.ssh_manager.execute_command(ssh, check_cmd)

                is_running = 'scrcpy' in stdout

                results.append({
                    'device': device_id,
                    'success': is_running,
                    'position': {'x': x_offset, 'y': y_offset, 'width': window_width, 'height': window_height}
                })

            self.ssh_manager.return_connection(ssh)

            successful = [r for r in results if r['success']]

            return {
                'success': len(successful) > 0,
                'results': results,
                'started_count': len(successful),
                'vnc_url': f"http://{host}:6080/vnc.html?autoconnect=true",
                'message': f"✅ 已启动{len(successful)}个投屏设备"
            }

        except Exception as e:
            if 'ssh' in locals():
                self.ssh_manager.return_connection(ssh)
            logger.error(f"Error showing device screens: {e}")
            return {'success': False, 'error': str(e)}

    def get_vnc_status(self) -> Dict[str, Any]:
        """
        获取VNC状态

        Returns:
            VNC状态信息
        """
        try:
            config = self.config_manager.load_config()
            host = config.get('ubuntu_host', '')
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'running': False, 'error': 'SSH连接失败'}

            # 检查VNC进程
            check_cmd = "pgrep -f 'x11vnc' | wc -l"
            stdout, stderr, code = self.ssh_manager.execute_command(ssh, check_cmd)

            vnc_count = int(stdout.strip()) if code == 0 else 0

            # 检查VNC端口
            port_check = "netstat -tuln | grep 6080"
            stdout, stderr, code = self.ssh_manager.execute_command(ssh, port_check)

            port_listening = code == 0 and '6080' in stdout

            self.ssh_manager.return_connection(ssh)

            return {
                'running': vnc_count > 0,
                'vnc_count': vnc_count,
                'port_listening': port_listening,
                'host': host,
                'url': f"http://{host}:6080/vnc.html"
            }

        except Exception as e:
            logger.error(f"Error getting VNC status: {e}")
            return {'running': False, 'error': str(e)}

    def start_desktop_vnc(
        self,
        host: str = None,
        password: str = None,
        vnc_password: str = None
    ) -> Dict[str, Any]:
        """启动Ubuntu主机桌面VNC（委托给start_vnc）"""
        return self.start_vnc(host, password, vnc_password)


# 全局VNC管理器实例
vnc_manager = VNCManager()
