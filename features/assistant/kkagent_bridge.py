"""kkagent 会话桥接层：对话 Agent ↔ kkagent headless 会话。

定位：kkagent 负责"理解、规划、诊断"——多轮自然语言澄清与只读取证；
实际执行始终由 Controller 工具层完成，设备工作流走 automation/cluster。这里复用
Redmine 模块已验证的 headless stream-json 子进程方式（不引入 ACP 依赖，
后续可平滑迁移）：

- ``kkagent -p <prompt> --output-format stream-json [--resume <sid>]``；
- 无 shell、无自动授权标志、继承宿主最小环境（剥离身份类 env）；
- 桥接会话 ID 与 WorkflowDraft / Automation Run 的关联通过
  ``link_payload`` 挂在 Agent 会话的 ``kkagent_bridge`` 字段上。

桥接失败返回明确错误类型和已接收到的会话 ID，供调用方有界重试。
运行时失败不得重放业务操作或改用另一套路由执行用户指令。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Any

from features.assistant.kkagent_runtime import read_bounded, restricted_runtime


logger = logging.getLogger(__name__)

KKAGENT_BINARY = "kkagent"
TURN_TIMEOUT_SECONDS = 120
STREAM_LINE_LIMIT_BYTES = 4 * 1024 * 1024

# 与 features/redmine/kkagent/process.py 同一原则（该模块属 redmine 内部，
# 此处按文档键位独立声明）：子进程不得继承宿主的 Agent 身份/凭据环境，
# 否则 MCP 会以错误 owner 取证。
_BRIDGE_STRIPPED_ENV_KEYS = frozenset({
    "GMS_RT_PROFILE",
    "GMS_AGENT_PROFILE",
    "GMS_AGENT_CLIENT",
    "GMS_REMOTE_TEST_SERVER",
    "GMS_AUTH_TOKEN_FILE",
    "GMS_AGENT_AUTH_MODE",
    "GMS_MCP_TOOLSETS",
})

_BRIDGE_PROMPT_PREFIX = (
    "你是 GMS 远程测试平台的规划助手。只做意图澄清、规划建议与只读诊断，"
    "不得直接执行烧写、AOA 或测试启动；实际执行由平台状态机完成。"
    "回答保持简洁中文。\n\n"
)


@dataclass
class BridgeTurnResult:
    """一次桥接回合的结果（文本 + 轨迹摘要）。"""

    ok: bool
    text: str = ""
    session_id: str = ""
    error: str = ""
    tools_used: list[str] = field(default_factory=list)
    error_code: str = ""


def availability() -> dict[str, Any]:
    """kkagent 是否可用于桥接（存在性探测，不启动进程）。"""
    path = shutil.which(KKAGENT_BINARY)
    return {
        "available": bool(path),
        "binary": path or "",
        "mode": "headless_stream_json",
    }


def child_env() -> dict[str, str]:
    """宿主环境减去身份类变量，供子进程显式注入。"""
    import os

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _BRIDGE_STRIPPED_ENV_KEYS
    }


def link_payload(
    *,
    kkagent_session_id: str = "",
    draft_id: str = "",
    automation_run_id: str = "",
) -> dict[str, str]:
    """三向关联记录：挂到 Agent 会话 ``kkagent_bridge`` 字段。"""
    return {
        "kkagent_session_id": kkagent_session_id,
        "draft_id": draft_id,
        "automation_run_id": automation_run_id,
    }


def _extract_events(raw_lines: list[str]) -> tuple[str, str, list[str]]:
    """从 stream-json 行里提取最终助手文本、会话 ID 与工具名列表。"""
    texts: list[str] = []
    session_id = ""
    tools: list[str] = []
    for line in raw_lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("session_id"):
            session_id = str(event["session_id"])
        if event.get("type") == "system" and event.get("subtype") in {
            "init", "session_started",
        }:
            session_id = str(event.get("session_id") or session_id)
        if event.get("type") == "message" and event.get("role") == "assistant":
            content = event.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                texts.extend(str(block["text"]) for block in content
                             if isinstance(block, dict) and block.get("type") == "text" and block.get("text"))
        if event.get("type") == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    tools.append(str(block.get("name") or ""))
        if event.get("type") == "result":
            session_id = str(event.get("session_id") or session_id)
            result_text = str(event.get("result") or event.get("message") or "")
            if result_text and not texts:
                texts.append(result_text)
    return "\n".join(texts).strip(), session_id, tools


async def _terminate_bridge_process(process: asyncio.subprocess.Process) -> None:
    """TERM→grace→KILL 回收超时/残留的桥接子进程（独立进程组）。"""
    import os
    import signal

    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - Windows fallback
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover
        process.kill()
    try:
        await process.wait()
    except Exception:  # pragma: no cover - reap best-effort
        pass


async def run_turn(
    message: str,
    *,
    session_id: str = "",
    context: str = "",
    timeout_seconds: int = TURN_TIMEOUT_SECONDS,
) -> BridgeTurnResult:
    """执行一次 kkagent 桥接回合（headless、只读、有界超时）。"""
    if not availability()["available"]:
        return BridgeTurnResult(ok=False, error="kkagent 未安装，桥接不可用", error_code="unavailable")
    prompt = _BRIDGE_PROMPT_PREFIX
    if context:
        bounded_context = context.encode("utf-8")[:90000].decode("utf-8", errors="ignore")
        prompt += f"上下文：{bounded_context}\n\n"
    prompt += message[:4000]

    raw_lines: list[str] = []
    process: asyncio.subprocess.Process | None = None
    resolved_session = session_id

    def remember_session(line: str) -> None:
        nonlocal resolved_session
        try:
            event = json.loads(line)
        except ValueError:
            return
        candidate = event.get("session_id") if isinstance(event, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate):
            resolved_session = candidate

    async def invoke() -> bytes:
        nonlocal process
        env = child_env()
        async with restricted_runtime(KKAGENT_BINARY, env) as (config, directory):
            command = [KKAGENT_BINARY, "--config", str(config), "--plan", "-p", prompt,
                       "--output-format", "stream-json", "--max-turns", "2"]
            if session_id:
                command += ["--resume", session_id]
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=directory, start_new_session=True,
            )
            stdout, _stderr, _ = await asyncio.gather(
                read_bounded(process.stdout, on_line=remember_session), read_bounded(process.stderr), process.wait(),
            )
            return stdout

    try:
        stdout = await asyncio.wait_for(invoke(), timeout=timeout_seconds)
        if process.returncode != 0:
            return BridgeTurnResult(
                ok=False, session_id=resolved_session,
                error=f"kkagent 退出码 {process.returncode}", error_code="process_error",
            )
        raw_lines = stdout.decode("utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return BridgeTurnResult(ok=False, session_id=resolved_session, error="kkagent 未安装，桥接不可用", error_code="unavailable")
    except asyncio.TimeoutError:
        return BridgeTurnResult(ok=False, session_id=resolved_session, error="kkagent 回合超时", error_code="timeout")
    except Exception:
        logger.debug("kkagent bridge turn failed", exc_info=True)
        return BridgeTurnResult(ok=False, session_id=resolved_session,
                                error="kkagent 调用失败，请检查模型连接或隔离策略", error_code="bridge_error")
    finally:
        if process is not None and process.returncode is None:
            await _terminate_bridge_process(process)

    text, resolved_session, tools = _extract_events(raw_lines)
    if not text:
        return BridgeTurnResult(
            ok=False, session_id=resolved_session or session_id,
            error="kkagent 未返回文本结果", tools_used=tools, error_code="empty_response",
        )
    return BridgeTurnResult(
        ok=True, text=text, session_id=resolved_session or session_id, tools_used=tools,
    )
