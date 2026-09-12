"""Durable dispatch helpers for Web-triggered Daily Brief work."""

from __future__ import annotations

from typing import Any

from .daily_brief_service import DailyBriefService


def enqueue_run(
    service: DailyBriefService, *, mode: str = "manual", force: bool = False
) -> dict[str, Any]:
    started = service.start_run(mode=mode, force=force)
    if "run_id" not in started or started.get("reused") or started.get("already_running"):
        return started
    job, created = service.repository.enqueue_job(started["run_id"], kind="run")
    return {
        "run_id": started["run_id"],
        "job_id": job["job_id"],
        "status": "pending",
        "queued": created,
    }


def enqueue_refresh(service: DailyBriefService, brief_date: str) -> dict[str, Any]:
    started = service.start_refresh(brief_date)
    if "run_id" not in started or started.get("already_running"):
        return started
    job, created = service.repository.enqueue_job(started["run_id"], kind="run")
    return {
        "run_id": started["run_id"],
        "job_id": job["job_id"],
        "status": "pending",
        "queued": created,
    }


def enqueue_reanalysis(
    service: DailyBriefService, brief_date: str, issue_id: int
) -> dict[str, Any]:
    run = service.latest_run(brief_date)
    if run is None:
        return {"error": f"no daily brief run for {brief_date}"}
    record = service.repository.get_issue(run.run_id, issue_id)
    if record is None:
        return {"error": f"issue {issue_id} not in run {run.run_id}"}
    if run.status not in ("completed", "partial", "failed"):
        return {"error": f"run {run.run_id} is still executing; retry after it finishes"}
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


__all__ = ["enqueue_reanalysis", "enqueue_refresh", "enqueue_run"]
