"""Redmine kkagent 集成层：分析器 / 轨迹 / 输出解析 / 预检 / 证据门禁。

进程协议（process/errors/config/sandbox）与 stream-json 通用核已统一到
:mod:`features.kkagent`（ADR 0034）；本包保留 Redmine 取证领域：
prompt/修复轮/版本键（analyzer）、证据门禁（evidence_gate）、取证预检
（evidence_preflight/auth_preflight/mcp_health）、输出解析（output）、
原生摘要（native_summary）与证据域轨迹（trace）。

注意 import 方向：analyzer 依赖叶子模块与 features.kkagent 引擎，且不
反向 import 包本身；包级 ``__init__`` 只做聚合 re-export（analyzer 放
最后）。
"""

from __future__ import annotations

from .analyzer import (
    DAILY_BRIEF_MCP_TOOLSETS,
    PROMPT_VERSION,
    KkAgentAnalysisResult,
    KkAgentRedmineAnalyzer,
    analyzer_version,
)
from .auth_preflight import preflight_gms_auth
from .evidence_gate import (
    MIN_HISTORY_SEARCHES,
    apply_gate,
    evaluate_evidence_gate,
    gate_and_errors,
    gate_errors,
)
from .output import extract_json, json_from_message, parse_issue_result
from .trace import KkAgentTrace, ToolTrace, consume_event, consume_line


__all__ = [
    "DAILY_BRIEF_MCP_TOOLSETS",
    "MIN_HISTORY_SEARCHES",
    "PROMPT_VERSION",
    "KkAgentAnalysisResult",
    "KkAgentRedmineAnalyzer",
    "KkAgentTrace",
    "ToolTrace",
    "analyzer_version",
    "apply_gate",
    "consume_event",
    "consume_line",
    "evaluate_evidence_gate",
    "extract_json",
    "gate_and_errors",
    "gate_errors",
    "json_from_message",
    "parse_issue_result",
    "preflight_gms_auth",
]
