"""Daily Brief 配置契约与 kkagent 分析器构建（从 service 拆出）。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from foundation.config import config_manager

from .daily_brief_snapshot import DEFAULT_LIST_LIMIT, DEFAULT_STALE_DAYS
from .kkagent import KkAgentRedmineAnalyzer


DEFAULT_BRIEF_CONFIG: dict[str, Any] = {
    # opt-in：晨报会消耗 kkagent 分析资源，默认关闭，owner 在设置里显式
    # 开启后才进入 nightly 调度（不得默认启用）。
    "enabled": False,
    # 每个 owner 的夜间全量晨报触发时间（Controller 所在主机本地时间）。
    # 固定为 HH:MM，供 systemd 的每分钟调度入口按 owner 筛选。
    "trigger_time": "00:00",
    # 当前唯一实现的分析后端。历史上允许 "direct" 但从未实现，已从枚举
    # 移除；旧配置里的 "direct" 会被规范化回 "kkagent"。
    "analysis_backend": "kkagent",
    "model": "",
    # 绑定到该 owner 的本机 kkagent agent profile 名（~/.config/gms-agent/
    # profiles/<name>.toml）。为空则 preflight fail-closed 拦截（MCP 取证
    # 是强制步骤），不回退他人凭据。
    "agent_profile": "",
    # 可选绑定的只读诊断设备 serial：非空时分析进程额外获得
    # device_evidence toolset（snapshot/logcat/只读 shell），prompt 会
    # 告知模型该设备可用于运行时佐证。空 = 不做设备实证。
    "device_serial": "",
    # Legacy configuration keys remain readable; analysis has no hard budget.
    "max_turns": 0,
    "issue_timeout_seconds": 0,
    "max_parallel_issues": 1,
    "max_issues": 50,
    "stale_days": DEFAULT_STALE_DAYS,
    "list_limit": DEFAULT_LIST_LIMIT,
}
RUNTIME_CONFIG_KEY = "redmine_daily_brief"
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_KKAGENT_CLIENT_RE = re.compile(r'^\s*client\s*=\s*["\']kkagent["\']\s*$', re.MULTILINE)
_DEVICE_SERIAL_RE = re.compile(r"^[A-Za-z0-9:._-]{2,64}$")
_TRIGGER_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# Compatibility export; unbudgeted analysis does not need additional rounds.
TEST_FAILURE_EXTRA_TURNS = 0
# kkagent's global thinking configuration is shared by interactive sessions and
# Daily Brief.  Some OpenAI-compatible local gateways deliberately reject the
# generic ``high`` spelling; set a process-local compatible value instead of
# mutating the operator's ~/.kkagent/config.toml.
MODEL_THINKING_EFFORTS = {
    "glm-5.3-flash": "xhigh",
}


def list_daily_brief_model_options(
    ai_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List enabled system models without exposing provider credentials or URLs."""
    config = ai_config if ai_config is not None else config_manager.get_ai_config()
    if not isinstance(config, dict) or not config.get("enabled", False):
        return {"models": [], "default_model": ""}
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return {"models": [], "default_model": ""}

    primary_provider = str(config.get("primary_provider") or "").strip()
    provider_names = list(providers)
    if primary_provider in providers:
        provider_names.remove(primary_provider)
        provider_names.insert(0, primary_provider)

    models: list[dict[str, str]] = []
    seen_models: set[str] = set()
    default_model = ""
    for provider_name in provider_names:
        provider = providers.get(provider_name)
        if not isinstance(provider, dict) or not provider.get("enabled", False):
            continue
        model = str(provider.get("model") or "").strip()
        if not model:
            continue
        if provider_name == primary_provider:
            default_model = model
        if model in seen_models:
            continue
        seen_models.add(model)
        display_name = str(provider.get("display_name") or model).strip() or model
        models.append({
            "model": model,
            "display_name": display_name,
            "provider": str(provider_name),
        })
    if not default_model and models:
        default_model = models[0]["model"]
    return {"models": models, "default_model": default_model}


def list_daily_brief_agent_profiles(
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
            contents = path.read_text(encoding="utf-8", errors="replace")[:8192]
        except OSError:
            continue
        if _KKAGENT_CLIENT_RE.search(contents):
            profiles.append(name)
    return {
        "profiles": profiles,
        "default_profile": profiles[0] if len(profiles) == 1 else "",
    }


def analyzer_env_extra(
    profile: Any, device_serial: str = "", model: str = ""
) -> dict[str, str]:
    """kkagent 子进程的 MCP 身份环境；未绑定 profile 时返回空。

    ``device_serial`` 非空时额外放行 device_evidence toolset（只读设备
    实证）， analyzer 对已存在的 GMS_MCP_TOOLSETS 不覆盖。
    """
    name = str(profile or "").strip()
    if not name:
        return {}
    env = {
        "GMS_RT_PROFILE": name,
        "GMS_AGENT_CLIENT": "kkagent",
        "GMS_AGENT_AUTH_MODE": "service-token",
    }
    if str(device_serial or "").strip():
        env["GMS_MCP_TOOLSETS"] = "evidence,device_evidence"
    effort = MODEL_THINKING_EFFORTS.get(str(model or "").strip())
    if effort:
        env["KKAGENT_THINKING_EFFORT"] = effort
    return env


def normalize_daily_brief_config(payload: dict[str, Any] | None) -> dict[str, Any]:
    """规范化配置；非法值回落默认，未知键由 API 层拒绝。"""
    payload = payload or {}
    config = dict(DEFAULT_BRIEF_CONFIG)

    def _int(key: str, lo: int, hi: int) -> None:
        try:
            config[key] = max(lo, min(hi, int(payload.get(key, config[key]))))
        except (TypeError, ValueError):
            pass

    enabled = payload.get("enabled", config["enabled"])
    if isinstance(enabled, bool):
        config["enabled"] = enabled
    elif isinstance(enabled, str):
        normalized = enabled.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            config["enabled"] = True
        elif normalized in ("0", "false", "no", "off"):
            config["enabled"] = False
    trigger_time = str(payload.get("trigger_time") or config["trigger_time"]).strip()
    config["trigger_time"] = (
        trigger_time if _TRIGGER_TIME_RE.fullmatch(trigger_time)
        else DEFAULT_BRIEF_CONFIG["trigger_time"]
    )
    backend = str(payload.get("analysis_backend") or config["analysis_backend"]).strip()
    config["analysis_backend"] = backend if backend in ("kkagent",) else "kkagent"
    config["model"] = str(payload.get("model") or "").strip()
    # profile 名只允许安全字符，避免注入 env / 路径。
    profile = str(payload.get("agent_profile") or "").strip()
    config["agent_profile"] = profile if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile) else ""
    # 设备 serial 出现在 prompt 与 MCP 调用参数里，只允许安全字符。
    serial = str(payload.get("device_serial") or "").strip()
    config["device_serial"] = serial if _DEVICE_SERIAL_RE.fullmatch(serial) else ""
    _int("max_parallel_issues", 1, 4)
    _int("max_issues", 1, 200)
    _int("stale_days", 1, 30)
    _int("list_limit", 1, 100)
    return config


def build_brief_analyzer(
    config: dict[str, Any], *, extra_turns: int = 0
) -> KkAgentRedmineAnalyzer:
    """Build unbudgeted analysis; legacy budgets cannot truncate evidence gathering."""
    return KkAgentRedmineAnalyzer(
        max_turns=0,
        timeout_seconds=0,
        model=str(config.get("model") or ""),
        env_extra=analyzer_env_extra(
            config.get("agent_profile"), config.get("device_serial"), config.get("model")
        ),
    )


__all__ = [
    "DEFAULT_BRIEF_CONFIG",
    "RUNTIME_CONFIG_KEY",
    "TEST_FAILURE_EXTRA_TURNS",
    "analyzer_env_extra",
    "build_brief_analyzer",
    "list_daily_brief_agent_profiles",
    "list_daily_brief_model_options",
    "normalize_daily_brief_config",
]
