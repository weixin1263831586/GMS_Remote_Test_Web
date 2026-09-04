"""Canonical command result type shared by sync/async/local executors.

4.txt 第二阶段（第 10/12 节）要求彻底停止使用裸 tuple 传递 SSH 执行结果——
``(stdout, stderr, exit_code)`` 的位置错用曾造成真实功能 bug（远程套件
下载把 stderr 当退出码比较）。所有执行器统一返回本 dataclass，此类 bug
从类型层消失。

``worker_agent.fastboot_workflow.CommandResult`` 历史上先在这里出现语义
（字段 stdout/stderr/code + output 合并属性），为保持既有 import 路径
兼容，worker_agent 从本模块 re-export。
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
