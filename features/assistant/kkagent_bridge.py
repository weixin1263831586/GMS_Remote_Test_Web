"""kkagent 会话桥接层：对话 Agent ↔ kkagent headless 会话。

定位：kkagent 负责"理解、规划、诊断"——多轮自然语言澄清与只读取证；
实际执行始终由 Controller 工具层完成，设备工作流走 automation/cluster。这里复用
Redmine 模块已验证的 headless stream-json 子进程方式（不引入 ACP 依赖，
后续可平滑迁移）：

- ``kkagent -p <prompt> --output-format stream-json [--resume <sid>]``；
- 无 shell、无自动授权标志、继承宿主最小环境（剥离身份类 env）；
- 桥接会话 ID 与 WorkflowDraft / Automation Run 的关联通过
  ``link_payload`` 挂在 Agent 会话的 ``kkagent_bridge`` 字段上。

桥接失败返回明确错误类型和已接收到的会话 ID，供调用方有界重试。
运行时失败不得重放业务操作或改用另一套路由执行用户指令。
"""

from __future__ import annotations

import logging
import shutil
import signal
from dataclasses import dataclass, field
from typing import Any

from features.kkagent import child_env as _engine_child_env
from features.kkagent import run_headless_turn


logger = logging.getLogger(__name__)

KKAGENT_BINARY = "kkagent"
TURN_TIMEOUT_SECONDS = 120
STREAM_LINE_LIMIT_BYTES = 4 * 1024 * 1024

# 与 features/redmine/kkagent/process.py 同一原则（该模块属 redmine 内部，
# 此处按文档键位独立声明）：子进程不得继承宿主的 Agent 身份/凭据环境，
# 否则 MCP 会以错误 owner 取证。
_BRIDGE_STRIPPED_ENV_KEYS = frozenset({
    "GMS_RT_PROFILE",
    "GMS_AGENT_PROFILE",
    "GMS_AGENT_CLIENT",
    "GMS_REMOTE_TEST_SERVER",
    "GMS_AUTH_TOKEN_FILE",
    "GMS_AGENT_AUTH_MODE",
    "GMS_MCP_TOOLSETS",
})

_BRIDGE_PROMPT_PREFIX = (
    "你是 GMS 远程测试平台的规划助手。只做意图澄清、规划建议与只读诊断，"
    "不得直接执行烧写、AOA 或测试启动；实际执行由平台状态机完成。"
    "回答保持简洁中文。\n\n"
)


@dataclass
class BridgeTurnResult:
    """一次桥接回合的结果（文本 + 轨迹摘要）。"""

    ok: bool
    text: str = ""
    session_id: str = ""
    error: str = ""
    tools_used: list[str] = field(default_factory=list)
    error_code: str = ""


# 最近一回合的模型可用性（installed ≠ 模型可用：配置损坏 / Provider
# 不可用时仍会先进入 kkagent 路由再失败）。availability() 把它并入返回，
# UI 可区分「已安装」「模型可用」「当前回合失败」三态。
_last_turn_state: dict[str, str] = {"state": "unknown", "detail": "", "at": ""}


def _record_turn_state(ok: bool, detail: str = "", error_code: str = "") -> None:
    from datetime import datetime, timezone

    _last_turn_state.update({
        "state": "ready" if ok else "failed",
        "detail": "" if ok else (detail or error_code or "turn failed"),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def availability() -> dict[str, Any]:
    """kkagent 是否可用于桥接（存在性探测，不启动进程）。

    ``available`` 仅表示可执行文件存在；模型链路可用性看 ``model_state``
    （unknown=尚无回合证据 / ready=最近回合成功 / failed=最近回合失败）。
    """
    path = shutil.which(KKAGENT_BINARY)
    return {
        "available": bool(path),
        "installed": bool(path),
        "binary": path or "",
        "mode": "headless_stream_json",
        "model_state": _last_turn_state["state"],
        "model_detail": _last_turn_state["detail"],
        "model_checked_at": _last_turn_state["at"],
    }


def availability_snapshot() -> dict[str, Any]:
    """可用性快照：探测异常时返回显式不可用，路由层无须再兜底。"""
    try:
        return availability()
    except Exception:
        return {"available": False, "binary": "", "mode": "headless_stream_json"}


def child_env() -> dict[str, str]:
    """宿主环境减去身份类变量，供子进程显式注入。

    键位与引擎 :data:`features.kkagent.process.MCP_IDENTITY_ENV_KEYS`
    一致；本桥接的 env 已剥离身份，可直接作为引擎 ``child_env({})`` 的
    ``env_extra`` 空注入使用。
    """
    import os

    return {
        key: value
        for key, value in os.environ.items()
        if key not in _BRIDGE_STRIPPED_ENV_KEYS
    }


def link_payload(
    *,
    kkagent_session_id: str = "",
    draft_id: str = "",
    automation_run_id: str = "",
) -> dict[str, str]:
    """三向关联记录：挂到 Agent 会话 ``kkagent_bridge`` 字段。"""
    return {
        "kkagent_session_id": kkagent_session_id,
        "draft_id": draft_id,
        "automation_run_id": automation_run_id,
    }


def _signal_exit_detail(returncode: int) -> str:
    """把负退出码翻译成信号名与可行动提示（日志与用户消息共用）。"""
    if returncode >= 0:
        return f"kkagent 退出码 {returncode}"
    try:
        signal_name = signal.Signals(-returncode).name
    except ValueError:
        signal_name = f"SIG{-returncode}"
    if signal_name == "SIGINT":
        return ("kkagent 进程被 SIGINT 中断（通常是 Controller 正在重启或停止）。"
                "等服务恢复后点击重试继续本轮规划；若提示重试已过期，请重新描述目标。")
    return f"kkagent 进程被 {signal_name} 终止，已保留本轮进度，可重试。"


async def run_turn(
    message: str,
    *,
    session_id: str = "",
    context: str = "",
    timeout_seconds: int = TURN_TIMEOUT_SECONDS,
) -> BridgeTurnResult:
    """执行一次 kkagent 桥接回合（headless、只读、有界超时）。

    进程协议（spawn/stream-json/超时/信号退出分类/进程组回收）统一走
    ``features.kkagent.run_headless_turn``；本函数保留 prompt 组装、
    可用性预检与用户可行动文案。
    """
    if not availability()["available"]:
        return BridgeTurnResult(ok=False, error="kkagent 未安装，桥接不可用", error_code="unavailable")
    prompt = _BRIDGE_PROMPT_PREFIX
    if context:
        prompt += f"上下文：{context}\n\n"
    prompt += message

    result = await run_headless_turn(
        prompt,
        env=_engine_child_env(child_env()),
        session_id=session_id,
        timeout_seconds=timeout_seconds,
    )

    if result.ok:
        _record_turn_state(True)
        return BridgeTurnResult(
            ok=True, text=result.text, session_id=result.session_id,
            tools_used=result.tools_used,
        )

    error_messages = {
        "unavailable": ("kkagent 未安装", "kkagent 未安装，桥接不可用"),
        "timeout": ("回合超时", "kkagent 回合超时"),
        "internal": ("调用失败", "kkagent 调用失败，请检查模型连接或隔离策略"),
        "empty_response": ("未返回文本结果", "kkagent 未返回文本结果"),
    }
    detail, message = error_messages.get(
        result.error_code, ("调用失败", "kkagent 调用失败，请检查模型连接或隔离策略")
    )
    if result.error_code in ("signalled", "process_error"):
        # 负退出码 = 被信号终止。Controller 以 systemd 管理且
        # KillSignal=SIGINT，stop/restart 会向服务 cgroup 内全部进程
        # 广播（含 start_new_session 的桥接子进程），在途回合因此以
        # -2 结束。这不是模型失败：区分错误码并给出可行动文案。
        detail = _signal_exit_detail(result.returncode or 0)
        message = detail
    error_code = "bridge_error" if result.error_code == "internal" else result.error_code
    _record_turn_state(False, detail, error_code)
    return BridgeTurnResult(
        ok=False, session_id=result.session_id or session_id,
        error=message, tools_used=result.tools_used,
        error_code=error_code,
    )
