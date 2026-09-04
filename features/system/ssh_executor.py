"""Unified SSH execution layer (sync + async + streaming).

4.txt 第 12 节：同步 ``SSHManager`` 与异步 ``SSHAsyncManager`` 两套实现
行为漂移（stdout/stderr API 混用、``recv_exit_status`` 顺序、tuple 顺序
错位），统一收敛到本模块——整个项目只有这一份 SSH 执行实现：

- :meth:`SSHExecutor.run`          同步执行，返回 :class:`CommandResult`
- :meth:`SSHExecutor.run_async`    线程池执行同一实现（FastAPI async 路由用）
- :meth:`SSHExecutor.run_stream`   异步流式执行，逐行回调，stderr 走
  ``recv_stderr`` API，drain 完成后再取退出码

``SSHManager.execute_command`` / ``SSHAsyncManager`` 只做连接管理与薄委托，
不存在第二份执行语义；所有结果统一为 :class:`CommandResult`，不再有
``(stdout, stderr, exit_code)`` 裸 tuple。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import paramiko

from foundation.command_result import CommandResult
from foundation.common_utils import CommonUtils

logger = logging.getLogger(__name__)

_READ_CHUNK = 65536
_POLL_INTERVAL = 0.01


class SSHExecutor:
    """Single execution implementation shared by sync and async managers.

    执行语义（唯一实现，杜绝双实现漂移）：
    1. 先 drain stdout/stderr，再 ``recv_exit_status()``——Paramiko 官方
       警告，大输出场景下先取退出码可能因 channel 窗口耗尽永久等待；
    2. stderr 一律走 ``recv_stderr_ready()/recv_stderr()``，与 stdout 的
       ``recv_ready()/recv()`` 分离——历史实现两个读取任务争抢同一个
       stdout channel，stderr 日志错乱/丢失；
    3. 异常一律折叠为 ``CommandResult(stdout='', stderr=<msg>, code=-1)``，
       调用方统一以 ``code == -1`` 判错（流式路径额外回调一条 error 日志）。
    """

    def run(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False,
    ) -> CommandResult:
        """Blocking execution on an established SSH client."""
        try:
            _stdin, stdout, stderr = ssh.exec_command(
                command, timeout=timeout, get_pty=get_pty,
            )
            stdout_text = CommonUtils.decode_ssh_output(stdout.read())
            stderr_text = CommonUtils.decode_ssh_output(stderr.read())
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(stdout=stdout_text, stderr=stderr_text, code=exit_code)
        except Exception as e:
            logger.error(f"[SSH] Command execution error: {e}")
            return CommandResult(stdout="", stderr=str(e), code=-1)

    async def run_async(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False,
    ) -> CommandResult:
        """Non-blocking execution for async routes (thread offload)."""
        return await asyncio.to_thread(self.run, ssh, command, timeout, get_pty)

    async def run_stream(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        log_callback: Callable[[str, str], Awaitable[None]],
        timeout: int = 300,
        get_pty: bool = False,
    ) -> CommandResult:
        """Streaming execution with per-line log callback.

        - ``get_pty=False``（默认）：stdout/stderr 是两条独立流，stderr 行
          以 ``error`` 级别回调；需要 tty 的命令（如 sudo 提示）显式传
          ``get_pty=True``，此时 stderr 合并进 stdout，统一按 ``info`` 回调；
        - 逐行回调的同时捕获全文，结束后返回带 stdout/stderr/exit code
          的 :class:`CommandResult`；
        - 退出状态就绪后仍继续 drain 缓冲数据，避免尾部输出丢失。
        """
        logger.info(f"[SSH] Executing command: {command[:100]}")
        try:
            _stdin, stdout, stderr = await asyncio.to_thread(
                ssh.exec_command,
                command,
                get_pty=get_pty,
                timeout=timeout,
            )

            async def relay(stream, log_type: str, reader) -> str:
                # stderr 必须走 recv_stderr API；两个任务争抢同一个 stdout
                # channel 会让 stderr 日志错乱/丢失（历史 P1）。
                captured: list[str] = []
                pending = ""
                while True:
                    if reader["ready"]():
                        data = await asyncio.to_thread(reader["recv"], _READ_CHUNK)
                        if data:
                            pending += CommonUtils.decode_ssh_output(data)
                            *lines, pending = pending.split("\n")
                            for line in lines:
                                if line.strip():
                                    captured.append(line)
                                    await log_callback(line.strip(), log_type)
                        continue
                    if stream.channel.exit_status_ready():
                        # 进程已退出：flush 跨 chunk 残留的最后一行。
                        if pending.strip():
                            captured.append(pending)
                            await log_callback(pending.strip(), log_type)
                        break
                    await asyncio.sleep(_POLL_INTERVAL)
                return "\n".join(captured)

            stdout_text, stderr_text = await asyncio.gather(
                relay(
                    stdout, "info",
                    {"ready": stdout.channel.recv_ready, "recv": stdout.channel.recv},
                ),
                relay(
                    stderr, "error",
                    {
                        "ready": stderr.channel.recv_stderr_ready,
                        "recv": stderr.channel.recv_stderr,
                    },
                ),
            )

            # drain 完成后再取退出码，并移入线程避免阻塞事件循环。
            exit_code = await asyncio.to_thread(stdout.channel.recv_exit_status)
            logger.info(f"[SSH] Command completed with exit code: {exit_code}")
            return CommandResult(
                stdout=stdout_text, stderr=stderr_text, code=exit_code,
            )

        except Exception as e:
            logger.error(f"[SSH] Error executing command: {e}")
            await log_callback(f"SSH 执行错误: {e!s}", "error")
            return CommandResult(stdout="", stderr=str(e), code=-1)


# 全局执行器实例（无状态，可在同步与异步上下文共用）
ssh_executor = SSHExecutor()
