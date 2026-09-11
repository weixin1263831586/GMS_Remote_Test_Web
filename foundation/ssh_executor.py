"""Unified SSH execution layer (sync + async + streaming).

同步 ``SSHManager`` 与异步 ``SSHAsyncManager`` 两套实现
行为漂移（stdout/stderr API 混用、``recv_exit_status`` 顺序、tuple 顺序
错位），统一收敛到本模块——整个项目只有这一份 SSH 执行实现：

- :meth:`SSHExecutor.run`          同步执行，返回 :class:`CommandResult`
- :meth:`SSHExecutor.run_async`    线程池执行同一实现（FastAPI async 路由用）
- :meth:`SSHExecutor.run_stream`   异步流式执行，逐行回调，stderr 走
  ``recv_stderr`` API，drain 完成后再取退出码

本模块位于 foundation 层，是 Build / System / Cluster 共用的 SSH 执行
原语（feature 之间禁止互相 import 内部模块，共享实现必须下沉到这里）。
历史的 ``features/system/ssh_executor.py`` 薄再导出已删除，所有调用方
直接 import 本模块。

``SSHManager.execute_command`` / ``SSHAsyncManager`` 只做连接管理与薄委托，
不存在第二份执行语义；所有结果统一为 :class:`CommandResult`，不再有
``(stdout, stderr, exit_code)`` 裸 tuple。

架构硬规则（由 ``tests/architecture/test_ssh_command_execution.py`` 强制）：
业务模块禁止直接调用 ``ssh.exec_command()``——命令执行必须经过本模块
（``SSHExecutor``/``SSHManager.execute_command``），例外仅限本文件与
已登记的 connection-health 原语。直接调用会重新引入 stdout/stderr
channel 窗口互锁死锁与双实现漂移。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

import paramiko

from foundation.command_result import CommandResult
from foundation.common_utils import CommonUtils


logger = logging.getLogger(__name__)

_READ_CHUNK = 65536
_POLL_INTERVAL = 0.01
_EXIT_DRAIN_GRACE_SECONDS = 0.05
# R11：退出状态就绪后允许的尾部 drain 上限——远端在 exit 之后仍持续
# 输出（如派生进程继承 channel）不能把执行器拖住。
_EXIT_TAIL_DRAIN_SECONDS = 10.0
# R11：单条命令在内存中捕获的每流输出上限；超出部分丢弃并打标记，
# 防止高输出任务把 Controller/Worker 内存打爆。
_MAX_CAPTURED_STREAM_BYTES = 8 * 1024 * 1024
_TRUNCATION_MARKER = "\n...[GMS: output truncated]"


class SSHExecutor:
    """Single execution implementation shared by sync and async managers.

    执行语义（唯一实现，杜绝双实现漂移）：
    1. 先 drain stdout/stderr，再 ``recv_exit_status()``——Paramiko 官方
       警告，大输出场景下先取退出码可能因 channel 窗口耗尽永久等待；
    2. stderr 一律走 ``recv_stderr_ready()/recv_stderr()``，与 stdout 的
       ``recv_ready()/recv()`` 分离——历史实现两个读取任务争抢同一个
       stdout channel，stderr 日志错乱/丢失；
    3. 异常一律折叠为 ``CommandResult(stdout='', stderr=<msg>, code=-1)``，
       调用方统一以 ``code == -1`` 判错（流式路径额外回调一条 error 日志）；
    4. R11（2026-09-08 审核）：执行结束（成功/超时/取消/异常）都在
       ``finally`` 中关闭 channel。注意这只释放本地 SSH channel 并向远端
       发送 EOF——脱离会话的远端进程（nohup/setsid）不会因此被终止；
       对需要强终止的长任务，应使用可追踪的远端任务/进程组协议，不能
       把"channel 已关闭"等同于"远端进程已退出"；
    5. R11：总体 deadline 在持续 drain 期间同样生效，退出后的尾部 drain
       与内存中的输出捕获均有上限；可选 ``should_cancel`` 回调提供协作
       式取消（检查点返回 True 时停止执行并关闭 channel）。
    """

    def __init__(
        self,
        exit_tail_drain_seconds: float = _EXIT_TAIL_DRAIN_SECONDS,
        max_captured_stream_bytes: int = _MAX_CAPTURED_STREAM_BYTES,
    ):
        # 默认值即生产语义；测试用小值验证尾部 drain 上限与截断标记。
        self._exit_tail_drain_seconds = float(exit_tail_drain_seconds)
        self._max_captured_stream_bytes = int(max_captured_stream_bytes)

    def run(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False,
        should_cancel: Callable[[], bool] | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        """Blocking execution on an established SSH client.

        ``input_text``：命令启动后写入 stdin 的数据（如 ``sudo -S`` 的密
        码行）。写入后立即关闭 stdin 送 EOF，避免远端继续等待输入。
        """
        channel = None
        try:
            _stdin, stdout, _stderr = ssh.exec_command(
                command, timeout=timeout, get_pty=get_pty,
            )
            if input_text is not None:
                # sudo -S 等交互输入：写完立即 EOF，不阻塞 drain 循环。
                try:
                    _stdin.write(input_text)
                    _stdin.flush()
                except Exception as exc:
                    logger.warning("[SSH] stdin write failed: %s", exc)
                with suppress(Exception):
                    _stdin.channel.shutdown_write()
            channel = stdout.channel
            # 缓冲原始字节、结束后整体解码：decode_ssh_output 的
            # utf-8→gbk 兜底链必须作用于完整流，逐 chunk 解码会把跨
            # chunk 的多字节字符误判为非 UTF-8 而产生乱码。
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            stdout_bytes = 0
            stderr_bytes = 0
            stdout_truncated = False
            stderr_truncated = False
            deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
            exit_seen_at: float | None = None
            max_captured = self._max_captured_stream_bytes

            def _over_deadline() -> bool:
                # 总体 deadline 在持续 drain 期间同样生效（R11），不能只
                # 在外层轮询处检查——高输出会一直走 recv 分支绕过它。
                return (
                    deadline is not None
                    and exit_seen_at is None
                    and time.monotonic() >= deadline
                )

            def _cancel_requested() -> bool:
                return should_cancel is not None and should_cancel()

            # stdout/stderr share one SSH channel window. Reading one stream to
            # EOF before touching the other can deadlock when the unconsumed
            # stream fills the remote window. Drain both streams concurrently.
            while True:
                made_progress = False
                while channel.recv_ready():
                    data = channel.recv(_READ_CHUNK)
                    if data:
                        if stdout_bytes + len(data) <= max_captured:
                            stdout_chunks.append(data)
                            stdout_bytes += len(data)
                        else:
                            stdout_truncated = True
                    made_progress = True
                    if _over_deadline():
                        raise TimeoutError(
                            f"SSH command timed out after {timeout} seconds"
                        )
                    if _cancel_requested():
                        raise RuntimeError("SSH command cancelled by caller")
                while channel.recv_stderr_ready():
                    data = channel.recv_stderr(_READ_CHUNK)
                    if data:
                        if stderr_bytes + len(data) <= max_captured:
                            stderr_chunks.append(data)
                            stderr_bytes += len(data)
                        else:
                            stderr_truncated = True
                    made_progress = True
                    if _over_deadline():
                        raise TimeoutError(
                            f"SSH command timed out after {timeout} seconds"
                        )
                    if _cancel_requested():
                        raise RuntimeError("SSH command cancelled by caller")

                now = time.monotonic()
                if channel.exit_status_ready():
                    if exit_seen_at is None:
                        exit_seen_at = now
                    if (
                        not channel.recv_ready()
                        and not channel.recv_stderr_ready()
                        and now - exit_seen_at >= _EXIT_DRAIN_GRACE_SECONDS
                    ):
                        break
                    if now - exit_seen_at >= self._exit_tail_drain_seconds:
                        # exit 之后仍有数据到达（远端派生进程持有 channel）：
                        # 尾部 drain 到上限为止，避免执行器被无限拖住。
                        logger.warning(
                            "[SSH] tail drain exceeded %.1fs after exit; "
                            "closing channel anyway",
                            self._exit_tail_drain_seconds,
                        )
                        break
                else:
                    exit_seen_at = None

                if _over_deadline():
                    raise TimeoutError(
                        f"SSH command timed out after {timeout} seconds"
                    )
                if _cancel_requested():
                    raise RuntimeError("SSH command cancelled by caller")
                if not made_progress:
                    time.sleep(_POLL_INTERVAL)

            stdout_text = CommonUtils.decode_ssh_output(b"".join(stdout_chunks))
            stderr_text = CommonUtils.decode_ssh_output(b"".join(stderr_chunks))
            if stdout_truncated:
                stdout_text += _TRUNCATION_MARKER
            if stderr_truncated:
                stderr_text += _TRUNCATION_MARKER
            exit_code = channel.recv_exit_status()
            return CommandResult(stdout=stdout_text, stderr=stderr_text, code=exit_code)
        except Exception as e:
            logger.error(f"[SSH] Command execution error: {e}")
            return CommandResult(stdout="", stderr=str(e), code=-1)
        finally:
            # R11：无论成功、超时、取消还是异常，都释放本地 channel，
            # 不再让调用方认为已结束的命令继续占用 SSH 资源。
            if channel is not None:
                with suppress(Exception):
                    channel.close()

    async def run_async(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        timeout: int = 30,
        get_pty: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        """Non-blocking execution for async routes (thread offload)."""
        return await asyncio.to_thread(
            self.run, ssh, command, timeout, get_pty, None, input_text,
        )

    async def run_stream(
        self,
        ssh: paramiko.SSHClient,
        command: str,
        log_callback: Callable[[str, str], Awaitable[None]],
        timeout: int = 300,
        get_pty: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> CommandResult:
        """Streaming execution with per-line log callback.

        - ``get_pty=False``（默认）：stdout/stderr 是两条独立流，stderr 行
          以 ``error`` 级别回调；需要 tty 的命令（如 sudo 提示）显式传
          ``get_pty=True``，此时 stderr 合并进 stdout，统一按 ``info`` 回调；
        - 逐行回调的同时捕获全文，结束后返回带 stdout/stderr/exit code
          的 :class:`CommandResult`；
        - 退出状态就绪后仍继续 drain 缓冲数据，避免尾部输出丢失；
        - R11：超时/取消/异常路径在 ``finally`` 中关闭 channel；持续高
          输出同样受总体 deadline 约束；内存捕获有上限（超出打标记），
          退出后的尾部 drain 有时限。
        """
        logger.info(f"[SSH] Executing command: {command[:100]}")
        channel = None
        try:
            _stdin, stdout, _stderr = await asyncio.to_thread(
                ssh.exec_command,
                command,
                get_pty=get_pty,
                timeout=timeout,
            )
            channel = stdout.channel
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            # 行缓冲以字节为单位、解码放在整行完成后：逐 chunk 直接
            # decode 会把跨 chunk 的多字节中文字符截断成替换符（与
            # run() 的整流解码约束一致，见其注释）。
            stdout_pending = b""
            stderr_pending = b""
            stdout_bytes = 0
            stderr_bytes = 0
            stdout_truncated = False
            stderr_truncated = False
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout if timeout and timeout > 0 else None
            exit_seen_at: float | None = None

            def _over_deadline() -> bool:
                # 总体 deadline 在持续 drain 期间同样生效（R11）。
                return (
                    deadline is not None
                    and exit_seen_at is None
                    and loop.time() >= deadline
                )

            def _cancel_requested() -> bool:
                return should_cancel is not None and should_cancel()

            max_captured = self._max_captured_stream_bytes

            async def consume_chunk(
                data: bytes,
                pending: bytes,
                captured: list[str],
                log_type: str,
                capture: bool = True,
            ) -> bytes:
                pending += data
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    raw, pending = pending[:newline], pending[newline + 1:]
                    line = raw.decode("utf-8", errors="replace")
                    if line.strip():
                        # R11：超过捕获上限后仍逐行回调（实时日志不中断），
                        # 但不再把行留在内存里。
                        if capture:
                            captured.append(line)
                        await log_callback(line.strip(), log_type)
                return pending

            # One loop owns the shared channel and drains both streams. This
            # avoids two relay tasks racing on exit_status_ready() and prevents
            # either stream from exiting before late tail data becomes ready.
            while True:
                made_progress = False
                while channel.recv_ready():
                    data = await asyncio.to_thread(channel.recv, _READ_CHUNK)
                    if data:
                        if stdout_bytes + len(data) <= max_captured:
                            stdout_bytes += len(data)
                        else:
                            stdout_truncated = True
                        stdout_pending = await consume_chunk(
                            data, stdout_pending, stdout_lines, "info",
                            capture=not stdout_truncated,
                        )
                    made_progress = True
                    if _over_deadline():
                        raise TimeoutError(
                            f"SSH command timed out after {timeout} seconds"
                        )
                    if _cancel_requested():
                        raise RuntimeError("SSH command cancelled by caller")
                while channel.recv_stderr_ready():
                    data = await asyncio.to_thread(channel.recv_stderr, _READ_CHUNK)
                    if data:
                        if stderr_bytes + len(data) <= max_captured:
                            stderr_bytes += len(data)
                        else:
                            stderr_truncated = True
                        stderr_pending = await consume_chunk(
                            data, stderr_pending, stderr_lines, "error",
                            capture=not stderr_truncated,
                        )
                    made_progress = True
                    if _over_deadline():
                        raise TimeoutError(
                            f"SSH command timed out after {timeout} seconds"
                        )
                    if _cancel_requested():
                        raise RuntimeError("SSH command cancelled by caller")

                now = loop.time()
                if channel.exit_status_ready():
                    if exit_seen_at is None:
                        exit_seen_at = now
                    if (
                        not channel.recv_ready()
                        and not channel.recv_stderr_ready()
                        and now - exit_seen_at >= _EXIT_DRAIN_GRACE_SECONDS
                    ):
                        break
                    if now - exit_seen_at >= self._exit_tail_drain_seconds:
                        # exit 之后仍有数据到达（远端派生进程持有 channel）：
                        # 尾部 drain 到上限为止，避免执行器被无限拖住。
                        logger.warning(
                            "[SSH] stream tail drain exceeded %.1fs after "
                            "exit; closing channel anyway",
                            self._exit_tail_drain_seconds,
                        )
                        break
                else:
                    exit_seen_at = None

                if _over_deadline():
                    raise TimeoutError(
                        f"SSH command timed out after {timeout} seconds"
                    )
                if _cancel_requested():
                    raise RuntimeError("SSH command cancelled by caller")
                if not made_progress:
                    await asyncio.sleep(_POLL_INTERVAL)

            if stdout_pending.strip():
                line = stdout_pending.decode("utf-8", errors="replace")
                stdout_lines.append(line)
                await log_callback(line.strip(), "info")
            if stderr_pending.strip():
                line = stderr_pending.decode("utf-8", errors="replace")
                stderr_lines.append(line)
                await log_callback(line.strip(), "error")

            stdout_text = "\n".join(stdout_lines)
            stderr_text = "\n".join(stderr_lines)
            if stdout_truncated:
                stdout_text += _TRUNCATION_MARKER
            if stderr_truncated:
                stderr_text += _TRUNCATION_MARKER

            # drain 完成后再取退出码，并移入线程避免阻塞事件循环。
            exit_code = await asyncio.to_thread(channel.recv_exit_status)
            logger.info(f"[SSH] Command completed with exit code: {exit_code}")
            return CommandResult(
                stdout=stdout_text, stderr=stderr_text, code=exit_code,
            )

        except Exception as e:
            logger.error(f"[SSH] Error executing command: {e}")
            with suppress(Exception):
                await log_callback(f"SSH 执行错误: {e!s}", "error")
            return CommandResult(stdout="", stderr=str(e), code=-1)
        finally:
            # R11：无论成功、超时、取消还是异常，都释放本地 channel。
            # 注意：这只是关闭 SSH channel（向远端送 EOF），不能保证
            # 脱离会话的远端进程退出；需要强终止的长任务须使用进程组
            # 级别的远端取消协议。
            if channel is not None:
                with suppress(Exception):
                    await asyncio.to_thread(channel.close)


# 全局执行器实例（无状态，可在同步与异步上下文共用）
ssh_executor = SSHExecutor()
