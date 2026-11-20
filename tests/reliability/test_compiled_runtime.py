"""Compiled deployment contracts exercised against disposable persistent roots."""

from __future__ import annotations

import base64
import io
import json
import os
import ssl
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from features.system.agent_package_builder import build_package_bytes
from scripts.release import build_binary_release as builder
from scripts.release import compiled_entry
from worker_agent.config import WorkerConfig


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root, state, configs = tmp_path / "release/runtime", tmp_path / "state", tmp_path / "etc/configs"
    root.mkdir(parents=True)
    configs.mkdir(parents=True)
    (configs / "examples").mkdir()
    (configs / "examples/config.example.json").write_text("{}")
    (root / "configs").symlink_to(configs, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir()
    for name in list(os.environ):
        if name.startswith(("GMS_WORKER_", "GMS_CONTROLLER_")):
            monkeypatch.delenv(name)
    for key, value in {
        "HOME": home, "GMS_PROJECT_ROOT": root, "GMS_DATA_ROOT": state,
        "GMS_ENV": "production", "GMS_SKIP_RUNTIME_ENV": "1",
        "GMS_WORKER_CONFIG": state / "local-worker/config.json",
    }.items():
        monkeypatch.setenv(key, str(value))
    compiled_entry._bootstrap(root, "localhost", 5001)
    (root / "data").symlink_to(state, target_is_directory=True)
    return root, state, configs


def test_generated_worker_trusts_the_instance_ca_and_reinstall_updates_managed_fields(deployment, tmp_path):
    root, state, configs = deployment
    worker = WorkerConfig.load()
    ssl.create_default_context(cafile=worker.controller_ca)
    assert worker.controller_url == "https://127.0.0.1:5001"
    token = worker.token
    key_path = configs / "secrets/backup.key"
    key = base64.urlsafe_b64decode(key_path.read_bytes())
    worker_path = state / "local-worker/config.json"
    settings = json.loads(worker_path.read_text())
    settings.update(max_jobs=7, suite_roots=[str(tmp_path / "custom-suites")])
    settings["controller_ca"] = (configs / "secrets/certs/gms-local-ca.crt").read_text()
    worker_path.write_text(json.dumps(settings))
    upgraded = tmp_path / "next-release/runtime"
    upgraded.mkdir(parents=True)
    (upgraded / "configs").symlink_to(configs, target_is_directory=True)
    compiled_entry._bootstrap(upgraded, "localhost", 5443)
    worker = WorkerConfig.load()
    assert worker.controller_url == "https://127.0.0.1:5443"
    assert worker.controller_ca == str(configs / "secrets/certs/gms-local-ca.crt")
    assert worker.token == token
    assert worker.max_jobs == 7
    assert worker.suite_roots == [tmp_path / "custom-suites"]
    assert base64.urlsafe_b64decode(key_path.read_bytes()) == key
    environment = json.loads((configs / "secrets/environment.json").read_text())
    assert all(str(root) not in value for name, value in environment.items() if name.endswith("_FILE"))


def test_embedded_agent_uses_exactly_the_canonical_public_archive(tmp_path, monkeypatch):
    module = builder._write_agent_module(tmp_path / "build-input")
    bundled = ModuleType("_gms_bundled_agent")
    exec(compile(module.read_text(), str(module), "exec"), bundled.__dict__)
    expected = build_package_bytes(ROOT / "plugins/gms-remote-test", bundled.VERSION)
    assert expected == bundled.ARCHIVE
    with zipfile.ZipFile(io.BytesIO(expected)) as archive:
        assert archive.read("gms-remote-test/scripts/gms-agent").decode() == bundled.BOOTSTRAP
    out = tmp_path / "build"
    dist = out / "compiled_entry.dist"
    dist.mkdir(parents=True)
    (dist / "gms-runtime").write_bytes(b"native placeholder")
    calls = []
    monkeypatch.setattr(builder, "_run", lambda command, **kw: calls.append((command, kw)))
    assert builder._compile_native(out) == dist
    assert "--include-module=_gms_bundled_agent" in calls[0][0]
    included = Path(calls[0][1]["env"]["PYTHONPATH"].split(os.pathsep)[0])
    assert (included / "_gms_bundled_agent.py").is_file()


@pytest.mark.parametrize("arguments", [
    ["create"], ["restore", "--data-root=/chosen-state", "--config-root=/chosen-configs"], ["verify"],
])
def test_compiled_backup_defaults_resolve_split_roots_without_overriding_explicit_options(
    tmp_path, monkeypatch, arguments,
):
    from scripts import gms_backup

    configs = tmp_path / "configs"
    configs.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "configs").symlink_to(configs, target_is_directory=True)
    monkeypatch.setenv("GMS_PROJECT_ROOT", str(runtime))
    monkeypatch.setenv("GMS_DATA_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["gms-runtime", "backup", *arguments])
    monkeypatch.chdir(tmp_path)
    forwarded = []
    monkeypatch.setattr(gms_backup, "main", lambda args: forwarded.extend(args) or 0)
    assert compiled_entry.main() == 0
    if arguments == ["create"]:
        assert forwarded == ["create", "--data-root", str(tmp_path / "state"), "--config-root", str(configs)]
    else:
        assert forwarded == arguments


@pytest.mark.parametrize(("failure", "worker_active"), [(0, True), (7, True), (0, False)])
def test_backup_wrapper_restores_only_previous_services_even_after_failure(tmp_path, failure, worker_active):
    tools = tmp_path / "bin"
    tools.mkdir()
    events = tmp_path / "events.jsonl"
    helper = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path
with Path(os.environ['TEST_BACKUP_EVENTS']).open('a') as output:
    output.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
if Path(sys.argv[0]).name == 'systemctl':
    if sys.argv[1] == 'is-active' and sys.argv[-1] == 'gms-local-worker.service':
        sys.exit(int(os.environ['TEST_WORKER_INACTIVE']))
else:
    sys.exit(int(os.environ['TEST_BACKUP_FAILURE']))
"""
    for name in ("systemctl", "gms-runtime"):
        executable = tools / name
        executable.write_text(helper)
        executable.chmod(0o755)
    env = {
        "PATH": str(tools) + os.pathsep + os.defpath,
        "GMS_PROJECT_ROOT": str(tmp_path / "release"), "GMS_RUN_HOME": str(tmp_path / "home"),
        "GMS_BACKUP_DIR": str(tmp_path / "backups"), "GMS_BACKUP_KEY_FILE": str(tmp_path / "key"),
        "GMS_CONTROLLER_SERVICE": "gms.service", "GMS_LOCAL_WORKER_SERVICE": "gms-local-worker.service",
        "GMS_BACKUP_RUNTIME": str(tools / "gms-runtime"),
        "GMS_BACKUP_DATA_ROOT": str(tmp_path / "state"), "GMS_BACKUP_CONFIG_ROOT": str(tmp_path / "configs"),
        "TEST_BACKUP_EVENTS": str(events), "TEST_BACKUP_FAILURE": str(failure),
        "TEST_WORKER_INACTIVE": str(int(not worker_active)),
    }
    result = subprocess.run(["bash", str(ROOT / "scripts/run_production_backup.sh")],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == failure, result.stderr
    calls = [json.loads(line) for line in events.read_text().splitlines()]
    lifecycle = [call for call in calls if call[1] != "is-active"]
    runtime_index = next(i for i, call in enumerate(lifecycle) if call[0] == "gms-runtime")
    before = [["systemctl", "stop", "gms-local-worker.service"]] if worker_active else []
    assert lifecycle[:runtime_index] == [*before, ["systemctl", "stop", "gms.service"]]
    after = [["systemctl", "start", "gms.service"]]
    if worker_active:
        after.append(["systemctl", "start", "gms-local-worker.service"])
    assert lifecycle[runtime_index + 1:] == after
    assert lifecycle[runtime_index][1:3] == ["backup", "create"]
    assert "--data-root" in lifecycle[runtime_index]
    assert "--config-root" in lifecycle[runtime_index]
