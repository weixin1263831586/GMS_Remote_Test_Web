"""AI provider configuration readiness and explicit health probes."""

from __future__ import annotations

import ipaddress
import os
import re
import time
from collections.abc import Callable
from urllib.parse import urlparse


def is_local_provider(provider_name: str, config: dict) -> bool:
    base_url = str(config.get("base_url") or "")
    host = (urlparse(base_url).hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
        local_address = address.is_private or address.is_loopback
    except ValueError:
        local_address = False
    return (
        "local" in str(provider_name or "").lower()
        or host in {"localhost", "0.0.0.0"}
        or local_address
    )


def auth_headers(
    provider_name: str,
    config: dict,
    header_name: str = "Authorization",
) -> dict:
    api_key = str(config.get("api_key") or "").strip()
    local = is_local_provider(provider_name, config)
    if not api_key and local:
        api_key = os.getenv("GMS_LOCAL_AI_API_KEY", "").strip()
    if not api_key and local and config.get("auth_required") is False:
        return {}
    if not api_key:
        raise ValueError(
            f"{provider_name} API密钥未配置（请在 ai_models.providers.{provider_name}"
            f".api_key 或环境变量 GMS_LOCAL_AI_API_KEY 中设置）"
        )
    if header_name == "x-api-key":
        return {"x-api-key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def provider_statuses(config: dict) -> list[dict]:
    """Return sanitized readiness without claiming a provider is online."""
    statuses = []
    for provider_name, provider in config.get("providers", {}).items():
        if not isinstance(provider, dict):
            continue
        local = is_local_provider(provider_name, provider)
        api_key = str(provider.get("api_key") or "").strip()
        if not api_key and local:
            api_key = os.getenv("GMS_LOCAL_AI_API_KEY", "").strip()
        auth_required = not (local and provider.get("auth_required") is False)
        configured = bool(provider.get("base_url") and provider.get("model"))
        credential_configured = bool(api_key) or not auth_required
        enabled = bool(provider.get("enabled", False))
        if not enabled:
            state = "disabled"
        elif not configured:
            state = "incomplete"
        elif not credential_configured:
            state = "credential_missing"
        else:
            state = "ready_to_probe"
        statuses.append({
            "provider": provider_name,
            "name": str(provider.get("name") or provider_name),
            "model": str(provider.get("model") or ""),
            "enabled": enabled,
            "local": local,
            "configured": configured,
            "credential_configured": credential_configured,
            "state": state,
            "available": None,
            "checked": False,
        })
    return statuses


def probe_provider(
    config: dict,
    provider_name: str,
    generate: Callable[[str, dict, str, str, int], dict],
) -> dict:
    """Perform a tiny inference against one provider without failover."""
    statuses = {item["provider"]: item for item in provider_statuses(config)}
    status = dict(statuses.get(provider_name) or {
        "provider": provider_name,
        "state": "missing",
        "available": False,
        "checked": True,
    })
    provider = config.get("providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        status.update({"available": False, "checked": True, "error": "模型 provider 不存在"})
        return status
    if status.get("state") != "ready_to_probe":
        reasons = {
            "disabled": "模型 provider 未启用",
            "incomplete": "模型地址或模型名称未配置完整",
            "credential_missing": "API 密钥未配置",
        }
        status.update({
            "available": False,
            "checked": True,
            "error": reasons.get(status.get("state"), "模型配置不可用"),
        })
        return status

    started = time.monotonic()
    result = generate(
        provider_name,
        provider,
        "仅回复 OK",
        "这是连通性检测。不要输出解释。",
        8,
    )
    latency_ms = round((time.monotonic() - started) * 1000)
    if result.get("success"):
        status.update({
            "available": True,
            "checked": True,
            "state": "available",
            "latency_ms": latency_ms,
        })
        return status

    raw_error = str(result.get("error") or "模型调用失败")
    base_url = str(provider.get("base_url") or "")
    if base_url:
        raw_error = raw_error.replace(base_url, "模型服务")
    raw_error = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[已隐藏]", raw_error)
    status.update({
        "available": False,
        "checked": True,
        "state": "unavailable",
        "latency_ms": latency_ms,
        "error": raw_error[:240],
    })
    return status
