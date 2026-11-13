"""kkagent stream-json 执行轨迹：session / tool_call / usage 采集（通用核）。

kkagent ``--output-format stream-json`` 的 NDJSON 事件流是取证链的事实
来源：session_id、真实 MCP 调用（tool_call/tool_result）、token usage、
LLM 重试都在这里。本模块只承载**通用**消费逻辑；领域扩展（Redmine 的
证据归属、artifact 覆盖、源码取证语义）通过两个多态钩子接入：

- :meth:`KkAgentStreamTrace.new_tool_call` —— 创建 tool_call 记录的
  工厂，子类返回带领域字段的记录；
- :meth:`KkAgentStreamTrace.record_tool_result` —— tool_result 并入
  记录后的领域富化钩子（引擎部分只做状态/哈希/预览）。

隐私约束：tool_result 的完整输出只保留 sha256 + 字节数 + 短 preview，
完整内容留在 kkagent session 里。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal


TOOL_OUTPUT_PREVIEW_CHARS = 200
TOOL_INPUT_JSON_CHARS = 500

# 截断 tool input 时必须保留的身份字段（跨领域审计键：issue/artifact/
# snapshot/path/query 等；归属核对只需要可辨识前缀）。
_IDENTITY_INPUT_KEYS = (
    "issue_id", "issue", "snapshot_id", "artifact_id",
    "source", "revision", "path", "query", "q", "mode",
)


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
    status: Literal["pending", "succeeded", "failed"] = "pending"
    output_sha256: str = ""
    output_bytes: int = 0
    output_preview: str = ""
    # Controller-owned preflight may classify a failure without exposing raw
    # diagnostics. Empty preserves the historical "unknown" tool result.
    failure_kind: str = ""

    @property
    def is_error(self) -> bool:
        """兼容旧的 trace 消费方；pending 既不是成功也不是失败。"""
        return self.status == "failed"

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_summary(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "status": self.status,
            "is_error": self.is_error,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "output_preview": self.output_preview,
            "failure_kind": self.failure_kind,
        }


@dataclass
class KkAgentStreamTrace:
    """一次 kkagent 调用（attempt）的通用运行时轨迹。

    领域子类（如 Redmine 的证据轨迹）在继承本类的基础上追加字段与
    ``history_search_*`` 等领域属性；``new_tool_call`` /
    ``record_tool_result`` 两个钩子保证事件消费保持多态。
    """

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
    last_round_output_tokens: int = 0
    last_round_visible_output: bool | None = None
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    llm_retries: int = 0
    repair_attempts: int = 0
    tool_calls: list[ToolTrace] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # 事件流里的最终 result 信封（dict），供输出解析器消费。
    final_event: dict[str, Any] | None = None

    # 状态标注（调用方在收尾时填写）：成功为 "completed"，否则为对应的
    # error_type（timeout/llm_timeout/...）。
    status: str = ""
    error_type: str = ""
    error: str = ""

    # ------------------------------------------------------- 多态工厂/钩子

    def new_tool_call(self, tool_call_id: str, tool_name: str, tool_input: dict[str, Any]) -> ToolTrace:
        """tool_call 事件工厂；子类返回带领域字段的记录。"""
        return ToolTrace(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    def record_tool_result(self, call: ToolTrace, event: dict[str, Any]) -> None:
        """tool_result 通用记录后的领域富化钩子（引擎默认无操作）。"""
        return

    # ------------------------------------------------------------- 通用摘要

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
            "last_round_output_tokens": self.last_round_output_tokens,
            "last_round_visible_output": self.last_round_visible_output,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "llm_retries": self.llm_retries,
            "repair_attempts": self.repair_attempts,
            "tool_call_count": len(self.tool_calls),
            "tools": [call.to_summary() for call in self.tool_calls],
            "errors": self.errors[:10],
            "status": self.status,
            "error_type": self.error_type,
            "error": self.error[:1000],
        }


def _bounded_tool_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= TOOL_INPUT_JSON_CHARS:
        return value
    # 身份字段永远保留（供证据归属核对），其余内容截断为原始 JSON 前缀。
    # 身份键的**值**也要截断：path/query 等值可被超大参数击穿总量上限，
    # 审计表随之膨胀；归属核对只需要可辨识前缀。
    kept = {
        key: (
            value[key]
            if isinstance(value[key], (int, float)) and not isinstance(value[key], bool)
            else str(value[key])[:TOOL_INPUT_JSON_CHARS]
        )
        for key in _IDENTITY_INPUT_KEYS
        if key in value
    }
    kept["_truncated_json"] = encoded[:TOOL_INPUT_JSON_CHARS]
    kept["_truncated"] = True
    return kept


def _apply_usage(trace: KkAgentStreamTrace, usage: Any, *, override: bool) -> None:
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


def _record_tool_result(trace: KkAgentStreamTrace, event: dict[str, Any]) -> None:
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
        target = trace.new_tool_call(
            tool_call_id=tool_call_id,
            tool_name=str(event.get("tool_name") or ""),
            tool_input={},
        )
        trace.tool_calls.append(target)
    target.status = "failed" if bool(event.get("is_error")) else "succeeded"
    target.output_sha256 = hashlib.sha256(encoded).hexdigest()
    target.output_bytes = len(encoded)
    target.output_preview = output_text[:TOOL_OUTPUT_PREVIEW_CHARS]
    trace.record_tool_result(target, event)


def consume_event(trace: KkAgentStreamTrace, event: dict[str, Any]) -> None:
    """把一条已解析的 stream-json 事件并入轨迹。"""
    event_type = str(event.get("type") or "")
    if event_type == "system":
        trace.kkagent_version = str(event.get("version") or trace.kkagent_version)
    elif event_type == "session":
        trace.session_id = str(event.get("session_id") or trace.session_id)
    elif event_type == "turn_start":
        trace.last_round_output_tokens = 0
        trace.last_round_visible_output = False
    elif event_type == "message":
        content = event.get("content")
        if event.get("role") == "assistant" and isinstance(content, str) and content.strip():
            trace.last_round_visible_output = True
    elif event_type == "tool_call":
        trace.tool_calls.append(trace.new_tool_call(
            tool_call_id=str(event.get("tool_call_id") or ""),
            tool_name=str(event.get("tool_name") or ""),
            tool_input=_bounded_tool_input(event.get("input")),
        ))
    elif event_type == "tool_result":
        _record_tool_result(trace, event)
    elif event_type == "usage":
        usage = event.get("usage") or event
        if isinstance(usage, dict):
            trace.last_round_output_tokens = _as_int(usage.get("output_tokens"))
        _apply_usage(trace, usage, override=False)
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


def consume_line(trace: KkAgentStreamTrace, raw_line: str | bytes, *, on_event: Any = None) -> bool:
    """消费一行 NDJSON；返回是否为可解析 JSON。

    非法行（二进制杂讯、截断行）返回 False 并留给调用方进入 raw 兜底
    解析路径——不做猜测修复。

    ``on_event``：可选的实时事件分流回调（分析进度时间线的接入点）。
    每条可解析事件在并入轨迹后原样回调；回调自身异常只吞掉并继续——
    进度观测永远不得影响取证轨迹的完整性。
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
    if on_event is not None:
        try:
            on_event(event)
        except Exception:
            pass
    return True


__all__ = [
    "TOOL_INPUT_JSON_CHARS",
    "TOOL_OUTPUT_PREVIEW_CHARS",
    "KkAgentStreamTrace",
    "ToolTrace",
    "consume_event",
    "consume_line",
]
