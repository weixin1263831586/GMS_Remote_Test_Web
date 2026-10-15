"""Private per-analysis kkagent config overlays for provider-declared limits."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import tomli_w


try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from .trace import KkAgentTrace, ToolTrace


class ContextBudgetError(Exception):
    """Safe diagnostic with no config contents or provider credentials."""


@contextmanager
def context_limited_config(model: str, limit: int, env_extra: dict[str, str]):
    """Preserve identity/security settings; never change the installed config."""
    source = Path(env_extra.get("HOME") or Path.home()) / ".kkagent/config.toml"
    try:
        config = tomllib.loads(source.read_text(encoding="utf-8"))
        alias = model or env_extra.get("KKAGENT_DEFAULT_MODEL") or os.environ.get(
            "KKAGENT_DEFAULT_MODEL"
        ) or config.get("default_model")
        selected = config.get("models", {}).get(alias)
        if not isinstance(selected, dict) or limit < 2048:
            raise ContextBudgetError("当前模型配置缺失，或服务端上下文不足以执行分析。")
        configured = selected.get("max_context_size") or limit
        ceiling = min(int(configured), limit)
        selected["max_context_size"] = ceiling
        # An input overflow does not establish an output-token ceiling. In
        # reasoning models a small output cap can be consumed before any
        # visible answer; preserve the installed output policy unchanged.
        control = config.setdefault("loop_control", {})
        control.update({
            "auto_compact": True,
            "reserved_context_size": min(4096, ceiling // 4),
            "compact_keep_last": 2,
            "compact_trigger_ratio": 0.70,
            "compact_block_ratio": 0.80,
            "token_counting": "measured+estimated",
        })
        body = tomli_w.dumps(config)
    except ContextBudgetError:
        raise
    except (OSError, ValueError, TypeError):
        raise ContextBudgetError("无法读取当前 kkagent 配置以应用服务端上下文上限。") from None
    # The overlay may contain existing provider credentials. Both directory
    # and file are private and removed after the subprocess tree is reaped.
    with TemporaryDirectory(prefix="gms-kkagent-context-") as directory:
        path = Path(directory) / "config.toml"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(body)
        yield path


def analysis_command(command: list[str], entry: dict[str, Any]) -> list[str]:
    path = entry.get("_kkagent_config_path")
    return [command[0], "--config", str(path), *command[1:]] if path else command


def trace_from_summary(summary: dict[str, Any]) -> KkAgentTrace:
    """Restore prior observed tools when resuming after a context overflow."""
    trace = KkAgentTrace(**{
        key: value for key, value in summary.items()
        if key in KkAgentTrace.__dataclass_fields__ and key != "tool_calls"
    })
    trace.tool_calls = [
        ToolTrace(**{key: value for key, value in item.items()
                     if key in ToolTrace.__dataclass_fields__})
        for item in summary.get("tools", [])
    ]
    return trace
