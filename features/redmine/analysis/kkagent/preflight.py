"""Read-only identity and MCP readiness probes before Redmine analysis.

Authentication selfcheck fails closed without a verified agent identity;
MCP doctor is skipped only for callers without an explicit agent profile.
Both probes preserve process cleanup on timeout and cancellation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field

from features.kkagent import child_env, settle_reader_future, terminate_process_tree

from .trace import ToolTrace


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


DOCTOR_TIMEOUT_SECONDS = 45.0
GMS_AGENT_BINARY = "gms-agent"
GMS_AGENT_RUNTIME_FALLBACK = os.path.expanduser(
    "~/.local/share/gms-remote-test/current/bin/gms-agent"
)


def doctor_command() -> list[str] | None:
    """定位 gms-agent CLI；与身份预检相同的"正典安装优先"策略。"""
    exe = shutil.which(GMS_AGENT_BINARY)
    if not exe and os.path.isfile(GMS_AGENT_RUNTIME_FALLBACK) and os.access(
        GMS_AGENT_RUNTIME_FALLBACK, os.X_OK
    ):
        exe = GMS_AGENT_RUNTIME_FALLBACK
    if not exe:
        return None
    return [exe, "doctor", "--client", "kkagent", "--json"]


def _repair_hint(profile: str) -> str:
    install = "gms-agent install --client kkagent" + (
        f" --profile {profile}" if profile else ""
    )
    return (
        "修复：先执行 gms-agent update，再执行 "
        f"{install} 重新绑定 current runtime；仓库开发环境还需运行 "
        "python tools/scripts/agent/sync_package.py . 同步生成包；详情见 "
        "gms-agent doctor --client kkagent --json。"
    )


def parse_doctor_payload(stdout: bytes) -> tuple[bool, str]:
    """解析 doctor --json 输出 → (通过, 拦截原因)。"""
    try:
        parsed = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        tail = stdout.decode("utf-8", errors="replace").strip()[-200:]
        return False, f"gms-agent doctor 输出不是合法 JSON：{tail}"
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        actions = ""
        if isinstance(parsed, dict):
            actions = "; ".join(
                str(action) for action in (parsed.get("actions") or [])
            )
        return (
            False,
            "gms-agent doctor 报告部署异常"
            + (f"：{actions}" if actions else "")
            + "。" + _repair_hint(""),
        )
    clients = [
        entry
        for entry in (parsed.get("clients") or [])
        if isinstance(entry, dict)
    ]
    client = next(
        (entry for entry in clients if entry.get("client") == "kkagent"), None
    )
    if client is None:
        return (
            False,
            "gms MCP 健康预检未通过：doctor 未包含 kkagent 客户端（gms 插件"
            "未安装到 kkagent）。" + _repair_hint(""),
        )
    problems: list[str] = []
    if client.get("skill_present") is not True:
        problems.append("gms skill 未安装")
    profile = client.get("profile") if isinstance(client.get("profile"), dict) else {}
    if profile.get("valid") is not True:
        problems.append(
            f"agent profile 无效（count={profile.get('count')}，fail-closed"
            " 要求恰好一个可选 profile）"
        )
    token = client.get("token") if isinstance(client.get("token"), dict) else {}
    if token.get("present") is not True:
        problems.append("agent token 缺失")
    elif token.get("mode_ok") is not True or token.get("owner_ok") is not True:
        problems.append("agent token 文件权限/属主异常（应为 0600 且属主正确）")
    mcp = client.get("mcp") if isinstance(client.get("mcp"), dict) else {}
    if mcp.get("registered") is not True:
        problems.append("gms MCP server 未注册到 kkagent 的 config.toml")
    elif mcp.get("launchable") is not True:
        # #654649 nightly 复盘：config.toml 注册块残留但插件 payload 被清空
        # 时，doctor 的静态注册检查误报 healthy，kkagent 会话内 gms MCP
        # server 起不来，整批晨报分析以 evidence_gate_failed 收场。新版
        # doctor 会检查注册指向的 mcp_launcher.py 是否真实存在。
        launcher = str(mcp.get("launcher_path") or "").strip()
        problems.append(
            "gms MCP 启动脚本缺失或 doctor 版本过旧，无法确认可启动性："
            + (launcher or "已注册的 mcp_launcher.py")
        )
    if problems:
        return (
            False,
            "gms MCP 健康预检未通过：" + "；".join(problems)
            + "。" + _repair_hint(str(profile.get("selected") or "")),
        )
    return True, ""


@dataclass
class McpHealthProbe:
    """一次 doctor 探活的结果（含可入库的 ToolTrace 溯源）。"""

    ok: bool
    reason: str = ""
    command: list[str] = field(default_factory=list)
    output_sha256: str = ""
    output_bytes: int = 0
    skipped: bool = False

    def tool_trace(self) -> ToolTrace:
        preview = (
            "controller mcp doctor probe succeeded"
            if self.ok
            else (self.reason[:200] or "controller mcp doctor probe failed")
        )
        return ToolTrace(
            tool_call_id="preflight:mcp_doctor:" + hashlib.sha256(
                json.dumps(self.command, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16],
            tool_name="mcp_doctor",
            tool_input={"--client": "kkagent"},
            status="succeeded" if self.ok else "failed",
            output_sha256=self.output_sha256,
            output_bytes=self.output_bytes,
            output_preview=preview,
            failure_kind="" if self.ok else "mcp_unavailable",
        )


async def probe_kkagent_mcp_health(
    env_extra: dict[str, str],
    *,
    timeout_seconds: float = DOCTOR_TIMEOUT_SECONDS,
) -> McpHealthProbe:
    """kkagent 启动前的只读探活；失败即快速失败，不烧 LLM 轮次。

    - env 未绑定 GMS_RT_PROFILE → skipped（不新增部署要求，老行为）；
    - gms-agent CLI 不存在 / 启动失败 / 超时 / 输出非法 / 任一健康位未过
      → ok=False 且 reason 带恢复步骤。
    """
    if not str(env_extra.get("GMS_RT_PROFILE") or "").strip():
        return McpHealthProbe(ok=True, skipped=True)
    command = doctor_command()
    if command is None:
        return McpHealthProbe(
            ok=False,
            reason=(
                "gms-agent CLI 不存在：agent 包未安装，kkagent 的 gms MCP "
                "server 必然不可用。请按 AGENT_PLAYBOOK 安装 agent 包后重试。"
            ),
        )
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
    except OSError as exc:
        return McpHealthProbe(
            ok=False,
            reason=f"gms-agent doctor 启动失败：{exc}",
            command=command,
        )
    except asyncio.TimeoutError:
        if process is not None:
            await terminate_process_tree(process)
        if communication is not None:
            await settle_reader_future(communication)
        return McpHealthProbe(
            ok=False,
            reason=(
                f"gms-agent doctor 探活超时（{timeout_seconds:g}s）：本机 "
                "gms agent 运行时无响应，kkagent 会话大概率同样挂起。"
            ),
            command=command,
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
    ok, reason = parse_doctor_payload(stdout)
    return McpHealthProbe(
        ok=ok,
        reason=reason,
        command=command,
        output_sha256=hashlib.sha256(stdout).hexdigest(),
        output_bytes=len(stdout),
    )


__all__ = [
    "DOCTOR_TIMEOUT_SECONDS",
    "GMS_SELFCHECK_BINARY",
    "GMS_SELFCHECK_SCRIPT",
    "SELFCHECK_TIMEOUT_SECONDS",
    "McpHealthProbe",
    "doctor_command",
    "parse_doctor_payload",
    "parse_selfcheck_payload",
    "preflight_gms_auth",
    "probe_kkagent_mcp_health",
    "selfcheck_command",
]
