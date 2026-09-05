"""Canonical command result type shared by sync/async/local executors.

``(stdout, stderr, exit_code)`` 裸 tuple 的位置错用曾造成真实功能 bug
（远程套件下载把 stderr 当退出码比较）。所有执行器统一返回本 dataclass，
此类 bug 从类型层消失。

``worker_agent.fastboot_workflow.CommandResult`` 与本模块字段语义一致，
worker_agent 从本模块 re-export 以维持既有 import 路径。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    """Unified result of one command execution (local or SSH)."""

    stdout: str = ""
    stderr: str = ""
    code: int = 0

    @property
    def output(self) -> str:
        """stdout/stderr 合并文本（非空段按序拼接），用于日志与错误详情。"""
        return "\n".join(
            value.strip() for value in (self.stdout, self.stderr) if value.strip()
        )

    @property
    def ok(self) -> bool:
        return self.code == 0
