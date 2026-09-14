"""Daily Brief 认证预检：agent token 失效时整 run 快速失败（fail-closed）。

GMS agent token 被吊销/过期时，headless 分析会把整轮 turn 预算消耗在
MCP 认证失败上；必须在 run 开始前用 selfcheck 快速失败并给出重注册指引。
必须走正典安装路径而非 PATH 上的 gms-rt-* 包装器：包装器内嵌安装时的
绝对路径，若安装发生在 mktemp 目录会整体失效。

Daily Brief 的 prompt 把 Redmine 核对 / 历史检索列为强制步骤——身份没有
确认时不存在"可靠晨报"。因此与旧版不同：selfcheck 不可用 / 超时也按
fail-closed 处理，返回 analysis_unavailable 而不是放行让模型凭上下文猜。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Any

from .process import child_env, settle_reader_future, terminate_process_tree


GMS_SELFCHECK_SCRIPT = os.path.expanduser(
    "~/.local/share/gms-remote-test/current/scripts/gms-remote-test.sh"
)
GMS_SELFCHECK_BINARY = "gms-rt-system-selfcheck"
SELFCHECK_TIMEOUT_SECONDS = 30.0


def selfcheck_command() -> list[str] | None:
    if os.path.isfile(GMS_SELFCHECK_SCRIPT):
        return ["bash", GMS_SELFCHECK_SCRIPT, "gms-rt-system-selfcheck", "--json"]
    if shutil.which(GMS_SELFCHECK_BINARY):
        return [GMS_SELFCHECK_BINARY, "--json"]
    return None


def _blocked_reason(profile: str, token_file: str) -> str:
    return (
        f"GMS agent token 失效或身份预检不可用（profile '{profile}'"
        + (f"，{token_file}" if token_file else "")
        + "）。Daily Brief 依赖 Redmine 只读取证，未确认身份时不生成报告。"
        "请管理员在 Web UI 重新签发注册码，并在本机执行: "
        "gms-rt-agent-enroll <CODE>"
    )


def parse_selfcheck_payload(
    stdout: bytes, env_extra: dict[str, str]
) -> tuple[bool, str]:
    """解析 selfcheck --json 输出 → (通过, 拦截原因)。"""
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False, _blocked_reason(str(env_extra.get("GMS_RT_PROFILE") or ""), "")
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        return False, _blocked_reason(str(env_extra.get("GMS_RT_PROFILE") or ""), "")
    auth = data.get("auth")
    status = auth.get("status") if isinstance(auth, dict) else None
    if not isinstance(status, dict):
        # selfcheck --json 的 auth.status 是嵌套结构；宽容兼容直接挂在
        # data 上的扁平 authenticated 字段。
        status = data if isinstance(data.get("authenticated"), bool) else None
    if status is None or status.get("authenticated") is not True:
        profile = str(data.get("profile") or env_extra.get("GMS_RT_PROFILE") or "")
        credential = data.get("credential")
        token_file = (
            str(credential.get("token_file") or "")
            if isinstance(credential, dict) else ""
        )
        return False, _blocked_reason(profile, token_file)
    return True, ""


async def preflight_gms_auth(
    env_extra: dict[str, str],
    *,
    timeout_seconds: float = SELFCHECK_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """分析前校验 GMS agent token；返回 (通过, 拦截原因)。

    fail-closed 语义（Daily Brief 证据链要求）：
    - 未配置 agent_profile → 拦截（MCP 取证是强制步骤，无身份即不可靠）；
    - selfcheck 不可用 / 超时 / 输出非法 → 拦截（明确提示
      analysis_unavailable 原因，不静默放行烧 turn 预算）；
    - selfcheck 明确 authenticated=false → 拦截并给出重注册指引。
    """
    if not str(env_extra.get("GMS_RT_PROFILE") or "").strip():
        return (
            False,
            "晨报未绑定 GMS agent profile：无法进行 Redmine 只读取证，"
            "请在晨报设置中配置 agent_profile 后重试（analysis_unavailable）。",
        )
    command = selfcheck_command()
    if command is None:
        return False, _blocked_reason(str(env_extra.get("GMS_RT_PROFILE")), "")
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Future[tuple[bytes, bytes]] | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=child_env(env_extra),
            start_new_session=os.name == "posix",
        )
        communication = asyncio.ensure_future(process.communicate())
        stdout, _ = await asyncio.wait_for(
            asyncio.shield(communication), timeout=timeout_seconds
        )
    except OSError:
        return False, _blocked_reason(str(env_extra.get("GMS_RT_PROFILE")), "")
    except asyncio.TimeoutError:
        if process is not None:
            await terminate_process_tree(process)
        if communication is not None:
            await settle_reader_future(communication)
        return (
            False,
            "GMS 身份预检超时（analysis_unavailable）：无法确认 MCP 取证"
            "身份，拒绝在未验证证据的情况下生成晨报。",
        )
    except asyncio.CancelledError:
        if process is not None:
            cleanup = asyncio.create_task(terminate_process_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        if communication is not None:
            await settle_reader_future(communication)
        raise
    return parse_selfcheck_payload(stdout, env_extra)


def mandatory_tools_ready(trace_tools: list[str]) -> tuple[bool, list[str]]:
    """校验一次分析的轨迹里是否出现过强制取证工具（供 Gate 复用）。"""
    missing: list[str] = []
    if not any("redmine_issue_fetch" in name or "redmine_issue" in name
               for name in trace_tools):
        missing.append("gms_rt_redmine_issue_fetch")
    if not any("redmine_journals" in name for name in trace_tools):
        missing.append("gms_rt_redmine_journals")
    return (not missing), missing


def capability_summary(payload: dict[str, Any]) -> str:
    """selfcheck data 的简短摘要，用于失败信息（不展开全文）。"""
    profile = str(payload.get("profile") or "")
    return f"profile={profile}" if profile else "selfcheck"


__all__ = [
    "GMS_SELFCHECK_BINARY",
    "GMS_SELFCHECK_SCRIPT",
    "SELFCHECK_TIMEOUT_SECONDS",
    "capability_summary",
    "mandatory_tools_ready",
    "parse_selfcheck_payload",
    "preflight_gms_auth",
    "selfcheck_command",
]
