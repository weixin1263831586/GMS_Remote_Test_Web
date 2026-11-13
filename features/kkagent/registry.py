"""模型目录：configs/local/config.json ``ai_models`` 段的唯一物化点。

历史上有两条模型配置链：直连 HTTP 读 ``ai_models``，kkagent 子进程读
宿主 ``~/.kkagent/config.toml`` 的 ``[models]`` 表，靠人工保持同步，
漂移即静默失败。统一后模型定义只存在于 ``ai_models``：每个 enabled
provider 物化为一个 :class:`ModelSpec`（alias 即 provider 的 ``model``
值，与 ``KKAGENT_DEFAULT_MODEL`` 现行语义一致），kkagent 临时配置、UI
模型列表、直连调用都从本模块取数（ADR 0034）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from features.kkagent.health import is_local_provider, provider_api_key
from features.kkagent.http_client import api_format_for


@dataclass(frozen=True)
class ModelSpec:
    """一个可用模型（provider 粒度；alias = provider.model）。"""

    provider: str
    alias: str
    display_name: str
    base_url: str
    model: str
    api_format: str
    api_key: str = field(default="", repr=False)
    local: bool = False
    enabled: bool = True
    max_tokens: int | None = None
    temperature: float | None = None
    capabilities: list[str] = field(default_factory=list)
    max_context_size: int | None = None
    default_effort: str | None = None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _spec_from_provider(provider_name: str, provider: dict[str, Any]) -> ModelSpec | None:
    model = str(provider.get("model") or "").strip()
    if not model:
        return None
    return ModelSpec(
        provider=provider_name,
        alias=model,
        display_name=str(provider.get("display_name") or provider.get("name") or model),
        base_url=str(provider.get("base_url") or ""),
        model=model,
        api_format=api_format_for(provider_name, provider),
        api_key=provider_api_key(provider_name, provider),
        local=is_local_provider(provider_name, provider),
        enabled=bool(provider.get("enabled", False)),
        max_tokens=_as_int(provider.get("max_tokens")),
        temperature=float(provider["temperature"]) if provider.get("temperature") is not None else None,
        capabilities=[str(item) for item in provider.get("capabilities") or []],
        max_context_size=_as_int(provider.get("max_context_size")),
        default_effort=str(provider["default_effort"]) if provider.get("default_effort") else None,
    )


def resolve_model_spec(ai_config: dict[str, Any] | None, alias: str) -> ModelSpec | None:
    """按 alias（= provider.model）取模型；未启用或不存在返回 None。"""
    for spec in model_specs(ai_config):
        if spec.alias == str(alias or "").strip():
            return spec
    return None


def model_specs(ai_config: dict[str, Any] | None) -> list[ModelSpec]:
    """全部 enabled provider 的模型，primary provider 的模型排最前。"""
    if not isinstance(ai_config, dict) or not ai_config.get("enabled", False):
        return []
    providers = ai_config.get("providers")
    if not isinstance(providers, dict):
        return []
    primary = str(ai_config.get("primary_provider") or "").strip()
    names = list(providers)
    if primary in providers:
        names.remove(primary)
        names.insert(0, primary)
    specs: list[ModelSpec] = []
    for provider_name in names:
        provider = providers.get(provider_name)
        if not isinstance(provider, dict) or not provider.get("enabled", False):
            continue
        spec = _spec_from_provider(provider_name, provider)
        if spec and spec.enabled:
            specs.append(spec)
    return specs


def default_model_alias(ai_config: dict[str, Any] | None) -> str:
    """默认模型 alias（primary provider 的 model；否则第一个 enabled）。"""
    specs = model_specs(ai_config)
    return specs[0].alias if specs else ""


def list_model_options(ai_config: dict[str, Any] | None) -> dict[str, Any]:
    """UI 选项形状（与原 ``list_daily_brief_model_options`` 返回一致）。

    只暴露 model/display_name/provider，绝不携带凭据或 base_url。
    """
    specs = model_specs(ai_config)
    models = []
    seen = set()
    for spec in specs:
        if spec.alias in seen:
            continue
        seen.add(spec.alias)
        models.append({
            "model": spec.alias,
            "display_name": spec.display_name,
            "provider": spec.provider,
        })
    return {"models": models, "default_model": default_model_alias(ai_config)}


def sanitized_model_catalog(ai_config: dict[str, Any] | None) -> dict[str, Any]:
    """脱敏目录：供 kkagent 配置生成等内部消费（不含 api_key 明文判断）。"""
    return list_model_options(ai_config)
