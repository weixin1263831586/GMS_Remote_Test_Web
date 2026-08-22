"""Calibrate AI diagnosis certainty before exposing it to operators."""

from __future__ import annotations

import re
from typing import Any

from foundation.redaction import redact_sensitive_text


_ROOT_PREFIX_RE = re.compile(
    r"^(?:(?:🎯|根本原因[:：]?|Root cause[:：]?|待验证假设[:：]?|待验证[:：]?|初步判断[:：]?)\s*)+",
    re.IGNORECASE,
)
_RESET_TIME_RE = re.compile(r"限额将在\s*([0-9-]+\s+[0-9:]+)\s*重置")


def public_provider_error(raw_error: Any) -> str:
    """Return a concise operator-facing error without LiteLLM internals."""
    raw = redact_sensitive_text(raw_error or "模型调用失败").strip()
    provider_match = re.match(r"^([A-Za-z0-9_-]*(?:local|glm|zhipu|openai)[A-Za-z0-9_-]*)\b", raw, re.IGNORECASE)
    provider = raw.split(":", 1)[0].strip() if ":" in raw else (provider_match.group(1) if provider_match else "AI")
    is_local = "local" in provider.lower() or "本地" in raw
    reset_match = _RESET_TIME_RE.search(raw)
    if "使用上限" in raw or "RateLimitError" in raw or "quota exceeded" in raw.lower():
        subject = "本地模型" if is_local else "模型"
        reset = f"，预计 {reset_match.group(1)} 恢复" if reset_match else ""
        return f"{provider}：{subject}额度已用尽{reset}。"
    if "authentication" in raw.lower() or "unauthorized" in raw.lower():
        return f"{provider}：鉴权失败，请检查 API key 配置。"
    if "timeout" in raw.lower() or "timed out" in raw.lower() or "请求超时" in raw:
        return f"{provider}：模型请求超时。"
    public = re.split(r"No fallback model group|Fallbacks=|LiteLLM Retried", raw, maxsplit=1)[0]
    public = re.sub(r"\*+\.", "", public).strip(" ;")
    return public[:240] or f"{provider}：模型调用失败。"


def _observed_failure(error_message: str, stack_trace: str) -> str:
    lines = [
        line.strip()
        for text in (error_message, stack_trace)
        for line in str(text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return "未提供明确的异常或断言信息"
    preferred = next((
        line for line in lines
        if not line.startswith("at ") and any(token in line.lower() for token in (
            "exception", "error", "assert", "timeout", "not called", "failed",
        ))
    ), lines[0])
    return preferred[:500]


def calibrate_ai_result(
    ai_result: dict[str, Any],
    error_message: str,
    stack_trace: str,
) -> dict[str, Any]:
    """Separate observed evidence from an AI hypothesis.

    Model output is never promoted to a verified root cause by self-assertion.
    A future deterministic verifier may set ``_trusted_root_cause`` internally.
    """
    result = dict(ai_result or {})
    observed = _observed_failure(error_message, stack_trace)
    root = _ROOT_PREFIX_RE.sub("", str(result.get("root_cause") or "")).strip()
    trusted = result.pop("_trusted_root_cause", False) is True
    evidence = [
        str(item).strip()[:300]
        for item in (result.get("root_cause_evidence") or result.get("evidence") or [])
        if str(item).strip()
    ][:5]

    if trusted:
        status, label, confidence = "verified", "已验证根因", "high"
    else:
        status, label, confidence = "hypothesis", "初步判断", "low"
        root = f"待验证：{root}" if root else "尚未定位导致该失败现象的上游原因"

    result.update({
        "root_cause": root,
        "root_cause_status": status,
        "root_cause_label": label,
        "root_cause_confidence": confidence,
        "root_cause_verified": trusted,
        "root_cause_evidence": evidence,
        "observed_failure": observed,
        "root_cause_note": "" if trusted else (
            f"当前直接证据只证明：{observed}。该判断仍需结合系统服务日志、源码路径或复现对照验证。"
        ),
    })
    return result
