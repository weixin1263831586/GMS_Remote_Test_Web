"""分析器身份绑定与启动期崩溃恢复测试（从 test_daily_brief_service 拆出）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.redmine.daily_brief_service import DEFAULT_BRIEF_CONFIG, normalize_daily_brief_config
from features.redmine.tests.test_daily_brief_service import make_service


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

    def test_analysis_ignores_legacy_step_budgets(self):
        self.assertEqual(DEFAULT_BRIEF_CONFIG["max_turns"], 0)
        analyzer = self.service._build_analyzer({})
        self.assertEqual(analyzer.max_turns, DEFAULT_BRIEF_CONFIG["max_turns"])
        analyzer = self.service._build_analyzer({"max_turns": 30})
        self.assertEqual(analyzer.max_turns, 0)


class CrashRecoveryTests(unittest.TestCase):
    """启动前把中断遗留的 run 标记为 failed。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))

    def test_start_run_recovers_interrupted(self):
        from datetime import datetime, timedelta

        from features.redmine.daily_brief_models import DailyBriefRun

        # brief_date 必须避开"今天"：恢复把 stale run 标记 failed 后，
        # start_run 会按产品逻辑复用【当天】的 nightly run 并重置为
        # pending 重试——与当天同日的 stale run 会遮蔽恢复断言
        # （该断言只在非当天日期下成立）。
        brief_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        stale = DailyBriefRun(
            owner_id="u1", brief_date=brief_date, mode="nightly",
            run_id="db_stale", status="analyzing",
            started_at=(datetime.now() - timedelta(days=3)).isoformat(timespec="seconds"),
        )
        self.service.repository.create_run(stale)
        self.service.start_run("nightly")
        recovered = self.service.repository.get_run("db_stale")
        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.error, "interrupted by process restart")


    def test_old_running_job_is_not_interrupted_by_age(self):
        from features.redmine.daily_brief_models import DailyBriefRun

        run = DailyBriefRun(owner_id='u1', brief_date='2020-01-01', mode='manual',
                            run_id='old-live', status='analyzing', started_at='2020-01-01T00:00:00')
        self.service.repository.create_run_and_enqueue_job(run)
        self.service.repository.update_run(run)
        self.service._recover_interrupted_runs({})
        self.assertEqual(self.service.repository.get_run('old-live').status, 'analyzing')


if __name__ == "__main__":
    unittest.main()
