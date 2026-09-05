"""
SSH管理器 - 同步SSH操作
"""
import asyncio
import logging
import os
import queue
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import paramiko

from features.system.ssh_executor import ssh_executor
from foundation.command_result import CommandResult
from foundation.config import get_ubuntu_user
from foundation.networking import split_host_port
from foundation.ssh import SSHD_INSTALL_GUIDE
from foundation.ssh_security import configure_strict_host_keys


logger = logging.getLogger(__name__)

# SFTP 性能优化常量
SFTP_TIMEOUT_SECONDS = 300  # 5分钟超时
SFTP_WINDOW_SIZE = 2147483647  # 2GB 最大窗口大小
SFTP_REKEY_BYTES = pow(2, 40)  # 减少重新密钥协商频率
SFTP_REKEY_PACKETS = pow(2, 40)

# Windows SSHD 安装命令常量
SSHD_UNINSTALL_CMD = 'Get-Service sshd | Stop-Service -Force; Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
SSHD_REMOVE_FILES_CMD = 'Remove-Item -Path "C:\\ProgramData\\ssh" -Recurse -Force -ErrorAction SilentlyContinue'
SSHD_INSTALL_CMD = 'Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0'
SSHD_CHECK_CMD = 'Get-WindowsCapability -Online | Where-Object Name -like \'OpenSSH*\''
SSHD_START_CMD = 'Start-Service sshd'
SSHD_ENABLE_CMD = 'Set-Service -Name sshd -StartupType \'Automatic\''

class SSHManager:
    """提供连接池、命令执行和超时控制的同步 SSH 管理器。"""

    def __init__(self, pool_size: int = 5):
        """初始化指定容量的连接池。"""
        self.pool: queue.Queue = queue.Queue(maxsize=pool_size)

    def _load_ssh_key(self, key_path: str) -> paramiko.PKey | None:
        """尝试按多种密钥类型加载 SSH 私钥。"""
        key_path = os.path.expanduser(key_path)
        key_error = None

        for key_class in [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.DSSKey]:
            try:
                key = key_class.from_private_key_file(key_path)
                logger.info(f"[SSH] Loaded {key_class.__name__} from {key_path}")
                return key
            except Exception as e:
                key_error = e
                continue

        logger.error(f"[SSH] Failed to load SSH key from {key_path}: {key_error}")
        return None

    def create_connection(
        self, config: dict, *, raise_on_error: bool = False
    ) -> paramiko.SSHClient | None:
        """根据配置创建 SSH 连接，失败时按需返回 None 或抛出异常。"""
        try:
            ssh = paramiko.SSHClient()
            configure_strict_host_keys(ssh)

            host = config.get('host') or config.get('hostname') or config.get('ubuntu_host')
            host, parsed_port = split_host_port(str(host or ''))
            port = int(config.get('port') or parsed_port)
            username = config.get('username') or config.get('ubuntu_user') or get_ubuntu_user()
            password = config.get('password') or config.get('ubuntu_pswd', '')

            if config.get('use_key_auth', False):
                key = self._load_ssh_key(config.get('private_key_path', '~/.ssh/id_rsa'))
                if key:
                    ssh.connect(
                        host,
                        port=port,
                        username=username,
                        pkey=key,
                        timeout=10
                    )
                else:
                    # 安装目录可能从另一台机器恢复，导致 runtime 配置仍指向
                    # 已不存在的专用密钥。use_key_auth 表示使用密钥认证，不应
                    # 因单个显式路径失效而禁止 Paramiko 的标准 Agent/default-key
                    # 发现；否则本机已有可用 ~/.ssh/id_rsa 也会被误报为 SSH 失败。
                    logger.warning(
                        "[SSH] Configured private key is unavailable; "
                        "trying SSH agent and default key files"
                    )
                    ssh.connect(
                        host,
                        port=port,
                        username=username,
                        timeout=10,
                        banner_timeout=10,
                        auth_timeout=10,
                        allow_agent=True,
                        look_for_keys=True,
                    )
            else:
                if not password:
                    logger.error("[SSH] No SSH password configured")
                    return None
                ssh.connect(
                    host,
                    port=port,
                    username=username,
                    password=password,
                    timeout=10,
                    # 优化连接参数
                    banner_timeout=10,
                    auth_timeout=10
                )

            logger.info(f"[SSH] Connected to {host}")
            # 保存目标身份，防止跨主机复用连接。
            ssh._gms_pool_identity = (str(host), str(port), str(username))
            return ssh

        except Exception as e:
            logger.error(f"[SSH] Connection error: {e}")
            if raise_on_error:
                raise
            return None

    def get_connection(self, config: dict) -> paramiko.SSHClient | None:
        """从连接池获取健康连接，必要时新建。"""
        requested_host, requested_port = split_host_port(str(
            config.get('host') or config.get('hostname') or config.get('ubuntu_host') or ''
        ))
        requested_identity = (
            requested_host,
            str(requested_port),
            str(config.get('username') or config.get('ubuntu_user') or get_ubuntu_user()),
        )

        # 尝试从池中获取有效连接，最多尝试 pool_size 次防止无限循环
        max_attempts = self.pool.maxsize
        for attempt in range(max_attempts):
            try:
                ssh = self.pool.get_nowait()
                if getattr(ssh, '_gms_pool_identity', None) != requested_identity:
                    logger.debug("[SSH] Discarding pooled connection for a different destination")
                    try:
                        ssh.close()
                    except Exception:
                        pass
                    continue
                # 测试连接是否仍然有效（轻量级检查）。
                # 注意：paramiko 的 recv_exit_status() 不接受 timeout 参数
                # MagicMock 测试掩盖了 TypeError，导致池内健康
                # 连接被误判为死连接、复用路径永远走不到）。这里靠
                # exec_command(timeout=2) 的 channel 读超时兜底。
                try:
                    _stdin, stdout, _stderr = ssh.exec_command('true', timeout=2)
                    stdout.channel.settimeout(2.0)
                    exit_code = stdout.channel.recv_exit_status()
                    if exit_code == 0:
                        logger.debug("[SSH] Reused connection from pool")
                        return ssh
                    logger.debug(f"[SSH] Pool health check exit_code={exit_code}")
                except Exception as e:
                    logger.debug(f"[SSH] Connection {attempt+1}/{max_attempts} is dead: {e}")
                    try:
                        ssh.close()
                    except Exception:
                        pass
            except queue.Empty:
                break

        # 池为空或所有连接都失效，创建新连接
        logger.debug("[SSH] Creating new connection")
        return self.create_connection(config)

    def return_connection(self, ssh: paramiko.SSHClient):
        """将连接归还连接池，池满时关闭连接。"""
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
    ) -> CommandResult:
        """执行 SSH 命令，统一返回 :class:`CommandResult`。

        同步/异步执行统一委托给
        :class:`~features.system.ssh_executor.SSHExecutor` 唯一实现，
        彻底废除 ``(stdout, stderr, exit_code)`` 裸 tuple（位置错用曾造成
        真实功能 bug）。
        """
        return ssh_executor.run(ssh, command, timeout=timeout, get_pty=get_pty)

    def check_sshd_installed(self, ssh) -> tuple[bool, str]:
        """检查 Windows SSHD 是否已安装。"""
        try:
            result = self.execute_command(ssh, 'Get-Service sshd')
            if result.code == 0 and result.stdout.strip():
                return True, result.stdout.strip()
            return False, ''
        except Exception as e:
            logger.error(f"Error checking sshd: {e}")
            return False, ''

    def install_sshd(self, ssh, config: dict[str, Any]) -> dict[str, Any]:
        """在 Windows 主机安装并启动 SSHD。"""
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
            install_result = self.execute_command(ssh, SSHD_INSTALL_CMD, timeout=180)

            if install_result.code == 0:
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
                error_msg = install_result.stderr or install_result.stdout

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

    @contextmanager
    def connection(self, config: dict):
        """获取 SSH 连接，并在退出时归还连接池。"""
        ssh = self.get_connection(config)
        if not ssh:
            raise RuntimeError("Failed to get SSH connection")

        try:
            yield ssh
        finally:
            self.return_connection(ssh)

    @contextmanager
    def optional_connection(self, config: dict):
        """允许连接为空，并在退出时归还有效连接。"""
        ssh = self.get_connection(config)
        try:
            yield ssh
        finally:
            if ssh is not None:
                self.return_connection(ssh)

    @asynccontextmanager
    async def async_optional_connection(self, config: dict):
        """None-safe connection context without blocking the event loop."""
        ssh = await asyncio.to_thread(self.get_connection, config)
        try:
            yield ssh
        finally:
            if ssh is not None:
                await asyncio.to_thread(self.return_connection, ssh)

    def optimize_sftp_performance(self, sftp):
        """配置 SFTP 超时、窗口和重新密钥阈值。"""
        sftp.get_channel().settimeout(SFTP_TIMEOUT_SECONDS)
        sftp.get_channel().transport.window_size = SFTP_WINDOW_SIZE
        sftp.get_channel().transport.packetizer.REKEY_BYTES = SFTP_REKEY_BYTES
        sftp.get_channel().transport.packetizer.REKEY_PACKETS = SFTP_REKEY_PACKETS


# 全局SSH管理器实例
ssh_manager = SSHManager()
