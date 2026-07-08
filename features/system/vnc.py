"""
VNC管理 - 核心业务逻辑

特性：
- VNC启动/停止
- 多主机VNC支持
- 设备屏幕显示（scrcpy）
"""

import logging
import os
import shlex
import shutil
import subprocess
import time
import uuid
from typing import Any

from features.devices.utils import DeviceUtils
from foundation.common_utils import CommonUtils
from foundation.config import config_manager, get_ubuntu_user
from foundation.processes import command_reports_running, start_detached_process
from foundation.security import sanitize_device_ids
from foundation.window_layout import calculate_window_positions

from .ssh import ssh_manager


logger = logging.getLogger(__name__)

VNC_DISPLAY = ':0'
VNC_PORT = 5900
NOVNC_WEB_PORT = 6080
REMOTE_NOVNC_DIR = '/opt/noVNC'


def x11vnc_port_pattern(port: int = VNC_PORT) -> str:
    return f'x11vnc.*-rfbport {port}'


def x11vnc_display_pattern(display: str = VNC_DISPLAY) -> str:
    return f'x11vnc.*{display}'


def websockify_pattern(web_port: int = NOVNC_WEB_PORT) -> str:
    return f'websockify.*{web_port}'


def novnc_url(host: str, *, web_port: int = NOVNC_WEB_PORT, autoconnect: bool = True,
              resize: str = 'scale') -> str:
    """Return a noVNC URL with autoconnect and scaling mode preset.

    The *resize* query parameter maps to noVNC's 'resize' setting:
      - 'off'     -> no scaling
      - 'scale'   -> local scaling
      - 'remote'  -> remote resizing
    """
    params = []
    if autoconnect:
        params.append('autoconnect=true')
    if resize:
        params.append(f'resize={resize}')
    if params:
        return f'http://{host}:{web_port}/vnc.html?{"&".join(params)}'
    return f'http://{host}:{web_port}/vnc.html'


def vnc_password_temp_path() -> str:
    return f'/tmp/.gms_vnc_passwd_{uuid.uuid4().hex}'


LOCAL_X11VNC_PATTERN = x11vnc_port_pattern()
X11VNC_DISPLAY_PATTERN = x11vnc_display_pattern()
WEBSOCKIFY_PATTERN = websockify_pattern()




class VNCManager:
    """Manages VNC start/stop, scrcpy device screens, locally and over SSH."""

    def __init__(self):
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def start_vnc(
        self,
        host: str = None,
        password: str = None,
        vnc_password: str = None,
        force_restart: bool = False
    ) -> dict[str, Any]:
        """
        启动VNC服务

        Args:
            host: 主机地址（如果不提供则使用配置）
            password: SSH密码
            vnc_password: VNC密码
            force_restart: 强制重启VNC进程（杀死旧的x11vnc/websockify）

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
                return self._start_local_vnc(force_restart=force_restart)

            return self._start_remote_vnc(host, password, vnc_password, config)

        except Exception as e:
            logger.error(f"Error starting VNC: {e}")
            return {'success': False, 'error': str(e)}

    def _start_local_vnc(self, force_restart: bool = False) -> dict[str, Any]:
        """启动本地VNC服务

        Args:
            force_restart: 强制杀死旧进程并重启（清理僵尸连接）
        """
        try:
            logger.info(f"[VNC] Starting local VNC services (force_restart={force_restart})...")
            novnc_web_dir = self._find_local_novnc_web_dir()
            if not novnc_web_dir:
                return {
                    'success': False,
                    'error': 'noVNC未安装',
                    'instructions': 'sudo apt-get install -y x11vnc novnc websockify'
                }
            if not shutil.which('x11vnc'):
                return {
                    'success': False,
                    'error': 'x11vnc未安装',
                    'instructions': 'sudo apt-get install -y x11vnc'
                }
            if not self._has_local_websockify():
                return {
                    'success': False,
                    'error': 'websockify未安装',
                    'instructions': 'sudo apt-get install -y websockify'
                }

            local_ip = CommonUtils.get_local_ip() or 'localhost'

            # 强制重启模式：杀死所有旧进程，清理环境
            if force_restart:
                logger.info("[VNC] Force restart: killing old x11vnc and websockify processes...")
                self._kill_local_processes(LOCAL_X11VNC_PATTERN, force=True)
                self._kill_local_processes(WEBSOCKIFY_PATTERN, force=True)
                time.sleep(1)
                logger.info("[VNC] Old processes killed")
            else:
                # 检查x11vnc是否运行
                x11vnc_running = self._is_local_process_running(X11VNC_DISPLAY_PATTERN)

                # 检查websockify是否运行
                websockify_running = self._is_local_process_running(WEBSOCKIFY_PATTERN)

                # 如果x11vnc正在运行，检查是否使用了密码模式
                if x11vnc_running:
                    check_password_mode = self._is_local_process_running('x11vnc.*-rfbauth')

                    if check_password_mode:
                        logger.info("[VNC] Found x11vnc running with password, restarting without password...")
                        self._kill_local_processes(X11VNC_DISPLAY_PATTERN)
                        time.sleep(1)
                        x11vnc_running = False

                # 如果已经运行且是免密码模式，返回成功
                if x11vnc_running and websockify_running:
                    return {
                        'success': True,
                        'message': '✅ VNC服务已在运行(本地)',
                        'x11vnc_running': True,
                        'websockify_running': True,
                        'vnc_port': VNC_PORT,
                        'web_port': NOVNC_WEB_PORT,
                        'url': novnc_url(local_ip),
                        'local': True
                    }

            x11vnc_cmd = [
                'x11vnc',
                '-display', VNC_DISPLAY,
                '-forever',
                '-shared',
                '-rfbport', str(VNC_PORT),
                '-nopw',
                '-bg'
            ]
            subprocess.run(x11vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            logger.info("[VNC] Started x11vnc")
            time.sleep(0.5)

            websockify_cmd = self._build_local_websockify_cmd(novnc_web_dir)
            start_detached_process(
                websockify_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                name=f'websockify_{NOVNC_WEB_PORT}',
            )
            logger.info("[VNC] Started websockify")
            time.sleep(0.5)

            # 验证服务是否运行
            x11vnc_running = self._is_local_process_running(X11VNC_DISPLAY_PATTERN)

            websockify_running = self._is_local_process_running(WEBSOCKIFY_PATTERN)

            if x11vnc_running and websockify_running:
                return {
                    'success': True,
                    'message': '✅ VNC服务已启动(本地)',
                    'x11vnc_running': True,
                    'websockify_running': True,
                    'vnc_port': VNC_PORT,
                    'web_port': NOVNC_WEB_PORT,
                    'url': novnc_url(local_ip),
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

    @staticmethod
    def _find_local_novnc_web_dir() -> str:
        """Return the local noVNC web root installed by source or apt packages."""
        for path in ('/opt/noVNC', '/usr/share/novnc'):
            if os.path.isdir(path) and os.path.exists(os.path.join(path, 'vnc.html')):
                return path
        return ''

    @staticmethod
    def _is_local_process_running(pattern: str) -> bool:
        return subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True).returncode == 0

    @staticmethod
    def _kill_local_processes(pattern: str, *, force: bool = False) -> None:
        command = ['pkill']
        if force:
            command.append('-9')
        command.extend(['-f', pattern])
        subprocess.run(command, capture_output=True)

    # Cached at class level: websockify availability doesn't change at runtime
    _websockify_available: bool | None = None
    _websockify_standalone: str | None = None

    @classmethod
    def _detect_websockify(cls) -> bool:
        """Detect websockify availability (standalone binary or python module)."""
        standalone = shutil.which('websockify')
        if standalone:
            cls._websockify_standalone = standalone
            return True
        result = subprocess.run(
            ['python3', '-m', 'websockify', '--help'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return result.returncode == 0

    @classmethod
    def _has_local_websockify(cls) -> bool:
        """Check if websockify is available. Result is cached after first check."""
        if cls._websockify_available is None:
            cls._websockify_available = cls._detect_websockify()
        return cls._websockify_available

    @classmethod
    def _build_local_websockify_cmd(cls, novnc_web_dir: str) -> list[str]:
        """Build websockify command, using cached standalone path when available."""
        base = [cls._websockify_standalone or 'python3']
        if not cls._websockify_standalone:
            base.extend(['-m', 'websockify'])
        base.extend([f'--web={novnc_web_dir}', str(NOVNC_WEB_PORT), f'localhost:{VNC_PORT}'])
        return base

    def _start_remote_vnc(
        self,
        host: str,
        password: str,
        vnc_password: str,
        config: dict[str, Any]
    ) -> dict[str, Any]:
        """启动远程VNC服务"""
        try:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}

            ubuntu_user = config.get('ubuntu_user') or get_ubuntu_user()
            quoted_ubuntu_user = shlex.quote(ubuntu_user)

            # 如果提供了VNC密码，需要创建密码文件；否则使用免密模式
            if vnc_password:
                # 创建VNC密码文件（使用SFTP写入避免shell注入）
                temp_passwd_path = vnc_password_temp_path()
                try:
                    passwd_content = f"{vnc_password}\n{vnc_password}\n"
                    sftp = ssh.open_sftp()
                    with sftp.file(temp_passwd_path, 'w') as f:
                        f.write(passwd_content)
                    sftp.close()
                    quoted_temp_passwd_path = shlex.quote(temp_passwd_path)
                    create_passwd_cmd = (
                        f"x11vnc -display {VNC_DISPLAY} "
                        f"-storepasswd $(head -1 {quoted_temp_passwd_path}) ~/.vnc/passwd && "
                        f"rm -f {quoted_temp_passwd_path}"
                    )
                    self.ssh_manager.execute_command(ssh, create_passwd_cmd, timeout=10)
                except Exception as e:
                    logger.warning(f"[VNC] Failed to create password file via SFTP: {e}")
                    self.ssh_manager.return_connection(ssh)
                    return {'success': False, 'error': '创建 VNC 密码文件失败'}
                time.sleep(0.5)  # 等待文件创建完成

            quoted_novnc_dir = shlex.quote(REMOTE_NOVNC_DIR)
            check_novnc_cmd = f"[ -d {quoted_novnc_dir} ] && echo 'exists' || echo 'missing'"
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
                display_cmd = f"export DISPLAY={VNC_DISPLAY} && xprop -root &>/dev/null && echo 'ready'"
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
            check_x11_cmd = f"pgrep -f -- {shlex.quote(X11VNC_DISPLAY_PATTERN)} && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdout, _, _ = self.ssh_manager.execute_command(ssh, check_x11_cmd)
            x11vnc_running = command_reports_running(stdout)

            # 如果x11vnc正在运行，检查是否使用了密码模式
            if x11vnc_running and not vnc_password:
                # 免密模式，检查是否需要从密码模式重启
                check_password_mode = "pgrep -f -- 'x11vnc.*-rfbauth' && echo 'PASSWORD' || echo 'NOPASSWORD'"
                stdout, _, _ = self.ssh_manager.execute_command(ssh, check_password_mode)

                if 'PASSWORD' in stdout:
                    # 当前是密码模式，需要重启为免密模式
                    logger.info("[VNC] Found x11vnc running with password, restarting without password...")
                    self.ssh_manager.execute_command(
                        ssh,
                        f"pkill -f -- {shlex.quote(X11VNC_DISPLAY_PATTERN)}",
                        timeout=5,
                    )
                    time.sleep(0.5)
                    x11vnc_running = False

            if not x11vnc_running:
                auth_param = "-rfbauth ~/.vnc/passwd" if vnc_password else ""
                x11vnc_cmd = (
                    f"export DISPLAY={VNC_DISPLAY} && "
                    f"export XAUTHORITY=/home/{quoted_ubuntu_user}/.Xauthority && "
                    f"x11vnc -display {VNC_DISPLAY} -forever -shared "
                    f"-rfbport {VNC_PORT} {auth_param} -bg -o ~/logs/x11vnc.log"
                )
                self.ssh_manager.execute_command(ssh, x11vnc_cmd, timeout=15)
                time.sleep(1)

            # 检查并启动websockify
            check_ws_cmd = f"pgrep -f -- {shlex.quote(WEBSOCKIFY_PATTERN)} && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdout, _, _ = self.ssh_manager.execute_command(ssh, check_ws_cmd)
            websockify_running = command_reports_running(stdout)

            if not websockify_running:
                novnc_cmd = (
                    f"cd {quoted_novnc_dir} && "
                    f"nohup ./utils/websockify/run --web {quoted_novnc_dir} "
                    f"{NOVNC_WEB_PORT} localhost:{VNC_PORT} "
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
                'vnc_port': VNC_PORT,
                'web_port': NOVNC_WEB_PORT,
                'url': novnc_url(target_ip)
            }

        except Exception as e:
            if 'ssh' in locals():
                self.ssh_manager.return_connection(ssh)
            logger.error(f"Error starting remote VNC: {e}")
            return {'success': False, 'error': str(e)}

    def stop_vnc(self, host: str = None) -> dict[str, Any]:
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
                self._kill_local_processes(X11VNC_DISPLAY_PATTERN)
                self._kill_local_processes(WEBSOCKIFY_PATTERN)
                return {'success': True, 'message': '✅ 本地VNC已停止'}
            else:
                # 停止远程VNC
                ssh = self.ssh_manager.get_connection(config)
                if not ssh:
                    return {'success': False, 'error': 'SSH连接失败'}

                self.ssh_manager.execute_command(ssh, f"pkill -f -- {shlex.quote(X11VNC_DISPLAY_PATTERN)}")
                self.ssh_manager.execute_command(ssh, f"pkill -f -- {shlex.quote(WEBSOCKIFY_PATTERN)}")

                self.ssh_manager.return_connection(ssh)

                return {'success': True, 'message': '✅ 远程VNC已停止'}

        except Exception as e:
            logger.error(f"Error stopping VNC: {e}")
            return {'success': False, 'error': str(e)}


    def get_vnc_status(self) -> dict[str, Any]:
        """Check whether x11vnc is running and noVNC is listening."""
        try:
            config = self.config_manager.load_config()
            host = config.get('ubuntu_host', '')
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'running': False, 'error': 'SSH连接失败'}

            check_cmd = "pgrep -f 'x11vnc' | wc -l"
            stdout, _, code = self.ssh_manager.execute_command(ssh, check_cmd)

            vnc_count = int(stdout.strip()) if code == 0 else 0

            port_check = f"netstat -tuln | grep {NOVNC_WEB_PORT}"
            stdout, _, code = self.ssh_manager.execute_command(ssh, port_check)

            port_listening = code == 0 and str(NOVNC_WEB_PORT) in stdout

            self.ssh_manager.return_connection(ssh)

            return {
                'running': vnc_count > 0,
                'vnc_count': vnc_count,
                'port_listening': port_listening,
                'host': host,
                'url': novnc_url(host, autoconnect=False)
            }

        except Exception as e:
            logger.error(f"Error getting VNC status: {e}")
            return {'running': False, 'error': str(e)}

    def start_desktop_vnc(
        self,
        host: str = None,
        password: str = None,
        vnc_password: str = None
    ) -> dict[str, Any]:
        """启动Ubuntu主机桌面VNC（委托给start_vnc）"""
        return self.start_vnc(host, password, vnc_password)


vnc_manager = VNCManager()
