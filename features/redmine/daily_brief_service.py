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
from .daily_brief_execution_view import issue_payload
from .daily_brief_models import (
    DailyBriefIssue,
    DailyBriefRun,
    derive_data_quality,
)
from .daily_brief_report import summarize_daily_brief
from .daily_brief_repository import (
    TERMINAL_RUN_STATUSES,
    DailyBriefRepository,
    owner_daily_brief_repository,
)
from .daily_brief_run_starter import DailyBriefRunStarterMixin
from .daily_brief_snapshot import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_STALE_DAYS,
    brief_date_today,
    build_daily_triage_snapshot,
    detect_delta,
)
from .kkagent_analyzer import (
    PROMPT_VERSION,
    KkAgentRedmineAnalyzer,
    preflight_gms_auth,
)
from .users import _now


logger = logging.getLogger(__name__)


# 配置契约 / 分析器构建已拆分至 daily_brief_config（service 保持编排职责）。
from features.system import sdk_sources_available as _sdk_sources_available  # noqa: E402

from .daily_brief_config import (  # noqa: E402
    DEFAULT_BRIEF_CONFIG,
    RUNTIME_CONFIG_KEY,
    TEST_FAILURE_EXTRA_TURNS,
    analyzer_env_extra,
    build_brief_analyzer,
    normalize_daily_brief_config,
)


class DailyBriefService(DailyBriefRunStarterMixin):
    """一个 owner 一个实例（内部持有该 owner 的 repository）。"""

    # 同 run_id 的执行协调器（跨实例/跨请求共享）：防止 force 重试、
    # refresh、reanalyze 与仍在运行的旧任务并发写同一 run。
    _RUN_EXECUTIONS: dict[str, asyncio.Task[DailyBriefRun | None]] = {}

    def __init__(self, owner_id: str, config_manager: Any | None = None):
        self.owner_id = str(owner_id or "anonymous")
        self.config_manager = config_manager
        self.repository: DailyBriefRepository = owner_daily_brief_repository(self.owner_id)

    # ------------------------------------------------------------------ config

    def get_config(self, config_manager: Any | None = None) -> dict[str, Any]:
        manager = config_manager or self.config_manager
        runtime = {}
        try:
            runtime = manager.get_runtime_config() if manager else {}
        except Exception:
            pass
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

    def find_run(self, brief_date: str, mode: str = "nightly") -> DailyBriefRun | None:
        return self.repository.find_run(self.owner_id, brief_date, mode)

    def run_payload(self, run: DailyBriefRun) -> dict[str, Any]:
        issues = self.repository.list_issues(run.run_id)
        executions = self.repository.latest_ai_executions_by_issue(run.run_id)
        return {
            "run": run.to_row(),
            "issues": [
                issue_payload(issue, executions.get(issue.issue_id))
                for issue in issues
            ],
        }

    _issue_payload = staticmethod(issue_payload)

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
        task = self._RUN_EXECUTIONS.get(run.run_id)
        if task is not None and not task.done():
            task.cancel()
        return {
            "run_id": run.run_id,
            "status": run.status,
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
        task = asyncio.current_task()
        if task is not None:
            self._RUN_EXECUTIONS[run_id] = task
        try:
            if self.repository.is_cancel_requested(run_id):
                raise cancellation.RunCancelledError()
            # 认证预检：token 被吊销时逐条分析只会把 turn 预算烧在 MCP
            # 401 上，不如整 run 快速失败并给出重注册指引（fail-closed；
            # 预检自身故障则放行，不阻塞分析）。
            auth_ok, auth_reason = await preflight_gms_auth(
                analyzer_env_extra(config.get("agent_profile"))
            )
            if not auth_ok:
                run.status = "failed"
                run.error = auth_reason[:1000]
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
            run.error = str(exc)[:1000]
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
        # Keep execution status separate from data quality.
        # 同步成功的快照生成时间即 last_sync_at；失败/跳过时留空，由
        # data_quality=sync_failed/unknown 表达"这不是最新数据"。
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
                entry = {**entries.get(issue_id, {}), "analysis_mode": "triage"}
                await self._analyze_one(run, issue_id, entry, analyzer, config)

        await cancellation.gather_cancel_on_error(
            [_one(issue_id) for issue_id in pending_ids]
        )

    def _build_analyzer(
        self, config: dict[str, Any], *, extra_turns: int = 0
    ) -> KkAgentRedmineAnalyzer:
        # 构建细节统一在 daily_brief_config.build_brief_analyzer（含 MCP
        # toolset / 设备绑定 env 注入）；轮次放宽按 entry 特征逐 issue 加。
        return build_brief_analyzer(config, extra_turns=extra_turns)

    @staticmethod
    def _extra_turns_for_entry(entry: dict[str, Any]) -> int:
        """仅单条深度诊断追加源码取证步数。"""
        from .kkagent.evidence_gate import is_test_failure_subject

        return TEST_FAILURE_EXTRA_TURNS if entry.get("analysis_mode") != "triage" and is_test_failure_subject(entry) else 0

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
        # 协作式取消点：每个 issue 开始前查一次标志位（廉价 SELECT）。
        # 用户点「停止分析」后，正在跑的 issue 由 task.cancel()/gather 兜底，
        # 未开始的 issue 从这里直接终止整个 phase。
        if self.repository.is_cancel_requested(run.run_id):
            raise cancellation.RunCancelledError()
        record.status = "running"
        record.started_at = _now()
        record.attempt_count += 1
        self.repository.upsert_issue(record)

        started = time.monotonic()
        try:
            analyze_entry = dict(entry) if entry else {"issue_id": issue_id}
            analyze_entry.setdefault("analysis_mode", "diagnostic")
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
        record.finished_at = _now()
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.raw_response = outcome.raw_output
        # 每次 AI attempt 的可审计轨迹（session/tool/usage）独立落库：
        # Evidence Provenance 的查询起点；不塞进 issue 单条记录。
        try:
            self.repository.record_ai_execution(
                run.run_id, issue_id,
                {
                    "attempt_no": record.attempt_count,
                    **(outcome.trace or {}),
                    "final_ok": bool(outcome.ok),
                    "failure_stage": outcome.error_type,
                    "failure_message": outcome.error,
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
            record.error = outcome.error
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
        config = self.get_config()
        # 认证预检：token 失效时直接拒绝单条重分析，
        # 不再让该 issue 烧满 turn 预算后以 max_turns 失败。
        auth_ok, auth_reason = await preflight_gms_auth(
            analyzer_env_extra(config.get("agent_profile"))
        )
        if not auth_ok:
            return {"error": auth_reason}
        # 优先取冻结快照里的 entry（完整字段），持久化快照不在时退化为
        # issue 记录上的标题、桶和规则优先级，避免 MCP 暂时不可用时退化
        # 成只有 issue id。
        entry = self._snapshot_entries(run.run_id).get(issue_id) or {
            "issue_id": issue_id,
            "subject": record.subject,
            "buckets": record.buckets,
            "priority_name": record.priority,
        }
        analyzer = self._build_analyzer(config)
        try:
            await self._analyze_one(run, issue_id, entry, analyzer, config)
        except cancellation.RunCancelledError:
            return cancellation.mark_reanalysis_cancelled(
                self.repository, run, issue_id
            )
        # 单条状态变化必须同步刷新整份汇总，否则页头的成功/失败/人工确认
        # 数量、Markdown 与 run.status 会互相矛盾。
        self._summarize_phase(run)
        refreshed = self.repository.get_issue(run.run_id, issue_id)
        return {"run_id": run.run_id, "issue_id": issue_id, "status": refreshed.status}

__all__ = ["DEFAULT_BRIEF_CONFIG", "DailyBriefService", "normalize_daily_brief_config"]
