"""DailyBriefService 编排测试（不启动真实 kkagent/Redmine）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.config import RedmineConfig
from features.redmine.daily_brief_repository import DailyBriefRepository
from features.redmine.daily_brief_service import (
    DEFAULT_BRIEF_CONFIG,
    DailyBriefService,
    normalize_daily_brief_config,
)
from features.redmine.kkagent_analyzer import KkAgentAnalysisResult


VALID = {
    "problem_summary": "summary",
    "customer_request": "request",
    "recommended_actions": [],
    "suggested_solution": "solution",
    "evidence": [],
    "confidence": 0.9,
    "root_cause_type": "likely",
}

SNAPSHOT = {
    "brief_date": "2026-09-13",
    "generated_at": "2026-09-13T00:00:03",
    "snapshot_hash": "hash-1",
    "owner": {"id": "u1", "names": ["张三"]},
    "counts": {"waiting_my_reply": 2, "no_reply_3_days": 1, "total": 2},
    "issues": [
        {"issue_id": 101, "subject": "A", "buckets": ["waiting_my_reply"],
         "priority": "P1", "priority_score": 90, "fingerprint": "fp-a",
         "status_name": "New", "updated_on": "t", "attachment_count": 1},
        {"issue_id": 102, "subject": "B", "buckets": ["waiting_my_reply", "no_reply_3_days"],
         "priority": "P2", "priority_score": 50, "fingerprint": "fp-b",
         "status_name": "New", "updated_on": "t", "attachment_count": 0},
    ],
}


class FakeConfigManager:
    def __init__(self):
        self.runtime: dict = {}

    def get_runtime_config(self):
        return dict(self.runtime)

    def save_runtime(self, runtime):
        self.runtime = dict(runtime)
        return True


def make_service(root: Path, owner: str = "u1") -> DailyBriefService:
    service = DailyBriefService.__new__(DailyBriefService)
    service.owner_id = owner
    service.config_manager = None
    service.repository = DailyBriefRepository(root / owner)
    return service


class ConfigTests(unittest.TestCase):
    def test_default_parallelism_is_single_kkagent_process(self):
        self.assertEqual(normalize_daily_brief_config({})["max_parallel_issues"], 1)

    def test_defaults_and_clamping(self):
        config = normalize_daily_brief_config({
            "max_parallel_issues": 99, "analysis_backend": "weird",
            "model": " glm-4 ", "max_turns": "bad",
        })
        self.assertEqual(config["max_parallel_issues"], 4)
        self.assertEqual(config["analysis_backend"], "kkagent")
        self.assertEqual(config["model"], "glm-4")
        self.assertEqual(config["max_turns"], DEFAULT_BRIEF_CONFIG["max_turns"])

    def test_enabled_defaults_to_opt_in(self):
        """晨报默认关闭：未显式 enabled=true 的 owner 不进入 nightly 调度。"""
        self.assertFalse(DEFAULT_BRIEF_CONFIG["enabled"])
        self.assertFalse(normalize_daily_brief_config({})["enabled"])
        self.assertTrue(normalize_daily_brief_config({"enabled": True})["enabled"])

    def test_backend_direct_is_rejected(self):
        """direct 从未实现，不得作为可配置枚举残留。"""
        self.assertEqual(
            normalize_daily_brief_config({"analysis_backend": "direct"})["analysis_backend"],
            "kkagent",
        )
        self.assertEqual(
            normalize_daily_brief_config({"analysis_backend": "kkagent"})["analysis_backend"],
            "kkagent",
        )

    def test_save_and_get_roundtrip(self):
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = make_service(Path(tmp))
            self.assertTrue(service.save_config(manager, {"max_issues": 10, "model": "m1"}))
            self.assertEqual(service.get_config(manager)["max_issues"], 10)
            self.assertEqual(service.get_config(manager)["model"], "m1")

    def test_redmine_config_facade_supports_daily_brief_runtime(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = RedmineConfig(project_root=root)
            manager.runtime_config_path = root / "owner" / "config_runtime.json"
            service = make_service(root)

            self.assertTrue(service.save_config(manager, {"agent_profile": "kkagent-owner"}))
            self.assertEqual(service.get_config(manager)["agent_profile"], "kkagent-owner")


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = make_service(self.root)

    def _patch_snapshot(self):
        from unittest.mock import AsyncMock
        mock = AsyncMock(return_value=dict(SNAPSHOT))
        return patch.object(self.service, "build_triage", mock)

    def test_start_run_idempotent_and_retry(self):
        first = self.service.start_run("nightly")
        run_id = first["run_id"]
        again = self.service.start_run("nightly")
        self.assertEqual(again["run_id"], run_id)
        self.assertTrue(again.get("already_running"))
        # failed → retry 复用同一 run
        run = self.service.repository.get_run(run_id)
        run.status = "failed"
        self.service.repository.update_run(run)
        retry = self.service.start_run("nightly")
        self.assertEqual(retry["run_id"], run_id)
        self.assertEqual(retry["status"], "pending")

    def test_force_reruns_completed_manual_run(self):
        first = self.service.start_run("manual")
        run = self.service.repository.get_run(first["run_id"])
        run.status = "completed"
        run.finished_at = "2026-09-12T10:00:00"
        run.report_json = {"counts": {"completed": 2}}
        run.report_markdown = "old"
        self.service.repository.update_run(run)

        reused = self.service.start_run("manual")
        self.assertTrue(reused["reused"])
        forced = self.service.start_run("manual", force=True)
        self.assertEqual(forced["run_id"], first["run_id"])
        self.assertEqual(forced["status"], "pending")
        refreshed = self.service.repository.get_run(first["run_id"])
        self.assertEqual(refreshed.finished_at, "")
        self.assertEqual(refreshed.report_json, {})
        self.assertEqual(refreshed.report_markdown, "")

    def test_force_rerun_replaces_old_issue_snapshot(self):
        import asyncio
        from unittest.mock import AsyncMock

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        started = self.service.start_run("manual")
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            asyncio.run(self.service.execute_run(started["run_id"]))
        self.assertEqual(
            {item.issue_id for item in self.service.repository.list_issues(started["run_id"])},
            {101, 102},
        )

        self.service.start_run("manual", force=True)
        replacement = dict(SNAPSHOT)
        replacement["issues"] = [dict(SNAPSHOT["issues"][0])]
        replacement["counts"] = {
            "waiting_my_reply": 1, "no_reply_3_days": 0, "total": 1
        }
        with patch.object(
            self.service, "build_triage", AsyncMock(return_value=replacement)
        ), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            run = asyncio.run(self.service.execute_run(started["run_id"]))

        self.assertEqual(run.issue_count, 1)
        self.assertEqual(
            [item.issue_id for item in self.service.repository.list_issues(started["run_id"])],
            [101],
        )

    def test_execute_run_completes_with_fake_analyzer(self):
        started = self.service.start_run("nightly")
        run_id = started["run_id"]

        async def fake_analyze(entry):
            if entry.get("issue_id") == 101:
                return KkAgentAnalysisResult(ok=True, result=dict(VALID), raw_output="{}")
            return KkAgentAnalysisResult(ok=False, error="boom", error_type="kkagent_error")

        import asyncio
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            analyzer = builder.return_value
            analyzer.analyze = fake_analyze
            run = asyncio.run(self.service.execute_run(run_id))

        self.assertEqual(run.status, "partial")  # 101 ok / 102 failed
        issues = self.service.repository.list_issues(run_id)
        by_id = {i.issue_id: i for i in issues}
        self.assertEqual(by_id[101].status, "completed")
        self.assertEqual(by_id[101].result["problem_summary"], "summary")
        self.assertEqual(by_id[102].status, "failed")
        self.assertEqual(by_id[102].error_type, "kkagent_error")
        self.assertEqual(run.report_json["counts"]["completed"], 1)
        self.assertIn("#101", run.report_markdown)

    def test_execute_run_all_failed_marks_failed(self):
        started = self.service.start_run("manual")
        run_id = started["run_id"]

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=False, error="down", error_type="kkagent_unavailable")

        import asyncio
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            run = asyncio.run(self.service.execute_run(run_id))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error, "2 issue analyses failed")

    def test_snapshot_freeze_and_counts(self):
        started = self.service.start_run("nightly")
        run_id = started["run_id"]

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        import asyncio
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            run = asyncio.run(self.service.execute_run(run_id))
        self.assertEqual(run.snapshot_hash, "hash-1")
        self.assertEqual(run.issue_count, 2)
        self.assertEqual(run.waiting_my_reply_count, 2)
        self.assertEqual(run.no_reply_3_days_count, 1)
        self.assertEqual(run.status, "completed")

    def test_run_payload_hides_raw_response(self):
        started = self.service.start_run("manual")
        run_id = started["run_id"]

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID), raw_output="SECRET-RAW")

        import asyncio
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            asyncio.run(self.service.execute_run(run_id))
        payload = self.service.run_payload(self.service.repository.get_run(run_id))
        for issue in payload["issues"]:
            self.assertNotIn("raw_response", issue)

    def test_reanalyze_refreshes_run_summary_and_uses_persisted_subject(self):
        started = self.service.start_run("manual")
        run_id = started["run_id"]

        async def initial_analyze(entry):
            if entry.get("issue_id") == 101:
                return KkAgentAnalysisResult(ok=True, result=dict(VALID))
            return KkAgentAnalysisResult(
                ok=False, error="bad output", error_type="schema_mismatch"
            )

        import asyncio
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = initial_analyze
            run = asyncio.run(self.service.execute_run(run_id))
        self.assertEqual(run.report_json["counts"]["completed"], 1)
        self.assertEqual(run.report_json["counts"]["failed"], 1)

        seen_entry = {}

        async def retry_analyze(entry):
            seen_entry.update(entry)
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        with patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = retry_analyze
            result = asyncio.run(self.service.reanalyze_issue(run.brief_date, 102))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen_entry["subject"], "B")
        refreshed = self.service.repository.get_run(run_id)
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.report_json["counts"]["completed"], 2)
        self.assertEqual(refreshed.report_json["counts"]["failed"], 0)
        subjects = {item["subject"] for item in refreshed.report_json["top_priorities"]}
        self.assertEqual(subjects, {"A", "B"})


class AnalyzerBindingTests(unittest.TestCase):
    """kkagent 分析器必须绑定 owner 的 profile，且不继承他人身份。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))

    def test_profile_binds_mcp_identity_env(self):
        analyzer = self.service._build_analyzer({"agent_profile": "owner-a"})
        self.assertEqual(analyzer.env_extra.get("GMS_RT_PROFILE"), "owner-a")
        self.assertEqual(analyzer.env_extra.get("GMS_AGENT_AUTH_MODE"), "service-token")

    def test_without_profile_no_identity_injected(self):
        analyzer = self.service._build_analyzer({})
        self.assertNotIn("GMS_RT_PROFILE", analyzer.env_extra)
        self.assertNotIn("GMS_AUTH_TOKEN_FILE", analyzer.env_extra)

    def test_profile_is_sanitized(self):
        config = normalize_daily_brief_config({"agent_profile": "bad name;rm -rf"})
        self.assertEqual(config["agent_profile"], "")


class CrashRecoveryTests(unittest.TestCase):
    """启动前把中断遗留的 run 标记为 failed。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))

    def test_start_run_recovers_interrupted(self):
        from datetime import datetime, timedelta

        from features.redmine.daily_brief_models import DailyBriefRun

        stale = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="nightly",
            run_id="db_stale", status="analyzing",
            started_at=(datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"),
        )
        self.service.repository.create_run(stale)
        self.service.start_run("nightly")
        recovered = self.service.repository.get_run("db_stale")
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.error, "interrupted by process restart")


class FrozenSnapshotTests(unittest.TestCase):
    """冻结快照持久化：崩溃重试不得改变同一 run 的输入事实。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = make_service(self.root)

    def _execute_failed_run(self, service) -> str:
        from unittest.mock import AsyncMock

        started = service.start_run("nightly")

        async def fail_analyze(entry):
            return KkAgentAnalysisResult(ok=False, error="crash", error_type="kkagent_error")

        with patch.object(service, "build_triage", AsyncMock(return_value=dict(SNAPSHOT))), \
                patch.object(service, "_build_analyzer") as builder:
            builder.return_value.analyze = fail_analyze
            import asyncio
            asyncio.run(service.execute_run(started["run_id"]))
        return started["run_id"]

    def test_crash_retry_reuses_persisted_frozen_snapshot(self):
        """进程崩溃（新 service 实例 = 新进程语义）后重试：不得重新拉取
        Redmine（build_triage 不再调用），分析输入仍是冻结快照。"""
        from unittest.mock import AsyncMock

        run_id = self._execute_failed_run(self.service)
        frozen_before = self.service.repository.get_snapshot(run_id)
        self.assertIsNotNone(frozen_before)

        # 模拟进程重启：全新 service 实例，仅共享 SQLite。
        restarted = make_service(self.root)
        retry = restarted.start_run("nightly")
        self.assertEqual(retry["run_id"], run_id)

        async def ok_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        triage = AsyncMock(return_value=dict(SNAPSHOT))
        with patch.object(restarted, "build_triage", triage), \
                patch.object(restarted, "_build_analyzer") as builder:
            builder.return_value.analyze = ok_analyze
            import asyncio
            run = asyncio.run(restarted.execute_run(run_id))

        triage.assert_not_called()  # 冻结快照优先，Redmine 变化不影响重试
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.snapshot_hash, "hash-1")
        self.assertEqual(
            {item.issue_id for item in restarted.repository.list_issues(run_id)},
            {101, 102},
        )

    def test_sync_failed_marks_run_and_report(self):
        from unittest.mock import AsyncMock

        snapshot = dict(SNAPSHOT)
        snapshot["source_sync_status"] = "sync_failed"
        snapshot["synced"] = False
        started = self.service.start_run("nightly")

        async def ok_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        with patch.object(self.service, "build_triage", AsyncMock(return_value=snapshot)), \
                patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = ok_analyze
            import asyncio
            run = asyncio.run(self.service.execute_run(started["run_id"]))

        self.assertEqual(run.source_sync_status, "sync_failed")
        self.assertEqual(run.report_json["source_sync_status"], "sync_failed")
        self.assertIn("本地镜像", run.report_markdown)

    def test_concurrent_execute_run_joins_single_execution(self):
        """同 run_id 并发 execute_run：只执行一次快照/分析（RunCoordinator）。"""
        from unittest.mock import AsyncMock

        started = self.service.start_run("nightly")
        calls: list[int] = []

        async def ok_analyze(entry):
            calls.append(entry["issue_id"])
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        triage = AsyncMock(return_value=dict(SNAPSHOT))

        async def scenario():
            with patch.object(self.service, "build_triage", triage), \
                    patch.object(self.service, "_build_analyzer") as builder:
                builder.return_value.analyze = ok_analyze
                await asyncio.gather(
                    self.service.execute_run(started["run_id"]),
                    self.service.execute_run(started["run_id"]),
                )

        import asyncio
        asyncio.run(scenario())

        triage.assert_called_once()
        self.assertEqual(sorted(calls), [101, 102])  # 每个 issue 只分析一次

    def test_reanalyze_blocked_while_run_executing(self):
        from unittest.mock import AsyncMock

        started = self.service.start_run("nightly")
        import asyncio

        async def scenario():
            async def slow_analyze(entry):
                await asyncio.sleep(0.05)
                return KkAgentAnalysisResult(ok=True, result=dict(VALID))

            triage = AsyncMock(return_value=dict(SNAPSHOT))
            with patch.object(self.service, "build_triage", triage), \
                    patch.object(self.service, "_build_analyzer") as builder:
                builder.return_value.analyze = slow_analyze
                task = asyncio.create_task(self.service.execute_run(started["run_id"]))
                await asyncio.sleep(0.01)  # 让 run 进入 executing
                blocked = await self.service.reanalyze_issue(
                    self.service.repository.get_run(started["run_id"]).brief_date, 101
                )
                run = await task
            return blocked, run

        blocked, run = asyncio.run(scenario())
        self.assertIn("still executing", blocked.get("error", ""))
        self.assertEqual(run.status, "completed")


def _async_value(value):  # pragma: no cover - 保留给未来同步 mock 使用
    async def coro(**kwargs):
        return value
    return coro()


if __name__ == "__main__":
    unittest.main()
