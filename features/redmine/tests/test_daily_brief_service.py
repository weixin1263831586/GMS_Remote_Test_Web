"""DailyBriefService 编排测试（不启动真实 kkagent/Redmine）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
    def test_defaults_and_clamping(self):
        config = normalize_daily_brief_config({
            "max_parallel_issues": 99, "analysis_backend": "weird",
            "model": " glm-4 ", "max_turns": "bad",
        })
        self.assertEqual(config["max_parallel_issues"], 4)
        self.assertEqual(config["analysis_backend"], "kkagent")
        self.assertEqual(config["model"], "glm-4")
        self.assertEqual(config["max_turns"], DEFAULT_BRIEF_CONFIG["max_turns"])

    def test_backend_direct_allowed(self):
        self.assertEqual(normalize_daily_brief_config({"analysis_backend": "direct"})["analysis_backend"], "direct")

    def test_save_and_get_roundtrip(self):
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = make_service(Path(tmp))
            self.assertTrue(service.save_config(manager, {"max_issues": 10, "model": "m1"}))
            self.assertEqual(service.get_config(manager)["max_issues"], 10)
            self.assertEqual(service.get_config(manager)["model"], "m1")


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


def _async_value(value):  # pragma: no cover - 保留给未来同步 mock 使用
    async def coro(**kwargs):
        return value
    return coro()


if __name__ == "__main__":
    unittest.main()
