from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))
from gms_agent import auto_update, profile_store  # noqa: E402


@pytest.mark.parametrize("selection", ["argument", "environment"])
def test_enable_installs_exact_profile_timer_without_credentials(tmp_path, monkeypatch, selection):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(profile_store, "PROFILE_ROOT", tmp_path / "gms-agent/profiles")
    monkeypatch.setattr(profile_store, "STATE_DIR", tmp_path / "state")
    profile_store.write_profile_toml("kimi-one", "kimi", "https://controller:5001", "/etc/controller-ca.pem")
    profile_store.write_profile_toml("kimi-two", "kimi", "https://other-controller:5001", "")
    monkeypatch.setenv("GMS_RT_PROFILE", "kimi-one" if selection == "environment" else "kimi-two")
    systemctl = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(auto_update, "systemctl", systemctl)
    assert auto_update.command(SimpleNamespace(action="enable", profile="kimi-one" if selection == "argument" else "")) == 0
    unit = (tmp_path / "systemd/user/gms-agent-update.service").read_text()
    assert "update --profile=kimi-one" in unit
    assert "Environment=GMS_RT_PROFILE=kimi-one" in unit
    assert "__GMS_PROFILE__" not in unit
    assert "doctor --client auto --json --profile=kimi-one" in unit
    assert "gms-rt-system-selfcheck --json --non-interactive" in unit
    assert "TOKEN" not in unit and "controller:5001" not in unit
    assert "OnCalendar=hourly" in (tmp_path / "systemd/user/gms-agent-update.timer").read_text()
    systemctl.assert_any_call("enable", "--now", "gms-agent-update.timer")


def test_multiple_profiles_require_selection_before_install(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("GMS_RT_PROFILE", raising=False)
    monkeypatch.setattr(profile_store, "PROFILE_ROOT", tmp_path / "profiles")
    for name in ("kimi-one", "codex-one"):
        profile_store.write_profile_toml(name, "kimi", "https://controller:5001", "")
    systemctl = Mock()
    monkeypatch.setattr(auto_update, "systemctl", systemctl)
    assert auto_update.command(SimpleNamespace(action="enable", profile="")) == 2
    systemctl.assert_not_called()
    assert not (tmp_path / "systemd").exists()


def test_disable_only_stops_timer(monkeypatch):
    systemctl = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(auto_update, "systemctl", systemctl)
    assert auto_update.command(SimpleNamespace(action="disable")) == 0
    systemctl.assert_called_once_with("disable", "--now", "gms-agent-update.timer")
