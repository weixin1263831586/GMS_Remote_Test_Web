"""RedmineConfig 持久化返回值语义（回归：失败必须返回 False）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.config import RedmineConfig as ConfigManager
from features.redmine.dashboard import normalize_redmine_stats_config


def _make_manager(root: Path) -> ConfigManager:
    (root / "core").mkdir(parents=True, exist_ok=True)
    configs = root / "configs"
    configs.mkdir(exist_ok=True)
    (configs / "config_runtime.json").write_text("{}", encoding="utf-8")
    return ConfigManager(base_dir=str(root / "core"))


class CredentialPersistenceResultTests(unittest.TestCase):
    def test_save_redmine_credentials_returns_false_when_persistence_fails(self):
        """持久化失败必须返回 False，而不是把 dict 当 bool 用。"""
        with TemporaryDirectory() as tmp:
            manager = _make_manager(Path(tmp))
            with patch.object(manager.manager, "save_runtime", return_value=False) as save_runtime:
                self.assertFalse(manager.save_redmine_credentials("user-1", "pass-1"))
                save_runtime.assert_called_once()

    def test_save_runtime_section_returns_real_persistence_result(self):
        """_save_runtime_section 不得无条件返回 True，且只合并单个 section。"""
        with TemporaryDirectory() as tmp:
            manager = _make_manager(Path(tmp))
            with patch.object(
                manager.manager, "save_runtime", return_value=False
            ) as save_runtime:
                self.assertFalse(manager.save_redmine_stats_config({"stale_days": 20}))
                # 只传入被修改的 section（经 normalize 后），避免陈旧全量文档覆盖其它键。
                save_runtime.assert_called_once_with({
                    "redmine_stats": normalize_redmine_stats_config({"stale_days": 20}),
                })


if __name__ == "__main__":
    unittest.main()
