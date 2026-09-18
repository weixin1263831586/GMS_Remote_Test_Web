"""单条分析的确定性取证预采集编排（从 DailyBriefService 拆出）。

深度诊断（issue: 模式）采集 Redmine 三件套 + 设备快照基线；晨报批量
triage 同样预采集 Redmine 基线（#653167 nightly 复盘：triage 无兜底时，
模型取证失败直接变成 gate 失败，没有任何确定性证据可用），只是不含设备
取证——triage 的证据门禁没有设备维度。
"""

from __future__ import annotations

from typing import Any

from . import daily_brief_cancellation as cancellation
from .kkagent.evidence_preflight import collect_deep_analysis_evidence


async def precollect_deep_evidence(
    *, repository: Any, run: Any, issue_id: int,
    analyzer: Any, entry: dict[str, Any],
) -> None:
    """Deterministic read-only evidence baseline for one analysis.

    成功时写入 ``entry["_precollected_tool_traces"]`` 与
    ``entry["_evidence_preflight"]``（模型可见的持久化溯源，原样输出不落
    库）。取消标志在每次 CLI 尝试与退避间隙被轮询；preflight 期间被请求
    停止时抛 ``RunCancelledError``，不再启动 kkagent 子进程。

    未绑定 agent profile 时跳过：认证预检已按 fail-closed 拦截该配置，
    preflight 不承担重复报错职责。triage 只收 Redmine 基线
    （``include_device=False``），设备取证仍是深度诊断专属。

    ``entry["_progress_recorder"]`` 存在时，preflight 每步 CLI 调用同步
    写入实时进度时间线（tool_started / tool_completed / tool_failed），
    让「查看分析」弹框覆盖 Controller 证据预采集阶段而不只 kkagent。
    """
    evidence_env = dict(getattr(analyzer, "env_extra", {}) or {})
    if not str(evidence_env.get("GMS_RT_PROFILE") or "").strip():
        return
    triage = entry.get("analysis_mode") == "triage"
    progress = entry.get("_progress_recorder")
    if progress is not None:
        progress.stage_changed("正在执行 Controller 证据预采集（Redmine/设备快照）")
    preflight = await collect_deep_analysis_evidence(
        issue_id=issue_id,
        device_serial="" if triage else str(entry.get("device_serial") or ""),
        include_device=not triage,
        env_extra=evidence_env,
        should_cancel=lambda: repository.is_cancel_requested(run.run_id),
        on_tool_event=(
            lambda tool_name, tool_input, phase, ok: _emit_preflight_progress(
                progress, tool_name, tool_input, phase, ok,
            )
        ) if progress is not None else None,
    )
    entry["_precollected_tool_traces"] = preflight.traces
    entry["_evidence_preflight"] = preflight.prompt_context()
    if repository.is_cancel_requested(run.run_id):
        raise cancellation.RunCancelledError()


def _emit_preflight_progress(
    progress: Any, tool_name: str, tool_input: Any, phase: str, ok: bool,
) -> None:
    if progress is None:
        return
    if phase == "started":
        progress.tool_started(tool_name, tool_input, stage="preflight")
        return
    progress.tool_finished(tool_name, None, ok=ok, stage="preflight")


__all__ = ["precollect_deep_evidence"]
