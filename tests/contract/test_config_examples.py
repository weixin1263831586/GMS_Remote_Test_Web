"""Configuration template migration and fresh-release compatibility."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from foundation.config import ConfigManager
from foundation.config_paths import example_config_path
from scripts.sanitize_release_config import sanitize_file


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("legacy", [False, True])
def test_template_fallback_and_local_write_target(tmp_path, legacy):
    configs = tmp_path / "configs"
    templates = configs if legacy else configs / "examples"
    templates.mkdir(parents=True)
    template = templates / "config.example.json"
    source = '{"ubuntu_user": "example", "sidebar_order": []}'
    template.write_text(source, encoding="utf-8")
    manager = ConfigManager(project_root=tmp_path)

    assert manager.load_config()["ubuntu_user"] == "example"
    assert manager._is_cache_valid(time.time())
    assert manager.save_config({"ubuntu_user": "local"})
    assert manager.update_runtime_config({"sidebar_order": ["devices"]})
    assert manager.load_config() == {
        "ubuntu_user": "local", "sidebar_order": ["devices"],
    }
    assert template.read_text(encoding="utf-8") == source
    assert (configs / "local/config.json").is_file()
    assert (tmp_path / "data/settings/preferences.json").is_file()


def test_grouped_template_takes_precedence_over_legacy(tmp_path):
    configs = tmp_path / "configs"
    (configs / "examples").mkdir(parents=True)
    (configs / "config.example.json").write_text('{"source": "legacy"}', encoding="utf-8")
    grouped = configs / "examples/config.example.json"
    grouped.write_text('{"source": "grouped"}', encoding="utf-8")

    assert example_config_path(tmp_path) == grouped
    assert ConfigManager(project_root=tmp_path).load_config() == {"source": "grouped"}


@pytest.mark.parametrize("legacy", [False, True])
def test_release_materializes_missing_configs_from_templates(tmp_path, legacy):
    configs = tmp_path / "configs"
    templates = configs if legacy else configs / "examples"
    templates.mkdir(parents=True)
    payloads = {
        "config": {"ubuntu_user": "example", "ubuntu_pswd": "test-secret"},
        "automation_profiles": {"profiles": [{"name": "example"}]},
        "build_servers": {"servers": [{"host": "example"}], "templates": []},
    }
    for name, payload in payloads.items():
        (templates / f"{name}.example.json").write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/sanitize_release_config.py"),
         *(str(configs / f"{name}.json") for name in payloads)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    product = json.loads((configs / "config.json").read_text(encoding="utf-8"))
    assert product["ubuntu_pswd"] == ""
    assert product["ubuntu_user"] == ""
    assert json.loads((configs / "automation_profiles.json").read_text()) == {"profiles": []}
    assert json.loads((configs / "build_servers.json").read_text()) == {"servers": [], "templates": []}
    for name, payload in payloads.items():
        assert json.loads((templates / f"{name}.example.json").read_text()) == payload


def test_release_prefers_grouped_template(tmp_path):
    configs = tmp_path / "configs"
    (configs / "examples").mkdir(parents=True)
    (configs / "config.example.json").write_text('{"marker": "legacy"}', encoding="utf-8")
    (configs / "examples/config.example.json").write_text('{"marker": "grouped"}', encoding="utf-8")

    sanitize_file(configs / "config.json")

    assert json.loads((configs / "config.json").read_text())["marker"] == "grouped"


def test_local_configuration_is_ignored_but_source_templates_are_visible(tmp_path):
    # Evaluate the repo's real .gitignore policy inside a throwaway git
    # repository: the developer worktree may contain symlinked local dirs
    # (e.g. configs/certs -> secrets/certs) that make git refuse pathspec
    # traversal with "beyond a symbolic link", which has nothing to do
    # with the ignore rules under test.
    shutil.copyfile(PROJECT_ROOT / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, capture_output=True, text=True, check=True,
    )
    local_paths = [
        "configs/config.json", "configs/runtime.json", "configs/config_runtime.json",
        "configs/worker_tokens.json", "configs/certs/test.key", "configs/config.json.bak",
        "configs/cluster.local.json", "configs/examples/local-secret.json",
        "configs/cluster.json", "configs/local/config.json", "configs/secrets/environment.json",
        "data/settings/preferences.json", "data/config-migration-backups/original.json",
    ]
    source_paths = ["configs/README.md", *(
        f"configs/examples/{name}.example.json"
        for name in ("config", "runtime", "automation_profiles", "build_servers", "cluster")
    )]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"], cwd=tmp_path,
        input="\n".join(local_paths + source_paths) + "\n",
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == set(local_paths)
