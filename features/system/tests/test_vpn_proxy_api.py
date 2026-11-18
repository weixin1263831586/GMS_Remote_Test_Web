"""Contract and security regression checks for local vpn-proxy integration."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from features.system import vpn_proxy_api as mod


def test_only_predeclared_control_actions_are_accepted():
    with pytest.raises(ValidationError):
        mod.ProxyControlRequest(action="restart")
    with pytest.raises(ValidationError):
        mod.ProxyControlRequest(action="start; reboot")


def test_installer_commands_from_deployed_config_and_no_injection():
    cfg = {"advertise_host": "proxy.internal", "installer_port": 8089,
           "installer_enabled": True}
    result = mod._install_scripts(cfg)
    assert "http://proxy.internal:8089/install.sh" in result["linux"]
    assert "http://proxy.internal:8089/install.ps1" in result["windows"]
    assert "\\n" not in result["linux"]
    assert "\\n" not in result["windows"]
    assert 'source "$_rk_file"' in result["linux"]
    assert 'rm -f -- "$_rk_file"' in result["linux"]
    assert 'Installer download failed' in result["windows"]
    assert mod._install_scripts({**cfg, "advertise_host": "evil; reboot"}) == {
        "linux": "", "windows": ""
    }
    assert mod._install_scripts({**cfg, "installer_enabled": False}) == {
        "linux": "", "windows": ""
    }


def test_state_is_read_from_deployed_config_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = Path(tmpdir) / "config.json"
        config.write_text(json.dumps({"advertise_host": "proxy.internal"}),
                          encoding="utf-8")
        with patch.object(mod, "DEPLOYED_CONFIG", config):
            assert mod._deployed_config()["advertise_host"] == "proxy.internal"
        config.write_text('{"broken":', encoding="utf-8")
        with patch.object(mod, "DEPLOYED_CONFIG", config):
            assert mod._deployed_config() == {}


def test_start_runs_firewall_first_and_stop_never_stops_firewall():
    cfg = {"advertise_host": "proxy.internal"}
    calls = []
    with patch.object(mod, "_deployed_config", return_value=cfg), patch.object(
        mod, "_run_admin_command", side_effect=lambda args: calls.append(args)
    ), patch.object(mod, "_service_state", side_effect=["active", "inactive"]):
        start = mod.proxy_control(mod.ProxyControlRequest(action="start"), _admin=MagicMock())
        stop = mod.proxy_control(mod.ProxyControlRequest(action="stop"), _admin=MagicMock())
    assert start["data"]["state"] == "active"
    assert stop["data"]["state"] == "inactive"
    assert calls == [
        ["start", "rk-vpn-proxy-firewall.service"],
        ["start", "rk-vpn-proxy.service"],
        ["stop", "rk-vpn-proxy.service"],
    ]


def test_control_refuses_missing_deployment():
    with patch.object(mod, "_deployed_config", return_value={}), pytest.raises(
        HTTPException
    ) as info:
        mod.proxy_control(mod.ProxyControlRequest(action="start"), _admin=MagicMock())
    assert info.value.status_code == 409


def test_privileged_operations_use_fixed_sudo_argv():
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return MagicMock(returncode=0)
    with patch.object(mod.subprocess, "run", side_effect=fake_run):
        mod._run_admin_command(["start", "rk-vpn-proxy.service"])
    assert seen == [["sudo", "-n", mod.SYSTEMCTL, "start", "rk-vpn-proxy.service"]]


def test_status_does_not_pretend_failed_stats_source_is_empty():
    with patch.object(mod, "_deployed_config", return_value={
        "advertise_host": "proxy.internal", "proxy_port": 7897,
        "stats_enabled": True, "allowed_clients": ["10.1.0.3/32"],
    }), patch.object(mod, "_service_state", return_value="active"), patch.object(
        mod, "_stats", return_value=(None, "统计服务不可用")
    ):
        result = mod.proxy_status(_user=None)["data"]
    assert result["clients"] is None
    assert result["stats_error"] == "统计服务不可用"
    assert result["config"]["proxy_url"] == "http://proxy.internal:7897"



def test_upstream_failed_or_stale_snapshot_is_not_reported_as_current_clients():
    now = 1800000000.0
    base = {
        "connected": [{"client": "10.1.0.3", "connections": 2}],
        "recent": [], "sources": {"connections": True, "access_log": True},
        "time": now,
    }
    cases = [
        ({**base, "failed": True, "error": "ss failed"}, "统计采集失败"),
        ({**base, "time": now - 120}, "已过期"),
    ]
    for snapshot, reason in cases:
        response = MagicMock(status=200)
        response.read.return_value = json.dumps(snapshot).encode("utf-8")
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("urllib.request.build_opener", return_value=opener), patch(
            "features.system.vpn_proxy_api.time.time", return_value=now
        ):
            clients, error = mod._stats(17898)
        assert clients is None
        assert reason in error


def test_vpn_proxy_management_routes_are_registered_in_app_composition():
    # Other test suites clear app/asset router state during lifecycle tests,
    # so inspecting that mutable shared instance is nondeterministic here.
    # The separate frozen API contract checks final runtime route registration.
    from bootstrap.routes import ALL_ROUTERS
    from features.system import assets

    assert assets.router in ALL_ROUTERS
    assert mod.router not in ALL_ROUTERS
    base = Path(__file__).resolve().parents[1]
    assets_source = (base / "assets.py").read_text(encoding="utf-8")
    api_source = (base / "vpn_proxy_api.py").read_text(encoding="utf-8")
    assert assets_source.count("from .vpn_proxy_api import router as vpn_proxy_router") == 1
    assert assets_source.count("router.include_router(vpn_proxy_router)") == 1
    for route in ('@router.get("/status")',
                  '@router.post("/control")',
                  '@router.post("/client-control", include_in_schema=False)'):
        assert route in api_source


def test_snapshot_requires_explicit_collection_source_health():
    snapshot = {
        "connected": [], "recent": [], "time": 1800000000.0,
        "sources": {"connections": False, "access_log": True},
    }
    response = MagicMock(status=200)
    response.read.return_value = json.dumps(snapshot).encode("utf-8")
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = response
    with patch("urllib.request.build_opener", return_value=opener), patch(
        "features.system.vpn_proxy_api.time.time", return_value=1800000000.0
    ):
        actual, error = mod._stats(17898)
    assert error is None
    assert actual["sources"]["connections"] is False

    del snapshot["sources"]
    response.read.return_value = json.dumps(snapshot).encode("utf-8")
    with patch("urllib.request.build_opener", return_value=opener), patch(
        "features.system.vpn_proxy_api.time.time", return_value=1800000000.0
    ):
        actual, error = mod._stats(17898)
    assert actual is None
    assert "统计来源状态缺失" in error


def test_client_access_rejects_nonexact_or_unlisted_source_ips():
    config = {"allowed_clients": ["172.16.14.0/24"]}
    assert mod._is_authorized_client("172.16.14.66", config)
    for client in ("127.0.0.1", "172.16.14.0/24", "10.10.10.10",
                   "172.16.14.66; reboot", "172.16.14.066"):
        assert not mod._is_authorized_client(client, config)


def test_disabled_client_state_is_read_from_installed_file_not_stats():
    with tempfile.TemporaryDirectory() as dirname:
        statefile = Path(dirname) / "disabled_clients.json"
        statefile.write_text(json.dumps({
            "version": 1, "disabled_clients": ["172.16.14.66"]
        }), encoding="utf-8")
        with patch.object(mod, "DISABLED_CLIENTS", statefile):
            assert mod._disabled_client_state() == (["172.16.14.66"], None)
            statefile.write_text("{broken", encoding="utf-8")
            items, error = mod._disabled_client_state()
            assert items == []
            assert error


def test_client_access_uses_only_fixed_root_helper_and_rechecks_reply():
    body = mod.ProxyClientAccessRequest(
        action="disable", client="172.16.14.66"
    )
    response = MagicMock(returncode=0, stdout=json.dumps({
        "action": "disable", "client": "172.16.14.66", "disabled": True,
    }))
    with patch.object(mod, "_deployed_config", return_value={
        "allowed_clients": ["172.16.14.0/24"],
    }), patch.object(mod, "_client_control_available", return_value=True), patch.object(
        mod, "_disabled_client_state", return_value=([], None)
    ), patch.object(mod.subprocess, "run", return_value=response) as run:
        actual = mod.proxy_client_control(body, _admin=MagicMock())
    assert actual["data"]["disabled"] is True
    assert run.call_args.args[0] == [
        "sudo", "-n", str(mod.CLIENT_CONTROL_HELPER),
        "disable", "172.16.14.66",
    ]

    # No success status from a process that reports a conflicting result.
    response.stdout = json.dumps({
        "action": "disable", "client": "172.16.14.66", "disabled": False,
    })
    with patch.object(mod, "_deployed_config", return_value={
        "allowed_clients": ["172.16.14.0/24"],
    }), patch.object(mod, "_client_control_available", return_value=True), patch.object(
        mod, "_disabled_client_state", return_value=([], None)
    ), patch.object(mod.subprocess, "run", return_value=response), pytest.raises(
        HTTPException
    ) as exc:
        mod.proxy_client_control(body, _admin=MagicMock())
    assert exc.value.status_code == 503


def test_client_access_requires_deployed_script_and_authorized_ip():
    body = mod.ProxyClientAccessRequest(action="disable", client="172.16.14.66")
    with patch.object(mod, "_deployed_config", return_value={
        "allowed_clients": ["172.16.14.0/24"],
    }), patch.object(mod, "_client_control_available", return_value=False), pytest.raises(
        HTTPException
    ) as exc:
        mod.proxy_client_control(body, _admin=MagicMock())
    assert exc.value.status_code == 503
    with patch.object(mod, "_deployed_config", return_value={
        "allowed_clients": ["10.0.0.0/8"],
    }), patch.object(mod, "_client_control_available", return_value=True), pytest.raises(
        HTTPException
    ) as exc:
        mod.proxy_client_control(body, _admin=MagicMock())
    assert exc.value.status_code == 400
