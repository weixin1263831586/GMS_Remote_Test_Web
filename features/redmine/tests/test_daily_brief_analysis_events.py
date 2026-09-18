"""分析进度时间线（daily_brief_analysis_events）回归。

覆盖评审意见的关键场景：
- 事件仓储生命周期（append/增量/reset/保留期/孤儿清理）；
- 事件词表收敛（8 种标准化事件，不保存 kkagent 原始协议）；
- 脱敏（summary 白名单 + scrub_secrets）；
- 两个 issue 并行/排队不串事件；
- 停止分析后仍产生 cancelled 事件；
- API 增量协议（after_sequence）与跨 owner ACL。
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import features.redmine.daily_brief_repository as brief_repo
from features.redmine.daily_brief_analysis_events import (
    EVENT_TYPES,
    AnalysisProgressRecorder,
    DailyBriefAnalysisEventStore,
    describe_tool_call,
    progress_to_payload,
    scrub_secrets,
)
from features.redmine.daily_brief_repository import DailyBriefRepository


class AnalysisEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "daily_brief.sqlite3"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE redmine_daily_brief_runs "
            "(run_id TEXT PRIMARY KEY, status TEXT DEFAULT '', finished_at TEXT DEFAULT '')"
        )
        conn.commit()
        conn.close()
        self.store = DailyBriefAnalysisEventStore(self.db_path)

    def test_event_vocabulary_is_normalized(self):
        """只允许 8 种标准化事件，拒绝 kkagent 原始协议直通。"""
        self.assertEqual(len(EVENT_TYPES), 8)
        self.assertNotIn("reasoning", EVENT_TYPES)
        self.assertNotIn("assistant_message", EVENT_TYPES)
        with self.assertRaises(ValueError):
            self.store.append("run-x", 1, {"event_type": "reasoning_delta"})

    def test_append_is_monotonic_and_increment_reads_are_bounded(self):
        recorder = AnalysisProgressRecorder(self.store, "run-1", 100)
        recorder.tool_started("gms_rt_redmine_issue_fetch", {"issue_id": 100})
        recorder.tool_finished("gms_rt_redmine_issue_fetch", {"issue_id": 100}, ok=True)
        recorder.progress("模型服务暂时不稳定，已自动重试")
        recorder.analysis_finished(ok=True, model_name="glm-5.3")
        events = self.store.list_after("run-1", 100)
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4, 5])
        incremental = self.store.list_after("run-1", 100, after_sequence=3)
        self.assertEqual([event["event_type"] for event in incremental], ["progress", "analysis_completed"])
        self.assertEqual(incremental[-1]["summary"], "分析完成 · 已执行 1 次工具调用 · 模型 glm-5.3")

    def test_reset_on_restart_keeps_issues_isolated(self):
        """重分析清空旧时间线；并行 issue 的事件互不串线。"""
        first = AnalysisProgressRecorder(self.store, "run-1", 100)
        first.progress("第一轮")
        AnalysisProgressRecorder(self.store, "run-1", 100)
        self.assertEqual(
            [event["event_type"] for event in self.store.list_after("run-1", 100)],
            ["analysis_started"],
        )
        self.store.append("run-1", 101, {"event_type": "analysis_started"})
        self.assertEqual(len(self.store.list_after("run-1", 100)), 1)
        self.assertEqual(len(self.store.list_after("run-1", 101)), 1)
        self.assertEqual(self.store.list_after("run-1", 102), [])

    def test_summary_never_stores_tool_output(self):
        """工具输出/凭据不进 summary；只有身份字段的白名单摘录。"""
        text = describe_tool_call(
            "gms_rt_apk_analyze",
            {"query": "CtsCamera", "token": "sup3rsecret", "output": "x" * 900},
        )
        self.assertIn("query=CtsCamera", text)
        self.assertNotIn("sup3rsecret", text)
        self.assertNotIn("x" * 100, text)
        self.assertLessEqual(len(scrub_secrets("authorization: Bearer abc.def.ghi rest")), 160)
        self.assertIn("***", scrub_secrets("api_key=abcd1234 后续说明"))

    def test_describe_tool_call_includes_keywords_before_path(self):
        """codesearch 类检索：keywords 是比 path 更强的身份信号，排在前面。"""
        text = describe_tool_call(
            "mcp__codesearch__search",
            {"keywords": "auto_brightness_adj", "path": "packages/SystemUI", "limit": 10},
        )
        self.assertIn("keywords=auto_brightness_adj", text)
        self.assertLess(text.index("keywords="), text.index("path="))

    def test_payload_projection_is_allowlist_only(self):
        recorder = AnalysisProgressRecorder(self.store, "run-1", 100)
        recorder.tool_started("gms_rt_redmine_journals", {"snapshot_id": "snap-1"})
        rows = progress_to_payload(self.store.list_after("run-1", 100))
        for row in rows:
            self.assertEqual(
                set(row),
                {"sequence", "event_type", "stage", "tool_name", "status", "summary", "duration_ms", "created_at"},
            )

    def test_cancelled_analysis_records_terminal_event(self):
        """停止分析也要收敛出 cancelled 终态事件（UI 显示「已停止」）。"""
        recorder = AnalysisProgressRecorder(self.store, "run-1", 100)
        recorder.tool_started("gms_rt_sdk_search", {"query": "binder"})
        recorder.analysis_finished(ok=False, cancelled=True, error_type="")
        last = self.store.list_after("run-1", 100)[-1]
        self.assertEqual(last["event_type"], "analysis_failed")
        self.assertEqual(last["status"], "cancelled")
        self.assertIn("已停止", last["summary"])

    def test_purge_expired_and_orphan_events(self):
        live_recorder = AnalysisProgressRecorder(self.store, "run-live", 100)
        live_recorder.progress("进行中")
        old_recorder = AnalysisProgressRecorder(self.store, "run-old", 200)
        old_recorder.analysis_finished(ok=True)
        # run-live 无 run 行（孤儿）；run-old 终态但时间在未来（未过期）。
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO redmine_daily_brief_runs VALUES ('run-old', 'completed', '2999-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.store.purge_expired(now="2999-01-01T00:00:00"), 2)
        self.assertEqual(self.store.list_after("run-live", 100), [])
        self.assertEqual(len(self.store.list_after("run-old", 200)), 2)
        # 过期后清理；活跃 run 不受影响。
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO redmine_daily_brief_runs VALUES ('run-live', 'analyzing', '')"
        )
        conn.commit()
        conn.close()
        live_recorder.progress("仍在执行")
        self.assertEqual(self.store.purge_expired(now="2999-06-01T00:00:00"), 2)
        self.assertEqual(self.store.list_after("run-old", 200), [])
        # 孤儿阶段的事件已被第一轮清理，只剩活跃 run 上的新事件。
        self.assertEqual(len(self.store.list_after("run-live", 100)), 1)


class RepositoryEventIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch(
            "features.redmine.daily_brief_repository.owner_redmine_root",
            lambda owner: Path(self.directory.name) / "owners" / str(owner),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        brief_repo._REPO_CACHE.clear()
        self.addCleanup(brief_repo._REPO_CACHE.clear)
        self.repository = DailyBriefRepository(Path(self.directory.name) / "owners" / "alice")

    def test_event_store_is_bound_per_owner_db(self):
        from features.redmine.daily_brief_analysis_events import event_store_for_repository

        store = event_store_for_repository(self.repository)
        self.assertEqual(store.db_path, self.repository.db_path)
        self.assertIs(store, event_store_for_repository(self.repository))
        store.append("run-1", 1, {"event_type": "analysis_started"})
        self.assertEqual(
            [event["event_type"] for event in store.list_after("run-1", 1)],
            ["analysis_started"],
        )

    def test_start_analysis_progress_is_never_fatal(self):
        from features.redmine.daily_brief_analysis_events import start_analysis_progress

        service_like = SimpleNamespace(events=None, db_path=self.repository.db_path)
        recorder = start_analysis_progress(service_like, self.repository, 1)
        self.assertIsNotNone(recorder)
        recorder.analysis_finished(ok=True)


if __name__ == "__main__":
    unittest.main()
