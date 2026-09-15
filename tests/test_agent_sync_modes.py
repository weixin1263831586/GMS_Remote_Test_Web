"""Generated module permissions must converge in both directions."""

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("content_matches", [True, False])
def test_sync_removes_executable_bit_from_plain_module(tmp_path, content_matches):
    path = Path(__file__).resolve().parents[1] / "tools/sync_agent_package.py"
    spec = importlib.util.spec_from_file_location("agent_sync_modes", path)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    source = tmp_path / "source.py"
    target = tmp_path / "target.py"
    source.write_text("VALUE = 1\n")
    target.write_text(source.read_text() if content_matches else "old\n")
    target.chmod(0o755)
    assert sync.sync_one(source, target)
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.read_bytes() == source.read_bytes()
    assert not sync.sync_one(source, target)


def _init_agent_repo(tmp_path: Path) -> tuple:
    """最小 agent 包仓库：package.yaml + skill + runtime anchor + manifests。"""
    import subprocess

    agent = tmp_path / "agent" / "gms-remote-test"
    for sub in ("skill", "runtime", "manifests", "templates"):
        (agent / sub).mkdir(parents=True)
    package_yaml = agent / "package.yaml"
    package_yaml.write_text("version: 0.1.0\n")
    (agent / "skill" / "SKILL.md").write_text("skill v1\n")
    (agent / "runtime" / "gms-remote-test.sh").write_text(
        '#!/bin/bash\nGMS_RT_VERSION="0.1.0"\n'
    )
    (agent / "manifests" / "kk.plugin.json").write_text(
        '{\n  "version": "0.1.0",\n  "name": "gms-remote-test"\n}\n'
    )
    (agent / "templates" / "README.md").write_text("readme\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-qm", "init"],
        check=True,
    )
    return tmp_path, agent, package_yaml


def _load_sync():
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "tools/sync_agent_package.py"
    spec = importlib.util.spec_from_file_location("agent_sync_guard", path)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    return sync


def test_tree_guard_allows_content_change_after_version_bump(tmp_path):
    """升版本后的首次 sync 不被 same-version guard 误拦（P1 回归）。"""
    sync = _load_sync()
    root, agent, package_yaml = _init_agent_repo(tmp_path)
    # 正常发布：bump canonical version + 修改 manifests/skill payload。
    package_yaml.write_text("version: 0.2.0\n")
    (agent / "manifests" / "kk.plugin.json").write_text(
        '{\n  "version": "0.2.0",\n  "name": "gms-remote-test"\n}\n'
    )
    (agent / "skill" / "SKILL.md").write_text("skill v2\n")
    for label, tree in (
        ("skill/", agent / "skill"),
        ("manifests/", agent / "manifests"),
    ):
        sync.r16_tree_guard(root, tree, "0.2.0", label, package_yaml)


def test_tree_guard_blocks_skill_change_at_same_version(tmp_path):
    sync = _load_sync()
    root, agent, package_yaml = _init_agent_repo(tmp_path)
    (agent / "skill" / "SKILL.md").write_text("drifted\n")
    with pytest.raises(SystemExit):
        sync.r16_tree_guard(root, agent / "skill", "0.1.0", "skill/", package_yaml)


def test_tree_guard_blocks_manifest_change_at_same_version(tmp_path):
    sync = _load_sync()
    root, agent, package_yaml = _init_agent_repo(tmp_path)
    (agent / "manifests" / "kk.plugin.json").write_text(
        '{\n  "version": "0.1.0",\n  "name": "changed"\n}\n'
    )
    with pytest.raises(SystemExit):
        sync.r16_tree_guard(root, agent / "manifests", "0.1.0", "manifests/", package_yaml)


def test_tree_guard_blocks_runtime_change_at_same_version(tmp_path):
    sync = _load_sync()
    root, agent, package_yaml = _init_agent_repo(tmp_path)
    (agent / "runtime" / "gms-remote-test.sh").write_text(
        '#!/bin/bash\nGMS_RT_VERSION="0.1.0"\n# drifted\n'
    )
    with pytest.raises(SystemExit):
        sync.r16_tree_guard(root, agent / "runtime", "0.1.0", "runtime/", package_yaml)
