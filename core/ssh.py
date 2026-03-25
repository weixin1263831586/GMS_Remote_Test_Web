"""
SSH管理器 - 同步SSH操作
"""
import paramiko
import logging
from typing import Tuple, Optional, Dict, Any
import queue

logger = logging.getLogger(__name__)

# Windows SSHD 安装命令常量
SSHD_UNINSTALL_CMD = 'Get-Service sshd | Stop-Service -Force; Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
SSHD_REMOVE_FILES_CMD = 'Remove-Item -Path "C:\\ProgramData\\ssh" -Recurse -Force -ErrorAction SilentlyContinue'
SSHD_INSTALL_CMD = 'Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
SSHD_CHECK_CMD = 'Get-WindowsCapability -Online | Where-Object Name -like \'OpenSSH*\''
SSHD_START_CMD = 'Start-Service sshd'
SSHD_ENABLE_CMD = 'Set-Service -Name sshd -StartupType \'Automatic\''

SSHD_INSTALL_GUIDE = """以【管理员身份】运行 PowerShell, 按照下面步骤安装:

1️⃣ 安装sshd
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Get-WindowsCapability -Online | Where-Object Name -like 'OpenSSH*'

2️⃣ 启动sshd
Start-Service sshd

3️⃣ 设置sshd开机自启动
Set-Service -Name sshd -StartupType 'Automatic'

⚠️ 若上述步骤安装失败，先以【管理员身份】执行卸载操作，再执行上面的安装步骤

1️⃣ 卸载sshd
Get-Service sshd | Stop-Service -Force
Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

2️⃣ 删除残留文件
Remove-Item -Path "C:\\ProgramData\\ssh" -Recurse -Force -ErrorAction SilentlyContinue

3️⃣ 重启计算机
Restart-Computer
"""


class SSHManager:
    """
    SSH管理器（同步版本）

    特性：
    - SSH连接池
    - 命令执行
    - 超时控制
    """

    def __init__(self, pool_size: int = 5):
        """
        初始化SSH管理器

        Args:
            pool_size: 连接池大小
        """
        self.pool: queue.Queue = queue.Queue(maxsize=pool_size)
        self._lock = None  # 用于简单的锁（如需）

    def create_connection(self, config: dict) -> Optional[paramiko.SSHClient]:
        """
        创建SSH连接

        Args:
            config: 配置字典，包含 host, username, password 等

        Returns:
            SSHClient 对象，失败则返回 None
        """
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            host = config.get('host') or config.get('ubuntu_host')
            username = config.get('username') or config.get('ubuntu_user', 'hcq')
            password = config.get('password') or config.get('ubuntu_pswd', '')

            if config.get('use_key_auth', False):
                key_path = config.get('private_key_path', '~/.ssh/id_rsa')
                key = paramiko.RSAKey.from_private_key_file(key_path)
                ssh.connect(
                    host,
                    username=username,
                    pkey=key,
                    timeout=10
                )
            else:
                if not password:
                    logger.error("[SSH] No SSH password configured")
                    return None
                ssh.connect(
                    host,
                    username=username,
                    password=password,
                    timeout=10
                )

            logger.info(f"[SSH] Connected to {host}")
            return ssh

        except Exception as e:
            logger.error(f"[SSH] Connection error: {e}")
            return None

    def get_connection(self, config: dict) -> Optional[paramiko.SSHClient]:
        """
        从连接池获取或创建连接

        Args:
            config: 配置字典

        Returns:
            SSHClient 对象
        """
        try:
            return self.pool.get_nowait()
        except queue.Empty:
            return self.create_connection(config)

    def return_connection(self, ssh: paramiko.SSHClient):
        """
        归还连接到连接池

        Args:
            ssh: SSHClient 对象
        """
        try:
            self.pool.put_nowait(ssh)
        except queue.Full:
            ssh.close()

    def execute_command(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False
    ) -> Tuple[str, str, int]:
        """
        执行SSH命令

        Args:
            ssh: SSHClient 对象
            command: 要执行的命令
            timeout: 超时时间（秒）
            get_pty: 是否获取伪终端

        Returns:
            (stdout, stderr, exit_code)
        """
        try:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout, get_pty=get_pty)

            stdout_text = stdout.read().decode('utf-8', errors='ignore')
            stderr_text = stderr.read().decode('utf-8', errors='ignore')
            exit_code = stdout.channel.recv_exit_status()

            return stdout_text, stderr_text, exit_code

        except Exception as e:
            logger.error(f"[SSH] Command execution error: {e}")
            return '', str(e), -1

    def check_sshd_installed(self, ssh) -> Tuple[bool, str]:
        """
        检查 SSHD 是否已安装

        Args:
            ssh: SSH 连接对象

        Returns:
            (是否安装, 状态信息)
        """
        try:
            stdout, stderr, code = self.execute_command(ssh, 'Get-Service sshd')
            if code == 0 and stdout.strip():
                return True, stdout.strip()
            return False, ''
        except Exception as e:
            logger.error(f"Error checking sshd: {e}")
            return False, ''

    def install_sshd(self, ssh, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        自动安装 SSHD 到 Windows 主机

        Args:
            ssh: SSH 连接对象
            config: 配置字典

        Returns:
            安装结果字典
        """
        try:
            # 检查 SSHD 是否已安装
            installed, status = self.check_sshd_installed(ssh)
            if installed:
                return {
                    'success': True,
                    'message': 'SSHD 已安装',
                    'status': status
                }

            # 尝试执行安装命令（会自动检查权限）
            stdout, stderr, code = self.execute_command(ssh, SSHD_INSTALL_CMD, timeout=180)

            if code == 0:
                # 启动 SSHD 服务并设置开机自启（合并命令以提高效率）
                combined_cmd = f'{SSHD_START_CMD}; {SSHD_ENABLE_CMD}'
                self.execute_command(ssh, combined_cmd, timeout=60)

                # 验证安装
                installed_verify, status_verify = self.check_sshd_installed(ssh)
                if installed_verify:
                    return {
                        'success': True,
                        'message': 'SSHD 安装成功并已启动',
                        'status': status_verify
                    }
                else:
                    return {
                        'success': True,
                        'message': 'SSHD 安装完成，请验证服务状态'
                    }
            else:
                error_msg = stderr or stdout

                # 检查是否是权限问题
                if 'Access denied' in error_msg or '管理员' in error_msg or 'administrator' in error_msg.lower():
                    error_msg = '需要管理员权限。请确保 Windows 上的 SSH 服务以管理员权限运行，或手动执行以下命令：\n\n' + SSHD_INSTALL_CMD

                return {
                    'success': False,
                    'error': f'安装失败: {error_msg}',
                    'install_guide': SSHD_INSTALL_GUIDE
                }

        except Exception as e:
            logger.error(f"Error installing sshd: {e}")
            return {
                'success': False,
                'error': str(e),
                'install_guide': SSHD_INSTALL_GUIDE
            }

    def close_all(self):
        """关闭所有连接"""
        while not self.pool.empty():
            try:
                ssh = self.pool.get_nowait()
                ssh.close()
            except queue.Empty:
                break


# 全局SSH管理器实例
ssh_manager = SSHManager()
