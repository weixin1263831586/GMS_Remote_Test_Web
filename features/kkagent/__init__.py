"""统一模型网关：模型配置、直连 HTTP 通道与 kkagent 集成（ADR 0034）。

单一职责面：所有"调用大模型"的入口都从这里走——
- ``registry``：模型目录（configs/local/config.json ``ai_models`` 段是
  唯一来源，UI 列表 / kkagent 模型 alias 都从这里物化）；
- ``analyzer`` / ``http_client`` / ``health``：openai/anthropic 直连通道
  （provider 选择、failover、健康探针）；
- ``process`` / ``sandbox`` / ``config`` / ``trace`` / ``session`` /
  ``errors``：kkagent headless 子进程引擎（供 assistant 桥接与 redmine
  晨报分析复用）。

依赖方向：assistant → kkagent、redmine → kkagent、kkagent → foundation。
对外只暴露 ``__all__`` 声明的公共面；跨 feature 引用禁止触及内部模块。
"""

from __future__ import annotations

from .analyzer import (
    EMOJI_CHART,
    EMOJI_CHECK,
    EMOJI_TARGET,
    UniversalAIAnalyzer,
    call_provider_chain,
    first_local_provider,
    get_universal_analyzer,
)
from .config import ContextBudgetError, analysis_command, context_limited_config, model_config
from .errors import (
    classify_failure,
    context_length_limit,
    has_textual_gms_tool_call,
    interrupted_exit,
    llm_stream_timeout,
    provider_overloaded,
    signal_label,
    strip_ansi,
    summarize_stderr,
    turn_limit_reached,
)
from .health import (
    auth_headers,
    is_local_provider,
    probe_provider,
    provider_statuses,
)
from .http_client import (
    API_FORMAT_ANTHROPIC,
    API_FORMAT_OPENAI,
    api_format_for,
    build_chat_request,
    endpoint_url,
    parse_response_text,
    post_chat,
    rejects_disabling_thinking,
)

# ---- kkagent headless 引擎（process / sandbox / config / trace / session）----
from .process import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    MCP_IDENTITY_ENV_KEYS,
    STREAM_LINE_LIMIT_BYTES,
    CappedCapture,
    child_env,
    read_stream_capped,
    settle_reader_future,
    terminate_process_tree,
)
from .registry import (
    ModelSpec,
    default_model_alias,
    list_model_options,
    resolve_model_spec,
    sanitized_model_catalog,
)
from .sandbox import read_bounded, restricted_runtime
from .session import (
    KKAGENT_BINARY,
    TURN_TIMEOUT_SECONDS,
    HeadlessTurnResult,
    extract_turn_events,
    run_headless_turn,
)
from .trace import (
    TOOL_INPUT_JSON_CHARS,
    TOOL_OUTPUT_PREVIEW_CHARS,
    KkAgentStreamTrace,
    ToolTrace,
    consume_event,
    consume_line,
)


__all__ = [
    "API_FORMAT_ANTHROPIC",
    "API_FORMAT_OPENAI",
    "CAPTURE_HEAD_BYTES",
    "CAPTURE_TAIL_BYTES",
    "EMOJI_CHART",
    "EMOJI_CHECK",
    "EMOJI_TARGET",
    "KKAGENT_BINARY",
    "MCP_IDENTITY_ENV_KEYS",
    "STREAM_LINE_LIMIT_BYTES",
    "TOOL_INPUT_JSON_CHARS",
    "TOOL_OUTPUT_PREVIEW_CHARS",
    "TURN_TIMEOUT_SECONDS",
    "CappedCapture",
    "ContextBudgetError",
    "HeadlessTurnResult",
    "KkAgentStreamTrace",
    "ModelSpec",
    "ToolTrace",
    "UniversalAIAnalyzer",
    "analysis_command",
    "api_format_for",
    "auth_headers",
    "build_chat_request",
    "call_provider_chain",
    "child_env",
    "classify_failure",
    "consume_event",
    "consume_line",
    "context_length_limit",
    "context_limited_config",
    "default_model_alias",
    "endpoint_url",
    "extract_turn_events",
    "first_local_provider",
    "get_universal_analyzer",
    "has_textual_gms_tool_call",
    "interrupted_exit",
    "is_local_provider",
    "list_model_options",
    "llm_stream_timeout",
    "model_config",
    "parse_response_text",
    "post_chat",
    "probe_provider",
    "provider_overloaded",
    "provider_statuses",
    "read_bounded",
    "read_stream_capped",
    "rejects_disabling_thinking",
    "resolve_model_spec",
    "restricted_runtime",
    "run_headless_turn",
    "sanitized_model_catalog",
    "settle_reader_future",
    "signal_label",
    "strip_ansi",
    "summarize_stderr",
    "terminate_process_tree",
    "turn_limit_reached",
]
