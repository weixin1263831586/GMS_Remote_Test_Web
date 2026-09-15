"""Durable dispatch helpers for Web-triggered Daily Brief work.

start_run / start_refresh / reanalyze 现在都在服务层完成原子入队
（run+job 同一事务）；本模块只做参数适配与响应整形，保持既有 API
返回结构（run_id/job_id/queued）不变。
"""

from __future__ import annotations

from typing import Any

from .daily_brief_service import DailyBriefService


def enqueue_run(
    service: DailyBriefService, *, mode: str = "manual", force: bool = False
) -> dict[str, Any]:
    started = service.start_run(mode=mode, force=force)
    return _with_job_fields(service, started)


def enqueue_refresh(service: DailyBriefService, brief_date: str) -> dict[str, Any]:
    started = service.start_refresh(brief_date)
    return _with_job_fields(service, started)


def enqueue_reanalysis(
    service: DailyBriefService, brief_date: str, issue_id: int,
    run_id: str | None = None,
) -> dict[str, Any]:
    """单 issue 重新分析入队。

    审核意见 P2：优先绑定显式 run_id 精确操作；未提供时才回落
    该日期最新 run（旧 API 兼容）。
    """
    if run_id:
        run = service.repository.get_run(run_id)
        if run is None or run.owner_id != service.owner_id:
            return {"error": f"daily brief run not found: {run_id}"}
    else:
        run = service.latest_run(brief_date)
        if run is None:
            return {"error": f"no daily brief run for {brief_date}"}
    record = service.repository.get_issue(run.run_id, issue_id)
    if record is None:
        return {"error": f"issue {issue_id} not in run {run.run_id}"}
    if run.status not in ("completed", "partial", "failed", "cancelled"):
        return {"error": f"run {run.run_id} is still executing; retry after it finishes", "code": "STATE_CONFLICT"}
    job, created = service.repository.enqueue_job(
        run.run_id, kind="issue", issue_id=issue_id
    )
    return {
        "run_id": run.run_id,
        "job_id": job["job_id"],
        "issue_id": issue_id,
        "status": "pending",
        "queued": created,
    }


def _with_job_fields(service: DailyBriefService, started: dict[str, Any]) -> dict[str, Any]:
    """start_run/start_refresh 的结果补齐 job_id/queued 字段（已有则透传）。"""
    if "run_id" not in started:
        return started
    if "job_id" in started:
        return started
    if started.get("reused") or started.get("already_running"):
        return started
    job = service.repository.get_active_run_job(started["run_id"])
    return {
        **started,
        "job_id": (job or {}).get("job_id", ""),
        "queued": bool(started.get("queued", True)),
    }


__all__ = ["enqueue_reanalysis", "enqueue_refresh", "enqueue_run"]
