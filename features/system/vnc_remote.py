"""远程主机 VNC 启动流程（SSH 执行边界内，ADR 0004）。

从 ``vnc.py`` 拆出以便本地/远程路径独立评审。全部命令经由
``ssh_manager.execute_command`` 下发，进程匹配模式与 x11vnc 旗标由
``vnc_defs`` 提供，禁止在本模块内拼接新的裸 shell 通道。
"""

import logging
import shlex
import time
from typing import Any

from foundation.common_utils import CommonUtils
from foundation.config import get_ubuntu_user
from foundation.novnc import NOVNC_WEB_PORT, novnc_url
from foundation.processes import command_reports_running

from .vnc_defs import (
    REMOTE_NOVNC_DIR,
    VNC_DISPLAY,
    VNC_PORT,
    WEBSOCKIFY_PATTERN,
    X11VNC_DISPLAY_PATTERN,
    X11VNC_INPUT_ARGS,
    X11VNC_KEYMAP_ARGS,
    X11VNC_PERF_ARGS,
    vnc_password_temp_path,
)


logger = logging.getLogger(__name__)


def start_remote_vnc(
    ssh_manager,
    host: str,
    password: str,
    vnc_password: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """在远程主机上启动 x11vnc/websockify，并等待两个端口都监听后才返回。"""
    try:
        ssh = ssh_manager.get_connection(config)
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
                ssh_manager.execute_command(ssh, create_passwd_cmd, timeout=10)
            except Exception as e:
                logger.warning(f"[VNC] Failed to create password file via SFTP: {e}")
                ssh_manager.return_connection(ssh)
                return {'success': False, 'error': '创建 VNC 密码文件失败'}
            time.sleep(0.5)  # 等待文件创建完成

        quoted_novnc_dir = shlex.quote(REMOTE_NOVNC_DIR)
        check_novnc_cmd = f"[ -d {quoted_novnc_dir} ] && echo 'exists' || echo 'missing'"
        novnc_check = ssh_manager.execute_command(ssh, check_novnc_cmd)

        if "missing" in novnc_check.stdout:
            ssh_manager.return_connection(ssh)
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
            display_result = ssh_manager.execute_command(ssh, display_cmd)
            if "ready" in display_result.stdout:
                display_ready = True
                break
            time.sleep(0.5)

        if not display_ready:
            ssh_manager.return_connection(ssh)
            return {
                'success': False,
                'error': 'DISPLAY未就绪',
                'warning': '需要在主机桌面环境中运行'
            }

        # 预先创建日志目录，避免后台服务因目录缺失启动失败。
        ssh_manager.execute_command(ssh, "mkdir -p ~/logs ~/.vnc", timeout=5)

        # 检查并启动x11vnc
        check_x11_cmd = (
            f"pgrep -f -- {shlex.quote(X11VNC_DISPLAY_PATTERN)} >/dev/null "
            f"&& ss -ltn | grep -q ':{VNC_PORT} ' "
            "&& echo 'RUNNING' || echo 'NOT_RUNNING'"
        )
        x11_check = ssh_manager.execute_command(ssh, check_x11_cmd)
        x11vnc_running = command_reports_running(x11_check.stdout)

        # 如果x11vnc正在运行，检查是否使用了密码模式
        if x11vnc_running and not vnc_password:
            # 免密模式，检查是否需要从密码模式重启
            check_password_mode = "pgrep -f -- 'x11vnc.*-rfbauth' && echo 'PASSWORD' || echo 'NOPASSWORD'"
            password_mode = ssh_manager.execute_command(ssh, check_password_mode)

            if 'PASSWORD' in password_mode.stdout:
                # 当前是密码模式，需要重启为免密模式
                logger.info("[VNC] Found x11vnc running with password, restarting without password...")
                ssh_manager.execute_command(
                    ssh,
                    f"pkill -f -- {shlex.quote(X11VNC_DISPLAY_PATTERN)}",
                    timeout=5,
                )
                time.sleep(0.5)
                x11vnc_running = False

        # x11vnc/websockify 已在运行时不会执行启动命令，两个结果变量
        # 预置 None，失败诊断分支按“是否真的启动过”取 stderr。
        x11_result = None
        novnc_result = None
        if not x11vnc_running:
            auth_param = "-rfbauth ~/.vnc/passwd" if vnc_password else ""
            x11vnc_cmd = (
                f"export DISPLAY={VNC_DISPLAY} && "
                f"export XAUTHORITY=/home/{quoted_ubuntu_user}/.Xauthority && "
                f"x11vnc -display {VNC_DISPLAY} -forever -shared "
                f"-rfbport {VNC_PORT} {auth_param} {X11VNC_PERF_ARGS} {X11VNC_INPUT_ARGS} {X11VNC_KEYMAP_ARGS} "
                f"-bg -o ~/logs/x11vnc.log && xset -display {VNC_DISPLAY} r on"
            )
            x11_result = ssh_manager.execute_command(
                ssh, x11vnc_cmd, timeout=15
            )
            if not x11_result.ok:
                logger.warning(
                    "[VNC] Remote x11vnc start failed: %s", x11_result.stderr
                )

        # 检查并启动websockify
        check_ws_cmd = (
            f"pgrep -f -- {shlex.quote(WEBSOCKIFY_PATTERN)} >/dev/null "
            f"&& ss -ltn | grep -q ':{NOVNC_WEB_PORT} ' "
            "&& echo 'RUNNING' || echo 'NOT_RUNNING'"
        )
        ws_check = ssh_manager.execute_command(ssh, check_ws_cmd)
        websockify_running = command_reports_running(ws_check.stdout)

        if not websockify_running:
            novnc_cmd = (
                f"cd {quoted_novnc_dir} && "
                f"nohup ./utils/websockify/run --web {quoted_novnc_dir} "
                f"{NOVNC_WEB_PORT} localhost:{VNC_PORT} "
                "> ~/logs/novnc.log 2>&1 &"
            )
            novnc_result = ssh_manager.execute_command(
                ssh, novnc_cmd, timeout=10
            )
            if not novnc_result.ok:
                logger.warning(
                    "[VNC] Remote websockify start failed: %s",
                    novnc_result.stderr,
                )

        # 启动命令在后台执行，命令本身返回 0 不代表监听已经成功。只有
        # x11vnc 与 websockify 两个端口都就绪时才向前端返回成功。
        verify_cmd = (
            f"ss -ltn | grep -q ':{VNC_PORT} ' && echo VNC_READY || echo VNC_FAILED; "
            f"ss -ltn | grep -q ':{NOVNC_WEB_PORT} ' && echo NOVNC_READY || echo NOVNC_FAILED"
        )
        stdout = ''
        stderr = ''
        vnc_ready = False
        novnc_ready = False
        # 后台进程在较慢主机上可能需要数秒才开始监听。
        for _ in range(10):
            verify_result = ssh_manager.execute_command(ssh, verify_cmd, timeout=5)
            stdout = verify_result.stdout
            stderr = verify_result.stderr
            vnc_ready = 'VNC_READY' in stdout
            novnc_ready = 'NOVNC_READY' in stdout
            if vnc_ready and novnc_ready:
                break
            time.sleep(0.3)
        if not vnc_ready or not novnc_ready:
            log_cmd = "tail -n 12 ~/logs/x11vnc.log ~/logs/novnc.log 2>/dev/null"
            logs_result = ssh_manager.execute_command(ssh, log_cmd, timeout=5)
            ssh_manager.return_connection(ssh)
            failed = []
            if not vnc_ready:
                failed.append(str(VNC_PORT))
            if not novnc_ready:
                failed.append(str(NOVNC_WEB_PORT))
            command_errors = '\n'.join(
                value.strip()
                for value in (
                    x11_result.stderr if x11_result is not None and not x11_result.ok else '',
                    novnc_result.stderr if novnc_result is not None and not novnc_result.ok else '',
                    logs_result.stdout,
                    stderr,
                )
                if value and value.strip()
            )
            detail = command_errors[-1200:]
            return {
                'success': False,
                'error': f"远程端口 {', '.join(failed)} 未监听，VNC 服务启动失败",
                'detail': detail,
            }

        target_ip = CommonUtils.extract_ip_from_host(host)

        ssh_manager.return_connection(ssh)

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
            ssh_manager.return_connection(ssh)
        logger.error(f"Error starting remote VNC: {e}")
        return {'success': False, 'error': str(e)}
