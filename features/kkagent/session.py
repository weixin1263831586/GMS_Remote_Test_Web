"""kkagent headless 回合协议：spawn + stream-json + 终态提取。

从 assistant/kkagent_bridge.run_turn 的进程协议核下沉（ADR 0034）：
环境隔离（child_env）、沙箱配置（restricted_runtime）、流式有界读取、
超时收敛、进程树回收与信号退出分类都在这里；调用方（assistant 桥接）
只保留 prompt 组装、可用性探测与用户文案。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from features.kkagent.process import terminate_process_tree
from features.kkagent.sandbox import read_bounded, restricted_runtime


logger = logging.getLogger(__name__)

KKAGENT_BINARY = "kkagent"
TURN_TIMEOUT_SECONDS = 120
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

# 错误码与 bridge 历史语义一致：unavailable（二进制缺失）、timeout、
# signalled（负退出码）、process_error（正退出码非零）、internal。
_ERROR_UNAVAILABLE = "unavailable"
_ERROR_TIMEOUT = "timeout"
_ERROR_SIGNALLED = "signalled"
_ERROR_PROCESS = "process_error"
_ERROR_INTERNAL = "internal"


@dataclass
class HeadlessTurnResult:
    """一次 headless 回合的协议结果。

    ``error_code`` 为空表示成功（此时 ``text`` 非空）；失败时调用方按
    ``error_code`` + ``returncode`` 生成用户文案。
    """

    ok: bool
    text: str = ""
    session_id: str = ""
    tools_used: list[str] = field(default_factory=list)
    returncode: int | None = None
    error_code: str = ""
    raw_lines: list[str] = field(default_factory=list)


def extract_turn_events(raw_lines: list[str]) -> tuple[str, str, list[str]]:
    """从 stream-json 行里提取**最终**助手文本、会话 ID 与工具名列表。

    一个回合可能产生多段助手文本（工具调用间的中间叙述）；调用方把整段
    文本当单个 JSON 决策解析，因此这里只保留最后一段文本——中间段落
    拼接会产生非法 JSON，让解析永远失败。
    """
    last_text = ""
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
            if isinstance(content, str) and content:
                last_text = content
            elif isinstance(content, list):
                blocks = [str(block["text"]) for block in content
                          if isinstance(block, dict) and block.get("type") == "text" and block.get("text")]
                if blocks:
                    last_text = blocks[-1]
        if event.get("type") == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    last_text = str(block["text"])
                elif block.get("type") == "tool_use":
                    tools.append(str(block.get("name") or ""))
        if event.get("type") == "result":
            session_id = str(event.get("session_id") or session_id)
            result_text = str(event.get("result") or event.get("message") or "")
            if result_text and not last_text:
                last_text = result_text
    return last_text.strip(), session_id, tools


async def run_headless_turn(
    prompt: str,
    *,
    env: dict[str, str],
    session_id: str = "",
    max_turns: int = 2,
    timeout_seconds: int = TURN_TIMEOUT_SECONDS,
    on_line: Callable[[str], None] | None = None,
) -> HeadlessTurnResult:
    """执行一次 headless kkagent 回合（plan 模式、有界超时、进程组回收）。

    ``env`` 应为 :func:`features.kkagent.process.child_env` 的产物；本
    函数不做业务判断，只负责进程协议与事件提取。
    """
    resolved_session = session_id

    def remember_session(line: str) -> None:
        nonlocal resolved_session
        event = None
        try:
            event = json.loads(line)
        except ValueError:
            return
        candidate = event.get("session_id") if isinstance(event, dict) else None
        if isinstance(candidate, str) and _SESSION_ID_RE.match(candidate):
            resolved_session = candidate

    process: asyncio.subprocess.Process | None = None
    def line_callback(line: str) -> None:
        remember_session(line)
        if on_line is not None:
            on_line(line)

    async def invoke() -> bytes:
        nonlocal process
        async with restricted_runtime(KKAGENT_BINARY, env) as (config, directory):
            command = [KKAGENT_BINARY, "--config", str(config), "--plan", "-p", prompt,
                       "--output-format", "stream-json", "--max-turns", str(max_turns)]
            if session_id:
                command += ["--resume", session_id]
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=directory, start_new_session=True,
            )
            readers = [
                asyncio.create_task(read_bounded(process.stdout, on_line=line_callback)),
                asyncio.create_task(read_bounded(process.stderr)),
                asyncio.create_task(process.wait()),
            ]
            try:
                stdout, _stderr, _ = await asyncio.gather(*readers)
                return stdout
            finally:
                # Keep the private config alive until the process tree is gone.
                if process.returncode is None:
                    await terminate_process_tree(process)
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

    try:
        stdout = await asyncio.wait_for(invoke(), timeout=timeout_seconds)
    except FileNotFoundError:
        return HeadlessTurnResult(ok=False, session_id=resolved_session,
                                  error_code=_ERROR_UNAVAILABLE)
    except asyncio.TimeoutError:
        return HeadlessTurnResult(ok=False, session_id=resolved_session,
                                  error_code=_ERROR_TIMEOUT)
    except Exception:
        logger.debug("kkagent headless turn failed", exc_info=True)
        return HeadlessTurnResult(ok=False, session_id=resolved_session,
                                  error_code=_ERROR_INTERNAL)
    returncode = process.returncode
    if returncode != 0:
        # 负退出码 = 被信号终止（如 systemd stop 向 cgroup 广播 SIGINT），
        # 不是模型失败；区分错误码供调用方给出可行动文案。
        return HeadlessTurnResult(
            ok=False, session_id=resolved_session, returncode=returncode,
            error_code=_ERROR_SIGNALLED if returncode is not None and returncode < 0 else _ERROR_PROCESS,
        )

    raw_lines = stdout.decode("utf-8", errors="replace").splitlines()
    text, extracted_session, tools = extract_turn_events(raw_lines)
    if not text:
        return HeadlessTurnResult(ok=False, session_id=extracted_session or resolved_session,
                                  returncode=returncode, raw_lines=raw_lines,
                                  error_code="empty_response", tools_used=tools)
    return HeadlessTurnResult(
        ok=True, text=text, session_id=extracted_session or resolved_session,
        tools_used=tools, returncode=returncode, raw_lines=raw_lines,
    )


__all__ = [
    "KKAGENT_BINARY",
    "TURN_TIMEOUT_SECONDS",
    "HeadlessTurnResult",
    "extract_turn_events",
    "run_headless_turn",
]
