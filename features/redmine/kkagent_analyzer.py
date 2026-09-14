"""兼容 shim：kkagent 集成已拆分至 ``features/redmine/kkagent/`` 包。

保留原 import 路径（``features.redmine.kkagent_analyzer``），既有调用方
（service / api / 旧测试）无需改动；新代码应直接从子包导入。
"""

from __future__ import annotations

from .kkagent import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    KKAGENT_BINARY,
    MCP_IDENTITY_ENV_KEYS,
    PROMPT_VERSION,
    KkAgentAnalysisResult,
    KkAgentRedmineAnalyzer,
    KkAgentTrace,
    child_env,
    preflight_gms_auth,
    read_stream_capped,
    summarize_stderr,
)


__all__ = [
    "CAPTURE_HEAD_BYTES",
    "CAPTURE_TAIL_BYTES",
    "KKAGENT_BINARY",
    "MCP_IDENTITY_ENV_KEYS",
    "PROMPT_VERSION",
    "KkAgentAnalysisResult",
    "KkAgentRedmineAnalyzer",
    "KkAgentTrace",
    "child_env",
    "preflight_gms_auth",
    "read_stream_capped",
    "summarize_stderr",
]
