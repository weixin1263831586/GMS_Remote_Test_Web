"""USB/IP source host inventory, SSH sessions and export lifecycle."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from foundation.networking import parse_host_address, split_host_port
from foundation.ssh_security import configure_strict_host_keys

from ..physical_identity import resolve_physical_device_identity
from ..usb import (
    configured_usbip_vid_pids,
    parse_usbipd_android_busids,
)
from .identity import (
    query_usbipd_busid_instance_ids,
    query_windows_usb_identities,
)
from .linux_source import ensure_ubuntu_usbip_server
from .protocol import parse_adb_device_states


logger = logging.getLogger(__name__)

# usbipd-win 的 BUSID 形如 "2-1.4"；来源命令按此白名单校验，
# 拒绝任何带 shell 元字符的输入。
_BUSID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")


def source_os_public(source_os: str) -> str:
    """Map internal OS kind to the public source_os API value."""
    return {"windows": "windows", "linux": "ubuntu"}.get(source_os, "")


def is_windows_host(ssh_manager, ssh) -> bool:
    try:
        result = ssh_manager.execute_command(ssh, 'ver 2>&1')
        return 'microsoft' in result.stdout.lower() or 'windows' in result.stdout.lower()
    except Exception:
        return False


def create_source_ssh(hostname: str, username: str, password: str, port: int = 22):
    """Open an SSH connection to a source host with strict host-key policy."""
    try:
        import paramiko
        ssh = paramiko.SSHClient()
        configure_strict_host_keys(ssh)
        ssh.connect(
            hostname=hostname,
            port=port,
            username=username,
            password=password,
            timeout=10
        )
        return ssh
    except Exception as e:
        logger.error(f"Error creating source SSH: {e}")
        return None


def _resolve_source_credentials(manager, device_host: str, config: dict[str, Any], device_password: str | None) -> str:
    password = (
        device_password
        or manager.config_manager.find_device_host_password(device_host, config)
        or config.get("device_pswd", "")
    )
    return password


def _open_source_session(manager, device_host: str, device_password: str | None):
    """Shared prologue: load config, resolve credentials, open source SSH.

    Returns ``(config, ssh, ssh_hostname, error_result)``; on failure
    ``ssh`` is None and ``error_result`` carries the API error payload.
    """
    config = manager.config_manager.load_config()
    password = _resolve_source_credentials(manager, device_host, config, device_password)
    if not password:
        return config, None, "", {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
    username, hostname = parse_host_address(device_host)
    ssh_hostname, ssh_port = split_host_port(hostname)
    ssh = manager._create_windows_ssh(ssh_hostname, username, password, ssh_port)
    if not ssh:
        return config, None, ssh_hostname, {"success": False, "error": f"SSH连接失败到 {device_host}"}
    return config, ssh, ssh_hostname, None


def _require_supported_source(manager, ssh) -> tuple[str, dict[str, Any] | None]:
    """Detect the source OS; returns (source_os, error_result)."""
    source_os = manager._detect_source_os(ssh)
    if source_os not in ("windows", "linux"):
        return source_os, {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
    return source_os, None


def probe_source_os(
    manager,
    device_host: str,
    device_password: str | None = None,
) -> dict[str, Any]:
    """Detect the OS of a source host via SSH; used for dropdown labels."""
    host = str(device_host or "").strip()
    if not host:
        return {"source_os": "", "error": "缺少设备主机地址"}
    _config, ssh, _ssh_hostname, error = _open_source_session(manager, host, device_password)
    if ssh is None:
        return {"source_os": "", "error": error["error"] if error else "SSH连接失败"}
    try:
        return {"source_os": manager._detect_source_os(ssh)}
    finally:
        ssh.close()


def ensure_source_export_ready(
    manager,
    device_host: str,
    busids: list[str] | None = None,
    device_password: str | None = None,
) -> dict[str, Any]:
    """Start the on-demand usbipd server for Ubuntu sources.

    Windows 来源的 usbipd-win 服务常驻，本方法为 no-op；Ubuntu 来源
    的用户态 usbipd 进程按需启动，用于 attach 前的 TCP 3240 预检
    补偿等场景。
    """
    config, ssh, ssh_hostname, error = _open_source_session(manager, device_host, device_password)
    if error:
        return error
    try:
        if manager._detect_source_os(ssh) != "linux":
            return {"success": True, "started": False}
        inventory = manager._find_android_devices_linux(ssh, config)
        selected_busids = {str(item or "") for item in busids or []}
        selected = [
            item for item in inventory
            if not selected_busids or item["busid"] in selected_busids
        ]
        server = ensure_ubuntu_usbip_server(
            manager.ssh_manager,
            ssh,
            serials=[
                item["serial"] for item in selected if item.get("serial")
            ],
            vids=sorted({
                item["vid_pid"].split(":", 1)[0]
                for item in selected if item.get("vid_pid")
            }),
            allow_worker_hosts=[
                config.get('usbip_attach_host') or ssh_hostname
            ],
            # Worker 侧凭据：平台配置的 Ubuntu/Worker 主机（用于在其上
            # 执行 ip route get 解析出口 IP）。
            worker_ssh_factory=(
                lambda _host: manager.ssh_manager.get_connection(config)
            ),
        )
        return {
            "success": bool(server.get("success")),
            "started": bool(server.get("started")),
            "detail": server.get("error") or server.get("detail") or "",
            "install_guide": server.get("install_guide") or "",
        }
    finally:
        ssh.close()


def _validate_busids(busids) -> list[str] | None:
    """Normalize/validate a busid selection; None when invalid."""
    selected = list(dict.fromkeys(
        str(item or "").strip() for item in busids or []
    ))
    if not selected or any(
        not _BUSID_PATTERN.fullmatch(item)
        for item in selected
    ):
        return None
    return selected


def _start_ubuntu_export(manager, ssh, config, ssh_hostname: str, selected_inventory: list[dict[str, Any]]) -> dict[str, Any]:
    """Start the Ubuntu user-space usbipd export for the given inventory."""
    return ensure_ubuntu_usbip_server(
        manager.ssh_manager,
        ssh,
        serials=[
            item["serial"] for item in selected_inventory
            if item.get("serial")
        ],
        vids=sorted({
            item["vid_pid"].split(":", 1)[0]
            for item in selected_inventory if item.get("vid_pid")
        }),
        allow_worker_hosts=[
            config.get("usbip_attach_host") or ssh_hostname
        ],
        # Worker 侧凭据：平台配置的 Ubuntu/Worker 主机（用于在其上
        # 执行 ip route get 解析出口 IP）。
        worker_ssh_factory=(
            lambda _host: manager.ssh_manager.get_connection(config)
        ),
    )


def bind_source_devices(
    manager,
    device_host: str,
    busids: list[str],
    device_password: str | None = None,
) -> dict[str, Any]:
    """Bind selected source USB devices for a remote Worker attach."""
    config, ssh, ssh_hostname, error = _open_source_session(manager, device_host, device_password)
    if error:
        return error
    try:
        source_os, os_error = _require_supported_source(manager, ssh)
        if os_error:
            return os_error
        selected = _validate_busids(busids)
        if selected is None:
            return {"success": False, "error": "无效的USB/IP BUSID"}
        if source_os == "linux":
            inventory = manager._find_android_devices_linux(ssh, config)
            unavailable = [
                item for item in selected
                if item not in {entry["busid"] for entry in inventory}
            ]
            if unavailable:
                return {
                    "success": False,
                    "error": (
                        "选择的USB设备已不可用，请刷新后重试: "
                        + ", ".join(unavailable)
                    ),
                }
            selected_inventory = [
                item for item in inventory if item["busid"] in set(selected)
            ]
            server = _start_ubuntu_export(manager, ssh, config, ssh_hostname, selected_inventory)
            if not server.get("success"):
                return {
                    "success": False,
                    "error": f"Ubuntu来源USB/IP服务启动失败: {server.get('error')}",
                    "install_guide": server.get("install_guide"),
                }
            return {
                "success": True,
                "device_host": device_host,
                "source_host": config.get("usbip_attach_host") or ssh_hostname,
                "source_os": source_os_public(source_os),
                "busids": selected,
            }

        if not manager.check_usbipd_installed(ssh)[0]:
            return {"success": False, "error": "usbipd未安装"}
        available = set(manager._find_android_devices(ssh, config))
        unavailable = [item for item in selected if item not in available]
        if unavailable:
            return {
                "success": False,
                "error": (
                    "选择的USB设备已不可用，请刷新后重试: "
                    + ", ".join(unavailable)
                ),
            }
        adb_release = manager._stop_windows_adb(ssh)
        if not adb_release.get("success"):
            return {
                "success": False,
                "error": f"释放Windows ADB占用失败: {adb_release.get('error')}",
            }
        bound = manager._bind_devices(ssh, selected)
        if set(bound) != set(selected):
            missing = [item for item in selected if item not in bound]
            return {
                "success": False,
                "error": "部分USB设备绑定失败: " + ", ".join(missing),
            }
        return {
            "success": True,
            "device_host": device_host,
            "source_host": config.get("usbip_attach_host") or ssh_hostname,
            "busids": bound,
        }
    finally:
        ssh.close()


def detach_source_sessions(
    manager,
    device_host: str,
    busids: list[str],
    device_password: str | None = None,
) -> dict[str, Any]:
    """Drop stale usbipd exports without removing persistent bindings."""
    selected = [str(item).strip() for item in busids or []]
    if not selected or any(
        not _BUSID_PATTERN.fullmatch(item)
        for item in selected
    ):
        return {"success": False, "error": "无效的USB/IP BUSID"}

    _config, ssh, _ssh_hostname, error = _open_source_session(manager, device_host, device_password)
    if error:
        return error

    try:
        source_os, os_error = _require_supported_source(manager, ssh)
        if os_error:
            return os_error
        if source_os == "linux":
            # Ubuntu 来源无每设备 usbipd 会话；断开由接入主机侧 vhci
            # detach 完成，来源侧只在整源断开时停止 usbipd 进程。
            return {
                "success": True,
                "source_os": source_os_public(source_os),
                "detached_busids": [],
                "errors": {},
            }
        if not manager.check_usbipd_installed(ssh)[0]:
            return {"success": False, "error": "usbipd未安装"}

        detached = []
        errors = {}
        for busid in selected:
            detach_result = manager.ssh_manager.execute_command(
                ssh,
                f"usbipd detach --busid {busid}",
                timeout=15,
            )
            detail = (detach_result.stderr or detach_result.stdout or "").strip()
            normalized_detail = detail.lower()
            if detach_result.ok or any(
                marker in normalized_detail
                for marker in (
                    "already not attached",
                    "is not attached",
                    "not currently attached",
                    "no devices are currently attached",
                )
            ):
                detached.append(busid)
            else:
                errors[busid] = (
                    detail
                    or f"usbipd detach exited with code {detach_result.code}"
                )
        return {
            "success": not errors,
            "detached_busids": detached,
            "errors": errors,
            "error": "; ".join(
                f"{busid}: {detail}" for busid, detail in errors.items()
            ),
        }
    finally:
        ssh.close()


def bind_usbipd_devices(
    ssh_manager,
    ssh,
    busids: list[str],
    track_newly_bound: list[str] | None = None,
) -> list[str]:
    """Bind USB devices on the Windows source host.

    ``track_newly_bound`` 收集本次调用真正执行 bind 的 busid（不含
    之前已处于 Shared 状态的设备），供 attach 失败时回滚，避免把
    本次事务之外预先存在的共享一并解除。
    """
    bound = []
    for busid in busids:
        if not _BUSID_PATTERN.fullmatch(str(busid or "")):
            logger.error("Rejected invalid USB/IP busid: %r", busid)
            continue
        try:
            list_result = ssh_manager.execute_command(
                ssh, f"usbipd list | findstr {busid}"
            )
            if list_result.code not in {0, 1}:
                logger.error(
                    "Failed to inspect USB/IP device %s: %s",
                    busid,
                    (list_result.stderr or list_result.stdout).strip(),
                )
                continue

            # usbipd list 输出按行解析:findstr 是子串匹配,busid "2-1" 会
            # 同时命中 "2-1"、"2-11" 等行;状态判定必须只看 busid 所属行,
            # 否则多设备同源时会把别的设备的状态归因到当前设备。
            # usbipd list 行格式:"  2-1    0abc  <vid:pid>  <desc>  <STATE>"。
            state_line = next(
                (
                    line
                    for line in list_result.stdout.splitlines()
                    if re.search(rf"(?:^|\s){re.escape(busid)}\s", line)
                ),
                "",
            )
            # usbipd 状态是整词（STATE 列：Not Shared / Shared / Attached），
            # "Not Shared" 含子串 "Shared"，必须按词边界判定而非 substring。
            if re.search(r'\bShared\b', state_line) and not re.search(
                r'\bNot Shared\b', state_line
            ):
                logger.info(f"Device {busid} already shared")
                bound.append(busid)
                continue
            elif re.search(r'\bAttached\b', state_line):
                detach_result = ssh_manager.execute_command(
                    ssh,
                    f"usbipd detach --busid {busid}",
                    timeout=15,
                )
                if not detach_result.ok:
                    logger.error(
                        "Failed to detach USB/IP device %s before bind: %s",
                        busid,
                        (detach_result.stderr or detach_result.stdout).strip(),
                    )
                    continue
                time.sleep(1)

            bind_result = ssh_manager.execute_command(
                ssh,
                f"usbipd bind --busid {busid}",
                timeout=15,
            )
            if not bind_result.ok:
                logger.error(
                    "Failed to bind USB/IP device %s: %s",
                    busid,
                    (bind_result.stderr or bind_result.stdout).strip(),
                )
                continue
            time.sleep(2)
            logger.info(f"Device {busid} bound")
            bound.append(busid)
            if track_newly_bound is not None:
                track_newly_bound.append(busid)

        except Exception as e:
            logger.error(f"Error binding device {busid}: {e}")

    return bound


def stop_windows_adb(ssh_manager, ssh) -> dict[str, Any]:
    """Gracefully stop Windows ADB and force it only when still running."""
    list_result = ssh_manager.execute_command(
        ssh,
        'tasklist /FI "IMAGENAME eq adb.exe" /NH',
        timeout=10,
    )
    if not list_result.ok:
        return {
            "success": False,
            "error": (list_result.stderr or list_result.stdout).strip()
            or "无法确认Windows ADB状态",
        }
    if "adb.exe" not in (list_result.stdout or "").lower():
        return {"success": True, "stopped": False, "forced": False}

    stop_result = ssh_manager.execute_command(
        ssh,
        "adb kill-server",
        timeout=15,
    )
    time.sleep(1)
    list_result = ssh_manager.execute_command(
        ssh,
        'tasklist /FI "IMAGENAME eq adb.exe" /NH',
        timeout=10,
    )
    if not list_result.ok:
        return {
            "success": False,
            "error": (list_result.stderr or list_result.stdout).strip()
            or "无法确认Windows ADB状态",
        }
    if "adb.exe" in (list_result.stdout or "").lower():
        force_result = ssh_manager.execute_command(
            ssh,
            "taskkill /F /IM adb.exe /T",
            timeout=15,
        )
        time.sleep(1)
        verify_result = ssh_manager.execute_command(
            ssh,
            'tasklist /FI "IMAGENAME eq adb.exe" /NH',
            timeout=10,
        )
        if not verify_result.ok or "adb.exe" in (verify_result.stdout or "").lower():
            return {
                "success": False,
                "error": "Windows adb.exe 仍在运行，USB设备句柄未释放: " + (
                    (
                        verify_result.stderr or verify_result.stdout
                        or force_result.stderr or force_result.stdout
                    ).strip()
                    or "unknown process state"
                ),
            }
        logger.warning(
            "Windows ADB required force stop before USB/IP export: "
            "code=%s detail=%s",
            force_result.code,
            (force_result.stderr or force_result.stdout).strip(),
        )
        return {"success": True, "stopped": True, "forced": True}
    logger.info(
        "Windows ADB stopped gracefully before USB/IP export: "
        "code=%s detail=%s",
        stop_result.code,
        (stop_result.stderr or stop_result.stdout).strip(),
    )
    return {"success": True, "stopped": True, "forced": False}




USBIPD_INSTALL_CMD = 'winget install dorssel.usbipd-win --source winget'

USBIPD_INSTALL_GUIDE = '''在Windows电脑上以【管理员身份】运行PowerShell执行：
{install_cmd}
验证安装：usbipd --version'''


def usbipd_not_installed_error() -> dict[str, Any]:
    """Standard error payload used whenever usbipd-win is missing."""
    return {
        'success': False,
        'error': 'usbipd未安装',
        'install_guide': USBIPD_INSTALL_GUIDE.format(
            install_cmd=USBIPD_INSTALL_CMD
        ),
    }


def check_usbipd_installed(ssh_manager, ssh) -> tuple[bool, str]:
    """Check whether usbipd is installed on the Windows host; return (installed, version)."""
    try:
        result = ssh_manager.execute_command(ssh, 'usbipd --version')
        if result.ok and result.stdout.strip():
            return True, result.stdout.strip()
        return False, ''
    except Exception as e:
        logger.error(f"Error checking usbipd: {e}")
        return False, ''


def install_usbipd(ssh_manager, ssh, config: dict[str, Any]) -> dict[str, Any]:
    """在 Windows 主机自动安装 usbipd。"""
    try:
        check_admin_cmd = 'whoami /groups | findstr S-1-16-12288'
        admin_result = ssh_manager.execute_command(ssh, check_admin_cmd)

        if not admin_result.ok or 'S-1-16-12288' not in admin_result.stdout:
            return {
                'success': False,
                'error': f'需要管理员权限。请在 Windows 上以【管理员身份】运行 PowerShell，然后执行: {USBIPD_INSTALL_CMD}'
            }

        install_cmd = f'{USBIPD_INSTALL_CMD} --accept-package-agreements --accept-source-agreements'
        install_result = ssh_manager.execute_command(ssh, install_cmd, timeout=120)

        if install_result.ok:
            installed, version = check_usbipd_installed(ssh_manager, ssh)
            if installed:
                return {
                    'success': True,
                    'message': f'usbipd 安装成功！版本: {version}',
                    'version': version
                }
            else:
                return {
                    'success': True,
                    'message': 'usbipd 安装完成，请验证版本'
                }
        else:
            return {
                'success': False,
                'error': f'安装失败: {install_result.stderr or install_result.stdout}'
            }

    except Exception as e:
        logger.error(f"Error installing usbipd: {e}")
        return {
            'success': False,
            'error': str(e)
        }


def list_source_devices(
    manager,
    device_host: str,
    device_password: str | None = None,
) -> dict[str, Any]:
    """List Android USB/IP busids on a Windows source without binding."""
    config = manager.config_manager.load_config()
    password = (
        device_password
        or manager.config_manager.find_device_host_password(device_host, config)
        or config.get("device_pswd", "")
    )
    if not password:
        return {"success": False, "error": f"未找到 {device_host} 的SSH凭据"}
    username, hostname = parse_host_address(device_host)
    ssh_hostname, ssh_port = split_host_port(hostname)
    ssh = manager._create_windows_ssh(ssh_hostname, username, password, ssh_port)
    if not ssh:
        return {"success": False, "error": f"SSH连接失败到 {device_host}"}
    try:
        source_os = manager._detect_source_os(ssh)
        if source_os not in ("windows", "linux"):
            return {"success": False, "error": "USB/IP仅支持Windows或Ubuntu主机"}
        if source_os == "linux":
            devices = _list_ubuntu_source_devices(manager, ssh, device_host, config)
            return {
                "success": True,
                "device_host": device_host,
                "source_os": manager._source_os_public(source_os),
                "devices": devices,
            }

        installed, _version = manager.check_usbipd_installed(ssh)
        if not installed:

            return usbipd_not_installed_error()
        output = _usbipd_list_output(manager.ssh_manager, ssh)
        busids = parse_usbipd_android_busids(
            output, configured_usbip_vid_pids(config)
        )
        labels = {}
        vid_pid_by_busid: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            parts = stripped.split()
            if parts and parts[0] in busids:
                clean = re.sub(r"\s+", " ", stripped)
                labels[parts[0]] = clean
                vp = re.search(r"([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})", clean)
                if vp:
                    vid_pid_by_busid[parts[0]] = f"{vp[1].lower()}:{vp[2].lower()}"
        serial_by_vid_pid = query_windows_usb_serials(
            manager.ssh_manager,
            ssh,
            {
                value.split(":", 1)[0]
                for value in vid_pid_by_busid.values()
            },
        )
        identity_by_vid_pid = query_windows_usb_identities(
            manager.ssh_manager,
            ssh,
            {
                value.split(":", 1)[0]
                for value in vid_pid_by_busid.values()
            },
        )
        pnp_instance_by_busid = query_usbipd_busid_instance_ids(
            manager.ssh_manager, ssh
        )
        serial_by_busid = {
            busid: (
                serial_by_vid_pid.get(vid_pid_by_busid[busid], "")
                or (
                    serial_by_vid_pid.get("*", "")
                    if len(busids) == 1 else ""
                )
            )
            for busid in busids
            if busid in vid_pid_by_busid
        }
        if len(busids) == 1 and not serial_by_busid.get(busids[0]):
            adb_serials = query_windows_adb_serials(manager.ssh_manager, ssh)
            if len(adb_serials) == 1:
                serial_by_busid[busids[0]] = adb_serials[0]
        devices: list[dict[str, Any]] = []
        for item in busids:
            vid_pid = vid_pid_by_busid.get(item, "")
            pnp_instance_id = pnp_instance_by_busid.get(item, "")
            identity = (
                identity_by_vid_pid.get(
                    f"pnp:{pnp_instance_id.casefold()}", {}
                )
                if pnp_instance_id
                else {}
            ) or identity_by_vid_pid.get(vid_pid, {})
            android_serial = serial_by_busid.get(item, "")
            physical = resolve_physical_device_identity(
                source_host=device_host,
                current_usb_busid=item,
                logical_android_serial=android_serial,
                usb_serial=identity.get("usb_serial", ""),
                container_id=identity.get("container_id", ""),
                pnp_instance_id=(
                    identity.get("pnp_instance_id", "")
                    or pnp_instance_id
                ),
                location_path=identity.get("location_path", ""),
                vid_pid=vid_pid,
            )
            devices.append({
                "busid": item,
                "serial": android_serial,
                "logical_device_id": (
                    android_serial
                    or identity.get("pnp_instance_id", "")
                    or item
                ),
                **physical.to_dict(),
                "vid_pid": vid_pid,
                # Backward-compatible alias; current_usb_busid is the new
                # explicit transport field.
                "current_busid": item,
                "label": _append_serial(
                    labels.get(item, item), android_serial
                ),
            })
        return {
            "success": True,
            "device_host": device_host,
            "source_os": manager._source_os_public(source_os),
            "devices": devices,
        }
    finally:
        ssh.close()


def _list_ubuntu_source_devices(
    manager, ssh, device_host: str, config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the source-device inventory for an Ubuntu/Linux source host."""
    items = manager._find_android_devices_linux(ssh, config)
    devices: list[dict[str, Any]] = []
    for item in items:
        android_serial = item.get("serial", "")
        physical = resolve_physical_device_identity(
            source_host=device_host,
            current_usb_busid=item["busid"],
            logical_android_serial=android_serial,
            usb_serial=android_serial,
            location_path=item.get("location_path", ""),
            vid_pid=item.get("vid_pid", ""),
        )
        devices.append({
            "busid": item["busid"],
            "serial": android_serial,
            "logical_device_id": android_serial or item["busid"],
            **physical.to_dict(),
            "vid_pid": item.get("vid_pid", ""),
            "current_busid": item["busid"],
            "label": _append_serial(item.get("label", ""), android_serial),
        })
    return devices


def query_windows_usb_serials(
    ssh_manager,
    ssh,
    vendor_ids: set[str] | None = None,
) -> dict[str, str]:
    """Query Windows for USB device serials, keyed by ``vid:pid``.

    Parses ``Get-PnpDevice`` output to extract device IDs like
    ``USB\\VID_xxxx&PID_yyyy\\SERIAL``. When multiple devices share the same
    VID:PID the value is cleared (``""``) since the serial cannot be
    uniquely mapped back to a busid.
    """
    ps = (
        "Get-PnpDevice -PresentOnly -Class USB | "
        "ForEach-Object { $_.InstanceId }"
    )
    try:
        result = ssh_manager.execute_command(
            ssh, f'powershell -NoProfile -Command "{ps}"', timeout=15
        )
    except Exception:
        return {}
    if not result.ok or not result.stdout:
        return {}
    raw: dict[str, list[str]] = {}
    candidates: list[str] = []
    for line in result.stdout.splitlines():
        match = re.search(
            r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})"
            r"([^\\]*)\\(.+)",
            line.strip(),
        )
        if not match:
            continue
        vid, pid, interface, rest = match.groups()
        vid = vid.lower()
        pid = pid.lower()
        if vendor_ids and vid not in vendor_ids:
            continue
        # Interface instance IDs (MI_XX) are Windows-generated values, not
        # stable Android serials.
        if "&MI_" in interface.upper():
            continue
        serial = rest.strip().split("&")[0].strip()
        if not serial or serial.startswith(("REV_", "MI_")):
            continue
        raw.setdefault(f"{vid}:{pid}", []).append(serial)
        candidates.append(serial)
    result_map = {
        key: (values[0] if len(set(values)) == 1 else "")
        for key, values in raw.items()
    }
    unique_candidates = set(candidates)
    if len(unique_candidates) == 1:
        # Android changes PID across adb/recovery/rockusb modes. When the
        # selected USB/IP inventory contains exactly one busid, this
        # vendor-scoped fallback still maps that physical device safely.
        result_map["*"] = next(iter(unique_candidates))
    return result_map


def query_windows_adb_serials(ssh_manager, ssh) -> list[str]:
    """Return stable Android serials visible to Windows ADB."""
    try:
        result = ssh_manager.execute_command(
            ssh,
            "adb devices",
            timeout=15,
        )
    except Exception:
        return []
    if not result.ok:
        logger.debug(
            "[USB/IP] Windows adb inventory failed: %s",
            (result.stderr or result.stdout or "").strip(),
        )
        return []
    states = parse_adb_device_states(result.stdout)
    return sorted({
        serial
        for serial, state in states.items()
        if state in {
            "device",
            "recovery",
            "sideload",
            "unauthorized",
            "offline",
        }
    })


def _append_serial(label: str, serial: str | None) -> str:
    if not serial:
        return label
    return f"{label}  [{serial}]"


def _usbipd_list_output(ssh_manager, ssh) -> str:
    # usbipd list 需要 PTY 才会返回完整设备表。
    result = ssh_manager.execute_command(
        ssh, "usbipd list", timeout=15, get_pty=True
    )
    output = "\n".join(
        part for part in (result.stdout, result.stderr) if part
    )
    logger.info("USB/IP devices (code=%s):\n%s", result.code, output)
    return output


__all__ = [
    "bind_source_devices",
    "bind_usbipd_devices",
    "create_source_ssh",
    "detach_source_sessions",
    "ensure_source_export_ready",
    "is_windows_host",
    "list_source_devices",
    "probe_source_os",
    "query_windows_adb_serials",
    "query_windows_usb_serials",
    "source_os_public",
    "stop_windows_adb",
]
