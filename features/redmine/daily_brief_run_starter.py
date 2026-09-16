"""Creation and durable enqueue of daily brief runs."""

from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun
from .daily_brief_owner_policy import (
    ADMIN_OWNER_MESSAGE,
    is_daily_brief_owner_eligible,
)
from .daily_brief_repository import new_run_id
from .daily_brief_snapshot import brief_date_today
from .kkagent_analyzer import PROMPT_VERSION
from .users import _now


class DailyBriefRunStarterMixin:
    def _owner_is_eligible(self) -> bool:
        return is_daily_brief_owner_eligible(self.owner_id)

    def start_issue_analysis(self, issue_id: int) -> dict[str, Any]:
        """Queue only the requested issue, independently of workload scans and nightly runs."""
        if not self._owner_is_eligible():
            return {"error": ADMIN_OWNER_MESSAGE, "code": "ADMIN_OWNER_FORBIDDEN"}
        run = DailyBriefRun(
            owner_id=self.owner_id, brief_date=brief_date_today(),
            mode=f"issue:{issue_id}", run_id=new_run_id(),
            started_at=_now(), prompt_version=PROMPT_VERSION, issue_count=1,
        )
        issue = DailyBriefIssue(run_id=run.run_id, issue_id=issue_id, buckets=[], subject=f"#{issue_id}")
        _created, job, queued = self.repository.create_run_and_enqueue_job(run, issue=issue)
        persisted = self.repository.get_run(job["run_id"])
        return {
            "run_id": job["run_id"], "job_id": job["job_id"],
            "issue_id": issue_id, "status": persisted.status, "queued": queued,
            "already_running": not queued,
        }

    def start_run(self, mode: str = "manual", *, force: bool = False) -> dict[str, Any]:
        """创建（或复用）当天 run，并在**同一事务**里入队 durable job。

        run 与 job 原先分两步提交，进程在
        两步之间崩溃会留下永不被执行的 pending run。现走
        repository.create_run_and_enqueue_job 的单一 BEGIN IMMEDIATE 事务；
        already_running 分支还做 has_active_job 兜底——万一存在历史孤儿
        （旧版本产物）也当场补队。
        """
        if not self._owner_is_eligible():
            return {"error": ADMIN_OWNER_MESSAGE, "code": "ADMIN_OWNER_FORBIDDEN"}
        self._recover_interrupted_runs(self.get_config())
        brief_date = brief_date_today()
        existing = self.repository.find_run(self.owner_id, brief_date, mode)
        if existing is not None:
            if existing.status in ("completed", "partial") and not force:
                return {"run_id": existing.run_id, "status": existing.status,
                        "reused": True}
            if existing.status in ("pending", "snapshotting", "analyzing"):
                if not self.repository.has_active_job(existing.run_id, kind="run"):
                    # 孤儿修复：run 活跃但没有 job（旧版本崩溃残留）——补队。
                    self.repository.enqueue_job(existing.run_id, kind="run")
                    return {"run_id": existing.run_id, "status": "pending",
                            "queued": True}
                return {"run_id": existing.run_id, "status": existing.status,
                        "already_running": True}
            # failed/cancelled（保留冻结快照重试）或人工 force（重新冻结）→ 复用 run。
            self._reset_run_for_retry(
                existing, refreeze=bool(force or existing.status in ("completed", "partial"))
            )
            self.repository.enqueue_job(existing.run_id, kind="run")
            return {"run_id": existing.run_id, "status": "pending", "queued": True}

        run = DailyBriefRun(
            owner_id=self.owner_id,
            brief_date=brief_date,
            mode=mode,
            run_id=new_run_id(),
            status="pending",
            started_at=_now(),
            prompt_version=PROMPT_VERSION,
        )
        return self._enqueue_new_run(run)

    def start_refresh(self, brief_date: str) -> dict[str, Any]:
        """delta 模式：基于当天 nightly 快照做增量重分析（入队 durable job）。"""
        if not self._owner_is_eligible():
            return {"error": ADMIN_OWNER_MESSAGE, "code": "ADMIN_OWNER_FORBIDDEN"}
        self._recover_interrupted_runs(self.get_config())
        nightly = self.repository.find_run(self.owner_id, brief_date, "nightly")
        if nightly is None:
            return {"error": f"no nightly run for {brief_date}; run nightly first"}
        existing = self.repository.find_run(self.owner_id, brief_date, "delta")
        if existing is not None:
            if existing.status in ("pending", "snapshotting", "analyzing"):
                if not self.repository.has_active_job(existing.run_id, kind="run"):
                    self.repository.enqueue_job(existing.run_id, kind="run")
                    return {"run_id": existing.run_id, "status": "pending",
                            "queued": True}
                return {"run_id": existing.run_id, "status": existing.status,
                        "already_running": True}
            # failed → 保留冻结快照重试；completed/partial → 新一轮增量，
            # 重新冻结（重新计算相对 nightly 的 delta）。
            self._reset_run_for_retry(
                existing, refreeze=existing.status in ("completed", "partial")
            )
            self.repository.enqueue_job(existing.run_id, kind="run")
            return {"run_id": existing.run_id, "status": "pending", "queued": True}
        run = DailyBriefRun(
            owner_id=self.owner_id,
            brief_date=brief_date,
            mode="delta",
            run_id=new_run_id(),
            status="pending",
            started_at=_now(),
            prompt_version=PROMPT_VERSION,
        )
        return self._enqueue_new_run(run)

    def _enqueue_new_run(self, run: DailyBriefRun) -> dict[str, Any]:
        created, job, job_created = self.repository.create_run_and_enqueue_job(run)
        persisted = self.repository.get_run(job["run_id"])
        return {
            "run_id": job["run_id"],
            "job_id": job["job_id"],
            "status": persisted.status,
            "queued": job_created,
            **({"already_running": True} if not created else {}),
        }
