"""Canonical model catalog and private kkagent runtime configuration (ADR 0034)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

import tomli_w


try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from features.kkagent.health import is_local_provider, provider_api_key
from features.kkagent.http_client import api_format_for
from foundation.config import config_manager as _config_manager


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


def model_catalog_fingerprint(config=None):
    """Bind owner approval to the complete enabled model routing catalog."""
    if config is None:
        config = _config_manager.get_ai_config()
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return ""
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return ""
    routes = []
    for name, provider in providers.items():
        if not isinstance(name, str) or not isinstance(provider, dict):
            return ""
        enabled = provider.get("enabled", False)
        if not isinstance(enabled, bool):
            return ""
        if not enabled:
            continue
        model, base_url = provider.get("model"), provider.get("base_url")
        if not isinstance(model, str) or not model.strip() or not isinstance(base_url, str) or not base_url.strip():
            return ""
        try:
            parsed = urlsplit(base_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                return ""
        except ValueError:
            return ""
        api_format = api_format_for(name, provider)
        if api_format not in {"anthropic", "openai"}:
            return ""
        routes.append({"name": name, "model": model, "base_url": base_url,
                       "api_format": api_format, "auth_required": provider.get("auth_required", True)})
    if not routes:
        return ""
    primary = config.get("primary_provider")
    if not isinstance(primary, str) and primary is not None:
        return ""
    names = [route["name"] for route in routes]
    primary = primary if primary in names else names[0]
    ordered = [route for route in routes if route["name"] == primary]
    ordered.extend(route for route in routes if route["name"] != primary)
    # Do not store credentials; a route/catalog change invalidates prior approval.
    payload = {"version": 2, "routes": ordered}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


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


# These selectors refer to the host model table, which the catalog replaces.
# Leave them unset so kkagent resolves each role from the selected default.
_HOST_MODEL_ROLE_KEYS = (
    "balance_model", "fast_model", "fallback_model", "quality_model", "compaction_model",
)


class ContextBudgetError(Exception):
    """Safe diagnostic with no config contents or provider credentials."""


def _host_config_path(env_extra: dict[str, str] | None = None) -> Path:
    env = {**os.environ, **(env_extra or {})}
    return Path(
        env.get("GMS_ASSISTANT_KKAGENT_CONFIG", "").strip()
        or Path(env.get("HOME") or Path.home()) / ".kkagent" / "config.toml"
    ).expanduser()


def _model_table(spec: ModelSpec) -> dict[str, Any]:
    table: dict[str, Any] = {
        "display_name": spec.display_name,
        "model": spec.model,
        "provider": spec.provider,
    }
    if spec.max_context_size:
        table["max_context_size"] = spec.max_context_size
    if spec.max_tokens:
        table["max_output_size"] = spec.max_tokens
    if spec.capabilities:
        table["capabilities"] = list(spec.capabilities)
    if spec.default_effort:
        table["default_effort"] = spec.default_effort
    return table


def build_config_tables(
    alias: str,
    ai_config: dict[str, Any] | None = None,
    *,
    context_limit: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Merge host policy with catalog models, preserving per-call selection."""
    host = _host_config_path(env_extra)
    try:
        config = tomllib.loads(host.read_text(encoding="utf-8")) if host.is_file() else {}
        env = {**os.environ, **(env_extra or {})}
        selected_alias = str(alias or env.get("KKAGENT_DEFAULT_MODEL") or "").strip()
        selected_alias = selected_alias or default_model_alias(ai_config)
        spec = resolve_model_spec(ai_config, selected_alias)
        if spec is None:
            raise ContextBudgetError("当前模型未在统一模型目录中启用。")
        for key in _HOST_MODEL_ROLE_KEYS:
            config.pop(key, None)
        if isinstance(config.get("goal"), dict):
            config["goal"].pop("judge_model", None)
        config["models"] = {spec.alias: _model_table(spec)}
        config["providers"] = {spec.provider: {
            "base_url": spec.base_url,
            "type": spec.api_format,
            **({"api_key_env": "GMS_MODEL_API_KEY"} if spec.api_key else {}),
        }}
        config["default_model"] = spec.alias
        if spec.api_key:
            config["_env"] = {"GMS_MODEL_API_KEY": spec.api_key}
        if context_limit is not None:
            selected = config["models"][config["default_model"]]
            ceiling = min(int(selected.get("max_context_size") or context_limit), context_limit)
            if ceiling < 2048:
                raise ContextBudgetError("当前模型配置缺失，或服务端上下文不足以执行分析。")
            selected["max_context_size"] = ceiling
            config.setdefault("loop_control", {}).update({
                "auto_compact": True,
                "reserved_context_size": min(4096, ceiling // 4),
                "compact_keep_last": 2,
                "compact_trigger_ratio": 0.70,
                "compact_block_ratio": 0.80,
                "token_counting": "measured+estimated",
            })
        return config
    except ContextBudgetError:
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        raise ContextBudgetError("无法读取当前 kkagent 配置以应用模型设置。") from None


def write_overlay_config(tables: dict[str, Any], directory: Path) -> tuple[Path, dict[str, str]]:
    """Write a private config, returning credentials separately for child env."""
    env = dict(tables.get("_env") or {})
    body_tables = {key: value for key, value in tables.items() if key != "_env"}
    path = directory / "config.toml"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(tomli_w.dumps(body_tables))
    return path, env


@contextmanager
def model_config(
    model: str, env_extra: dict[str, str], *, context_limit: int | None = None,
    ai_config: dict[str, Any] | None = None,
):
    """Keep config and env alive until the caller has reaped the process tree."""
    tables = build_config_tables(
        model, _config_manager.get_ai_config() if ai_config is None else ai_config,
        context_limit=context_limit, env_extra=env_extra,
    )
    with TemporaryDirectory(prefix="gms-kkagent-context-") as directory:
        yield write_overlay_config(tables, Path(directory))


@contextmanager
def context_limited_config(model: str, limit: int, env_extra: dict[str, str]):
    """Compatibility entry point yielding a path and scoped credential env."""
    with model_config(model, env_extra, context_limit=limit) as (path, config_env):
        original = dict(env_extra)
        env_extra.update(config_env)
        try:
            yield path
        finally:
            for key in config_env:
                if key in original:
                    env_extra[key] = original[key]
                else:
                    env_extra.pop(key, None)


def analysis_command(command: list[str], entry: dict[str, Any]) -> list[str]:
    path = entry.get("_kkagent_config_path")
    return [command[0], "--config", str(path), *command[1:]] if path else command


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_KKAGENT_CLIENT_RE = re.compile(r'^\s*client\s*=\s*["\']kkagent["\']\s*$', re.MULTILINE)


def normalize_agent_profile_name(value: Any) -> str:
    name = str(value or "").strip()
    return name if _PROFILE_NAME_RE.fullmatch(name) else ""


def list_agent_profiles(
    profiles_root: Path | None = None,
) -> dict[str, Any]:
    """List local kkagent profile names without reading token material into memory."""
    root = profiles_root or (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "gms-agent" / "profiles"
    )
    if not root.is_dir():
        return {"profiles": [], "default_profile": ""}
    profiles: list[str] = []
    for path in sorted(root.glob("*.toml")):
        name = path.stem
        if not _PROFILE_NAME_RE.fullmatch(name) or not path.is_file():
            continue
        try:
            # The profile's client declaration is enough for this selector.
            # Never parse or return controller, CA, or token-file fields.
            with path.open(encoding="utf-8", errors="replace") as stream:
                contents = stream.read(8192)
        except OSError:
            continue
        if _KKAGENT_CLIENT_RE.search(contents):
            profiles.append(name)
    return {
        "profiles": profiles,
        "default_profile": profiles[0] if len(profiles) == 1 else "",
    }
