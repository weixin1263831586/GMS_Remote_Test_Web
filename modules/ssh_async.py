#!/usr/bin/env python3
"""
SSH 异步管理器 - 异步执行 SSH 命令并实时推送日志
"""

import asyncio
import paramiko
from typing import Optional, Dict, List, Callable
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class SSHAsyncManager:
    """
    SSH 异步管理器

    特性：
    - 异步执行 SSH 命令
    - 实时流式输出日志
    - 支持超时控制
    - 连接池管理
    """

    def __init__(self):
        """初始化 SSH 异步管理器"""
        # SSH 连接池 {host: paramiko.SSHClient}
        self.connections: Dict[str, paramiko.SSHClient] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 22,
        timeout: int = 10
    ) -> paramiko.SSHClient:
        """
        异步建立 SSH 连接

        Args:
            host: 主机地址
            username: 用户名
            password: 密码
            port: SSH 端口
            timeout: 连接超时时间

        Returns:
            SSHClient 对象
        """
        async with self._lock:
            # 检查是否已有连接
            if host in self.connections:
                try:
                    # 测试连接是否仍然有效
                    transport = self.connections[host].get_transport()
                    if transport and transport.is_active():
                        logger.debug(f"[SSH] Reusing existing connection to {host}")
                        return self.connections[host]
                except:
                    pass

            # 建立新连接
            logger.info(f"[SSH] Connecting to {host}...")

            def _connect():
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
                self.connections[host] = ssh
                logger.info(f"[SSH] Connected to {host}")
                return ssh
            except Exception as e:
                logger.error(f"[SSH] Failed to connect to {host}: {e}")
                raise

    async def execute_command_with_stream(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        log_callback: Callable[[str, str], None],
        timeout: int = 300
    ) -> int:
        """
        执行命令并实时流式输出日志

        Args:
            ssh: SSH 连接对象
            command: 要执行的命令
            log_callback: 日志回调函数 (message, type) -> None
            timeout: 命令超时时间（秒）

        Returns:
            命令退出码
        """
        logger.info(f"[SSH] Executing command: {command[:100]}")

        try:
            # 执行命令
            stdin, stdout, stderr = ssh.exec_command(
                command,
                get_pty=True,
                timeout=timeout
            )

            # 异步读取标准输出
            stdout_task = asyncio.create_task(
                self._read_stream(stdout, log_callback, 'info')
            )

            # 异步读取标准错误
            stderr_task = asyncio.create_task(
                self._read_stream(stderr, log_callback, 'error')
            )

            # 等待两个读取任务完成
            await asyncio.gather(stdout_task, stderr_task)

            # 获取退出码
            exit_code = stdout.channel.recv_exit_status()

            logger.info(f"[SSH] Command completed with exit code: {exit_code}")
            return exit_code

        except Exception as e:
            logger.error(f"[SSH] Error executing command: {e}")
            await log_callback(f"SSH 执行错误: {str(e)}", 'error')
            return -1

    async def _read_stream(
        self,
        stream: paramiko.ChannelFile,
        log_callback: Callable[[str, str], None],
        log_type: str
    ):
        """
        异步读取流

        Args:
            stream: 输入流
            log_callback: 日志回调函数
            log_type: 日志类型
        """
        try:
            while not stream.channel.exit_status_ready():
                if stream.channel.recv_ready():
                    # 在线程池中读取数据
                    data = await asyncio.to_thread(stream.channel.recv, 65536)
                    if data:
                        text = data.decode('utf-8', errors='replace')
                        # 按行分割并发送日志
                        for line in text.split('\n'):
                            if line.strip():
                                await log_callback(line.strip(), log_type)
                else:
                    # 没有数据时短暂休眠
                    await asyncio.sleep(0.01)

        except Exception as e:
            logger.error(f"[SSH] Error reading stream: {e}")

    async def execute_command_simple(
        self,
        host: str,
        username: str,
        password: str,
        command: str,
        timeout: int = 30
    ) -> tuple[int, str, str]:
        """
        简单执行命令（非流式）

        Args:
            host: 主机地址
            username: 用户名
            password: 密码
            command: 命令
            timeout: 超时时间

        Returns:
            (退出码, 标准输出, 标准错误)
        """
        ssh = await self.connect(host, username, password, timeout=timeout)

        def _exec():
            stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            stdout_text = stdout.read().decode('utf-8', errors='replace')
            stderr_text = stderr.read().decode('utf-8', errors='replace')
            return exit_code, stdout_text, stderr_text

        try:
            result = await asyncio.to_thread(_exec)
            return result
        except Exception as e:
            logger.error(f"[SSH] Error in simple execute: {e}")
            return -1, '', str(e)

    def close(self, host: str):
        """
        关闭指定主机的连接

        Args:
            host: 主机地址
        """
        if host in self.connections:
            try:
                self.connections[host].close()
                del self.connections[host]
                logger.info(f"[SSH] Closed connection to {host}")
            except Exception as e:
                logger.error(f"[SSH] Error closing connection to {host}: {e}")

    def close_all(self):
        """关闭所有连接"""
        for host in list(self.connections.keys()):
            self.close(host)


# 全局 SSH 异步管理器实例
ssh_async_manager = SSHAsyncManager()
