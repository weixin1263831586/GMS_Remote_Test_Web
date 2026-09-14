"""Daily Brief 配置契约与 kkagent 分析器构建（从 service 拆出）。"""

from __future__ import annotations

import re
from typing import Any

from .daily_brief_snapshot import DEFAULT_LIST_LIMIT, DEFAULT_STALE_DAYS
from .kkagent import KkAgentRedmineAnalyzer


DEFAULT_BRIEF_CONFIG: dict[str, Any] = {
    # opt-in：晨报会消耗 kkagent 分析资源，默认关闭，owner 在设置里显式
    # 开启后才进入 nightly 调度（不得默认启用）。
    "enabled": False,
    # 当前唯一实现的分析后端。历史上允许 "direct" 但从未实现，已从枚举
    # 移除；旧配置里的 "direct" 会被规范化回 "kkagent"。
    "analysis_backend": "kkagent",
    "model": "",
    # 绑定到该 owner 的本机 kkagent agent profile 名（~/.config/gms-agent/
    # profiles/<name>.toml）。为空则 preflight fail-closed 拦截（MCP 取证
    # 是强制步骤），不回退他人凭据。
    "agent_profile": "",
    # 健全经验值：完整跑一轮 fetch+journals+history_search+综合，12 步常在
    # 证据链较长时不够（issue #646220 实测烧满后以 max_turns 中断）；
    # 20 步覆盖典型分析，仍可按 owner 在设置里调 1-50。
    "max_turns": 20,
    "issue_timeout_seconds": 600,
    "max_parallel_issues": 1,
    "max_issues": 50,
    "stale_days": DEFAULT_STALE_DAYS,
    "list_limit": DEFAULT_LIST_LIMIT,
}
RUNTIME_CONFIG_KEY = "redmine_daily_brief"


def analyzer_env_extra(profile: Any) -> dict[str, str]:
    """kkagent 子进程的 MCP 身份环境；未绑定 profile 时返回空。"""
    name = str(profile or "").strip()
    if not name:
        return {}
    return {
        "GMS_RT_PROFILE": name,
        "GMS_AGENT_CLIENT": "kkagent",
        "GMS_AGENT_AUTH_MODE": "service-token",
    }


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
    backend = str(payload.get("analysis_backend") or config["analysis_backend"]).strip()
    config["analysis_backend"] = backend if backend in ("kkagent",) else "kkagent"
    config["model"] = str(payload.get("model") or "").strip()
    # profile 名只允许安全字符，避免注入 env / 路径。
    profile = str(payload.get("agent_profile") or "").strip()
    config["agent_profile"] = profile if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile) else ""
    _int("max_turns", 1, 50)
    _int("issue_timeout_seconds", 60, 3600)
    _int("max_parallel_issues", 1, 4)
    _int("max_issues", 1, 200)
    _int("stale_days", 1, 30)
    _int("list_limit", 1, 100)
    return config


def build_brief_analyzer(config: dict[str, Any]) -> KkAgentRedmineAnalyzer:
    """按 owner 配置构建分析器（evidence-only toolset 在 analyzer 内注入）。"""
    return KkAgentRedmineAnalyzer(
        max_turns=int(config.get("max_turns") or DEFAULT_BRIEF_CONFIG["max_turns"]),
        timeout_seconds=int(config.get("issue_timeout_seconds") or 600),
        model=str(config.get("model") or ""),
        env_extra=analyzer_env_extra(config.get("agent_profile")),
    )


__all__ = [
    "DEFAULT_BRIEF_CONFIG",
    "RUNTIME_CONFIG_KEY",
    "analyzer_env_extra",
    "build_brief_analyzer",
    "normalize_daily_brief_config",
]
