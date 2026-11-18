"""Local rk-vpn-proxy systemd control and loopback-only statistics bridge.

This adapter never runs browser-provided commands and never forwards to a
user-specified URL.  The deployed stats service stays on 127.0.0.1.  Privileged
start/stop requires a human admin elevation plus narrow sudoers rules.
"""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from features.auth import (
    CurrentUser,
    require_authenticated_user_when_auth_required,
    require_elevated_admin,
)


router = APIRouter(prefix="/api/tools/vpn-proxy", tags=["vpn-proxy"])
DEPLOYED_CONFIG = Path("/etc/rk-vpn-proxy/config.json")
DISABLED_CLIENTS = Path("/etc/rk-vpn-proxy/disabled_clients.json")
CLIENT_CONTROL_HELPER = Path("/usr/local/libexec/rk-vpn-proxy-client-control")
SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
SERVICES = {
    "proxy": "rk-vpn-proxy.service",
    "firewall": "rk-vpn-proxy-firewall.service",
    "stats": "rk-vpn-proxy-stats.service",
    "installer": "rk-vpn-proxy-installer.service",
}
_CONTROL_LOCK = threading.Lock()


class ProxyControlRequest(BaseModel):
    action: Literal["start", "stop"]


class ProxyClientAccessRequest(BaseModel):
    action: Literal["enable", "disable"]
    client: str


def _deployed_config() -> dict:
    try:
        # Treat only the installed config as the source of truth.  The GitHub
        # checkout config.json may differ from the running service.
        if DEPLOYED_CONFIG.stat().st_size > 65536:
            raise ValueError("proxy configuration is too large")
        config = json.loads(DEPLOYED_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("proxy configuration must be an object")
        return config
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _port(value, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if 1 <= result <= 65535 else default


def _service_state(unit: str) -> str:
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-active", unit],
            capture_output=True, text=True, timeout=4, check=False,
        )
        state = result.stdout.strip()
        return state if state in ("active", "inactive", "failed", "activating",
                                   "deactivating") else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _stats(port: int) -> tuple[dict | None, str | None]:
    # Localhost is fixed to prevent SSRF.  No proxy environment may interfere.
    url = f"http://127.0.0.1:{port}/api/stats"
    try:
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            if response.status != 200:
                return None, "统计服务响应异常"
            payload = response.read(256 * 1024 + 1)
            if len(payload) > 256 * 1024:
                return None, "客户端统计数据过大"
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                return None, "统计服务返回格式无效"
            # Upstream client_stats.py keeps last good rows when polling fails.
            # Do not present those old clients as currently connected.
            if parsed.get("failed") or parsed.get("error"):
                return None, "统计采集失败，请检查客户端统计服务日志"
            if not isinstance(parsed.get("connected"), list) or not isinstance(
                parsed.get("recent"), list
            ):
                return None, "统计服务返回格式无效"
            sources = parsed.get("sources")
            if not isinstance(sources, dict) or not all(
                isinstance(sources.get(key), bool)
                for key in ("connections", "access_log")
            ):
                return None, "统计来源状态缺失，无法确认客户端数据是否完整"
            try:
                age = time.time() - float(parsed["time"])
            except (KeyError, TypeError, ValueError):
                return None, "统计服务缺少采集时间"
            if not -5 <= age <= 45:
                return None, "统计快照已过期，请检查统计服务"
            return parsed, None
    except (URLError, HTTPError, OSError, ValueError, TimeoutError) as error:
        return None, f"统计服务不可用：{type(error).__name__}"


def _host(config: dict) -> str:
    hostname = str(config.get("advertise_host") or config.get("listen_host") or "")
    # Only ASCII hostnames/IPs may appear in generated shell snippets.
    return hostname if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", hostname) else ""


def _install_scripts(config: dict) -> dict:
    host = _host(config)
    if not host or not config.get("installer_enabled"):
        return {"linux": "", "windows": ""}
    port = _port(config.get("installer_port"), 8089)
    prefix = f"http://{host}:{port}"
    # Both values are complete, executable single-line commands.
    # Download before executing; clean up and propagate failure.
    linux = (
        '_rk_file=$(mktemp) && { '
        f'if curl --noproxy "*" -fsSL "{prefix}/install.sh" -o "$_rk_file"; then '
        'source "$_rk_file"; _rk_rc=$?; '
        'else echo "Installer download failed" >&2; _rk_rc=1; fi; '
        'rm -f -- "$_rk_file"; test "$_rk_rc" -eq 0; }'
    )
    windows = (
        f'$proxyScript = curl.exe --noproxy "*" -fsSL "{prefix}/install.ps1"; '
        'if ($LASTEXITCODE -ne 0) { throw "Installer download failed" }; '
        '& ([scriptblock]::Create(($proxyScript -join "`n"))) -SystemProxy'
    )
    return {"linux": linux, "windows": windows}


def _run_admin_command(command: list[str]) -> None:
    # None of the command/args comes from the HTTP request.
    try:
        result = subprocess.run(
            ["sudo", "-n", SYSTEMCTL, *command],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(status_code=503, detail="代理控制命令不可用或超时") from error
    if result.returncode:
        # Never send arbitrary privileged stderr or local paths to the browser.
        raise HTTPException(
            status_code=503,
            detail="服务操作失败；检查 systemd 服务及 GMS 服务账号的最小权限 sudoers",
        )


def _client_control_available() -> bool:
    try:
        meta = CLIENT_CONTROL_HELPER.stat()
        return (not CLIENT_CONTROL_HELPER.is_symlink()
                and stat.S_ISREG(meta.st_mode)
                and meta.st_uid == 0
                and (meta.st_mode & 0o022) == 0)
    except OSError:
        return False


def _disabled_client_state() -> tuple[list[str], str | None]:
    try:
        if not DISABLED_CLIENTS.exists():
            return [], None
        if DISABLED_CLIENTS.is_symlink() or DISABLED_CLIENTS.stat().st_size > 32768:
            raise ValueError("invalid client state file")
        state = json.loads(DISABLED_CLIENTS.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError("invalid client state version")
        clients = state.get("disabled_clients")
        if not isinstance(clients, list) or len(clients) > 256:
            raise ValueError("invalid client list")
        parsed = [str(ipaddress.ip_address(value)) for value in clients]
        if len(set(parsed)) != len(parsed):
            raise ValueError("duplicate client addresses")
        return sorted(parsed), None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return [], "客户端禁用记录不可读；已暂停操作以避免覆盖现有策略"


def _is_authorized_client(client: str, config: dict) -> bool:
    try:
        address = ipaddress.ip_address(client)
        if str(address) != client or address.is_loopback or address.is_unspecified:
            return False
        networks = config.get("allowed_clients")
        return isinstance(networks, list) and any(
            address in ipaddress.ip_network(network, strict=False)
            for network in networks
        )
    except ValueError:
        return False


@router.get("/status")
def proxy_status(
    _user: CurrentUser | None = Depends(require_authenticated_user_when_auth_required),
) -> dict:
    config = _deployed_config()
    installed = bool(config)
    services = {name: _service_state(unit) for name, unit in SERVICES.items()}
    port = _port(config.get("stats_port"), 17898)
    stats, stats_error = _stats(port) if installed else (None, "服务尚未部署或配置不可读")
    host = _host(config)
    proxy_url = f"http://{host}:{_port(config.get('proxy_port'), 7897)}" if host else ""
    disabled, disabled_error = _disabled_client_state()
    return {"success": True, "data": {
        "installed": installed,
        "services": services,
        "config": {
            "proxy_url": proxy_url,
            "installer_enabled": bool(config.get("installer_enabled")),
            "allowed_clients": config.get("allowed_clients", [])
            if isinstance(config.get("allowed_clients"), list) else [],
        },
        "clients": stats,
        "stats_error": stats_error,
        "client_access": {
            "available": bool(installed and _client_control_available() and not disabled_error),
            "disabled": disabled,
            "error": disabled_error,
        },
        "install": _install_scripts(config),
    }}


@router.post("/control")
def proxy_control(
    body: ProxyControlRequest,
    _admin: CurrentUser = Depends(require_elevated_admin),
) -> dict:
    if not _deployed_config():
        raise HTTPException(status_code=409, detail="代理尚未部署，不能操作服务")
    if not _CONTROL_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有代理服务操作进行中")
    try:
        if body.action == "start":
            # Firewall first: systemd and proxy ExecStartPre enforce policy.
            _run_admin_command(["start", SERVICES["firewall"]])
            _run_admin_command(["start", SERVICES["proxy"]])
        else:
            # Keep the firewall running; never tear down the kill-switch.
            _run_admin_command(["stop", SERVICES["proxy"]])
        current = _service_state(SERVICES["proxy"])
        desired = "active" if body.action == "start" else "inactive"
        if current != desired:
            raise HTTPException(status_code=503, detail="服务未达到目标状态，请查看 systemd 日志")
        return {"success": True, "data": {"action": body.action, "state": current}}
    finally:
        _CONTROL_LOCK.release()



@router.post("/client-control", include_in_schema=False)
def proxy_client_control(
    body: ProxyClientAccessRequest,
    _admin: CurrentUser = Depends(require_elevated_admin),
) -> dict:
    """Grant/revoke one configured source IP through the deployed root helper.

    The helper is fixed and root-owned, and validates the exact client again.
    No browser argument can name a command, file, systemd unit or network CIDR.
    """
    config = _deployed_config()
    if not config:
        raise HTTPException(status_code=409, detail="vpn-proxy 尚未安装")
    if not _is_authorized_client(body.client, config):
        raise HTTPException(status_code=400, detail="IP 不属于当前授权网段或格式不合法")
    if not _client_control_available():
        raise HTTPException(status_code=503, detail="客户端访问控制尚未部署，请先升级 vpn-proxy")
    _, state_error = _disabled_client_state()
    if state_error:
        raise HTTPException(status_code=503, detail=state_error)
    if not _CONTROL_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有代理控制操作进行中")
    try:
        try:
            result = subprocess.run(
                ["sudo", "-n", str(CLIENT_CONTROL_HELPER), body.action, body.client],
                capture_output=True, text=True, timeout=25, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HTTPException(status_code=503, detail="客户端访问控制不可用或超时") from error
        if result.returncode:
            raise HTTPException(
                status_code=503,
                detail="客户端访问控制失败；检查 sudoers、Squid ACL 和 systemd 日志",
            )
        try:
            reported = json.loads(result.stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=503, detail="客户端控制结果格式无效") from error
        expected_disabled = body.action == "disable"
        if (not isinstance(reported, dict)
                or reported.get("client") != body.client
                or reported.get("disabled") is not expected_disabled):
            raise HTTPException(status_code=503, detail="客户端控制结果与请求不一致")
        return {"success": True, "data": reported}
    finally:
        _CONTROL_LOCK.release()
