"""usbipd-win installation checks and guided setup for Windows source hosts."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)

# usbipd 安装命令常量
USBIPD_INSTALL_CMD = 'winget install dorssel.usbipd-win --source winget'

USBIPD_INSTALL_GUIDE = '''在Windows电脑上以【管理员身份】运行PowerShell执行：
{install_cmd}
验证安装：usbipd --version'''


def usbipd_not_installed_error() -> dict[str, Any]:
    """Standard error payload used whenever usbipd-win is missing."""
    return {
        'success': False,
        'error': 'usbipd未安装',
        'install_guide': USBIPD_INSTALL_GUIDE.format(
            install_cmd=USBIPD_INSTALL_CMD
        ),
    }


def check_usbipd_installed(ssh_manager, ssh) -> tuple[bool, str]:
    """Check whether usbipd is installed on the Windows host; return (installed, version)."""
    try:
        stdout, _stderr, code = ssh_manager.execute_command(ssh, 'usbipd --version')
        if code == 0 and stdout.strip():
            return True, stdout.strip()
        return False, ''
    except Exception as e:
        logger.error(f"Error checking usbipd: {e}")
        return False, ''


def install_usbipd(ssh_manager, ssh, config: dict[str, Any]) -> dict[str, Any]:
    """在 Windows 主机自动安装 usbipd。"""
    try:
        # 检查是否已经是管理员权限
        check_admin_cmd = 'whoami /groups | findstr S-1-16-12288'
        stdout, stderr, code = ssh_manager.execute_command(ssh, check_admin_cmd)

        if code != 0 or 'S-1-16-12288' not in stdout:
            return {
                'success': False,
                'error': f'需要管理员权限。请在 Windows 上以【管理员身份】运行 PowerShell，然后执行: {USBIPD_INSTALL_CMD}'
            }

        # 执行自动安装命令（添加自动接受参数）
        install_cmd = f'{USBIPD_INSTALL_CMD} --accept-package-agreements --accept-source-agreements'
        stdout, stderr, code = ssh_manager.execute_command(ssh, install_cmd, timeout=120)

        if code == 0:
            # 验证安装
            installed, version = check_usbipd_installed(ssh_manager, ssh)
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
