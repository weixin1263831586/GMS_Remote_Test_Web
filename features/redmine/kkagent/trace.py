"""Redmine 领域轨迹：证据归属 / artifact 覆盖 / 源码取证语义。

通用 stream-json 消费核在 :mod:`features.kkagent.trace`（引擎侧）；
本模块只做 Redmine 取证域扩展：

- ``KkAgentTrace(KkAgentStreamTrace)``：追加证据域字段（issue 归属、
  attachment manifest、snapshot、源码取证可复现性）与 Evidence Gate
  消费的派生属性（ledger、coverage、窗口读取判定）；
- ``record_tool_result`` 钩子：把 tool_result 的领域语义并入记录；
- ``source_evidence_result`` / ``trace_from_summary``：UI 消费与
  context-overflow 续跑的恢复入口。

隐私约束与事件语义见引擎模块文档。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from features.kkagent import (
    TOOL_INPUT_JSON_CHARS,
    TOOL_OUTPUT_PREVIEW_CHARS,
    KkAgentStreamTrace,
    consume_line,
)
from features.kkagent import (
    ToolTrace as _EngineToolTrace,
)
from features.kkagent import (
    consume_event as _engine_consume_event,
)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class ToolTrace(_EngineToolTrace):
    """一次工具调用的证据记录（引擎字段 + Redmine 取证域字段）。"""

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
    # 工具进程成功不等于获得源码证据：搜索 0 命中、仅 resolve 工件、仅
    # 启动反编译任务都不能通过 Evidence Gate。
    source_evidence_valid: bool = False
    source_result_count: int = 0
    # 搜索工具（artifact_search/batch）输出是否声明 coverage_complete=true
    # （False 时零命中不可当否定证据，Gate 也不计入覆盖）。
    search_coverage_complete: bool = False
    # 搜索调用命中的 artifact id（从 matches/artifact_id 提取，供覆盖归属）。
    search_artifact_ids: list[str] = field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            **super().to_summary(),
            "evidence_issue_ids": self.evidence_issue_ids,
            "attachment_manifest_parsed": self.attachment_manifest_parsed,
            "attachment_count": self.attachment_count,
            "text_artifact_ids": self.text_artifact_ids,
            "all_artifact_ids": self.all_artifact_ids,
            "snapshot_ids": self.snapshot_ids,
            "source_reproducible": self.source_reproducible,
            "source_evidence_valid": self.source_evidence_valid,
            "source_result_count": self.source_result_count,
            "search_coverage_complete": self.search_coverage_complete,
            "search_artifact_ids": self.search_artifact_ids,
        }


def _is_source_evidence_tool(tool_name: str) -> bool:
    """是否为能直接返回源码内容/命中的取证工具。

    ``apk_resolve`` / ``apk_analyze`` / ``apk_status`` 只是定位工件或管理
    反编译任务；即使调用成功也不是源码证据。必须继续 search/read。
    """
    normalized = tool_name.lower()
    return any(marker in normalized for marker in (
        "sdk_search", "sdk_read", "apk_search", "apk_source_search", "apk_source_read",
        "codesearch__search",
    ))


def _source_evidence_result(tool_name: str, value: Any) -> tuple[bool, int]:
    """从源码工具结果判定是否真的返回至少一条可用证据。"""
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (TypeError, ValueError):
            parsed = None

    counts: list[int] = []
    nonempty_payload = False

    def walk(item: Any) -> None:
        nonlocal nonempty_payload
        if isinstance(item, dict):
            for key, child in item.items():
                lowered = str(key).lower()
                if lowered in {
                    "total", "result_count", "returned", "returned_chars",
                    "match_count", "matches_count",
                }:
                    count = _as_int(child)
                    if count >= 0:
                        counts.append(count)
                elif lowered in {"results", "matches", "items", "files"}:
                    if isinstance(child, list):
                        counts.append(len(child))
                elif lowered in {"text", "content", "source", "snippet"}:
                    if isinstance(child, str) and child.strip():
                        nonempty_payload = True
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(parsed)
    normalized = tool_name.lower()
    # Code Search 的 MCP 输出是可读文本而非 JSON。
    if parsed is None and isinstance(value, str) and "codesearch__search" in normalized:
        match = re.search(r"\bresult_count:\s*(\d+)", value)
        count = int(match.group(1)) if match else 0
        return count > 0, count
    positive = max(counts or [0])
    if "search" in normalized:
        return positive > 0, positive
    # A file's total line count is metadata; an empty read is not evidence.
    return nonempty_payload, positive


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


def source_evidence_result(tool_name: str, value: Any) -> dict[str, Any] | None:
    """Return Controller-derived source-evidence semantics for UI consumers."""
    if not _is_source_evidence_tool(tool_name):
        return None
    valid, result_count = _source_evidence_result(tool_name, value)
    return {
        "kind": "source",
        "valid": valid,
        "result_count": result_count,
        "reproducible": _source_reproducible_flag(value),
    }


@dataclass
class KkAgentTrace(KkAgentStreamTrace):
    """一次 kkagent 调用（attempt）的完整运行时轨迹（含 Redmine 取证域）。"""

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

    def search_covered_artifact_ids(self) -> set[str]:
        """搜索已覆盖全文的 artifact（coverage_complete=true 的成功搜索）。

        区分"调用成功"与"日志覆盖完整"：coverage_complete
        为 false 时搜索只扫了部分证据，其命中的 artifact 不计入。
        """
        covered: set[str] = set()
        for call in self.tool_calls:
            if not call.succeeded or "artifact_search" not in call.tool_name:
                continue
            if not call.search_coverage_complete:
                continue
            covered.update(call.search_artifact_ids)
        return covered

    def window_read_only_artifact_ids(self) -> set[str]:
        """只被 artifact_read 读过窗口、但没有任何覆盖完整搜索的 artifact。

        读过 4096 字符首窗不等于看过日志尾部——这些 artifact 需要
        search（或更长的 read）才算取证完整。"""
        return self.read_artifact_ids() - self.search_covered_artifact_ids()

    # ---------------------------------------------------- 归属（target-scoped）

    @property
    def source_evidence_tool_count(self) -> int:
        """成功且实际返回命中/源码内容的源码级取证调用数。"""
        return sum(
            1
            for call in self.tool_calls
            if call.succeeded
            and _is_source_evidence_tool(call.tool_name)
            and call.source_evidence_valid
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
            and call.source_evidence_valid
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
            is_source = (
                _is_source_evidence_tool(call.tool_name)
                and call.source_evidence_valid
            )
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
            path = str(call.tool_input.get("path") or call.tool_input.get("file") or "")
            task_id = str(call.tool_input.get("task_id") or "")
            if is_source and path:
                refs.add(f"src:{task_id}:{path}" if task_id else f"file:{path}")
            if task_id and "apk_manifest" in call.tool_name:
                refs.add(f"apk:{task_id}:manifest")
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
            **super().to_summary(),
            "history_search_count": self.history_search_count,
            "distinct_history_search_count": self.distinct_history_search_count,
        }

    # ------------------------------------------------- 引擎多态钩子实现

    def new_tool_call(self, tool_call_id: str, tool_name: str, tool_input: dict[str, Any]) -> ToolTrace:
        return ToolTrace(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    def record_tool_result(self, call: ToolTrace, event: dict[str, Any]) -> None:
        """引擎通用记录后的取证域富化（issue/artifact/snapshot/源码语义）。"""
        output = event.get("output")
        if "history_search" in call.tool_name or "redmine_issue_fetch" in call.tool_name:
            ids = set(_structured_issue_ids(output))
            if "redmine_issue_fetch" in call.tool_name:
                try:
                    input_issue_id = int(call.tool_input.get("issue_id") or 0)
                except (TypeError, ValueError):
                    input_issue_id = 0
                if input_issue_id > 0:
                    ids.add(input_issue_id)
            call.evidence_issue_ids = sorted(ids)[:100]
        if "redmine_attachments" in call.tool_name:
            parsed, count, text_ids, all_ids = _attachment_manifest(output)
            call.attachment_manifest_parsed = parsed
            call.attachment_count = count
            call.text_artifact_ids = text_ids
            call.all_artifact_ids = all_ids
        call.snapshot_ids = _collect_snapshot_ids(output)
        if "artifact_search" in call.tool_name:
            call.search_coverage_complete, call.search_artifact_ids = (
                _search_coverage(output)
            )
        if _is_source_evidence_tool(call.tool_name):
            call.source_reproducible = _source_reproducible_flag(output)
            (
                call.source_evidence_valid,
                call.source_result_count,
            ) = _source_evidence_result(call.tool_name, output)


def _normalize_history_query(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


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
        if (str(item.get("kind") or "") in {"text", "log"} or item.get("text_available") is True)
        and str(item.get("status") or "") in {"ready", "partial"}
        and str(item.get("artifact_id") or "").strip()
    }
    return True, len(artifacts), sorted(text_ids), all_ids


def _search_coverage(value: Any) -> tuple[bool, list[str]]:
    """解析搜索工具输出 → (coverage_complete, 命中的 artifact ids)。

    兼容单查询端点（data.coverage_complete + matches[].artifact_id）与
    批量端点（data.coverage_complete + queries[].matches[].artifact_id）。
    解析失败按 (False, []) 处理——宁可让 Gate 少记一次覆盖，也不能把
    未覆盖的搜索当已覆盖。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return False, []
    if not isinstance(value, dict):
        return False, []
    # CLI/MCP 输出是双层 envelope（CLI data → HTTP success/data），
    # 循环解包直到不再嵌套（与 run_history.report_snapshot 同模式）。
    while isinstance(value, dict) and isinstance(value.get("data"), dict):
        value = value["data"]
    data = value if isinstance(value, dict) else {}
    coverage = (data.get("coverage_complete", data.get("index_complete", False)) is True
                and data.get("index_complete", True) is True)
    artifact_ids: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            artifact_id = str(item.get("artifact_id") or "").strip()
            if artifact_id:
                artifact_ids.add(artifact_id)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(data.get("matches"))
    for row in data.get("queries") or []:
        if isinstance(row, dict):
            walk(row.get("matches"))
    return bool(coverage), sorted(artifact_ids)[:100]


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


__all__ = [
    "TOOL_INPUT_JSON_CHARS",
    "TOOL_OUTPUT_PREVIEW_CHARS",
    "KkAgentTrace",
    "ToolTrace",
    "consume_event",
    "consume_line",
    "source_evidence_result",
    "trace_from_summary",
]


def consume_event(trace: KkAgentTrace, event: dict[str, Any]) -> None:
    """把一条已解析的 stream-json 事件并入 Redmine 轨迹（透传引擎核）。"""
    _engine_consume_event(trace, event)
