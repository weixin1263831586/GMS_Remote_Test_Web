"""kkagent 失败分类：stderr 摘要与 error_type 归类。"""

from __future__ import annotations

import re
import signal


# ANSI 转义序列（颜色/光标控制/回车）：kkagent 的 stderr 日志即使设了
# NO_COLOR 也可能残留控制符，入库前统一剥除，避免 Web 端显示乱码。
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\r")

# stderr 摘要长度上限。
STDERR_SUMMARY_LIMIT = 500

# kkagent stderr 日志里的 RFC3339 时间戳(2026-09-13T10:31:49.070374Z)
# 对 UI 展示不友好:入库前统一转成 "2026-09-13 10:31:49.070"。
_ISO_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?Z?\b"
)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text).strip()


def normalize_log_timestamps(text: str) -> str:
    def _fmt(match: re.Match[str]) -> str:
        frac = (match.group(3) or "")[:3]
        return f"{match.group(1)} {match.group(2)}" + (f".{frac}" if frac else "")

    return _ISO_TS_RE.sub(_fmt, text)


def summarize_stderr(text: str) -> str:
    """从 kkagent 的日志流里提取可读的失败原因。

    失败的真实原因（LLM 限流、配置错误等）几乎总在 ERROR 级行里；头部
    的 INFO/WARN 启动信息对排障没用。优先 ERROR 行，不足再从日志
    **尾部**补齐（越靠后越接近失败点），绝不只截开头。

    展示约束：行与行之间保留真实换行（详情弹窗按 pre-wrap 渲染，
    单行拼接 " | " 会把多行日志糊成一条无法阅读的长串）；RFC3339
    时间戳统一转为本地友好的 "YYYY-MM-DD HH:MM:SS.mmm" 格式。
    """
    lines = [
        normalize_log_timestamps(ln.strip())
        for ln in strip_ansi(text).splitlines()
        if ln.strip()
    ]
    error_lines = [
        ln for ln in lines if " ERROR " in ln or ln.startswith("ERROR")
    ]
    if error_lines:
        return "\n".join(error_lines)[:STDERR_SUMMARY_LIMIT]
    tail: list[str] = []
    for ln in reversed(lines):
        candidate = "\n".join([ln, *tail])
        if len(candidate) > STDERR_SUMMARY_LIMIT:
            break
        tail.insert(0, ln)
    return "\n".join(tail)[:STDERR_SUMMARY_LIMIT]


def turn_was_interrupted(stderr: str) -> bool:
    """单独的 ``Turn interrupted`` 只表示 turn 被取消，不是步数用尽。"""
    return "Turn interrupted" in stderr


def llm_stream_timeout(stderr: str) -> bool:
    """LLM 流式请求超时（kkagent stderr 实测形态）：

    ``LLM stream error: error sending request for url (...)
    [kind=request, kind=timeout]: operation timed out``
    （reqwest 总超时/连接超时都以 kind=timeout 呈现。）
    """
    if "LLM stream error" not in stderr:
        return False
    return "kind=timeout" in stderr or "operation timed out" in stderr


def turn_limit_reached(raw: str, stderr: str, *, envelope_subtype: str = "") -> bool:
    """--max-turns 预算用尽。

    只接受结构化 subtype 或 kkagent 明确的上限日志；
    ``Turn interrupted`` 是取消语义，不能据此猜测步数用尽。
    """
    if envelope_subtype == "max_turns":
        return True
    return "Agent turn limit reached" in stderr


def interrupted_exit(exit_code: int | None, stderr: str) -> bool:
    if exit_code is None:
        return False
    signal_codes = {
        -signal.SIGINT,
        -signal.SIGTERM,
        128 + signal.SIGINT,
        128 + signal.SIGTERM,
    }
    return exit_code in signal_codes or "KeyboardInterrupt" in stderr


def signal_label(exit_code: int | None) -> str:
    if exit_code in (-signal.SIGINT, 128 + signal.SIGINT):
        return "SIGINT"
    if exit_code in (-signal.SIGTERM, 128 + signal.SIGTERM):
        return "SIGTERM"
    return f"exit {exit_code}"


def classify_failure(
    exit_code: int | None,
    raw: str,
    stderr_text: str,
    *,
    envelope_subtype: str = "",
    max_turns: int = 0,
) -> tuple[str, str]:
    """非零退出的 error_type / 可读错误归类。

    LLM 流式超时是根因时优先归类（kkagent 0.4.x 对长上下文请求有
    300s 硬编码单请求超时，超时后 step 重试耗尽会连带 turn 预算用尽
    ——信封 subtype=max_turns 只是表象）。
    """
    summary = summarize_stderr(stderr_text)
    if llm_stream_timeout(stderr_text):
        return "llm_timeout", (
            "模型服务流式响应超时（大上下文请求超过服务端/客户端"
            "时限）。将自动重试一次；若反复出现请在晨报设置中"
            "换用更快的模型（如 glm-5.3-flash）。\n"
            + (summary or f"exit {exit_code}")
        )
    if turn_limit_reached(raw, stderr_text, envelope_subtype=envelope_subtype):
        return "max_turns", (
            f"kkagent 未在 {max_turns} 步预算内完成分析"
            "（可在晨报设置中调大步数上限后重试）。\n"
            + (summary or f"exit {exit_code}")
        )
    interrupted = interrupted_exit(exit_code, stderr_text) or turn_was_interrupted(
        stderr_text
    )
    if interrupted:
        return "interrupted", (
            f"kkagent turn interrupted ({signal_label(exit_code)})\n" + summary
        ).rstrip()
    return "kkagent_error", summary or f"exit {exit_code}"


__all__ = [
    "ANSI_ESCAPE_RE",
    "classify_failure",
    "interrupted_exit",
    "llm_stream_timeout",
    "normalize_log_timestamps",
    "signal_label",
    "strip_ansi",
    "summarize_stderr",
    "turn_limit_reached",
    "turn_was_interrupted",
]
