"""USB/IP state needed specifically by firmware mode transitions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from foundation.networking import parse_host_address, split_host_port

from . import runtime
from .usbip import usbip_manager


def _normalize_host(host: str) -> str:
    host = str(host or "").strip()
    return host.split("@", 1)[-1] if "@" in host else host


def _local_worker_id() -> str:
    try:
        from foundation.cluster_port import get_cluster_service

        return str(get_cluster_service().config.local_worker_id or "")
    except Exception:
        return ""


def _known_sources() -> dict[str, dict[str, Any]]:
    with runtime.global_state.usbip_devices_source_lock:
        sources = dict(runtime.global_state.usbip_devices_source)
    sources.update(getattr(usbip_manager, "device_sources", {}) or {})
    runtime_config = runtime.config_manager.get_runtime_config() or {}
    persisted = runtime_config.get("usbip_devices_source") or {}
    if isinstance(persisted, dict):
        sources.update(persisted)
    return sources


def resolve_usbip_flash_routes(
    device_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Resolve local source host/BUSID routes for USB re-enumeration."""
    requested = {
        str(device_id or "").strip()
        for device_id in device_ids or ()
        if str(device_id or "").strip()
    }
    if not requested:
        return []
    runtime_config = runtime.config_manager.get_runtime_config() or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    if not isinstance(assignments, dict):
        return []

    local_worker = _local_worker_id()
    candidates = []
    for info in assignments.values():
        if not isinstance(info, dict) or str(info.get("status") or "") not in {
            "attaching", "attached", "unknown", "cleanup_required",
        }:
            continue
        worker_id = str(info.get("worker_id") or "")
        if local_worker and worker_id and worker_id != local_worker:
            continue
        busid = str(info.get("busid") or "").strip()
        device_host = str(info.get("device_host") or "").strip()
        if not busid or not device_host:
            continue
        candidates.append({
            "device_host": device_host,
            "source_host": str(info.get("source_host") or "").strip()
            or _normalize_host(device_host),
            "busid": busid,
            "device_serials": {
                str(value or "").strip()
                for value in info.get("device_serials") or []
                if str(value or "").strip()
            },
        })

    known_sources = _known_sources()
    routes: dict[tuple[str, str], dict[str, Any]] = {}
    for device_id in sorted(requested):
        known_host = str(
            (known_sources.get(device_id) or {}).get("source") or ""
        ).strip()
        matches = [
            item for item in candidates
            if device_id in item["device_serials"]
            and (not known_host or _normalize_host(item["device_host"])
                 == _normalize_host(known_host))
        ]
        if not matches and known_host:
            host_matches = [
                item for item in candidates
                if _normalize_host(item["device_host"])
                == _normalize_host(known_host)
            ]
            if len(host_matches) == 1:
                matches = host_matches
        for item in matches:
            key = (item["device_host"], item["source_host"])
            route = routes.setdefault(key, {
                "device_host": item["device_host"],
                "source_host": item["source_host"],
                "busids": [],
                "device_ids": [],
            })
            if item["busid"] not in route["busids"]:
                route["busids"].append(item["busid"])
            if device_id not in route["device_ids"]:
                route["device_ids"].append(device_id)
    return list(routes.values())


def ensure_usbip_auto_bind_policies(
    device_host: str,
    busids: list[str],
    device_password: str | None = None,
) -> dict[str, Any]:
    """Create usbipd-win AutoBind rules for explicitly assigned ports."""
    selected = list(dict.fromkeys(
        str(item or "").strip() for item in busids or []
        if str(item or "").strip()
    ))
    if not selected or any(
        not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item) for item in selected
    ):
        return {"success": False, "error": "无效的USB/IP BUSID"}

    config = usbip_manager.config_manager.load_config()
    password = (
        device_password
        or usbip_manager.config_manager.find_device_host_password(
            device_host, config
        )
        or config.get("device_pswd", "")
    )
    if not password:
        return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
    try:
        username, hostname = parse_host_address(device_host)
        ssh_hostname, ssh_port = split_host_port(hostname)
    except Exception as exc:
        return {"success": False, "error": f"无效的设备主机地址: {exc}"}
    ssh = usbip_manager._create_windows_ssh(
        ssh_hostname, username, password, ssh_port
    )
    if not ssh:
        return {"success": False, "error": f"SSH连接失败到 {device_host}"}

    try:
        if not usbip_manager._is_windows_host(ssh):
            return {"success": False, "error": "USB/IP仅支持Windows主机"}
        installed, version = usbip_manager.check_usbipd_installed(ssh)
        if not installed:
            return {"success": False, "error": "usbipd未安装"}
        policy_out, policy_err, policy_code = (
            usbip_manager.ssh_manager.execute_command(
                ssh, "usbipd policy list", timeout=15
            )
        )
        if policy_code != 0:
            detail = (policy_err or policy_out or "").strip()
            return {
                "success": False,
                "error": (
                    "当前 usbipd-win 不支持 AutoBind 策略；"
                    "固件烧写需要 4.2.0 或更高版本"
                    + (f"（当前 {version}）" if version else "")
                    + (f": {detail}" if detail else "")
                ),
                "version": version,
            }

        def covered(output: str, busid: str) -> bool:
            return any(
                busid.casefold() in line.casefold()
                and "allow" in line.casefold()
                and "autobind" in line.casefold().replace("-", "")
                for line in (output or "").splitlines()
            )

        added, existing, errors = [], [], {}
        for busid in selected:
            if covered(policy_out, busid):
                existing.append(busid)
                continue
            add_out, add_err, add_code = (
                usbip_manager.ssh_manager.execute_command(
                    ssh,
                    "usbipd policy add --effect allow "
                    f"--operation AutoBind --busid {busid}",
                    timeout=15,
                )
            )
            if add_code == 0:
                added.append(busid)
            else:
                errors[busid] = (add_err or add_out or str(add_code)).strip()

        verify_out, verify_err, verify_code = (
            usbip_manager.ssh_manager.execute_command(
                ssh, "usbipd policy list", timeout=15
            )
        )
        verified = [
            busid for busid in selected
            if verify_code == 0 and covered(verify_out, busid)
        ]
        missing = [busid for busid in selected if busid not in verified]
        if missing:
            detail = "; ".join(
                f"{busid}: {errors.get(busid, '')}".rstrip(": ")
                for busid in missing
            )
            verify_detail = (verify_err or "").strip()
            return {
                "success": False,
                "error": (
                    "无法为固件烧写启用USB/IP AutoBind策略。"
                    "请确认Windows SSH账号具有管理员权限"
                    + (f": {detail}" if detail else "")
                    + (f"; {verify_detail}" if verify_detail else "")
                ),
                "missing_busids": missing,
                "version": version,
            }
        return {
            "success": True,
            "busids": verified,
            "added_busids": added,
            "existing_busids": existing,
            "version": version,
        }
    finally:
        ssh.close()
