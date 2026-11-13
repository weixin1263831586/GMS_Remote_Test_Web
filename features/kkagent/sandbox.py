"""Per-call kkagent policy: model reasoning only, project tools run in Controller.

引擎侧沙箱（ADR 0034）：受限运行时（空临时工作区 + 私有 config.toml 副本 +
deny-all 工具策略）与有界流读取。assistant 桥接与 redmine 分析复用同一实现，
不再各自维护。"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager

from features.kkagent.config import model_config
from foundation.processes import run_local_command


_workspace: tempfile.TemporaryDirectory | None = None


def _empty_workspace() -> str:
    # Keep the workspace stable across --resume calls. No user files, credentials
    # or project configuration are placed here; session IDs remain owner-bound
    # in the Controller session registry.
    global _workspace
    if _workspace is None:
        _workspace = tempfile.TemporaryDirectory(prefix="gms-agent-workspace-")
    return _workspace.name


@asynccontextmanager
async def restricted_runtime(binary: str, env: dict[str, str]):
    with model_config("", env) as (config, config_env):
        env.update(config_env)
        directory = str(config.parent)
        # Use the installed CLI's own schema writer; never modify host config.
        settings = {
            "default_permission_mode": '"manual"',
            "permission.rules": '[{decision="deny", pattern="*", scope="user"}]',
            "mcp_servers": "{}", "hooks": "[]",
        }
        for key, value in settings.items():
            result = await asyncio.to_thread(
                run_local_command,
                [binary, "--config", str(config), "config", "set", key, value],
                timeout=10, env=env, cwd=directory,
            )
            if not result.ok:
                raise RuntimeError("kkagent 无法应用隔离工具策略，已停止模型调用")
        yield config, _empty_workspace()


async def read_bounded(
    stream: asyncio.StreamReader, limit: int = 2 * 1024**2,
    *, on_line: Callable[[str], None] | None = None,
) -> bytes:
    parts, size = [], 0
    pending = b""
    while chunk := await stream.read(65536):
        size += len(chunk)
        if size > limit:
            raise RuntimeError("kkagent 输出超过大小限制")
        parts.append(chunk)
        if on_line:
            pending += chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                on_line(line.decode("utf-8", errors="replace"))
    if on_line and pending:
        on_line(pending.decode("utf-8", errors="replace"))
    return b"".join(parts)
