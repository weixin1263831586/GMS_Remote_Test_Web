"""Fresh-clone and upgrade regression checks.

fresh-clone：模拟新 clone 的仓库（无 data/、无 configs/local、
无 configs/secrets）能否完成 Controller 启动初始化；同时校验根
.gitignore 的 source/local 契约仍然成立（历史教训：提交信息声称更新
.gitignore 而文件实际丢失，导致 fresh clone 直接 FileNotFoundError）。

upgrade：模拟"旧版本部署数据 + 新版本代码"的最小升级面——
旧库（缺列/user_version=0）在升级后的代码上打开时迁移成功且
user_version 盖章。完整 upgrade matrix（旧 Agent、旧证书）由
 nightly soak 覆盖，本文件守住"每次提交至少不能炸在已知的迁移点"。

在 CI 中归入 repo-contract gate（tests/architecture 全量运行）。
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FreshCloneContractTests(unittest.TestCase):
    """fresh clone 的仓库状态契约（不实际 clone，校验 tracked 状态）。"""

    def test_repository_tracks_root_gitignore(self):
        self.assertTrue((ROOT / '.gitignore').is_file())
        tracked = subprocess.run(
            ['git', '-C', str(ROOT), 'ls-files', '--error-unmatch', '.gitignore'],
            capture_output=True,
            text=True,
        )
        self.assertEqual(tracked.returncode, 0, '.gitignore must be tracked')

    def test_gitignore_tracks_local_contract(self):
        text = (ROOT / '.gitignore').read_text(encoding='utf-8')
        # test_config_examples.py 依赖这些规则遮蔽运行时/本地文件。
        # /configs/* 通配已覆盖 configs/local 与 configs/secrets。
        for required in ('/configs/*', '/data/', '.pytest_cache'):
            self.assertIn(required, text, f'.gitignore 缺少 {required}')

    def test_no_tracked_local_runtime_files(self):
        tracked = subprocess.run(
            ['git', '-C', str(ROOT), 'ls-files',
             'configs/local', 'configs/secrets', 'data/'],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        offenders = [
            path for path in tracked
            if not path.endswith('.gitkeep') and 'example' not in path
        ]
        self.assertEqual(offenders, [])


class UpgradeMigrationSmokeTests(unittest.TestCase):
    """旧库在新代码上的最小升级冒烟（daily_brief 为当前唯一版本化库）。"""

    def test_legacy_daily_brief_db_upgrades_in_place(self):
        import sqlite3
        import sys

        sys.path.insert(0, str(ROOT))
        from features.redmine.daily_brief_repository import DailyBriefRepository

        with tempfile.TemporaryDirectory() as tmp:
            owner = Path(tmp)
            db = owner / 'daily_brief.sqlite3'
            conn = sqlite3.connect(db)
            # 上一版完整 schema：基础列齐全，仅缺后续迁移加的列。
            conn.execute(
                "CREATE TABLE redmine_daily_brief_runs ("
                "run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
                "brief_date TEXT NOT NULL, mode TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', "
                "started_at TEXT NOT NULL DEFAULT '', "
                "finished_at TEXT NOT NULL DEFAULT '', "
                "snapshot_at TEXT NOT NULL DEFAULT '', "
                "snapshot_hash TEXT NOT NULL DEFAULT '', "
                "issue_count INTEGER NOT NULL DEFAULT 0, "
                "waiting_my_reply_count INTEGER NOT NULL DEFAULT 0, "
                "no_reply_3_days_count INTEGER NOT NULL DEFAULT 0, "
                "urgent_count INTEGER NOT NULL DEFAULT 0, "
                "analysis_backend TEXT NOT NULL DEFAULT '', "
                "model_name TEXT NOT NULL DEFAULT '', "
                "prompt_version TEXT NOT NULL DEFAULT '', "
                "report_json TEXT NOT NULL DEFAULT '{}', "
                "report_markdown TEXT NOT NULL DEFAULT '', "
                "error TEXT NOT NULL DEFAULT '', "
                "created_at TEXT NOT NULL DEFAULT '', "
                "updated_at TEXT NOT NULL DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO redmine_daily_brief_runs "
                "(run_id, owner_id, brief_date, mode, status) "
                "VALUES ('db_old', 'u1', '2026-09-13', 'nightly', 'completed')"
            )
            conn.commit()
            conn.close()

            repo = DailyBriefRepository(owner)
            self.assertEqual(repo.get_run('db_old').status, 'completed')

            conn = sqlite3.connect(db)
            try:
                version = conn.execute('PRAGMA user_version').fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(version, DailyBriefRepository._SCHEMA_VERSION)


if __name__ == '__main__':
    unittest.main()
