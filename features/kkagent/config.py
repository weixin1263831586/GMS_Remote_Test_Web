"""Private kkagent configs from the unified model catalog (ADR 0034).

Host configuration contributes identity and policy settings. Model tables come
from ai_models, with a temporary, explicitly logged host-model fallback.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import tomli_w


try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from features.kkagent.registry import ModelSpec, default_model_alias, resolve_model_spec
from foundation.config import config_manager as _config_manager


logger = logging.getLogger(__name__)

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
        if spec:
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
        else:
            selected_alias = selected_alias or config.get("default_model") or ""
            selected = config.get("models", {}).get(selected_alias)
            if not isinstance(selected, dict):
                raise ContextBudgetError("当前模型配置缺失，或服务端上下文不足以执行分析。")
            config["default_model"] = selected_alias
            logger.warning("模型目录中没有 alias=%r，回退宿主配置 %s", selected_alias, host)
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
def model_config(model: str, env_extra: dict[str, str], *, context_limit: int | None = None):
    """Keep config and env alive until the caller has reaped the process tree."""
    tables = build_config_tables(
        model, _config_manager.get_ai_config(),
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
