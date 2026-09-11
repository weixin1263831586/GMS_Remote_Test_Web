"""${PROJECT_ROOT} 占位符展开：静态配置与运行时环境两条加载链路。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bootstrap import env_loader
from foundation import config as config_module
from foundation.config import config_manager


def test_config_placeholder_project_root_partial_expansion():
    value = config_manager._replace_placeholders("${PROJECT_ROOT}/tools/jadx/bin/jadx")
    assert value == str(Path(config_module.PROJECT_ROOT) / "tools/jadx/bin/jadx")


def test_config_placeholder_project_root_bare_value():
    assert config_manager._replace_placeholders("${PROJECT_ROOT}") == config_module.PROJECT_ROOT


def test_config_placeholder_project_root_is_builtin(monkeypatch):
    """内置占位符不取同名环境变量，避免宿主任意 PROJECT_ROOT 劫持部署路径。"""

    monkeypatch.setenv("PROJECT_ROOT", "/somewhere/else")
    assert config_manager._replace_placeholders("${PROJECT_ROOT}/x") == (
        config_module.PROJECT_ROOT + "/x"
    )


def test_config_placeholder_leaves_unknown_names_untouched():
    value = config_manager._replace_placeholders("${DEFINITELY_UNSET_VAR_42}/x")
    assert value == "${DEFINITELY_UNSET_VAR_42}/x"


def test_env_loader_expands_project_root_relative_to_runtime_file(tmp_path, monkeypatch):
    tree = tmp_path / "deploy"
    (tree / "configs").mkdir(parents=True)
    runtime_file = tree / "configs" / "runtime.json"
    runtime_file.write_text(
        json.dumps(
            {
                "_comment": "schema only",
                "GMS_TEST_PROBE_PATH": "${PROJECT_ROOT}/data/secrets/k.pem",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(env_loader, "_candidate_paths", lambda: [runtime_file])
    monkeypatch.delenv("GMS_SKIP_RUNTIME_ENV", raising=False)
    monkeypatch.delenv("GMS_TEST_PROBE_PATH", raising=False)
    try:
        applied = env_loader.load_runtime_env()
        expected = str(tree / "data" / "secrets" / "k.pem")
        assert applied["GMS_TEST_PROBE_PATH"] == expected
        assert os.environ["GMS_TEST_PROBE_PATH"] == expected
    finally:
        os.environ.pop("GMS_TEST_PROBE_PATH", None)
