"""Execute restart control flow with isolated files and mocked OS operations."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("layout", ["structured", "legacy", "process_only"])
def test_restart_completes_without_obsolete_environment_file_variable(tmp_path, layout):
    project = tmp_path / "project"
    project.mkdir()
    # Exercise the actual environment/path loaders, without importing deployment
    # config, connecting to hosts, or touching the running service's files.
    for relative in (
        "bootstrap/env_loader.py", "foundation/config_paths.py",
        "foundation/runtime_settings.py", "foundation/private_config.py",
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for package in ("foundation", "bootstrap"):
        (project / package / "__init__.py").touch()
    certificate_dir = project / ("configs/certs" if layout == "legacy" else "configs/secrets/certs")
    certificate_dir.mkdir(parents=True)
    for name in ("gms-local.crt", "gms-local.key"):
        (certificate_dir / name).write_text("fixture certificate", encoding="utf-8")
    if layout != "process_only":
        relative = "configs/runtime.json" if layout == "legacy" else "configs/local/environment.json"
        environment_file = project / relative
        environment_file.parent.mkdir(parents=True, exist_ok=True)
        environment_file.write_text(json.dumps({"RESTART_FIXTURE": "loaded"}), encoding="utf-8")
    if layout == "structured":
        (project / "configs/secrets/environment.json").write_text(
            json.dumps({"RESTART_SECRET_FIXTURE": "private-fixture"}), encoding="utf-8",
        )

    harness = r'''
find() { return 0; }
sudo() { "$@"; }
systemctl() { return 0; }
lsof() { return 1; }
fuser() { echo 'unexpected process termination' >&2; return 1; }
sleep() { return 0; }
timeout() { return 0; }
nohup() { return 0; }
source "$1"
[[ "${RESTART_FIXTURE}" == loaded ]]
if [[ "$2" == structured ]]; then
    [[ "${RESTART_SECRET_FIXTURE}" == private-fixture ]]
fi
'''
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "GMS_WEB_APP_DIR": str(project),
        "GMS_PYTHON_BIN": sys.executable,
        "PYTHONPATH": str(project),
    }
    environment.pop("GMS_SKIP_RUNTIME_ENV", None)
    environment.pop("ENV_FILE", None)
    environment.pop("RESTART_FIXTURE", None)
    environment.pop("RESTART_SECRET_FIXTURE", None)
    if layout == "process_only":
        environment["RESTART_FIXTURE"] = "loaded"
    result = subprocess.run(
        ["bash", "-c", harness, "restart-test", str(ROOT / "restart_services.sh"), layout],
        env=environment, cwd=project, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "[3/4]" in result.stdout
    assert "服务管理完成" in result.stdout
    assert "unbound variable" not in result.stderr
    assert "private-fixture" not in result.stdout + result.stderr
