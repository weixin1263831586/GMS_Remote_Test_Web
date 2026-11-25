"""AI-first report failure diagnosis with explicit rule fallback metadata."""

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


def analyze_failure_with_ai(
    test_name: str,
    error_message: str,
    stack_trace: str,
    module: str,
    class_names: list[str] | None,
    *,
    analyzer_factory: Any,
    parse_failure_info: Any,
    rule_based_analysis: Any,
    config_manager: Any,
    stack_trace_utils: Any,
    logger: Any,
) -> dict[str, Any]:
    class_names = class_names or []
    failure_location = stack_trace_utils.extract_failure_location(stack_trace)
    if failure_location:
        logger.info(
            "从堆栈提取失败位置: %s.%s:%s",
            failure_location['file_name'],
            failure_location['file_type'],
            failure_location['line_number'],
        )

    ai_error = ''
    preferred_provider = ''
    attempted_providers = []
    try:
        if analyzer_factory is None:
            raise RuntimeError("Universal AI analyzer is not configured")
        analyzer = analyzer_factory()
        preferred_provider = analyzer.get_local_provider() or ''
        failure_info = parse_failure_info(test_name, error_message)
        result = analyzer.analyze_test_failure(
            class_name=failure_info.get('class_name', ''),
            method_name=failure_info.get('method_name'),
            error_message=error_message,
            stack_trace=stack_trace,
            auto_fetch_source=True,
            preferred_provider=preferred_provider or None,
        )
        attempted_providers = result.get('attempted_providers') or []
        if result['success']:
            provider_name = result.get('provider', 'unknown')
            provider_config = config_manager.get_ai_provider_config(provider_name)
            provider_display = (
                provider_config.get('name', f'{provider_name.upper()} AI')
                if provider_config else f'{provider_name.upper()} AI'
            )
            raw_provider_errors = result.get('provider_errors') or []
            safe_provider_errors = [
                public_provider_error(error) for error in raw_provider_errors
            ]
            if raw_provider_errors:
                logger.warning("AI provider 降级: %s", "; ".join(safe_provider_errors))
            response = {
                'root_cause': result.get('root_cause', ''),
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'solution': result.get('solution'),
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name,
                'ai_fallback_used': bool(result.get('fallback_used')),
                'ai_preferred_provider': result.get('preferred_provider', ''),
                'ai_providers_attempted': attempted_providers,
                'ai_provider_errors': safe_provider_errors,
                'root_cause_evidence': result.get('evidence') or [],
                'stack_trace': stack_trace,
            }
            source_info = result.get('source_info')
            if source_info:
                response.update({
                    'source_code_fetched': True,
                    'source_file_path': source_info.get('file_path', ''),
                    'source_url': source_info.get('url', ''),
                    'source_project': source_info.get('project', ''),
                })
                logger.info(
                    "成功获取源码信息: %s",
                    source_info.get('file_path', 'unknown'),
                )
            return calibrate_ai_result(response, error_message, stack_trace)
        ai_error = public_provider_error(result.get('error') or 'AI分析失败')
        logger.warning("AI分析失败: %s", redact_sensitive_text(ai_error))
    except ImportError:
        ai_error = '通用AI分析器未安装'
        logger.warning("通用AI分析器未安装，使用基于规则的分析")
    except Exception as exc:
        ai_error = public_provider_error(exc)
        logger.warning(
            "通用AI分析失败: %s，使用基于规则的分析", redact_sensitive_text(ai_error)
        )

    fallback = rule_based_analysis(
        test_name, error_message, stack_trace, module
    )
    fallback.update({
        'ai_attempted': True,
        'ai_error': public_provider_error(
            ai_error or '所有已配置 AI provider 均不可用'
        ),
        'ai_provider': preferred_provider,
        'ai_providers_attempted': attempted_providers,
    })
    return calibrate_ai_result(fallback, error_message, stack_trace)
