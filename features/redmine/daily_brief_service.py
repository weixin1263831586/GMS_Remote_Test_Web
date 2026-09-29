"""RedmineDailyBriefService：晨报编排（ADR 0008）。

职责：
- 快照冻结（build_daily_triage_snapshot 的唯一编排入口）；
- 幂等 run（owner+date+mode 唯一；completed 复用 / failed 可重试）；
- 并发控制（max_parallel_issues，默认 1）与单 issue 失败隔离；
- 汇总报告（counts / top_priorities / 每日 Markdown）。

不负责：调度（systemd/手动 API 触发）、UI、Redmine 写操作（全链路只读）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import daily_brief_cancellation as cancellation
from .daily_brief_analysis_events import mask_secrets, start_analysis_progress
from .daily_brief_models import (
    DailyBriefIssue,
    DailyBriefRun,
    derive_data_quality,
)
from .daily_brief_owner_policy import (
    ADMIN_OWNER_MESSAGE,
    is_daily_brief_owner_eligible,
)
from .daily_brief_report import (
    issue_payload,
    summarize_daily_brief,
    summarize_execution_statistics,
)
from .daily_brief_repository import (
    TERMINAL_RUN_STATUSES,
    DailyBriefRepository,
    canonical_owner_id,
    new_run_id,
    owner_daily_brief_repository,
)
from .daily_brief_snapshot import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_STALE_DAYS,
    brief_date_today,
    build_daily_triage_snapshot,
    detect_delta,
)
from .kkagent import (
    PROMPT_VERSION,
    KkAgentRedmineAnalyzer,
    preflight_gms_auth,
)
from .kkagent.evidence_preflight import collect_deep_analysis_evidence
from .users import _now


logger = logging.getLogger(__name__)


# 配置契约 / 分析器构建已拆分至 daily_brief_config（service 保持编排职责）。
from features.system import sdk_sources_available as _sdk_sources_available  # noqa: E402

from .daily_brief_config import (  # noqa: E402
    DEFAULT_BRIEF_CONFIG,
    RUNTIME_CONFIG_KEY,
    analyzer_env_extra,
    build_brief_analyzer,
    normalize_daily_brief_config,
)


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


class DailyBriefService:
    """一个 owner 一个实例（内部持有该 owner 的 repository）。"""

    # 同 run_id 的执行协调器（跨实例/跨请求共享）：防止 force 重试、refresh、
    # reanalyze 与仍在运行的旧任务并发写同一 run。仅本进程有效——跨进程正确
    # 性依赖 SQLite job/lease/run 状态与唯一约束，此 map 不得升级为跨进程锁。
    _RUN_EXECUTIONS: dict[str, asyncio.Task[DailyBriefRun | None]] = {}

    def __init__(self, owner_id: str, config_manager: Any | None = None):
        # owner 身份统一收敛为 sanitize 目录名：Web 匿名会话传 display id、
        # systemd/CLI 传目录名，两者必须指向同一份 per-owner 数据。
        self.owner_id = canonical_owner_id(str(owner_id or "anonymous"))
        self.config_manager = config_manager
        self.repository: DailyBriefRepository = owner_daily_brief_repository(self.owner_id)

    # ------------------------------------------------------------------ config

    def get_config(self, config_manager: Any | None = None) -> dict[str, Any]:
        manager = config_manager or self.config_manager
        runtime = {}
        try:
            runtime = manager.get_runtime_config() if manager else {}
        except Exception:
            logger.info("daily brief runtime config unavailable", exc_info=True)
        return normalize_daily_brief_config((runtime or {}).get(RUNTIME_CONFIG_KEY) or {})

    def save_config(self, config_manager: Any | None, config: dict[str, Any]) -> bool:
        manager = config_manager or self.config_manager
        if manager is None:
            return False
        try:
            runtime = dict(manager.get_runtime_config() or {})
            runtime[RUNTIME_CONFIG_KEY] = normalize_daily_brief_config(config)
            return bool(manager.save_runtime(runtime))
        except Exception:
            logger.exception("daily brief config save failed")
            return False

    # ------------------------------------------------------------------ query

    def latest_run(self, brief_date: str | None = None) -> DailyBriefRun | None:
        return self.repository.latest_run(self.owner_id, brief_date)

    def latest_active_issue_run(self) -> DailyBriefRun | None:
        return self.repository.latest_active_issue_run(self.owner_id)

    def latest_issue_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Saved standalone analyses, one newest entry for each issue id."""
        return [self.run_payload(run) for run in self.repository.latest_issue_runs(
            self.owner_id, limit=limit
        )]

    def latest_issue_run(self, issue_id: int) -> dict[str, Any] | None:
        """Return one saved standalone analysis for an exact Redmine number."""
        run = self.repository.latest_issue_run(self.owner_id, issue_id)
        return self.run_payload(run) if run is not None else None

    async def sync_display_subjects(self) -> dict[str, Any]:
        """Pull latest Redmine subjects and refresh display titles.

        晨报行与单号分析条目的 subject 在入队时冻结；用户点击刷新时把当前
        展示的单号标题与 Redmine 现值同步一次（镜像库与展示记录一起更新）。
        任一步失败都不阻断刷新，返回同步摘要供前端提示。
        """
        runs_by_issue: dict[int, DailyBriefRun] = {}
        for run in self.repository.latest_issue_runs(self.owner_id, limit=30):
            for issue in self.repository.list_issues(run.run_id):
                runs_by_issue.setdefault(issue.issue_id, run)
        latest = self.repository.latest_run(self.owner_id)
        if latest is not None:
            for issue in self.repository.list_issues(latest.run_id):
                runs_by_issue.setdefault(issue.issue_id, latest)
        issue_ids = sorted(runs_by_issue)
        if not issue_ids:
            return {"checked": 0, "updated": 0}
        try:
            from .api import get_redmine_service_for_owner

            redmine = get_redmine_service_for_owner(self.owner_id)
            client = redmine.agent._make_client()
            try:
                subjects = await client.fetch_issue_subjects(issue_ids)
            finally:
                await client.close()
        except Exception:
            logger.info("subject fetch unavailable", exc_info=True)
            return {"checked": len(issue_ids), "updated": 0, "error": "redmine_unavailable"}
        try:
            mirror_changed = redmine.repository.update_issue_subjects(subjects)
        except Exception:
            mirror_changed = 0
            logger.info("redmine mirror subject update failed", exc_info=True)
        try:
            display_changed = self.repository.update_issue_display_subjects(subjects)
        except Exception:
            display_changed = 0
            logger.info("display subject update failed", exc_info=True)
        renamed = [
            {"issue_id": issue_id, "subject": subject}
            for issue_id, subject in sorted(subjects.items())
            if str(subject or "").strip()
        ]
        return {
            "checked": len(issue_ids),
            "updated": display_changed,
            "mirror_updated": mirror_changed,
            "issues": renamed,
        }

    def find_run(self, brief_date: str, mode: str = "nightly") -> DailyBriefRun | None:
        return self.repository.find_run(self.owner_id, brief_date, mode)

    def run_payload(self, run: DailyBriefRun) -> dict[str, Any]:
        issues = self.repository.list_issues(run.run_id)
        latest_executions = self.repository.latest_ai_executions_by_issue(run.run_id)
        attempts_by_issue: dict[int, list[dict[str, Any]]] = {}
        for attempt in self.repository.list_ai_executions_for_run(run.run_id):
            issue_id = int(attempt.get("issue_id") or 0)
            if issue_id:
                attempts_by_issue.setdefault(issue_id, []).append(attempt)
        def display_issue(issue: DailyBriefIssue) -> dict[str, Any]:
            payload = issue_payload(
                issue,
                latest_executions.get(issue.issue_id),
                summarize_execution_statistics(
                    attempts_by_issue.get(issue.issue_id, []),
                    fallback_model=run.model_name,
                ),
            )
            # 防御旧库尚未完成迁移或外部旧 Worker 写回 pending：已经取消的
            # run 内条目不能再呈现为“排队中”。
            if run.status == "cancelled" and payload.get("status") in ("pending", "running"):
                payload["status"] = "cancelled"
            if not str((payload.get("result") or {}).get("detailed_report") or "").strip():
                previous = self.repository.latest_issue_run_with_report(
                    self.owner_id, issue.issue_id, exclude_run_id=run.run_id,
                )
                if previous is not None:
                    previous_run, previous_issue = previous
                    payload["previous_analysis"] = {
                        "run_id": previous_run.run_id,
                        "finished_at": previous_run.finished_at,
                        "result": previous_issue.result,
                    }
            # 旧版单号 run 只保存了 ``#<id>``。若同 owner 的 Redmine
            # 本地镜像已有标题，读时补齐展示而不改动历史分析结论。
            if payload.get("subject") not in ("", f"#{issue.issue_id}"):
                return payload
            try:
                from .api import get_redmine_service_for_owner

                stored = get_redmine_service_for_owner(self.owner_id).repository.get_issue(
                    issue.issue_id
                ) or {}
                subject = str(stored.get("subject") or "").strip()
                if subject:
                    payload["subject"] = subject
            except Exception:
                logger.debug("local Redmine title unavailable for #%s", issue.issue_id)
            return payload

        return {
            "run": {
                **run.to_row(),
                "cancel_requested": self.repository.is_cancel_requested(run.run_id),
            },
            "issues": [display_issue(issue) for issue in issues],
        }

    # ------------------------------------------------------------------ triage

    async def build_triage(
        self,
        stale_days: int = DEFAULT_STALE_DAYS,
        list_limit: int = DEFAULT_LIST_LIMIT,
        refresh: bool = True,
    ) -> dict[str, Any]:
        return await build_daily_triage_snapshot(
            self.owner_id, stale_days=stale_days, list_limit=list_limit,
            refresh=refresh,
        )

    # ------------------------------------------------------------------ run

    def _owner_is_eligible(self) -> bool:
        return is_daily_brief_owner_eligible(self.owner_id)

    def start_issue_analysis(
        self, issue_id: int, *, analysis_mode: str = "incremental", device_serial: str = "",
        analysis_hint: str = "", subject: str = "",
    ) -> dict[str, Any]:
        """Queue an incremental retry or a fresh full analysis for one issue."""
        if not self._owner_is_eligible():
            return {"error": ADMIN_OWNER_MESSAGE, "code": "ADMIN_OWNER_FORBIDDEN"}
        analysis_hint = analysis_hint.strip()
        if analysis_mode == "incremental":
            previous = self.repository.latest_issue_run(self.owner_id, issue_id)
            # 选择另一台实机意味着取证上下文已经改变，必须保留旧 run 并
            # 新建记录，不能把新设备证据混进原分析历史。
            if previous is not None and (
                not device_serial or previous.device_serial == device_serial
            ) and previous.analysis_hint == analysis_hint:
                record = self.repository.get_issue(previous.run_id, issue_id)
                if record is not None:
                    job, queued = self.repository.enqueue_job(
                        previous.run_id, kind="issue", issue_id=issue_id
                    )
                    persisted = self.repository.get_run(previous.run_id)
                    return {
                        "run_id": previous.run_id, "job_id": job["job_id"],
                        "issue_id": issue_id, "status": persisted.status,
                        "queued": queued, "already_running": not queued,
                        "analysis_mode": "incremental",
                    }
        # A full analysis gets a new run rather than replacing the previous
        # conclusion.  This keeps the saved Redmine-number history auditable.
        mode = (
            f"issue:{issue_id}:{analysis_mode}:{new_run_id()[-12:]}"
            if analysis_mode == "full" or device_serial or analysis_hint else f"issue:{issue_id}"
        )
        run = DailyBriefRun(
            owner_id=self.owner_id, brief_date=brief_date_today(),
            mode=mode, run_id=new_run_id(),
            started_at=_now(), prompt_version=PROMPT_VERSION, issue_count=1,
            device_serial=device_serial, analysis_hint=analysis_hint,
        )
        issue = DailyBriefIssue(
            run_id=run.run_id, issue_id=issue_id, buckets=[],
            subject=subject or f"#{issue_id}",
        )
        _created, job, queued = self.repository.create_run_and_enqueue_job(run, issue=issue)
        persisted = self.repository.get_run(job["run_id"])
        return {
            "run_id": job["run_id"], "job_id": job["job_id"],
            "issue_id": issue_id, "status": persisted.status, "queued": queued,
            "already_running": not queued,
            "analysis_mode": analysis_mode,
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

    def _reset_run_for_retry(self, run: DailyBriefRun, *, refreeze: bool) -> None:
        """失败/强制重跑时复用同一 run 记录。

        refreeze=True（force / 已完成 run 的新一轮）：删除冻结快照与旧
        issue，重跑会重新冻结当日最新事实；refreeze=False（崩溃/失败重试）：
        保留冻结快照，重试与首次执行消费完全相同的输入。
        """
        run.status = "pending"
        run.error = ""
        run.started_at = _now()
        run.finished_at = ""
        run.report_json = {}
        run.report_markdown = ""
        run.prompt_version = PROMPT_VERSION
        if refreeze:
            self.repository.delete_snapshot(run.run_id)
            self.repository.delete_issues(run.run_id)
            # 重冻结会删掉全部 issue 行；仍在排队的 issue-job 必须一并
            # 作废，否则它们随后被领取时读不到 issue 行。
            self.repository.jobs.cancel_queued_issue_jobs(run.run_id)
        # 复用 run 重新执行前清除上一轮的取消标志。
        self.repository.clear_cancel(run.run_id)
        self.repository.update_run(run)

    def request_cancel(self, brief_date: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        """请求停止一次 run（协作式取消）。

        同一天可能同时存在 nightly/manual/delta 多个 run，
        "取消该日期最新一次"会停错目标。优先使用显式 run_id 精确取消；
        未提供 run_id 时才回落到日期最新 run（旧 API 兼容）。

        独立 Worker 执行时靠 DB 标志位：执行循环在 issue 边界轮询并收敛
        为 cancelled 终态；同进程执行（Web 侧复用路径）额外对执行任务
        task.cancel()，让正在跑的单个 issue 也尽快停止。
        """
        if run_id:
            run = self.repository.get_run(run_id)
            if run is None or run.owner_id != self.owner_id:
                return {"error": f"daily brief run not found: {run_id}"}
        else:
            date = brief_date or brief_date_today()
            run = self.repository.latest_run(self.owner_id, date)
            if run is None:
                return {"error": f"no daily brief run for {date}"}
        if run.status in TERMINAL_RUN_STATUSES:
            return {
                "run_id": run.run_id,
                "status": run.status,
                "already_terminal": True,
            }
        requested = self.repository.request_cancel(run.run_id)
        cancelled_now = requested and self.repository.cancel_queued_issue_run(run.run_id)
        task = self._RUN_EXECUTIONS.get(run.run_id)
        if task is not None and not task.done():
            task.cancel()
        return {
            "run_id": run.run_id,
            "status": "cancelled" if cancelled_now else run.status,
            "cancel_requested": bool(requested),
        }


    async def execute_run(self, run_id: str) -> DailyBriefRun | None:
        """执行 run 全流程：快照 → 分析 → 汇总。异常只落到 run.error。

        同 run_id 的并发调用会汇合到进行中的任务（RunCoordinator 语义），
        避免 force/refresh/reanalyze 与残留旧任务并发写同一 run。
        """
        running = self._RUN_EXECUTIONS.get(run_id)
        if running is not None and not running.done():
            try:
                await running
            except Exception:
                logger.exception("joined daily brief run %s failed", run_id)
            return self.repository.get_run(run_id)

        run = self.repository.get_run(run_id)
        if run is None:
            logger.error("daily brief run %s not found", run_id)
            return None
        if run.status in TERMINAL_RUN_STATUSES:
            # 已收敛（含用户停止后的 cancelled）：迟到/重复的队列任务
            # 直接返回持久状态，不得复活已终态的 run。
            return run
        config = self.get_config()
        # 单号分析的设备选择随 run 持久化；不回写晨报全局配置，也不让后来
        # 的设置修改悄悄改变历史重分析的实机取证目标。
        if run.device_serial:
            config["device_serial"] = run.device_serial
        if run.analysis_hint:
            config["analysis_hint"] = run.analysis_hint
        task = asyncio.current_task()
        if task is not None:
            self._RUN_EXECUTIONS[run_id] = task
        try:
            if self.repository.is_cancel_requested(run_id):
                raise cancellation.RunCancelledError()
            # 认证预检：token 被吊销时整 run 快速失败并给出重注册指引
            #（fail-closed）；预检自身故障则放行，不阻塞分析。
            auth_ok, auth_reason = await preflight_gms_auth(
                analyzer_env_extra(config.get("agent_profile"))
            )
            if not auth_ok:
                run.status = "failed"
                run.error = mask_secrets(auth_reason, 1000)
                run.finished_at = _now()
                self.repository.update_run(run)
                return self.repository.get_run(run_id)
            run = await self._snapshot_phase(run, config)
            await self._analyze_phase(run, config)
            run = self._summarize_phase(run)
        except cancellation.RunCancelledError:
            # 用户请求停止：收敛为明确的终态 cancelled（非错误），
            # 已完成的 issue 结果保留，未开始的保持 pending 可续跑。
            run.status = "cancelled"
            run.error = ""
            run.finished_at = _now()
            self.repository.update_run(run)
        except asyncio.CancelledError:
            if self.repository.is_cancel_requested(run_id):
                # request_cancel 对进程内执行任务的定向 task.cancel()：
                # 属于用户停止，收敛为 cancelled 终态后正常返回（不外抛）。
                run.status = "cancelled"
                run.error = ""
                run.finished_at = _now()
                self.repository.update_run(run)
                return self.repository.get_run(run_id)
            # 独立 Worker 停止时先留下明确、可恢复的持久状态；Worker 的
            # job requeue 随后会把它转回 pending。
            run.status = "failed"
            run.error = "interrupted while analysis worker was stopping"
            run.finished_at = _now()
            self.repository.update_run(run)
            raise
        except Exception as exc:
            logger.exception("daily brief run %s failed", run_id)
            run.status = "failed"
            # 落库前打码：异常文本可能含 URL/header/token 片段（HTTP 层
            # 已隐藏细节，DB 层是最后防线）。
            run.error = mask_secrets(str(exc), 1000)
            run.finished_at = _now()
            self.repository.update_run(run)
        finally:
            if task is not None and self._RUN_EXECUTIONS.get(run_id) is task:
                self._RUN_EXECUTIONS.pop(run_id, None)
        return self.repository.get_run(run_id)

    @classmethod
    def run_is_executing(cls, run_id: str) -> bool:
        """该 run_id 是否仍有存活执行任务（API 幂等入口参考）。"""
        task = cls._RUN_EXECUTIONS.get(run_id)
        return task is not None and not task.done()

    async def _snapshot_phase(self, run: DailyBriefRun, config: dict[str, Any]) -> DailyBriefRun:
        run.status = "snapshotting"
        self.repository.update_run(run)

        # 冻结快照优先：失败重试/崩溃恢复复用同一份输入事实，Redmine 数据
        # 变化不得改变同一 run 的分析基准（快照已随 run 持久化到 SQLite）。
        frozen = self.repository.get_snapshot(run.run_id)
        if frozen is None:
            snapshot = await self.build_triage(
                stale_days=int(config.get("stale_days") or DEFAULT_STALE_DAYS),
                list_limit=int(config.get("list_limit") or DEFAULT_LIST_LIMIT),
            )
            issues = list(snapshot["issues"])[: int(config.get("max_issues") or 50)]

            if run.mode == "delta":
                previous = self.repository.find_run(self.owner_id, run.brief_date, "nightly")
                prev_issues = []
                if previous is not None:
                    prev_issues = [
                        {"issue_id": item.issue_id, "fingerprint": item.fingerprint}
                        for item in self.repository.list_issues(previous.run_id)
                    ]
                delta = detect_delta(prev_issues, issues)
                issues = delta["changed"]
                snapshot["counts"]["delta_total"] = len(issues)

            frozen = {
                "brief_date": snapshot.get("brief_date", run.brief_date),
                "generated_at": snapshot.get("generated_at", ""),
                "snapshot_hash": snapshot.get("snapshot_hash", ""),
                "source_sync_status": str(
                    snapshot.get("source_sync_status")
                    or ("synced" if snapshot.get("synced") else "skipped")
                ),
                "counts": dict(snapshot.get("counts") or {}),
                "issues": issues,
            }
            self.repository.save_snapshot(run.run_id, frozen)

        issues = list(frozen.get("issues") or [])
        counts = dict(frozen.get("counts") or {})
        run.snapshot_at = str(frozen.get("generated_at") or "")
        run.snapshot_hash = str(frozen.get("snapshot_hash") or "")
        run.source_sync_status = str(frozen.get("source_sync_status") or "")
        # Keep execution status separate from data quality：同步成功的快照
        # 生成时间即 last_sync_at；失败/跳过时留空，由 data_quality 表达。
        if run.source_sync_status == "synced":
            run.last_sync_at = run.snapshot_at
        run.data_quality = derive_data_quality(
            run.source_sync_status, run.snapshot_at
        )
        run.issue_count = len(issues)
        run.waiting_my_reply_count = int(counts.get("waiting_my_reply") or 0)
        run.no_reply_3_days_count = int(counts.get("no_reply_3_days") or 0)
        run.analysis_backend = str(config.get("analysis_backend") or "kkagent")
        run.model_name = str(config.get("model") or "")
        run.status = "analyzing"
        run.started_at = run.started_at or _now()
        self.repository.update_run(run)

        # retry/force(refreeze) 会复用 run_id；先删旧快照条目，避免已不在
        # 今日待办中的 issue 残留在新晨报里。
        self.repository.delete_issues(run.run_id)
        for entry in issues:
            self.repository.upsert_issue(DailyBriefIssue(
                run_id=run.run_id,
                issue_id=int(entry["issue_id"]),
                buckets=list(entry.get("buckets") or []),
                priority=str(entry.get("priority") or "P3"),
                priority_score=int(entry.get("priority_score") or 0),
                fingerprint=str(entry.get("fingerprint") or ""),
                subject=str(entry.get("subject") or ""),
                status="pending",
            ))
        return run

    def _snapshot_entries(self, run_id: str) -> dict[int, dict[str, Any]]:
        """冻结快照的 issue entries（持久化读取，崩溃重试后仍然可用）。"""
        frozen = self.repository.get_snapshot(run_id)
        entries: dict[int, dict[str, Any]] = {}
        for entry in (frozen or {}).get("issues") or []:
            try:
                entries[int(entry["issue_id"])] = entry
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    async def _analyze_phase(self, run: DailyBriefRun, config: dict[str, Any]) -> None:
        entries = self._snapshot_entries(run.run_id)
        pending_ids = [
            item.issue_id for item in self.repository.list_issues(run.run_id)
            if item.status in ("pending", "failed")
        ]
        semaphore = asyncio.Semaphore(max(1, int(config.get("max_parallel_issues") or 1)))
        analyzer = self._build_analyzer(config)

        async def _one(issue_id: int) -> None:
            async with semaphore:
                # 与「Redmine 单号分析」完全同语义（reanalyze_issue →
                # _analyze_one，深度诊断 diagnostic）：晨报只是触发来源不同
                # （固定时间调度），不降级为轻量 triage（仅渲染历史结果用）。
                entry = dict(entries.get(issue_id, {}))
                await self._analyze_one(run, issue_id, entry, analyzer, config)

        await cancellation.gather_cancel_on_error(
            [_one(issue_id) for issue_id in pending_ids]
        )

    def _build_analyzer(self, config: dict[str, Any]) -> KkAgentRedmineAnalyzer:
        # 构建细节统一在 daily_brief_config.build_brief_analyzer（含 MCP
        # toolset / 设备绑定 env 注入）；分析不设轮次预算。
        return build_brief_analyzer(config)

    def _recover_interrupted_runs(self, config: dict[str, Any]) -> int:
        """Recover orphan runs without judging live jobs by their elapsed time."""
        try:
            marked = self.repository.reset_stale_running(_now())
            if marked:
                logger.info("marked %d interrupted daily-brief run(s) as failed", marked)
            return marked
        except Exception:
            logger.exception("daily brief crash recovery failed")
            return 0

    async def _analyze_one(
        self,
        run: DailyBriefRun,
        issue_id: int,
        entry: dict[str, Any],
        analyzer: KkAgentRedmineAnalyzer,
        config: dict[str, Any],
    ) -> None:
        record = self.repository.get_issue(run.run_id, issue_id)
        if record is None:
            return
        # 协作式取消点：每个 issue 开始前查一次标志位；用户点「停止分析」后，
        # 未开始的 issue 从这里直接终止整个 phase（正在跑的由 cancel 兜底）。
        if self.repository.is_cancel_requested(run.run_id):
            raise cancellation.RunCancelledError()
        record.status = "running"
        record.started_at = _now()
        record.attempt_count += 1
        self.repository.upsert_issue(record)
        # 实时进度时间线（preflight/kkagent/终态统一落库点；终态事件由
        # analyze_with_persisted_cancel 收敛为完成/已停止）。
        progress = start_analysis_progress(self.repository, run, issue_id, record)

        started = time.monotonic()
        try:
            analyze_entry = dict(entry) if entry else {"issue_id": issue_id}
            analyze_entry.setdefault("analysis_mode", "diagnostic")
            if config.get("device_serial"):
                analyze_entry.setdefault("device_serial", config["device_serial"])
            if config.get("analysis_hint"):
                analyze_entry.setdefault("analysis_hint", config["analysis_hint"])
            # Deep analysis owns a deterministic read-only baseline（含取消
            # 轮询与 profile 判定；原独立模块，已并回服务层编排）。
            analyze_entry["_progress_recorder"] = progress
            await precollect_deep_evidence(
                repository=self.repository, run=run, issue_id=issue_id,
                analyzer=analyzer, entry=analyze_entry,
            )
            # 部署事实 hint：SDK 源可用性决定源码取证门禁是否强制
            #（evidence_gate 降级依据），只进本次调用，不回写快照。
            analyze_entry["sdk_sources_available"] = _sdk_sources_available()
            outcome = await cancellation.analyze_with_persisted_cancel(
                self.repository, run.run_id, analyzer,
                analyze_entry,
            )
        except (cancellation.RunCancelledError, asyncio.CancelledError):
            cancellation.reset_cancelled_issue(self.repository, record)
            raise
        except Exception as exc:
            # 通用异常也必须收敛 issue 行（否则停留 running、汇总计数错），
            # 标记 failed 后按原语义继续向上传播（run 级收敛由调用方负责）。
            record.status = "failed"
            record.finished_at = _now()
            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.error = mask_secrets(f"analysis crashed: {exc}", 200)
            record.error_type = "exception"
            self.repository.upsert_issue(record)
            raise
        record.finished_at = _now()
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.raw_response = outcome.raw_output
        # 每次 AI attempt 的可审计轨迹（session/tool/usage）独立落库（Evidence
        # Provenance 的查询起点），不塞进 issue 单条记录。
        try:
            self.repository.record_ai_execution(
                run.run_id, issue_id,
                {
                    "attempt_no": record.attempt_count,
                    "model_name": run.model_name,
                    **(outcome.trace or {}),
                    "final_ok": bool(outcome.ok),
                    "failure_stage": outcome.error_type,
                    "failure_message": mask_secrets(outcome.error, 1000),
                    "wall_duration_ms": record.duration_ms,
                },
            )
        except Exception:
            logger.exception("failed to persist ai execution trace")
        if outcome.ok and outcome.result:
            record.status = "completed"
            record.error = ""
            record.error_type = ""
            record.result = dict(outcome.result)
            record.result["issue_id"] = issue_id
            record.result["buckets"] = record.buckets
            record.result["priority"] = record.priority
        else:
            record.status = "failed"
            # 与 run.error 同规：stderr/异常文本可能含 URL/header/token
            # 片段，落库前打码（该字段会经 issue_payload 进 API）。
            record.error = mask_secrets(outcome.error, 1000)
            record.error_type = outcome.error_type
        self.repository.upsert_issue(record)

    def _summarize_phase(self, run: DailyBriefRun) -> DailyBriefRun:
        return summarize_daily_brief(
            self.repository,
            run,
            self._snapshot_entries(run.run_id),
        )

    # ------------------------------------------------------------------ single

    async def reanalyze_issue(self, brief_date: str, issue_id: int, *, run_id: str = "") -> dict[str, Any]:
        """单 issue 重新分析（写入当天最新 run，不新建 run）。"""
        run = self.repository.get_run(run_id) if run_id else self.repository.latest_run(self.owner_id, brief_date)
        if run is None or run.owner_id != self.owner_id:
            return {"error": f"no daily brief run for {brief_date}"}
        record = self.repository.get_issue(run.run_id, issue_id)
        if record is None:
            return {"error": f"issue {issue_id} not in run {run.run_id}"}
        if self.run_is_executing(run.run_id):
            return {"error": f"run {run.run_id} is still executing; retry after it finishes"}
        if run.mode.startswith("issue:") and record.subject in ("", f"#{issue_id}"):
            # 单号任务没有晨报快照可提供标题。先用同 owner 的 Redmine
            # 元数据同步补齐标题；失败不阻断后续 AI 取证，行内会退化为单号。
            try:
                from .api import get_redmine_service_for_owner

                metadata = await get_redmine_service_for_owner(
                    self.owner_id
                ).refresh_issue_metadata(issue_id)
                subject = str((metadata.get("data") or {}).get("issue", {}).get("subject") or "").strip()
                if subject:
                    record.subject = subject
                    self.repository.upsert_issue(record)
            except Exception:
                logger.info("single issue %s subject refresh unavailable", issue_id, exc_info=True)
        config = self.get_config()
        # 认证预检：token 失效时直接拒绝单条重分析，
        # 不再让该 issue 烧满 turn 预算后以 max_turns 失败。
        auth_ok, auth_reason = await preflight_gms_auth(
            analyzer_env_extra(config.get("agent_profile"))
        )
        if not auth_ok:
            return {"error": auth_reason}
        # 优先取冻结快照里的 entry（完整字段）；快照不在时退化为 issue 行上的
        # 标题/桶/优先级，避免 MCP 暂时不可用时退化成只有 issue id。
        entry = self._snapshot_entries(run.run_id).get(issue_id) or {
            "issue_id": issue_id,
            "subject": record.subject,
            "buckets": record.buckets,
            "priority_name": record.priority,
        }
        if not str(entry.get("author_name") or "").strip():
            # 单号 run 没有晨报快照，entry 缺报告人/指派/建单时间会让分析
            # prompt 去附件里翻 author；本地镜像已有这些列，直接补齐。
            try:
                from .api import get_redmine_service_for_owner

                stored = get_redmine_service_for_owner(self.owner_id).repository.get_issue(
                    issue_id
                ) or {}
                for key in (
                    "author_name", "assigned_to_name", "created_on",
                    "updated_on", "status_name",
                ):
                    value = str(stored.get(key) or "").strip()
                    if value and not str(entry.get(key) or "").strip():
                        entry[key] = value
            except Exception:
                logger.debug("local Redmine metadata unavailable for #%s", issue_id)
        if run.device_serial:
            entry = {**entry, "device_serial": run.device_serial}
        if run.analysis_hint:
            entry = {**entry, "analysis_hint": run.analysis_hint}
        analyzer = self._build_analyzer(config)
        try:
            # 终态 run 重分析前先回非终态（防 purge 误删实时事件 + 恢复实时轮询）。
            cancellation.revive_run_for_reanalysis(self.repository, run)
            await self._analyze_one(run, issue_id, entry, analyzer, config)
        except cancellation.RunCancelledError:
            return cancellation.mark_reanalysis_cancelled(self.repository, run, issue_id)
        finally:
            cancellation.converge_reanalysis_run(self.repository, run, self._summarize_phase)
        refreshed = self.repository.get_issue(run.run_id, issue_id)
        if refreshed is None:
            # 并发重跑可能已清空该 run 的 issue 行;不伪造状态,同入参校验语义。
            return {"error": f"issue {issue_id} no longer in run {run.run_id}"}
        return {"run_id": run.run_id, "issue_id": issue_id, "status": refreshed.status}

__all__ = ["DEFAULT_BRIEF_CONFIG", "DailyBriefService", "normalize_daily_brief_config"]
