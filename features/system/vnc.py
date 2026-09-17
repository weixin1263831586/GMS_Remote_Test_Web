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
from typing import Any

from foundation.common_utils import CommonUtils
from foundation.config import config_manager, get_ubuntu_user
from foundation.networking import is_local_host
from foundation.novnc import NOVNC_WEB_PORT, novnc_url
from foundation.processes import start_detached_process

from .ssh import ssh_manager

# vnc_defs 是端口/进程模式/x11vnc 旗标的唯一定义点；vnc_remote 承载远程
# 启动流程。这里继续 re-export vnc_password_temp_path 以保持既有导入路径。
from .vnc_defs import (
    LOCAL_X11VNC_PATTERN,
    VNC_DISPLAY,
    VNC_PORT,
    WEBSOCKIFY_PATTERN,
    X11VNC_DISPLAY_PATTERN,
    X11VNC_INPUT_FLAGS,
    X11VNC_KEYMAP_FLAGS,
    X11VNC_PERF_FLAGS,
    vnc_password_temp_path,  # noqa: F401 -- 兼容旧导入路径(test_vnc 等)
)
from .vnc_remote import start_remote_vnc


logger = logging.getLogger(__name__)




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
        """在本机或指定主机启动 VNC 服务。"""
        try:
            config = self.config_manager.load_config()

            # 解析主机信息
            if not host:
                host = config.get('ubuntu_host', '')

            if not host:
                return {'success': False, 'error': '未配置主机地址'}

            # 提取IP部分并检查是否本地
            host_ip = CommonUtils.extract_ip_from_host(host)
            is_local = is_local_host(host_ip)

            if is_local:
                return self._start_local_vnc(force_restart=force_restart)

            remote_user, remote_ip = CommonUtils.parse_host_address(host)
            remote_config = dict(config)
            remote_config.update({
                'host': remote_ip,
                'hostname': remote_ip,
                'ubuntu_host': remote_ip,
                'username': remote_user or get_ubuntu_user(),
                'ubuntu_user': remote_user or get_ubuntu_user(),
                'password': password or '',
                'ubuntu_pswd': password or '',
            })
            return self._start_remote_vnc(host, password, vnc_password, remote_config)

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

            # 强制重启时清理现有 VNC 进程。
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
                '-localhost',
                '-nopw',
                *X11VNC_PERF_FLAGS,
                *X11VNC_INPUT_FLAGS,
                *X11VNC_KEYMAP_FLAGS,
                '-bg'
            ]
            subprocess.run(x11vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            logger.info("[VNC] Started x11vnc")
            repeat_result = subprocess.run(
                ['xset', '-display', VNC_DISPLAY, 'r', 'on'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if repeat_result.returncode:
                logger.warning("[VNC] Failed to enable X11 key auto repeat")
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

    # websockify 可用性在进程内缓存。
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
        base.extend([
            f'--web={novnc_web_dir}',
            f'127.0.0.1:{NOVNC_WEB_PORT}',
            f'localhost:{VNC_PORT}',
        ])
        return base

    def _start_remote_vnc(
        self,
        host: str,
        password: str,
        vnc_password: str,
        config: dict[str, Any]
    ) -> dict[str, Any]:
        """启动远程VNC服务（实现在 vnc_remote.start_remote_vnc）。"""
        return start_remote_vnc(self.ssh_manager, host, password, vnc_password, config)


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

            is_local = is_local_host(host)

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
            if is_local_host(host):
                # The Controller commonly points ubuntu_host at one of its own
                # LAN addresses. Avoid an SSH round trip back into the same
                # machine on every cold desktop mount.
                x11vnc_running = self._is_local_process_running(X11VNC_DISPLAY_PATTERN)
                websockify_running = self._is_local_process_running(WEBSOCKIFY_PATTERN)
                return {
                    'running': x11vnc_running and websockify_running,
                    'vnc_count': int(x11vnc_running),
                    'port_listening': websockify_running,
                    'host': host,
                    'url': novnc_url(host, autoconnect=False),
                    'local': True,
                }

            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'running': False, 'error': 'SSH连接失败'}

            check_cmd = "pgrep -f 'x11vnc' | wc -l"
            count_result = self.ssh_manager.execute_command(ssh, check_cmd)

            vnc_count = (
                int(count_result.stdout.strip()) if count_result.ok else 0
            )

            port_check = f"netstat -tuln | grep {NOVNC_WEB_PORT}"
            port_result = self.ssh_manager.execute_command(ssh, port_check)

            port_listening = (
                port_result.ok and str(NOVNC_WEB_PORT) in port_result.stdout
            )

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
