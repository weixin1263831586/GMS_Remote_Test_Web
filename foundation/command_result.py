"""Canonical result shared by local and remote command executors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    """Unified result of one command execution (local or SSH)."""

    stdout: str = ""
    stderr: str = ""
    code: int = 0
    timed_out: bool = False

    @property
    def output(self) -> str:
        """stdout/stderr 合并文本（非空段按序拼接），用于日志与错误详情。"""
        return "\n".join(
            value.strip() for value in (self.stdout, self.stderr) if value.strip()
        )

    @property
    def ok(self) -> bool:
        return self.code == 0
