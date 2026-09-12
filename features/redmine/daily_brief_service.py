"""RedmineDailyBriefService：晨报编排（Phase 9/23/27/33）。

职责：
- 快照冻结（build_daily_triage_snapshot 的唯一编排入口）；
- 幂等 run（owner+date+mode 唯一；completed 复用 / failed 可重试）；
- 并发控制（max_parallel_issues，默认 2）与单 issue 失败隔离；
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
from .daily_brief_repository import (
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
from .kkagent_analyzer import PROMPT_VERSION, KkAgentRedmineAnalyzer
from .users import _now


logger = logging.getLogger(__name__)

DEFAULT_BRIEF_CONFIG: dict[str, Any] = {
    "enabled": True,
    "analysis_backend": "kkagent",
    "model": "",
    # 绑定到该 owner 的本机 kkagent agent profile 名（~/.config/gms-agent/
    # profiles/<name>.toml）。为空则不注入任何 MCP 身份——分析仍可运行，
    # 但 MCP 取证工具会以未配置身份失败（fail-closed，不回退他人凭据）。
    "agent_profile": "",
    "max_turns": 12,
    "issue_timeout_seconds": 600,
    "max_parallel_issues": 2,
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
    backend = str(payload.get("analysis_backend") or config["analysis_backend"])
    config["analysis_backend"] = backend if backend in ("kkagent", "direct") else "kkagent"
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

    def start_run(self, mode: str = "manual") -> dict[str, Any]:
        """创建（或复用）当天 run。幂等规则见 repository.create_run。"""
        self._recover_interrupted_runs(self.get_config())
        brief_date = brief_date_today()
        existing = self.repository.find_run(self.owner_id, brief_date, mode)
        if existing is not None:
            if existing.status in ("completed", "partial"):
                return {"run_id": existing.run_id, "status": existing.status,
                        "reused": True}
            if existing.status in ("pending", "snapshotting", "analyzing"):
                return {"run_id": existing.run_id, "status": existing.status,
                        "already_running": True}
            # failed → 允许 retry：复用同一 run 记录。
            existing.status = "pending"
            existing.error = ""
            existing.started_at = _now()
            self.repository.update_run(existing)
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
        """执行 run 全流程：快照 → 分析 → 汇总。异常只落到 run.error。"""
        run = self.repository.get_run(run_id)
        if run is None:
            logger.error("daily brief run %s not found", run_id)
            return None
        config = self.get_config()
        try:
            run = await self._snapshot_phase(run, config)
            await self._analyze_phase(run, config)
            run = self._summarize_phase(run)
        except Exception as exc:
            logger.exception("daily brief run %s failed", run_id)
            run.status = "failed"
            run.error = str(exc)[:1000]
            run.finished_at = _now()
            self.repository.update_run(run)
        return self.repository.get_run(run_id)

    async def _snapshot_phase(self, run: DailyBriefRun, config: dict[str, Any]) -> DailyBriefRun:
        run.status = "snapshotting"
        self.repository.update_run(run)
        snapshot = await self.build_triage(
            stale_days=int(config.get("stale_days") or DEFAULT_STALE_DAYS),
            list_limit=int(config.get("list_limit") or DEFAULT_LIST_LIMIT),
        )
        issues = snapshot["issues"][: int(config.get("max_issues") or 50)]

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

        run.snapshot_at = snapshot["generated_at"]
        run.snapshot_hash = snapshot["snapshot_hash"]
        run.issue_count = len(issues)
        run.waiting_my_reply_count = snapshot["counts"]["waiting_my_reply"]
        run.no_reply_3_days_count = snapshot["counts"]["no_reply_3_days"]
        run.analysis_backend = str(config.get("analysis_backend") or "kkagent")
        run.model_name = str(config.get("model") or "")
        run.status = "analyzing"
        run.started_at = run.started_at or _now()
        self.repository.update_run(run)

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
        # 快照 entry 暂存到内存（run 级），分析阶段直接消费冻结数据。
        self._snapshot_entries(run.run_id, {int(e["issue_id"]): e for e in issues})
        return run

    _SNAPSHOT_ENTRY_CACHE: dict[str, dict[int, dict[str, Any]]] = {}

    def _snapshot_entries(self, run_id: str, entries: dict[int, dict[str, Any]] | None = None):
        if entries is not None:
            self._SNAPSHOT_ENTRY_CACHE[run_id] = entries
        return self._SNAPSHOT_ENTRY_CACHE.get(run_id, {})

    async def _analyze_phase(self, run: DailyBriefRun, config: dict[str, Any]) -> None:
        entries = self._snapshot_entries(run.run_id)
        pending_ids = [
            item.issue_id for item in self.repository.list_issues(run.run_id)
            if item.status in ("pending", "failed")
        ]
        semaphore = asyncio.Semaphore(max(1, int(config.get("max_parallel_issues") or 2)))
        analyzer = self._build_analyzer(config)

        async def _one(issue_id: int) -> None:
            async with semaphore:
                await self._analyze_one(run, issue_id, entries.get(issue_id, {}), analyzer, config)

        await asyncio.gather(*[_one(issue_id) for issue_id in pending_ids])

    def _build_analyzer(self, config: dict[str, Any]) -> KkAgentRedmineAnalyzer:
        env_extra: dict[str, str] = {}
        profile = str(config.get("agent_profile") or "").strip()
        if profile:
            env_extra["GMS_RT_PROFILE"] = profile
            env_extra["GMS_AGENT_CLIENT"] = "kkagent"
            env_extra["GMS_AGENT_AUTH_MODE"] = "service-token"
        return KkAgentRedmineAnalyzer(
            max_turns=int(config.get("max_turns") or 12),
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
            parallel = max(1, int(config.get("max_parallel_issues") or 2))
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
        issues = self.repository.list_issues(run.run_id)
        completed = [i for i in issues if i.status == "completed"]
        failed = [i for i in issues if i.status == "failed"]

        counts = {
            "total": len(issues),
            "waiting_my_reply": run.waiting_my_reply_count,
            "no_reply_3_days": run.no_reply_3_days_count,
            "completed": len(completed),
            "failed": len(failed),
            "needs_human_review": sum(
                1 for i in completed
                if bool((i.result or {}).get("needs_human_review"))
            ),
        }
        top = [
            {
                "issue_id": i.issue_id,
                "priority": i.priority,
                "subject": (self._snapshot_entries(run.run_id).get(i.issue_id) or {}).get("subject", ""),
                "problem_summary": (i.result or {}).get("problem_summary", ""),
                "confidence": (i.result or {}).get("confidence"),
            }
            for i in sorted(completed, key=lambda x: -x.priority_score)[:5]
        ]
        run.report_json = {
            "brief_date": run.brief_date,
            "generated_at": _now(),
            "counts": counts,
            "top_priorities": top,
            "snapshot_hash": run.snapshot_hash,
            "analysis_backend": run.analysis_backend,
            "model": run.model_name,
        }
        run.report_markdown = self._render_markdown(run, completed, failed)
        if failed and not completed:
            run.status = "failed"
            run.error = f"{len(failed)} issue analyses failed"
        elif failed or len(completed) < len(issues):
            run.status = "partial"
            run.error = "" if completed else "all issue analyses failed"
        else:
            run.status = "completed"
            run.error = ""
        run.finished_at = _now()
        self.repository.update_run(run)
        self._SNAPSHOT_ENTRY_CACHE.pop(run.run_id, None)
        return run

    @staticmethod
    def _render_markdown(run: DailyBriefRun, completed: list[DailyBriefIssue], failed: list[DailyBriefIssue]) -> str:
        lines = [f"# Redmine AI 晨报 {run.brief_date}", ""]
        counts = run.report_json.get("counts", {})
        lines.append(
            f"共 {counts.get('total', 0)} 个待处理（待回复 {counts.get('waiting_my_reply', 0)} / "
            f"超 {run.no_reply_3_days_count and ''}3 天未回复 {counts.get('no_reply_3_days', 0)}），"
            f"成功分析 {counts.get('completed', 0)}，失败 {counts.get('failed', 0)}。"
        )
        lines.append("")
        for issue in completed:
            result = issue.result or {}
            lines.append(f"## {issue.priority} #{issue.issue_id} {result.get('problem_summary', '')}")
            lines.append(f"- 客户诉求：{result.get('customer_request', '')}")
            lines.append(f"- 根因（{result.get('root_cause_type', 'unknown')}）：{result.get('root_cause', '')}")
            lines.append(f"- 建议：{result.get('suggested_solution', '')}")
            confidence = result.get("confidence")
            lines.append(f"- 置信度：{confidence}{'（需人工确认）' if result.get('needs_human_review') else ''}")
            lines.append("")
        if failed:
            lines.append("## 分析失败")
            lines.extend(f"- #{i.issue_id}: {i.error}" for i in failed)
        return "\n".join(lines)

    # ------------------------------------------------------------------ single

    async def reanalyze_issue(self, brief_date: str, issue_id: int) -> dict[str, Any]:
        """单 issue 重新分析（写入当天最新 run，不新建 run）。"""
        run = self.repository.latest_run(self.owner_id, brief_date)
        if run is None:
            return {"error": f"no daily brief run for {brief_date}"}
        record = self.repository.get_issue(run.run_id, issue_id)
        if record is None:
            return {"error": f"issue {issue_id} not in run {run.run_id}"}
        config = self.get_config()
        entry = {"issue_id": issue_id}
        analyzer = self._build_analyzer(config)
        await self._analyze_one(run, issue_id, entry, analyzer, config)
        run.report_json = dict(run.report_json or {})
        run.status = "partial" if any(
            i.status == "failed" for i in self.repository.list_issues(run.run_id)
        ) else "completed"
        self.repository.update_run(run)
        refreshed = self.repository.get_issue(run.run_id, issue_id)
        return {"run_id": run.run_id, "issue_id": issue_id, "status": refreshed.status}


__all__ = ["DEFAULT_BRIEF_CONFIG", "DailyBriefService", "normalize_daily_brief_config"]
