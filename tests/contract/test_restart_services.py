"""Execute restart control flow with isolated files and mocked OS operations."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_restart(
    tmp_path, layout, service_case, *, legacy_unit=False, clean_cache=False,
    fresh_certificate=False, server_hostname=None,
):
    project = tmp_path / "project"
    project.mkdir()
    # Exercise the actual environment/path loaders, without importing deployment
    # config, connecting to hosts, or touching the running service's files.
    for relative in (
        "bootstrap/env_loader.py", "foundation/config_paths.py",
        "foundation/runtime_settings.py", "foundation/private_config.py",
        "tools/scripts/deployment/update_systemd_tls.py", "tools/scripts/_common.py",
    ):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for package in ("foundation", "bootstrap"):
        (project / package / "__init__.py").touch()
    for relative in ("features/__pycache__/fixture.pyc", "features/loose.pyo",
                     "data/pycache/keep.pyc", ".venv/lib/keep.pyc", "tools/vendor/keep.pyc"):
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"cache fixture")
    vnc_module = project / "features/system/vnc.py"
    vnc_module.parent.mkdir(parents=True)
    (project / "features/__init__.py").touch()
    (project / "features/system/__init__.py").touch()
    vnc_module.write_text(
        """
class _VNCManager:
    def _start_local_vnc(self, *, force_restart):
        assert force_restart is False
        return {"success": True, "message": "VNC fixture ready"}


vnc_manager = _VNCManager()
""".lstrip(),
        encoding="utf-8",
    )
    certificate_dir = project / ("configs/certs" if layout == "legacy" else "configs/secrets/certs")
    certificate_dir.mkdir(parents=True)
    if not fresh_certificate:
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
# 忠实模拟 systemctl_admin 的 sudo 探测语义：`sudo -n true` 是凭据探测
# （不执行 systemctl），`sudo -n CMD` 去掉 -n 后执行目标命令。不能简化
# 成 `"$@"` 直通：那会把 `-n true` 当成命令 `-n`（127），迫使脚本走
# --no-ask-password 兜底，破坏契约断言的调用序列。
sudo() {
    case "$1" in
        --)
            shift
            [[ "$RESTART_CASE" == unit_update_fails ]] && return 1
            "$@"
            ;;
        -n) shift; if [[ "$1" == "true" ]]; then return 0; fi; "$@" ;;
        -v) return 0 ;;
        *) "$@" ;;
    esac
}
systemctl() {
    printf '%s\n' "$*" >> "$RESTART_CALLS"
    case "$*" in
        'daemon-reload') [[ "$RESTART_CASE" != unit_reload_fails ]] ;;
        'show gms-redmine-daily-brief-worker.service --property=LoadState --value')
            [[ "$RESTART_CASE" == missing ]] && echo not-found || echo loaded
            ;;
        *'stop gms-redmine-daily-brief-worker.service')
            [[ "$RESTART_CASE" != brief_stop_fails ]] ;;
        *'restart gms-redmine-daily-brief-worker.service')
            [[ "$RESTART_CASE" != brief_restart_fails ]] ;;
        *'is-active --quiet gms-redmine-daily-brief-worker.service')
            [[ "$RESTART_CASE" != brief_inactive ]] ;;
        '--user restart gms-worker-agent')
            [[ "$RESTART_CASE" != device_restart_fails ]] ;;
        *'restart gms-web-app.service')
            [[ "$RESTART_CASE" != web_restart_fails ]] ;;
    esac
}
lsof() { return 1; }
fuser() { echo 'unexpected process termination' >&2; return 1; }
sleep() { printf 'sleep %s\n' "$*" >> "$RESTART_CALLS"; SECONDS=$((SECONDS + 1)); }
curl() {
    printf 'health-probe\n' >> "$RESTART_CALLS"
    if [[ "$RESTART_CASE" == health_fails ]]; then return 1; fi
    if [[ "$RESTART_CASE" == health_retry && "${HEALTH_PROBES:-0}" == 0 ]]; then
        HEALTH_PROBES=1
        return 1
    fi
    return 0
}
timeout() { return 0; }
nohup() { return 0; }
script=$1
layout=$2
shift 2
source <(sed 's|^SYSTEMD_UNIT_FILE=.*|SYSTEMD_UNIT_FILE="${GMS_WEB_APP_DIR}/web.service"|' "$script") "$@"
[[ "${RESTART_FIXTURE}" == loaded ]]
if [[ "$layout" == structured ]]; then
    [[ "${RESTART_SECRET_FIXTURE}" == private-fixture ]]
fi
'''
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "GMS_WEB_APP_DIR": str(project),
        "GMS_PYTHON_BIN": sys.executable,
        "PYTHONPATH": str(project),
        "RESTART_CASE": service_case,
        "RESTART_CALLS": str(tmp_path / "service-calls"),
    }
    unit = project / "web.service"
    if legacy_unit:
        unit.write_text(
            f"[Service]\nWorkingDirectory={project}\n"
            f"ExecStart=/usr/bin/python3 -m uvicorn app:app --port 5001 "
            f"--ssl-keyfile {project}/configs/certs/gms-local.key "
            f"--ssl-certfile {project}/configs/certs/gms-local.crt\n"
            "Restart=always\n",
            encoding="utf-8",
        )
    else:
        unit.touch()
    user_unit = Path(environment["HOME"]) / ".config/systemd/user/gms-worker-agent.service"
    user_unit.parent.mkdir(parents=True)
    user_unit.touch()
    environment.pop("GMS_SKIP_RUNTIME_ENV", None)
    environment.pop("ENV_FILE", None)
    environment.pop("RESTART_FIXTURE", None)
    environment.pop("RESTART_SECRET_FIXTURE", None)
    if layout == "process_only":
        environment["RESTART_FIXTURE"] = "loaded"
    if server_hostname:
        environment["GMS_SERVER_HOSTNAME"] = server_hostname
    result = subprocess.run(
        ["bash", "-c", harness, "restart-test", str(ROOT / "restart_services.sh"), layout,
         *(["--clean-cache"] if clean_cache else [])],
        env=environment, cwd=project, capture_output=True, text=True, timeout=15,
    )
    calls = Path(environment["RESTART_CALLS"]).read_text().splitlines()
    return result, calls, project


@pytest.mark.parametrize("layout", ["structured", "legacy", "process_only"])
@pytest.mark.parametrize("service_case", ["success", "missing", "brief_stop_fails", "brief_restart_fails", "brief_inactive", "device_restart_fails", "web_restart_fails"])
def test_restart_reports_actual_worker_outcome(tmp_path, layout, service_case):
    result, calls, _project = run_restart(tmp_path, layout, service_case)
    if service_case not in ("success", "missing"):
        assert result.returncode != 0
        assert "服务管理完成" not in result.stdout
        if service_case.startswith("brief_"):
            assert "✓ 晨报 Worker 已重启" not in result.stdout
        if service_case == "device_restart_fails":
            assert "✓ Worker Agent 已重启" not in result.stdout
        return
    assert result.returncode == 0, result.stderr
    assert "[3/6]" in result.stdout
    assert "[5/6] 确认本机 VNC" in result.stdout
    assert "VNC fixture ready" in result.stdout
    assert "服务管理完成" in result.stdout
    assert "unbound variable" not in result.stderr
    assert "private-fixture" not in result.stdout + result.stderr
    if service_case == "success":
        assert calls.index('stop gms-redmine-daily-brief-worker.service') < calls.index('stop gms-web-app.service')
        assert calls.index('restart gms-web-app.service') < calls.index('restart gms-redmine-daily-brief-worker.service')
        assert 'is-active --quiet gms-redmine-daily-brief-worker.service' in calls
        assert "✓ 晨报 Worker 已重启" in result.stdout
    else:
        assert "晨报 Worker 未安装" in result.stdout
        assert not any('restart gms-redmine-daily-brief-worker.service' in call for call in calls)
    assert '--user is-active --quiet gms-worker-agent' in calls


def test_restart_updates_installed_tls_paths_before_stopping_services(tmp_path):
    result, calls, project = run_restart(tmp_path, "structured", "success", legacy_unit=True)
    assert result.returncode == 0, result.stderr
    unit = project / "web.service"
    updated = unit.read_text()
    assert f"--ssl-keyfile {project}/configs/secrets/certs/gms-local.key" in updated
    assert f"--ssl-certfile {project}/configs/secrets/certs/gms-local.crt" in updated
    assert "--port 5001" in updated
    assert "Restart=always" in updated
    assert "-m bootstrap.server app:app" in updated
    backups = list(project.glob("web.service.pre-canonical-tls-*"))
    assert len(backups) == 1
    assert f"{project}/configs/certs/gms-local.key" in backups[0].read_text()
    assert calls.index("daemon-reload") < calls.index("stop gms-redmine-daily-brief-worker.service")
    assert not (project / "configs/certs").is_symlink()
    assert not (project / "configs/config.json").is_symlink()


@pytest.mark.parametrize("service_case", ["unit_update_fails", "unit_reload_fails"])
def test_tls_update_failure_keeps_services_running(tmp_path, service_case):
    result, calls, _project = run_restart(tmp_path, "structured", service_case, legacy_unit=True)
    assert result.returncode != 0
    assert not any(call.startswith(("stop ", "restart ")) for call in calls)
    assert "服务管理完成" not in result.stdout


def test_systemd_path_update_is_dry_run_and_repeatable(tmp_path):
    project = tmp_path / "project with spaces"
    unit = tmp_path / "web.service"
    original = (
        "[Service]\n"
        f'ExecStart=/usr/bin/python3 --ssl-keyfile "{project}/configs/certs/gms-local.key" '
        f'--ssl-certfile "{project}/configs/certs/gms-local.crt"\n'
        "Environment=KEEP_THIS=unchanged\n"
    )
    unit.write_text(original, encoding="utf-8")
    unit.chmod(0o640)
    command = [
        sys.executable, str(ROOT / "tools/scripts/deployment/update_systemd_tls.py"),
        "--project-root", str(project), "--unit", str(unit),
    ]
    dry_run = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert dry_run.returncode == 3
    assert unit.read_text() == original
    assert list(tmp_path.glob("web.service.pre-canonical-tls-*")) == []
    applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, timeout=10)
    assert applied.returncode == 0, applied.stderr
    assert unit.read_text() == original.replace("/configs/certs/", "/configs/secrets/certs/")
    assert unit.stat().st_mode & 0o777 == 0o640
    repeated = subprocess.run([*command, "--apply"], capture_output=True, text=True, timeout=10)
    assert repeated.returncode == 0, repeated.stderr
    backups = list(tmp_path.glob("web.service.pre-canonical-tls-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original


@pytest.mark.parametrize("clean_cache", [False, True])
def test_restart_retains_caches_unless_explicitly_requested(tmp_path, clean_cache):
    result, calls, project = run_restart(tmp_path, "structured", "success", clean_cache=clean_cache)
    assert result.returncode == 0, result.stderr
    assert (project / "features/__pycache__/fixture.pyc").exists() is not clean_cache
    assert (project / "features/loose.pyo").exists() is not clean_cache
    for relative in ("data/pycache/keep.pyc", ".venv/lib/keep.pyc", "tools/vendor/keep.pyc"):
        assert (project / relative).read_bytes() == b"cache fixture"
    assert "sleep 3" not in calls
    assert "sleep 1" not in calls  # Healthy service is probed immediately.
    assert "总耗时:" in result.stdout
    assert "daemon-reload" not in calls  # Current unit needs no reload.


def test_restart_health_retry_sleeps_only_after_failure(tmp_path):
    result, calls, _project = run_restart(tmp_path, "structured", "health_retry")
    assert result.returncode == 0, result.stderr
    start = calls.index("health-probe")
    assert calls[start:start + 3] == ["health-probe", "sleep 1", "health-probe"]


def test_restart_health_timeout_does_not_report_success(tmp_path):
    result, calls, _project = run_restart(tmp_path, "structured", "health_fails")
    assert result.returncode != 0
    assert "健康检查失败" in result.stdout
    assert "服务管理完成" not in result.stdout
    assert 1 < calls.count("health-probe") <= 60
    assert "restart gms-redmine-daily-brief-worker.service" not in calls


def test_systemd_protocol_update_preserves_explicit_options(tmp_path):
    unit = tmp_path / "web.service"
    original = ("[Service]\nExecStart=/usr/bin/python3 -m uvicorn app:app --http h11 "
                "--port 5001 --timeout-keep-alive 120\nRestart=always\n")
    unit.write_text(original)
    command = [sys.executable, str(ROOT / "tools/scripts/deployment/update_systemd_tls.py"),
               "--project-root", str(tmp_path), "--unit", str(unit)]
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 3
    assert unit.read_text() == original
    applied = subprocess.run([*command, "--apply"], capture_output=True, text=True, timeout=10)
    assert applied.returncode == 0, applied.stderr
    assert unit.read_text() == original.replace("-m uvicorn", "-m bootstrap.server")
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0


@pytest.mark.parametrize(
    ("hostname", "expected_san"),
    [("ats-worker-244", "DNS:ats-worker-244"),
     ("172.16.14.233", "IP Address:172.16.14.233")],
)
def test_restart_first_boot_certificate_uses_correct_san_type(tmp_path, hostname, expected_san):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for certificate integration test")
    result, _calls, project = run_restart(
        tmp_path, "structured", "success",
        fresh_certificate=True, server_hostname=hostname,
    )
    assert result.returncode == 0, result.stderr
    certificate = project / "configs/secrets/certs/gms-local.crt"
    assert certificate.is_file()
    san = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-ext", "subjectAltName"],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout
    assert expected_san in san
