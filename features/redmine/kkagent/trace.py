"""kkagent stream-json 执行轨迹：session / tool_call / usage 采集。

kkagent ``--output-format stream-json`` 的 NDJSON 事件流是晨报证据链的
事实来源：session_id、真实 MCP 调用（tool_call/tool_result）、token
usage、LLM 重试都在这里。运行时据此计算 Evidence Gate，不再信任模型
自报的 history_checked。

隐私约束：tool_result 的完整输出（Redmine journal/附件可能含客户数据）
只保留 sha256 + 字节数 + 短 preview，完整内容留在 kkagent session 里。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# 单条 preview / 输入回显的入库上限。
TOOL_OUTPUT_PREVIEW_CHARS = 200
TOOL_INPUT_JSON_CHARS = 500

_KNOWN_EVENT_TYPES = frozenset({
    "system", "session", "turn_start", "message", "tool_call", "tool_result",
    "usage", "llm_retry", "approval_requested", "question_asked", "turn_end",
    "error", "result",
})


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class ToolTrace:
    """一次工具调用的最小证据记录（不含完整输出）。"""

    tool_call_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    output_sha256: str = ""
    output_bytes: int = 0
    output_preview: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "is_error": self.is_error,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "output_preview": self.output_preview,
        }


@dataclass
class KkAgentTrace:
    """一次 kkagent 调用（attempt）的完整运行时轨迹。"""

    session_id: str = ""
    kkagent_version: str = ""
    subtype: str = ""
    exit_code: int | None = None
    resumed: bool = False
    duration_ms: int = 0
    rounds: int = 0
    turns: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    llm_retries: int = 0
    tool_calls: list[ToolTrace] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # 事件流里的最终 result 信封（dict），供输出解析器消费。
    final_event: dict[str, Any] | None = None

    # 状态标注（analyzer 在收尾时填写）：成功为 "completed"，否则为
    # 对应的 error_type（timeout/llm_timeout/...）。
    status: str = ""
    error_type: str = ""
    error: str = ""

    # ------------------------------------------------------------- 观测值

    @property
    def history_search_count(self) -> int:
        return sum(
            1
            for call in self.tool_calls
            if not call.is_error and "history_search" in call.tool_name
        )

    def successful_tool_names(self) -> list[str]:
        return [call.tool_name for call in self.tool_calls if not call.is_error]

    def to_summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "kkagent_version": self.kkagent_version,
            "subtype": self.subtype,
            "exit_code": self.exit_code,
            "resumed": self.resumed,
            "duration_ms": self.duration_ms,
            "rounds": self.rounds,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "llm_retries": self.llm_retries,
            "tool_call_count": len(self.tool_calls),
            "history_search_count": self.history_search_count,
            "tools": [call.to_summary() for call in self.tool_calls],
            "errors": self.errors[:10],
            "status": self.status,
            "error_type": self.error_type,
            "error": self.error[:1000],
        }


def _record_tool_result(trace: KkAgentTrace, event: dict[str, Any]) -> None:
    tool_call_id = str(event.get("tool_call_id") or "")
    output = event.get("output")
    output_text = output if isinstance(output, str) else json.dumps(
        output, ensure_ascii=False
    ) if output is not None else ""
    encoded = output_text.encode("utf-8", errors="replace")
    target: ToolTrace | None = None
    for call in reversed(trace.tool_calls):
        if call.tool_call_id and call.tool_call_id == tool_call_id:
            target = call
            break
    if target is None:
        # 乱序/缺失 tool_call 的容错：仍要记录这次调用发生过。
        target = ToolTrace(
            tool_call_id=tool_call_id,
            tool_name=str(event.get("tool_name") or ""),
        )
        trace.tool_calls.append(target)
    target.is_error = bool(event.get("is_error"))
    target.output_sha256 = hashlib.sha256(encoded).hexdigest()
    target.output_bytes = len(encoded)
    target.output_preview = output_text[:TOOL_OUTPUT_PREVIEW_CHARS]


def _apply_usage(trace: KkAgentTrace, usage: Any, *, override: bool) -> None:
    if not isinstance(usage, dict):
        return
    if override:
        trace.input_tokens = _as_int(usage.get("input_tokens"))
        trace.output_tokens = _as_int(usage.get("output_tokens"))
        trace.cache_read_tokens = _as_int(usage.get("cache_read_input_tokens"))
        trace.cache_creation_tokens = _as_int(usage.get("cache_creation_input_tokens"))
        return
    trace.input_tokens += _as_int(usage.get("input_tokens"))
    trace.output_tokens += _as_int(usage.get("output_tokens"))
    trace.cache_read_tokens += _as_int(usage.get("cache_read_input_tokens"))
    trace.cache_creation_tokens += _as_int(usage.get("cache_creation_input_tokens"))


def consume_event(trace: KkAgentTrace, event: dict[str, Any]) -> None:
    """把一条已解析的 stream-json 事件并入轨迹。"""
    event_type = str(event.get("type") or "")
    if event_type == "system":
        trace.kkagent_version = str(event.get("version") or trace.kkagent_version)
    elif event_type == "session":
        trace.session_id = str(event.get("session_id") or trace.session_id)
    elif event_type == "tool_call":
        trace.tool_calls.append(ToolTrace(
            tool_call_id=str(event.get("tool_call_id") or ""),
            tool_name=str(event.get("tool_name") or ""),
            tool_input=event.get("input") if isinstance(event.get("input"), dict) else {},
        ))
    elif event_type == "tool_result":
        _record_tool_result(trace, event)
    elif event_type == "usage":
        _apply_usage(trace, event.get("usage") or event, override=False)
    elif event_type == "llm_retry":
        trace.llm_retries += 1
    elif event_type == "error":
        message = str(event.get("message") or event)
        if len(trace.errors) < 20:
            trace.errors.append(message[:500])
    elif event_type == "result":
        trace.final_event = event
        if event.get("session_id"):
            trace.session_id = str(event["session_id"])
        trace.subtype = str(event.get("subtype") or "")
        if "exit_code" in event:
            trace.exit_code = _as_int(event.get("exit_code"))
        trace.duration_ms = _as_int(event.get("duration_ms")) or trace.duration_ms
        trace.rounds = _as_int(event.get("rounds")) or trace.rounds
        trace.turns = _as_int(event.get("turns")) or trace.turns
        trace.resumed = bool(event.get("resumed", trace.resumed))
        if event.get("usage") is not None:
            # 最终 result 的 usage 是权威累计值，覆盖事件流求和。
            _apply_usage(trace, event.get("usage"), override=True)


def consume_line(trace: KkAgentTrace, raw_line: str | bytes) -> bool:
    """消费一行 NDJSON；返回是否为可解析 JSON。

    非法行（二进制杂讯、截断行）返回 False 并留给调用方进入 raw 兜底
    解析路径——不做猜测修复。
    """
    text = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
    text = text.strip()
    if not text:
        return True
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict):
        return False
    consume_event(trace, event)
    return True


__all__ = [
    "TOOL_INPUT_JSON_CHARS",
    "TOOL_OUTPUT_PREVIEW_CHARS",
    "KkAgentTrace",
    "ToolTrace",
    "consume_event",
    "consume_line",
]
