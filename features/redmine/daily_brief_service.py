"""RedmineDailyBriefService：晨报编排（Phase 9/23/27/33）。

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
import re
import time
from datetime import datetime, timedelta
from typing import Any

from .daily_brief_models import (
    DailyBriefIssue,
    DailyBriefRun,
)
from .daily_brief_report import summarize_daily_brief
from .daily_brief_repository import (
    TERMINAL_RUN_STATUSES,
    DailyBriefRepository,
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
from .kkagent_analyzer import (
    PROMPT_VERSION,
    KkAgentRedmineAnalyzer,
    preflight_gms_auth,
)
from .users import _now


logger = logging.getLogger(__name__)


class _RunCancelledError(Exception):
    """协作式取消：执行循环在 issue 边界读到取消标志后抛出。"""


def _analyzer_env_extra(profile: Any) -> dict[str, str]:
    """kkagent 子进程的 MCP 身份环境；未绑定 profile 时返回空（fail-closed）。"""
    name = str(profile or "").strip()
    if not name:
        return {}
    return {
        "GMS_RT_PROFILE": name,
        "GMS_AGENT_CLIENT": "kkagent",
        "GMS_AGENT_AUTH_MODE": "service-token",
    }


DEFAULT_BRIEF_CONFIG: dict[str, Any] = {
    # opt-in：晨报会消耗 kkagent 分析资源，默认关闭，owner 在设置里显式
    # 开启后才进入 nightly 调度（不得默认启用）。
    "enabled": False,
    # 当前唯一实现的分析后端。历史上允许 "direct" 但从未实现，已从枚举
    # 移除；旧配置里的 "direct" 会被规范化回 "kkagent"。
    "analysis_backend": "kkagent",
    "model": "",
    # 绑定到该 owner 的本机 kkagent agent profile 名（~/.config/gms-agent/
    # profiles/<name>.toml）。为空则不注入任何 MCP 身份——分析仍可运行，
    # 但 MCP 取证工具会以未配置身份失败（fail-closed，不回退他人凭据）。
    "agent_profile": "",
    # 健全经验值：完整跑一轮 fetch+journals+history_search+综合，12 步常在
    # 证据链较长时不够（issue #646220 实测烧满后以 max_turns 中断）；
    # 20 步覆盖典型分析，仍可按 owner 在设置里调 1-50。
    "max_turns": 20,
    "issue_timeout_seconds": 600,
    "max_parallel_issues": 1,
    "max_issues": 50,
    "stale_days": DEFAULT_STALE_DAYS,
    "list_limit": DEFAULT_LIST_LIMIT,
}
RUNTIME_CONFIG_KEY = "redmine_daily_brief"


def normalize_daily_brief_config(payload: dict[str, Any] | None) -> dict[str, Any]:
    """规范化配置；非法值回落默认，未知键由 API 层拒绝。"""
    payload = payload or {}
    config = dict(DEFAULT_BRIEF_CONFIG)

    def _int(key: str, lo: int, hi: int) -> None:
        try:
            config[key] = max(lo, min(hi, int(payload.get(key, config[key]))))
        except (TypeError, ValueError):
            pass

    config["enabled"] = bool(payload.get("enabled", config["enabled"]))
    backend = str(payload.get("analysis_backend") or config["analysis_backend"]).strip()
    config["analysis_backend"] = backend if backend in ("kkagent",) else "kkagent"
    config["model"] = str(payload.get("model") or "").strip()
    # profile 名只允许安全字符，避免注入 env / 路径。
    profile = str(payload.get("agent_profile") or "").strip()
    config["agent_profile"] = profile if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", profile) else ""
    _int("max_turns", 1, 50)
    _int("issue_timeout_seconds", 60, 3600)
    _int("max_parallel_issues", 1, 4)
    _int("max_issues", 1, 200)
    _int("stale_days", 1, 30)
    _int("list_limit", 1, 100)
    return config


class DailyBriefService:
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
        return {
            "run": run.to_row(),
            "issues": [self._issue_payload(issue) for issue in issues],
        }

    @staticmethod
    def _issue_payload(issue: DailyBriefIssue) -> dict[str, Any]:
        payload = issue.to_row()
        payload.pop("raw_response", None)  # 原始输出仅供排障，不进 UI payload
        return payload

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

    def request_cancel(self, brief_date: str | None = None) -> dict[str, Any]:
        """请求停止该日期最新的一次 run（协作式取消）。

        独立 Worker 执行时靠 DB 标志位：执行循环在 issue 边界轮询并收敛
        为 cancelled 终态；同进程执行（Web 侧复用路径）额外对执行任务
        task.cancel()，让正在跑的单个 issue 也尽快停止。
        """
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

    def start_run(self, mode: str = "manual", *, force: bool = False) -> dict[str, Any]:
        """创建（或复用）当天 run。幂等规则见 repository.create_run。"""
        self._recover_interrupted_runs(self.get_config())
        brief_date = brief_date_today()
        existing = self.repository.find_run(self.owner_id, brief_date, mode)
        if existing is not None:
            if existing.status in ("completed", "partial") and not force:
                return {"run_id": existing.run_id, "status": existing.status,
                        "reused": True}
            if existing.status in ("pending", "snapshotting", "analyzing"):
                return {"run_id": existing.run_id, "status": existing.status,
                        "already_running": True}
            # failed/cancelled（保留冻结快照重试）或人工 force（重新冻结）→ 复用 run。
            self._reset_run_for_retry(
                existing, refreeze=bool(force or existing.status in ("completed", "partial"))
            )
            return {"run_id": existing.run_id, "status": "pending"}

        run = DailyBriefRun(
            owner_id=self.owner_id,
            brief_date=brief_date,
            mode=mode,
            run_id=new_run_id(),
            status="snapshotting",
            started_at=_now(),
            prompt_version=PROMPT_VERSION,
        )
        created = self.repository.create_run(run)
        if created is not None and created.run_id != run.run_id:
            return {"run_id": created.run_id, "status": created.status,
                    "already_running": True}
        return {"run_id": run.run_id, "status": "snapshotting"}

    def start_refresh(self, brief_date: str) -> dict[str, Any]:
        """delta 模式：基于当天 nightly 快照做增量重分析。"""
        self._recover_interrupted_runs(self.get_config())
        nightly = self.repository.find_run(self.owner_id, brief_date, "nightly")
        if nightly is None:
            return {"error": f"no nightly run for {brief_date}; run nightly first"}
        existing = self.repository.find_run(self.owner_id, brief_date, "delta")
        if existing is not None:
            if existing.status in ("pending", "snapshotting", "analyzing"):
                return {"run_id": existing.run_id, "status": existing.status,
                        "already_running": True}
            # failed → 保留冻结快照重试；completed/partial → 新一轮增量，
            # 重新冻结（重新计算相对 nightly 的 delta）。
            self._reset_run_for_retry(
                existing, refreeze=existing.status in ("completed", "partial")
            )
            return {"run_id": existing.run_id, "status": "pending"}
        run = DailyBriefRun(
            owner_id=self.owner_id,
            brief_date=brief_date,
            mode="delta",
            run_id=new_run_id(),
            status="snapshotting",
            started_at=_now(),
            prompt_version=PROMPT_VERSION,
        )
        created = self.repository.create_run(run)
        if created is not None and created.run_id != run.run_id:
            return {"run_id": created.run_id, "status": created.status,
                    "already_running": True}
        return {"run_id": run.run_id, "status": "snapshotting"}

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
                raise _RunCancelledError()
            # 认证预检：token 被吊销时逐条分析只会把 turn 预算烧在 MCP
            # 401 上，不如整 run 快速失败并给出重注册指引（fail-closed；
            # 预检自身故障则放行，不阻塞分析）。
            auth_ok, auth_reason = await preflight_gms_auth(
                _analyzer_env_extra(config.get("agent_profile"))
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
        except _RunCancelledError:
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
                await self._analyze_one(run, issue_id, entries.get(issue_id, {}), analyzer, config)

        await asyncio.gather(*[_one(issue_id) for issue_id in pending_ids])

    def _build_analyzer(self, config: dict[str, Any]) -> KkAgentRedmineAnalyzer:
        env_extra = _analyzer_env_extra(config.get("agent_profile"))
        return KkAgentRedmineAnalyzer(
            max_turns=int(config.get("max_turns") or DEFAULT_BRIEF_CONFIG["max_turns"]),
            timeout_seconds=int(config.get("issue_timeout_seconds") or 600),
            model=str(config.get("model") or ""),
            env_extra=env_extra,
        )

    def _recover_interrupted_runs(self, config: dict[str, Any]) -> int:
        """启动前把前次进程中断遗留的 run 标记为 failed（可重试）。

        阈值取该 owner 配置的最坏分析时长再加 1 小时缓冲——正常进行中的
        长 run 不会被误标。
        """
        try:
            timeout = max(1, int(config.get("issue_timeout_seconds") or 600))
            max_issues = max(1, int(config.get("max_issues") or 50))
            parallel = max(1, int(config.get("max_parallel_issues") or 1))
            worst_seconds = timeout * ((max_issues + parallel - 1) // parallel)
            cutoff = datetime.now() - timedelta(seconds=worst_seconds + 3600)
            marked = self.repository.reset_stale_running(cutoff.isoformat(timespec="seconds"))
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
            raise _RunCancelledError()
        record.status = "running"
        record.started_at = _now()
        record.attempt_count += 1
        self.repository.upsert_issue(record)

        started = time.monotonic()
        outcome = await analyzer.analyze(entry if entry else {"issue_id": issue_id})
        record.finished_at = _now()
        record.duration_ms = int((time.monotonic() - started) * 1000)
        record.raw_response = outcome.raw_output
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

    async def reanalyze_issue(self, brief_date: str, issue_id: int) -> dict[str, Any]:
        """单 issue 重新分析（写入当天最新 run，不新建 run）。"""
        run = self.repository.latest_run(self.owner_id, brief_date)
        if run is None:
            return {"error": f"no daily brief run for {brief_date}"}
        record = self.repository.get_issue(run.run_id, issue_id)
        if record is None:
            return {"error": f"issue {issue_id} not in run {run.run_id}"}
        if self.run_is_executing(run.run_id):
            return {"error": f"run {run.run_id} is still executing; retry after it finishes"}
        config = self.get_config()
        # 认证预检：token 失效时直接拒绝单条重分析（HTTP 404 携带原因），
        # 不再让该 issue 烧满 turn 预算后以 max_turns 失败。
        auth_ok, auth_reason = await preflight_gms_auth(
            _analyzer_env_extra(config.get("agent_profile"))
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
        await self._analyze_one(run, issue_id, entry, analyzer, config)
        # 单条状态变化必须同步刷新整份汇总，否则页头的成功/失败/人工确认
        # 数量、Markdown 与 run.status 会互相矛盾。
        self._summarize_phase(run)
        refreshed = self.repository.get_issue(run.run_id, issue_id)
        return {"run_id": run.run_id, "issue_id": issue_id, "status": refreshed.status}


__all__ = ["DEFAULT_BRIEF_CONFIG", "DailyBriefService", "normalize_daily_brief_config"]
