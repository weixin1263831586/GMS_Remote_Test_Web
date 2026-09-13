"""USB/IP 来源主机会话操作（source-side session boundary）。

从 ``usbip.py`` 抽出的来源侧模块：与 Windows(usbipd-win)/Ubuntu(用户态
usbipd) 来源主机建立 SSH 会话、识别系统类型、以及围绕来源导出的四个
公开操作（probe/ensure_export/bind/detach）。共享同一套凭据解析与
会话建立流程：

    resolve_source_password → create_source_ssh → detect_source_os → 操作

实现为模块级函数并显式注入 ``manager``（提供 ``ssh_manager`` /
``config_manager`` 与可被测试覆盖的 ``_bind_devices`` 等实例方法），
与 ``usbip_transaction`` 的依赖注入风格一致。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from foundation.networking import parse_host_address, split_host_port
from foundation.ssh_security import configure_strict_host_keys

from .usbip_linux_source import ensure_ubuntu_usbip_server


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


def detect_source_os(ssh_manager, ssh) -> str:
    """Classify a source host: 'windows', 'linux' or '' (unsupported)."""
    if is_windows_host(ssh_manager, ssh):
        return "windows"
    try:
        result = ssh_manager.execute_command(
            ssh, "uname -s", timeout=8,
        )
    except Exception:
        return ""
    if result.ok and "linux" in (result.stdout or "").strip().lower():
        return "linux"
    return ""


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
                # Detach first
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

            # Bind
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


__all__ = [
    "bind_source_devices",
    "bind_usbipd_devices",
    "create_source_ssh",
    "detach_source_sessions",
    "detect_source_os",
    "ensure_source_export_ready",
    "is_windows_host",
    "probe_source_os",
    "source_os_public",
    "stop_windows_adb",
]
