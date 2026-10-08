"""dist/gms-remote-test old-version pruning during package builds.

build_package.py 在构建成功后自动清理旧版本目录；这里直接测 prune
函数的边界：保留数量、当前版本永不删、非版本名目录不动。
"""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/scripts/agent/build_package.py"
spec = importlib.util.spec_from_file_location("agent_build_package", SCRIPT)
build_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_package)


def _make_versions(base: Path, names: list[str]) -> None:
    for name in names:
        (base / name).mkdir(parents=True)
        (base / name / "manifest.json").write_text("{}", encoding="utf-8")


def test_prune_keeps_newest_and_current(tmp_path):
    _make_versions(tmp_path, ["0.22.35", "0.22.36", "0.22.37", "0.22.38"])
    pruned = build_package.prune_old_versions(tmp_path, "0.22.36", keep=3)
    # keep=3 保留最新三个 0.22.36/37/38；0.22.35 最旧被清理。
    assert pruned == ["0.22.35"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0.22.36", "0.22.37", "0.22.38"]


def test_prune_never_deletes_current_version(tmp_path):
    _make_versions(tmp_path, ["0.1.0", "0.10.0", "0.9.0"])
    # 当前版本 0.1.0 数值上最旧，也必须保留；keep=2 → 最新 0.10.0 + 0.9.0
    # + current 0.1.0。
    pruned = build_package.prune_old_versions(tmp_path, "0.1.0", keep=2)
    assert pruned == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["0.1.0", "0.10.0", "0.9.0"]


def test_prune_sorts_numerically_not_lexically(tmp_path):
    _make_versions(tmp_path, ["0.9.0", "0.10.0", "0.22.38"])
    pruned = build_package.prune_old_versions(tmp_path, "0.22.38", keep=2)
    # 词法排序会把 0.9.0 排在 0.10.0 之后；数值排序必须正确淘汰 0.9.0。
    assert pruned == ["0.9.0"]


def test_prune_ignores_non_version_dirs_and_files(tmp_path):
    _make_versions(tmp_path, ["0.22.37", "0.22.38"])
    (tmp_path / "logs").mkdir()
    (tmp_path / "README.md").write_text("keep me", encoding="utf-8")
    pruned = build_package.prune_old_versions(tmp_path, "0.22.38", keep=1)
    assert pruned == ["0.22.37"]
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "README.md").exists()


def test_prune_keep_zero_or_missing_base_disables(tmp_path):
    _make_versions(tmp_path, ["0.22.35", "0.22.36"])
    assert build_package.prune_old_versions(tmp_path, "0.22.36", keep=0) == []
    assert (tmp_path / "0.22.35").exists()
    assert build_package.prune_old_versions(tmp_path / "absent", "0.22.36", keep=3) == []


@pytest.mark.parametrize("name,eligible", [
    ("0.22.38", True), ("1.0", True), ("7", True),
    ("0.22.38-beta", False), ("v1.2", False), ("..", False), (".hidden", False),
    ("0.22..38", False), ("0.22.38.", False),
])
def test_version_dir_pattern(name, eligible):
    assert bool(build_package.VERSION_DIR_RE.fullmatch(name)) is eligible
