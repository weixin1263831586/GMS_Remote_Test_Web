"""kkagent 会话启动前的 gms MCP 健康前置检查（快速失败）。

#653167 nightly 复盘：kkagent 的 gms MCP server 未连接（插件 payload 损坏）
时，模型全程用 Bash 降级跑 gms-rt CLI 取证，runtime evidence gate 按 MCP
工具名判失败，两轮 resume 修复也无法恢复——单条分析烧掉 9 分钟与 40 万
tokens，最终以难诊断的 evidence_gate_failed 收场。

本模块在启动 kkagent 子进程之前用 ``gms-agent doctor --client kkagent
--json``（项目规定的唯一部署验收路径，覆盖 skill/profile/token/MCP 注册）
探活；探活失败直接返回可操作的恢复步骤，不再进入 LLM 会话。

仅在显式绑定 agent profile（env 有 GMS_RT_PROFILE）时启用：未绑定 profile
的调用方（单元测试、无认证部署形态）保持原有行为，不新增部署要求。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field

from .process import child_env, settle_reader_future, terminate_process_tree
from .trace import ToolTrace


DOCTOR_TIMEOUT_SECONDS = 45.0
GMS_AGENT_BINARY = "gms-agent"
GMS_AGENT_RUNTIME_FALLBACK = os.path.expanduser(
    "~/.local/share/gms-remote-test/current/bin/gms-agent"
)


def doctor_command() -> list[str] | None:
    """定位 gms-agent CLI；与 auth_preflight 相同的"正典安装优先"策略。"""
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
        f"修复：先在仓库内运行 python tools/scripts/agent/sync_package.py . 同步插件"
        f" payload，再执行 {install}；详情见 gms-agent doctor --client"
        " kkagent --json。"
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
    "McpHealthProbe",
    "doctor_command",
    "parse_doctor_payload",
    "probe_kkagent_mcp_health",
]
