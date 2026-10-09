"""dist/gms-remote-test old-version pruning during package builds.

build_package.py 在构建成功后自动清理旧版本目录；这里直接测 prune
函数的边界：保留数量、当前版本永不删、非版本名目录不动。
"""

import hashlib
import importlib.util
import json
import multiprocessing
import queue
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/scripts/agent/build_package.py"
spec = importlib.util.spec_from_file_location("agent_build_package", SCRIPT)
build_package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_package)


def _make_versions(base: Path, names: list[str]) -> None:
    for name in names:
        directory = base / name / "universal"
        directory.mkdir(parents=True)
        archive = directory / f"gms-remote-test-{name}-universal.zip"
        data = b"complete package"
        archive.write_bytes(data)
        (base / name / "manifest.json").write_text(json.dumps({
            "name": "gms-remote-test", "version": name, "clients": ["universal"],
            "artifacts": {"universal": {"path": str(archive), "size": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest()}},
        }), encoding="utf-8")


def _directories(base):
    return sorted(path.name for path in base.iterdir() if path.is_dir())


def test_prune_keeps_newest_and_current(tmp_path):
    _make_versions(tmp_path, ["0.22.35", "0.22.36", "0.22.37", "0.22.38"])
    pruned = build_package.prune_old_versions(tmp_path, "0.22.36", keep=3)
    # keep=3 保留最新三个 0.22.36/37/38；0.22.35 最旧被清理。
    assert pruned == ["0.22.35"]
    assert _directories(tmp_path) == ["0.22.36", "0.22.37", "0.22.38"]


def test_prune_never_deletes_current_version(tmp_path):
    _make_versions(tmp_path, ["0.1.0", "0.10.0", "0.9.0"])
    # 当前版本 0.1.0 数值上最旧，也必须保留；keep=2 → 最新 0.10.0 + 0.9.0
    # + current 0.1.0。
    pruned = build_package.prune_old_versions(tmp_path, "0.1.0", keep=2)
    assert pruned == []
    assert _directories(tmp_path) == ["0.1.0", "0.10.0", "0.9.0"]


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


def test_prune_preserves_incomplete_and_corrupt_versions_and_symlinks(tmp_path):
    _make_versions(tmp_path, ["0.7.0", "0.8.0", "0.9.0", "1.0.0"])
    (tmp_path / "0.7.0/manifest.json").unlink()
    archive = tmp_path / "0.8.0/universal/gms-remote-test-0.8.0-universal.zip"
    archive.write_bytes(b"incorrect digest".ljust(len(b"complete package"), b"!"))
    (tmp_path / "0.6.0").symlink_to(tmp_path / "0.7.0", target_is_directory=True)
    assert build_package.prune_old_versions(tmp_path, "1.0.0", keep=1) == ["0.9.0"]
    assert (tmp_path / "0.7.0").exists() and (tmp_path / "0.8.0").exists()
    assert (tmp_path / "0.6.0").is_symlink()


def test_failed_rebuild_keeps_previously_published_artifacts(tmp_path, monkeypatch):
    _make_versions(tmp_path, ["1.0.0"])
    original = (tmp_path / "1.0.0/manifest.json").read_bytes()
    calls = []

    def fail_mid_build(_plugin, _version, *, client):
        calls.append(client)
        if len(calls) == 2:
            raise FileNotFoundError("missing payload")
        return b"new package"

    monkeypatch.setattr(build_package, "build_package_bytes", fail_mid_build)
    with pytest.raises(FileNotFoundError):
        build_package.build_packages(tmp_path, "1.0.0", 1)
    assert (tmp_path / "1.0.0/manifest.json").read_bytes() == original
    assert build_package._completed_version(tmp_path / "1.0.0")
    assert not list(tmp_path.glob(".building-*"))


def _prune_in_process(base, started, result):
    started.set()
    result.put(build_package.prune_old_versions(Path(base), "1.0.0", 1))


def test_pruning_waits_for_another_process_build_lock(tmp_path):
    _make_versions(tmp_path, ["0.9.0", "1.0.0"])
    context = multiprocessing.get_context("spawn")
    started, result = context.Event(), context.Queue()
    process = context.Process(target=_prune_in_process, args=(str(tmp_path), started, result))
    try:
        with build_package._build_lock(tmp_path):
            process.start()
            assert started.wait(10)
            with pytest.raises(queue.Empty):
                result.get(timeout=0.2)
            assert (tmp_path / "0.9.0").exists()
        assert result.get(timeout=10) == ["0.9.0"]
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        result.close()


def _build_in_process(base, version, started, entered, release):
    def build(_plugin, _version, *, client):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("build test was not released")
        return (version + client).encode()

    build_package.build_package_bytes = build
    started.set()
    build_package.build_packages(Path(base), version, keep=1)


def test_two_process_builds_never_prune_the_version_being_written(tmp_path):
    _make_versions(tmp_path, ["0.8.0", "0.9.0"])
    context = multiprocessing.get_context("spawn")
    started, entered, release = context.Event(), context.Event(), context.Event()
    next_started, next_entered, next_release = context.Event(), context.Event(), context.Event()
    next_release.set()
    first = context.Process(target=_build_in_process, args=(str(tmp_path), "0.9.0", started, entered, release))
    second = context.Process(target=_build_in_process, args=(str(tmp_path), "0.10.0", next_started, next_entered, next_release))
    try:
        first.start()
        assert entered.wait(10)
        second.start()
        assert next_started.wait(10)
        assert not next_entered.wait(0.2)
        assert (tmp_path / "0.9.0").exists()
        release.set()
        first.join(timeout=15)
        second.join(timeout=15)
        assert first.exitcode == second.exitcode == 0
        assert _directories(tmp_path) == ["0.10.0"]
        assert build_package._completed_version(tmp_path / "0.10.0")
    finally:
        release.set()
        for process in (first, second):
            if process.is_alive():
                process.kill()
                process.join()
