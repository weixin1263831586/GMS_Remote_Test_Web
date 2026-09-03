"""USB/IP state needed specifically by firmware mode transitions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from foundation.networking import parse_host_address, split_host_port

from . import runtime
from .usbip import usbip_manager
from .usbip_linux_source import ensure_ubuntu_usbip_server


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
            "generation": int(info.get("generation") or 0),
            "operation_id": str(info.get("operation_id") or ""),
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
                "bindings": [],
            })
            if item["busid"] not in route["busids"]:
                route["busids"].append(item["busid"])
            if device_id not in route["device_ids"]:
                route["device_ids"].append(device_id)
                route["bindings"].append({
                    "device_id": device_id,
                    "busid": item["busid"],
                    "generation": item["generation"],
                    "operation_id": item["operation_id"],
                })
    return list(routes.values())


def open_usbip_source_ssh(
    device_host: str,
    device_password: str | None = None,
) -> tuple[Any | None, str]:
    """Open an SSH session to a USB/IP source host (Windows or Ubuntu)."""
    host = str(device_host or "").strip()
    if not host:
        return None, "缺少设备主机地址"
    config = usbip_manager.config_manager.load_config()
    password = (
        device_password
        or usbip_manager.config_manager.find_device_host_password(host, config)
        or config.get("device_pswd", "")
    )
    if not password:
        return None, f"未找到 {host} 的SSH凭据"
    try:
        username, hostname = parse_host_address(host)
        ssh_hostname, ssh_port = split_host_port(hostname)
    except Exception as exc:
        return None, f"无效的设备主机地址: {exc}"
    ssh = usbip_manager._create_windows_ssh(
        ssh_hostname, username, password, ssh_port
    )
    if not ssh:
        return None, f"SSH连接失败到 {host}"
    return ssh, ""


def _valid_usbip_busid(busid: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(busid or "").strip()))


def bind_usbip_busid_via_ssh(
    ssh, busid: str, *, force: bool = False,
) -> dict[str, Any]:
    """Share one present instance, optionally keeping the VBox stub resident."""
    busid = str(busid or "").strip()
    if not _valid_usbip_busid(busid):
        return {"success": False, "error": "无效的USB/IP BUSID"}
    command = f"usbipd bind --busid {busid}"
    if force:
        command += " --force"
    out, err, code = usbip_manager.ssh_manager.execute_command(
        ssh, command, timeout=15,
    )
    detail = (err or out or "").strip()
    return {"success": code == 0, "code": code, "detail": detail}


def usbipd_list_via_ssh(ssh) -> tuple[str, str]:
    """Capture the usbipd device table for post-mortem diagnosis."""
    out, err, code = usbip_manager.ssh_manager.execute_command(
        ssh, "usbipd list", timeout=15,
    )
    if code != 0:
        return "", (err or out or f"usbipd list exited with code {code}").strip()
    return (out or "").strip(), ""


def usbipd_policy_list_via_ssh(ssh) -> str:
    """Capture `usbipd policy list` for post-mortem AutoBind diagnosis."""
    out, err, code = usbip_manager.ssh_manager.execute_command(
        ssh, "usbipd policy list", timeout=15,
    )
    if code != 0:
        return (err or out or f"usbipd policy list exited with code {code}").strip()
    return (out or "").strip(), ""


def usbipd_policy_line_covers_busid(output: str, busid: str) -> bool:
    """Return True when a policy line already allows AutoBind for the exact busid.

    BUSID 必须整词匹配：子串判断会让 "1-1" 误命中 "1-11" 的既有规则，
    预检通过但实际 1-1 并没有 AutoBind 策略，MaskROM 重挂载随即失败。
    """
    token = re.compile(
        rf"(?<![A-Za-z0-9._-]){re.escape(str(busid or ''))}(?![A-Za-z0-9._-])",
        re.IGNORECASE,
    )
    for line in (output or "").splitlines():
        normalized = line.replace("-", "").casefold()
        if (
            token.search(line)
            and "allow" in line.casefold()
            and "autobind" in normalized
        ):
            return True
    return False


def _assigned_serials_for_busids(
    device_host: str,
    busids: list[str],
) -> set[str]:
    """Return ADB serials recorded for these BUSIDs in persistent assignments."""
    runtime_config = runtime.config_manager.get_runtime_config() or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    if not isinstance(assignments, dict):
        return set()
    selected = {str(item or "").strip() for item in busids}
    serials: set[str] = set()
    for info in assignments.values():
        if not isinstance(info, dict):
            continue
        if (
            str(info.get("device_host") or "").strip() == device_host
            and str(info.get("busid") or "").strip() in selected
        ):
            serials.update(
                str(serial or "").strip()
                for serial in info.get("device_serials") or []
                if str(serial or "").strip()
            )
    return serials


def _ensure_ubuntu_export(
    ssh,
    device_host: str,
    selected: list[str],
) -> dict[str, Any]:
    """Keep the Ubuntu usbipd server exporting the assigned devices.

    Windows 来源靠 usbipd-win AutoBind 策略在设备重枚举后自动重新共享；
    Ubuntu 来源的等价物是用户态 usbipd 进程：它按序列号过滤导出，并在
    设备以相同序列号重新枚举时自动重连。这里确保进程存活且过滤器覆盖
    持久分配的序列号。
    """
    config = usbip_manager.config_manager.load_config()
    inventory = usbip_manager._find_android_devices_linux(ssh, config)
    selected_busids = set(selected)
    assigned_serials = _assigned_serials_for_busids(device_host, selected)
    export_serials = sorted(
        assigned_serials
        or {
            item["serial"]
            for item in inventory
            if item["busid"] in selected_busids and item.get("serial")
        }
    )
    export_vids = sorted({
        item["vid_pid"].split(":", 1)[0]
        for item in inventory
        if item["busid"] in selected_busids and item.get("vid_pid")
    })
    server = ensure_ubuntu_usbip_server(
        usbip_manager.ssh_manager,
        ssh,
        serials=export_serials,
        vids=export_vids if not export_serials else (),
    )
    if not server.get("success"):
        return {
            "success": False,
            "error": f"Ubuntu来源USB/IP导出服务不可用: {server.get('error')}",
            "install_guide": server.get("install_guide"),
        }
    return {
        "success": True,
        "busids": selected,
        "source_os": "ubuntu",
        "reused": bool(server.get("reused")),
        "detail": (
            "usbipd导出进程已在运行并覆盖分配序列号"
            if server.get("reused") else "已启动usbipd导出进程"
        ),
    }


def ensure_usbip_auto_bind_policies(
    device_host: str,
    busids: list[str],
    device_password: str | None = None,
) -> dict[str, Any]:
    """Ensure rebind-after-reenumeration works for assigned ports.

    Windows 来源创建 usbipd-win AutoBind 规则；Ubuntu 来源确保用户态
    usbipd 导出进程存活并覆盖分配的序列号。
    """
    selected = list(dict.fromkeys(
        str(item or "").strip() for item in busids or []
        if str(item or "").strip()
    ))
    if not selected or any(not _valid_usbip_busid(item) for item in selected):
        return {"success": False, "error": "无效的USB/IP BUSID"}

    ssh, ssh_error = open_usbip_source_ssh(device_host, device_password)
    if not ssh:
        return {"success": False, "error": ssh_error}

    try:
        source_os = usbip_manager._detect_source_os(ssh)
        if source_os == "linux":
            return _ensure_ubuntu_export(ssh, device_host, selected)
        if source_os != "windows":
            return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
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
            return usbipd_policy_line_covers_busid(output, busid)

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
