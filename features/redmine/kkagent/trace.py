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
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal


# 单条 preview / 输入回显的入库上限。
TOOL_OUTPUT_PREVIEW_CHARS = 200
TOOL_INPUT_JSON_CHARS = 500

# 截断 tool input 时必须保留的身份字段。
_IDENTITY_INPUT_KEYS = (
    "issue_id", "issue", "snapshot_id", "artifact_id",
    "source", "revision", "path", "query", "q", "mode",
)

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
    status: Literal["pending", "succeeded", "failed"] = "pending"
    output_sha256: str = ""
    output_bytes: int = 0
    output_preview: str = ""
    # Controller-owned preflight may classify a failure without exposing raw
    # diagnostics. Empty preserves the historical "unknown" tool result.
    failure_kind: str = ""
    evidence_issue_ids: list[int] = field(default_factory=list)
    attachment_manifest_parsed: bool = False
    attachment_count: int = 0
    text_artifact_ids: list[str] = field(default_factory=list)
    # 该调用返回/引用的全部 artifact id（供 artifact→issue 归属推导）。
    all_artifact_ids: list[str] = field(default_factory=list)
    # 该调用返回的 snapshot id（供 snapshot→issue 归属推导）。
    snapshot_ids: list[str] = field(default_factory=list)
    # 源码级取证调用是否可复现（provider 返回的 reproducible 标志；
    # None = 输出里没有该标志，无法判定）。
    source_reproducible: bool | None = None

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
            "evidence_issue_ids": self.evidence_issue_ids,
            "attachment_manifest_parsed": self.attachment_manifest_parsed,
            "attachment_count": self.attachment_count,
            "text_artifact_ids": self.text_artifact_ids,
            "all_artifact_ids": self.all_artifact_ids,
            "snapshot_ids": self.snapshot_ids,
            "source_reproducible": self.source_reproducible,
        }


def _is_source_evidence_tool(tool_name: str) -> bool:
    """是否为源码级取证调用（SDK 源码检索/读取 + 反编译 APK 取证）。"""
    return (
        tool_name.startswith(("gms_rt_sdk_", "gms_rt_apk_"))
        or "_sdk_" in tool_name
        or "_apk_" in tool_name
    )


def _source_reproducible_flag(value: Any) -> bool | None:
    """从源码取证工具结果提取 reproducible 标志。

    CLI/MCP 输出是 ``{"success": true, "data": {..., "reproducible": ...}}``
    形态的信封；遍历嵌套取值。同一输出出现多个标志时，任一 True 即视为
    存在可复现证据；完全没有标志时返回 None（无法判定，按不可复现处理
    由 evidence gate 决定，trace 只忠实记录）。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    found: list[bool] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "reproducible" and isinstance(child, bool):
                    found.append(child)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return any(found) if found else None


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
    repair_attempts: int = 0
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
            if call.succeeded and "history_search" in call.tool_name
        )

    @property
    def distinct_history_queries(self) -> list[str]:
        queries: list[str] = []
        seen: set[str] = set()
        for call in self.tool_calls:
            if not call.succeeded or "history_search" not in call.tool_name:
                continue
            query = _normalize_history_query(
                call.tool_input.get("q", call.tool_input.get("query"))
            )
            if query and query not in seen:
                seen.add(query)
                queries.append(query)
        return queries

    @property
    def distinct_history_search_count(self) -> int:
        return len(self.distinct_history_queries)

    def successful_tool_names(self) -> list[str]:
        return [call.tool_name for call in self.tool_calls if call.succeeded]

    def evidenced_issue_ids(self) -> set[int]:
        return {
            issue_id
            for call in self.tool_calls
            if call.succeeded
            for issue_id in call.evidence_issue_ids
        }

    def listed_text_artifact_ids(self, calls: list[ToolTrace] | None = None) -> set[str]:
        selected = self.tool_calls if calls is None else calls
        return {
            artifact_id
            for call in selected
            if call.succeeded and "redmine_attachments" in call.tool_name
            for artifact_id in call.text_artifact_ids
        }

    def read_artifact_ids(self) -> set[str]:
        """被读取（成功）或确认不可读（失败但原因不可恢复）的 artifact。

        「该 artifact 没有可用文本 / artifact 文件缺失」是基础设施事实，
        重试无济于事；把这类失败也视为已核验，避免证据门禁把模型锁死在
        无法成功的重试循环里（2026-09-24 晨报批次真实发生）。其它失败
        （如 ID 写错）不算读，模型纠正后重读仍然必要。
        """
        unreadable_markers = ("没有可用文本", "artifact 文件缺失")
        checked: set[str] = set()
        for call in self.tool_calls:
            if "artifact_read" not in call.tool_name:
                continue
            artifact_id = str(call.tool_input.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            if call.succeeded or any(
                marker in call.output_preview for marker in unreadable_markers
            ):
                checked.add(artifact_id)
        return checked

    # ---------------------------------------------------- 归属（target-scoped）

    @property
    def source_evidence_tool_count(self) -> int:
        """成功过的源码级取证调用数（SDK 源码检索/读取 + 反编译 APK 取证）。"""
        return sum(
            1
            for call in self.tool_calls
            if call.succeeded and _is_source_evidence_tool(call.tool_name)
        )

    @property
    def reproducible_source_evidence_count(self) -> int:
        """结果里带 ``reproducible: true`` 的成功源码取证调用数。

        OpenGrok/code-search 等动态索引返回 ``reproducible: false``（只
        保证"当前索引里有"，不保证指定 commit）；local git 才返回 true。
        Evidence Gate 据此约束 root_cause_type=confirmed。
        """
        return sum(
            1
            for call in self.tool_calls
            if call.succeeded
            and _is_source_evidence_tool(call.tool_name)
            and call.source_reproducible is True
        )

    def evidence_ledger(self) -> list[dict[str, Any]]:
        """Claim ↔ Evidence Ledger：给每次成功调用分配稳定证据 ID。

        全局布尔计数（``*_checked`` / ``*_count``）只能证明"某次调用发生过"，
        不能证明"结论引用的证据就是这次调用"。Ledger 为每条证据分配
        ``EV-<n>`` 稳定 ID 并携带可匹配的引用 token（issue/artifact/snapshot
        id、路径、查询词），供 evidence_gate 把模型声称的 evidence 条目
        绑定回真实调用——root_cause_type=confirmed 时要求引用的 evidence
        能绑定到一条可复现的源码取证调用，而不是仅"轨迹里存在过一次"。
        """
        ledger: list[dict[str, Any]] = []
        for index, call in enumerate(self.tool_calls, start=1):
            if not call.succeeded:
                continue
            is_source = _is_source_evidence_tool(call.tool_name)
            refs: set[str] = set()
            for key in ("issue", "issue_id"):
                value = _as_int(call.tool_input.get(key))
                if value:
                    refs.update({f"#{value}", str(value)})
            for key in ("artifact_id", "snapshot_id", "path", "query", "file", "q"):
                value = str(call.tool_input.get(key) or "").strip()
                if value:
                    refs.add(value)
            refs.update(str(value) for value in call.all_artifact_ids if value)
            for value in call.evidence_issue_ids:
                if value:
                    refs.update({f"#{value}", str(value)})
            refs.update(str(value) for value in call.snapshot_ids if value)
            ledger.append({
                "evidence_id": f"EV-{index:03d}",
                "kind": "source" if is_source else "tool",
                "tool_name": call.tool_name,
                "reproducible": call.source_reproducible if is_source else None,
                "refs": sorted(refs),
            })
        return ledger

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
            "repair_attempts": self.repair_attempts,
            "tool_call_count": len(self.tool_calls),
            "history_search_count": self.history_search_count,
            "distinct_history_search_count": self.distinct_history_search_count,
            "tools": [call.to_summary() for call in self.tool_calls],
            "errors": self.errors[:10],
            "status": self.status,
            "error_type": self.error_type,
            "error": self.error[:1000],
        }


def _normalize_history_query(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _bounded_tool_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) <= TOOL_INPUT_JSON_CHARS:
        return value
    # 身份字段永远保留（供 Evidence Gate 按目标 issue 归属核对），其余
    # 内容截断为原始 JSON 前缀。身份键的**值**也要截断：
    # path/query 等值可被超大参数击穿总量上限，审计表随之膨胀；归属核对
    # 只需要可辨识前缀。
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


def _structured_issue_ids(value: Any) -> list[int]:
    """只从结构化工具结果提取 issue_id，避免把任意正文数字当证据。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    found: set[int] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "issue_id":
                    try:
                        issue_id = int(child)
                    except (TypeError, ValueError):
                        pass
                    else:
                        if issue_id > 0:
                            found.add(issue_id)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return sorted(found)[:100]


def _attachment_manifest(value: Any) -> tuple[bool, int, list[str], list[str]]:
    """解析附件清单 → (parsed, count, text_ids, all_ids)。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return False, 0, [], []
    if not isinstance(value, dict):
        return False, 0, [], []
    data = value.get("data") if isinstance(value.get("data"), dict) else value
    artifacts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(artifacts, list):
        return False, 0, [], []
    rows = [item for item in artifacts if isinstance(item, dict)]
    all_ids = sorted({
        str(item.get("artifact_id") or "").strip()
        for item in rows
        if str(item.get("artifact_id") or "").strip()
    })
    text_ids = {
        str(item.get("artifact_id") or "").strip()
        for item in rows
        if str(item.get("kind") or "") in {"text", "log"}
        and str(item.get("status") or "") in {"ready", "partial"}
        and str(item.get("artifact_id") or "").strip()
    }
    return True, len(artifacts), sorted(text_ids), all_ids


def _collect_snapshot_ids(value: Any) -> list[str]:
    """从工具输出里提取 snapshot_id 字段（含嵌套/JSON 字符串）。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    found: list[str] = []
    seen: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key == "snapshot_id":
                    sid = str(child or "").strip()
                    if sid and sid not in seen:
                        seen.add(sid)
                        found.append(sid)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found[:10]


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
    target.status = "failed" if bool(event.get("is_error")) else "succeeded"
    target.output_sha256 = hashlib.sha256(encoded).hexdigest()
    target.output_bytes = len(encoded)
    target.output_preview = output_text[:TOOL_OUTPUT_PREVIEW_CHARS]
    if "history_search" in target.tool_name or "redmine_issue_fetch" in target.tool_name:
        ids = set(_structured_issue_ids(output))
        if "redmine_issue_fetch" in target.tool_name:
            try:
                input_issue_id = int(target.tool_input.get("issue_id") or 0)
            except (TypeError, ValueError):
                input_issue_id = 0
            if input_issue_id > 0:
                ids.add(input_issue_id)
        target.evidence_issue_ids = sorted(ids)[:100]
    if "redmine_attachments" in target.tool_name:
        parsed, count, text_ids, all_ids = _attachment_manifest(output)
        target.attachment_manifest_parsed = parsed
        target.attachment_count = count
        target.text_artifact_ids = text_ids
        target.all_artifact_ids = all_ids
    target.snapshot_ids = _collect_snapshot_ids(output)
    if _is_source_evidence_tool(target.tool_name):
        target.source_reproducible = _source_reproducible_flag(output)


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
            tool_input=_bounded_tool_input(event.get("input")),
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


def consume_line(trace: KkAgentTrace, raw_line: str | bytes, *, on_event: Any = None) -> bool:
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
    "KkAgentTrace",
    "ToolTrace",
    "consume_event",
    "consume_line",
]
