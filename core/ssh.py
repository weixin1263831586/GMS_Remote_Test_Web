"""
SSH管理器 - 同步SSH操作
"""
import paramiko
import logging
from typing import Tuple, Optional
import queue

logger = logging.getLogger(__name__)


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
