"""kkagent 集成层：进程管理 / 轨迹 / 输出解析 / 预检 / 证据门禁。

注意 import 方向：analyzer 依赖叶子模块（process/errors/trace/output/
evidence_gate），且不反向 import 包本身；包级 ``__init__`` 只做聚合
re-export（analyzer 放最后）。
"""

from __future__ import annotations

from .analyzer import (
    DAILY_BRIEF_MCP_TOOLSETS,
    KKAGENT_BINARY,
    PROMPT_VERSION,
    KkAgentAnalysisResult,
    KkAgentRedmineAnalyzer,
)
from .auth_preflight import preflight_gms_auth
from .errors import classify_failure, summarize_stderr
from .evidence_gate import (
    MIN_HISTORY_SEARCHES,
    apply_gate,
    evaluate_evidence_gate,
    gate_and_errors,
    gate_errors,
)
from .output import extract_json, json_from_message, parse_issue_result
from .process import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    MCP_IDENTITY_ENV_KEYS,
    STREAM_LINE_LIMIT_BYTES,
    child_env,
    read_stream_capped,
    settle_reader_future,
    terminate_process_tree,
)
from .trace import KkAgentTrace, ToolTrace, consume_event, consume_line


__all__ = [
    "CAPTURE_HEAD_BYTES",
    "CAPTURE_TAIL_BYTES",
    "DAILY_BRIEF_MCP_TOOLSETS",
    "KKAGENT_BINARY",
    "MCP_IDENTITY_ENV_KEYS",
    "MIN_HISTORY_SEARCHES",
    "PROMPT_VERSION",
    "STREAM_LINE_LIMIT_BYTES",
    "KkAgentAnalysisResult",
    "KkAgentRedmineAnalyzer",
    "KkAgentTrace",
    "ToolTrace",
    "apply_gate",
    "child_env",
    "classify_failure",
    "consume_event",
    "consume_line",
    "evaluate_evidence_gate",
    "extract_json",
    "gate_and_errors",
    "gate_errors",
    "json_from_message",
    "parse_issue_result",
    "preflight_gms_auth",
    "read_stream_capped",
    "settle_reader_future",
    "summarize_stderr",
    "terminate_process_tree",
]
