#!/usr/bin/env python3
"""
SSH 异步管理器 - 异步执行 SSH 命令并实时推送日志

执行实现统一委托给
:class:`~features.system.ssh_executor.SSHExecutor`（与同步 SSHManager
共用同一实现，杜绝行为漂移）；本类只保留异步连接管理和既有 API 兼容。
"""

import asyncio
import logging

import paramiko

from features.system.ssh_executor import ssh_executor
from foundation.command_result import CommandResult
from foundation.ssh_security import configure_strict_host_keys


logger = logging.getLogger(__name__)


class SSHAsyncManager:
    """
    SSH 异步管理器

    特性：
    - 异步执行 SSH 命令（SSHExecutor.run_async 线程池实现）
    - 实时流式输出日志（stderr 走 recv_stderr API）
    - 支持超时控制
    - 按目标身份缓存的连接管理
    """

    def __init__(self):
        """初始化 SSH 异步管理器"""
        # SSH connections are identity-scoped; a host-only key could reuse an
        # authenticated session for the wrong user.
        self.connections: dict[tuple[str, int, str], paramiko.SSHClient] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        timeout: int = 10
    ) -> paramiko.SSHClient:
        """在线程中建立 SSH 连接并按目标身份缓存。"""
        connection_key = (host, port, username)
        async with self._lock:
            # 检查是否已有连接
            if connection_key in self.connections:
                try:
                    # 测试连接是否仍然有效
                    transport = self.connections[connection_key].get_transport()
                    if transport and transport.is_active():
                        logger.debug(f"[SSH] Reusing existing connection to {host}")
                        return self.connections[connection_key]
                except Exception:
                    pass

            # 建立新连接
            logger.info(f"[SSH] Connecting to {host}...")

            def _connect():
                ssh = paramiko.SSHClient()
                configure_strict_host_keys(ssh)
                ssh.connect(
                    hostname=host,
                    username=username,
                    password=password,
                    port=port,
                    timeout=timeout,
                    look_for_keys=False,
                    allow_agent=False
                )
                return ssh

            try:
                # 在线程池中执行同步的 SSH 连接
                ssh = await asyncio.to_thread(_connect)
                self.connections[connection_key] = ssh
                logger.info(f"[SSH] Connected to {host}")
                return ssh
            except Exception as e:
                logger.error(f"[SSH] Failed to connect to {host}: {e}")
                raise

    async def execute_command_with_stream(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        log_callback,
        timeout: int = 300,
        get_pty: bool = False,
    ) -> CommandResult:
        """执行 SSH 命令，通过回调输出日志并返回 :class:`CommandResult`。

        实现委托给 SSHExecutor.run_stream（stdout 走 ``recv``、stderr 走
        ``recv_stderr``，drain 完成后再取退出码）。
        """
        return await ssh_executor.run_stream(
            ssh, command, log_callback, timeout, get_pty=get_pty,
        )


    async def execute_command_simple(
        self,
        host: str,
        username: str,
        password: str,
        command: str,
        timeout: int = 30
    ) -> CommandResult:
        """执行非流式 SSH 命令，统一返回 :class:`CommandResult`。"""
        ssh = await self.connect(host, username, password, timeout=timeout)
        return await ssh_executor.run_async(ssh, command, timeout=timeout)

    def close(self, host: str):
        """
        关闭指定主机的连接

        Args:
            host: 主机地址
        """
        keys = [key for key in self.connections if key[0] == host]
        for key in keys:
            try:
                self.connections[key].close()
                del self.connections[key]
            except Exception as e:
                logger.error(f"[SSH] Error closing connection to {host}: {e}")
        if keys:
            logger.info(f"[SSH] Closed {len(keys)} connection(s) to {host}")

    def close_all(self):
        """关闭所有连接"""
        for host in {key[0] for key in self.connections}:
            self.close(host)


# 全局 SSH 异步管理器实例
ssh_async_manager = SSHAsyncManager()
