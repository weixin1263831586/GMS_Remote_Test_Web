"""DailyBriefService 编排测试（不启动真实 kkagent/Redmine）。

preflight fail-closed 语义（2026-09 收紧）由 kkagent/auth_preflight 的
专项单测覆盖；本文件的编排路径统一把 preflight patch 为通过。
run_payload 执行视图/脱敏契约见 test_daily_brief_execution_view.py。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.redmine.config import RedmineConfig
from features.redmine.daily_brief_repository import DailyBriefRepository
from features.redmine.daily_brief_service import (
    DEFAULT_BRIEF_CONFIG,
    DailyBriefService,
    normalize_daily_brief_config,
)
from features.redmine.kkagent import KkAgentAnalysisResult
from features.redmine.kkagent.evidence_preflight import EvidencePreflight


PASS_PREFLIGHT_TARGET = "features.redmine.daily_brief_service.preflight_gms_auth"


def patch_preflight_ok():
    return patch(
        PASS_PREFLIGHT_TARGET, AsyncMock(return_value=(True, ""))
    )


VALID = {
    "problem_summary": "summary",
    "customer_request": "request",
    "recommended_actions": [],
    "suggested_solution": "solution",
    "detailed_report": "## 一、问题概况\n\n| 项目 | 内容 |\n|---|---|\n| Issue | #101 |",
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
        self.assertFalse(normalize_daily_brief_config({"enabled": "false"})["enabled"])
        self.assertTrue(normalize_daily_brief_config({"enabled": " yes "})["enabled"])
        self.assertFalse(normalize_daily_brief_config({"enabled": "invalid"})["enabled"])

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
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)
        evidence_preflight = patch(
            "features.redmine.daily_brief_deep_analysis.collect_deep_analysis_evidence",
            AsyncMock(return_value=EvidencePreflight()),
        )
        evidence_preflight.start()
        self.addCleanup(evidence_preflight.stop)
        self.evidence_preflight = evidence_preflight

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
        # cancelled → 同样可重试,且重试清掉取消标志
        run = self.service.repository.get_run(run_id)
        run.status = "cancelled"
        self.service.repository.update_run(run)
        self.service.repository.request_cancel(run_id)  # 残留标志
        retry2 = self.service.start_run("nightly")
        self.assertEqual(retry2["run_id"], run_id)
        self.assertFalse(self.service.repository.is_cancel_requested(run_id))

    def test_request_cancel_converges_run_to_cancelled(self):
        """用户停止:执行循环在 issue 边界读到标志,收敛为 cancelled 终态。"""
        import asyncio

        started = self.service.start_run("nightly")
        run_id = started["run_id"]

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        async def scenario():
            with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
                builder.return_value.analyze = fake_analyze
                task = asyncio.create_task(self.service.execute_run(run_id))
                await asyncio.sleep(0)  # 让执行任务起跑
                requested = self.service.request_cancel()
                self.assertTrue(requested.get("cancel_requested"))
                return await task

        run = asyncio.run(scenario())
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.error, "")
        self.assertTrue(run.finished_at)

    def test_request_cancel_is_noop_for_terminal_run(self):
        started = self.service.start_run("nightly")
        run = self.service.repository.get_run(started["run_id"])
        run.status = "completed"
        self.service.repository.update_run(run)

        result = self.service.request_cancel()

        self.assertTrue(result.get("already_terminal"))
        self.assertFalse(self.service.repository.is_cancel_requested(started["run_id"]))

    def test_execute_run_returns_cancelled_state_for_late_queue_task(self):
        """cancelled 已终态:迟到的队列任务直接返回,不复活 run。"""
        import asyncio

        started = self.service.start_run("nightly")
        run = self.service.repository.get_run(started["run_id"])
        run.status = "cancelled"
        run.finished_at = "2026-09-13T10:00:00"
        self.service.repository.update_run(run)

        result = asyncio.run(self.service.execute_run(started["run_id"]))

        self.assertEqual(result.status, "cancelled")

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
        self.assertNotIn("### 详细分析报告", run.report_markdown)
        self.assertNotIn("## 一、问题概况", run.report_markdown)
        self.assertIn("工单详情", run.report_markdown)

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

    def test_deep_analysis_precollects_bound_evidence_before_kkagent(self):
        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
        from features.redmine.kkagent.trace import ToolTrace

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-16", mode="issue:652654",
            run_id="db_deep", device_serial="RK3576GMS1", status="analyzing",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=652654, buckets=[], subject="CTS failure",
        ))
        evidence = EvidencePreflight(
            traces=[ToolTrace(
                tool_name="gms_rt_devices_snapshot", status="succeeded",
                tool_input={"device": "RK3576GMS1"},
            )],
            snapshot_id="ev_652654", device_status="succeeded",
        )
        self.evidence_preflight.stop()
        collector = patch(
            "features.redmine.daily_brief_deep_analysis.collect_deep_analysis_evidence",
            AsyncMock(return_value=evidence),
        )
        collector_mock = collector.start()
        self.addCleanup(collector.stop)
        seen: dict = {}

        class Analyzer:
            env_extra = {"GMS_RT_PROFILE": "kkagent-profile"}

            async def analyze(self, entry):
                seen.update(entry)
                return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        import asyncio
        asyncio.run(self.service._analyze_one(
            run, 652654, {
                "issue_id": 652654, "analysis_mode": "diagnostic",
                "device_serial": "RK3576GMS1",
            },
            Analyzer(), {},
        ))

        self.assertEqual(seen["_evidence_preflight"], {
            "snapshot_id": "ev_652654", "device_status": "succeeded",
        })
        self.assertEqual(seen["_precollected_tool_traces"], evidence.traces)
        self.assertEqual(collector_mock.await_args.kwargs["issue_id"], 652654)
        self.assertEqual(collector_mock.await_args.kwargs["device_serial"], "RK3576GMS1")

    def test_triage_precollects_redmine_baseline_without_device(self):
        """triage 模式的预采集只收 Redmine 基线、不含设备维度（#653167
        nightly 复盘的兜底产物）。批量晨报现已默认 diagnostic（与单号分析
        同语义），此分支保留用于历史持久化结果的兼容渲染；profile 未绑定
        时保持跳过。"""
        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
        from features.redmine.kkagent.trace import ToolTrace

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-18", mode="nightly",
            run_id="db_triage", device_serial="RK3576GMS1", status="analyzing",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=653167, buckets=["waiting_my_reply"],
            subject="rk3588 POWER",
        ))
        evidence = EvidencePreflight(
            traces=[ToolTrace(
                tool_name="gms_rt_redmine_issue_fetch", status="succeeded",
                tool_input={"issue_id": 653167}, snapshot_ids=["ev_t"],
            )],
            snapshot_id="ev_t",
        )
        self.evidence_preflight.stop()
        collector = patch(
            "features.redmine.daily_brief_deep_analysis.collect_deep_analysis_evidence",
            AsyncMock(return_value=evidence),
        )
        collector_mock = collector.start()
        self.addCleanup(collector.stop)
        seen: dict = {}

        class Analyzer:
            env_extra = {"GMS_RT_PROFILE": "kkagent-profile"}

            async def analyze(self, entry):
                seen.update(entry)
                return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        import asyncio
        asyncio.run(self.service._analyze_one(
            run, 653167, {
                "issue_id": 653167, "analysis_mode": "triage",
                "device_serial": "RK3576GMS1",
            },
            Analyzer(), {},
        ))

        self.assertEqual(seen["_evidence_preflight"], {
            "snapshot_id": "ev_t", "device_status": "not_requested",
        })
        self.assertEqual(seen["_precollected_tool_traces"], evidence.traces)
        kwargs = collector_mock.await_args.kwargs
        self.assertTrue("include_device" in kwargs or "device_serial" in kwargs)
        # triage 不做设备取证：序列号为空 + include_device=False（任一信号）。
        self.assertFalse(kwargs.get("include_device", True))
        self.assertEqual(kwargs.get("device_serial", ""), "")

    def test_batch_phase_matches_single_issue_analysis_mode(self):
        """晨报逐 issue 分析与「Redmine 单号分析」完全同语义：批量阶段
        不再强制 triage，entry 不携带 analysis_mode，由 _analyze_one
        统一 setdefault 成 diagnostic（晨报只差在固定时间触发）。"""
        import asyncio

        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-18", mode="nightly",
            run_id="db_deep_batch", status="analyzing",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=652654, buckets=["waiting_my_reply"],
            subject="CTS failure",
        ))
        seen: list[dict] = []

        async def fake_analyze(run_arg, issue_id, entry, analyzer, config):
            seen.append(dict(entry))

        with patch.object(self.service, "_analyze_one", side_effect=fake_analyze):
            asyncio.run(self.service._analyze_phase(run, {}))

        self.assertEqual(len(seen), 1)
        self.assertNotIn("analysis_mode", seen[0])

class ReanalyzeMetadataTests(unittest.TestCase):
    """单号 run 无晨报快照时，fallback entry 须从本地镜像补齐报告人等元数据。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = make_service(self.root)
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)
        evidence_preflight = patch(
            "features.redmine.daily_brief_deep_analysis.collect_deep_analysis_evidence",
            AsyncMock(return_value=EvidencePreflight()),
        )
        evidence_preflight.start()
        self.addCleanup(evidence_preflight.stop)

    def test_reanalyze_fallback_entry_enriches_local_metadata(self):
        import asyncio

        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-18", mode="issue:653167",
            run_id="db_meta", status="pending",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=653167, buckets=[], subject="rk3588 POWER",
        ))
        stored = {
            "author_name": "张三", "assigned_to_name": "黄超群",
            "created_on": "2026-09-16", "updated_on": "2026-09-18",
            "status_name": "进行中",
        }
        seen: dict = {}

        class Analyzer:
            async def analyze(self, entry):
                seen.update(entry)
                return KkAgentAnalysisResult(ok=True, result={"detailed_report": "ok"})

        async def scenario():
            with patch(
                "features.redmine.api.get_redmine_service_for_owner",
                lambda owner: SimpleNamespace(
                    repository=SimpleNamespace(get_issue=lambda iid: stored),
                ),
            ), patch.object(self.service, "_build_analyzer") as builder:
                builder.return_value = Analyzer()
                await self.service.reanalyze_issue("2026-09-18", 653167, run_id="db_meta")

        asyncio.run(scenario())
        self.assertEqual(seen.get("author_name"), "张三")
        self.assertEqual(seen.get("assigned_to_name"), "黄超群")
        self.assertEqual(seen.get("created_on"), "2026-09-16")
        self.assertEqual(seen.get("status_name"), "进行中")


class SyncDisplaySubjectsTests(unittest.TestCase):
    """刷新时同步 Redmine 最新标题：展示记录与本地镜像一起换新。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = make_service(self.root)

    def test_sync_updates_display_records_and_mirror(self):
        import asyncio

        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-18", mode="issue:653167",
            run_id="db_subj", status="completed",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=653167, buckets=[], subject="rk3588 POWER",
        ))
        new_subject = "rk3588 Android16 SSI GMS测试项支持----POWER问题"
        mirror_updates: dict = {}
        fake_client = SimpleNamespace(
            fetch_issue_subjects=AsyncMock(return_value={653167: new_subject}),
            close=AsyncMock(),
        )
        fake_redmine = SimpleNamespace(
            agent=SimpleNamespace(_make_client=lambda: fake_client),
            repository=SimpleNamespace(
                update_issue_subjects=lambda subjects: mirror_updates.update(subjects) or 1,
            ),
        )

        async def scenario():
            with patch(
                "features.redmine.api.get_redmine_service_for_owner",
                lambda owner: fake_redmine,
            ):
                return await self.service.sync_display_subjects()

        summary = asyncio.run(scenario())
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(mirror_updates, {653167: new_subject})
        self.assertEqual(
            self.service.repository.get_issue(run.run_id, 653167).subject,
            new_subject,
        )

    def test_sync_survives_redmine_unavailability(self):
        from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun

        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-18", mode="nightly",
            run_id="db_subj2", status="completed",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=652135, buckets=[], subject="旧标题",
        ))

        async def scenario():
            with patch(
                "features.redmine.api.get_redmine_service_for_owner",
                side_effect=RuntimeError("no credentials"),
            ):
                return await self.service.sync_display_subjects()

        summary = __import__("asyncio").run(scenario())
        self.assertEqual(summary["updated"], 0)
        self.assertIn("error", summary)
        # Redmine 不可用时本地标题保持原样，不阻断刷新。
        self.assertEqual(
            self.service.repository.get_issue(run.run_id, 652135).subject, "旧标题",
        )


class FrozenSnapshotTests(unittest.TestCase):
    """冻结快照持久化：崩溃重试不得改变同一 run 的输入事实。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.service = make_service(self.root)
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)

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


if __name__ == "__main__":
    unittest.main()
