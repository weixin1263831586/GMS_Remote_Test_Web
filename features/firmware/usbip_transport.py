"""USB/IP coordination used by firmware and GSI mode transitions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time

from features.devices import (
    DeviceUtils,
    bind_usbip_busid_via_ssh,
    ensure_usbip_auto_bind_policies,
    open_usbip_source_ssh,
    parse_adb_device_states,
    resolve_usbip_flash_routes,
    usbipd_list_via_ssh,
    usbipd_policy_list_via_ssh,
)
from features.devices import reconnect as usbip_reconnect
from features.devices.usbip_identity import (
    query_usbipd_busid_instance_ids,
    query_usbipd_device_states,
)
from features.devices.usbip_transaction import (
    USBIP_PORT_COMMAND,
    parse_usbip_port_entries,
)

from . import runtime


logger = logging.getLogger(__name__)
ROCKUSB_LOADER_COUNT_RE = re.compile(
    r"List\s+of\s+rockusb\s+connected\(\s*(\d+)\s*\)", re.IGNORECASE
)
# usbipd list Connected 区行：BUSID  VID:PID  DEVICE  STATE（STATE 可缺省）。
_USBIPD_CONNECTED_ROW_RE = re.compile(
    r"^(?P<busid>[0-9]+-[0-9]+(?:\.[0-9]+)*)\s+"
    r"(?P<vid_pid>[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})\s+"
    r"(?P<device>.+?)"
    r"(?:\s{2,}(?P<state>Not shared|Allowed|Shared(?: \([^)]*\))?|Attached.*?))?$"
)
ROCKCHIP_USB_VENDOR_ID = "2207"
FIRMWARE_RECONNECT_STOP_SECONDS = 20.0
FIRMWARE_FAILED_RECONNECT_COOLDOWN_SECONDS = 5 * 60


def parse_usbipd_connected_rows(output: str) -> list[dict[str, str]]:
    """Parse the Connected section of `usbipd list` into structured rows."""
    rows, in_connected = [], False
    for line in (output or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Connected:"):
            in_connected = True
            continue
        if stripped.startswith("Persisted:"):
            break
        if not in_connected:
            continue
        match = _USBIPD_CONNECTED_ROW_RE.match(line)
        if match:
            rows.append({
                "busid": match.group("busid"),
                "vid_pid": match.group("vid_pid").lower(),
                "device": (match.group("device") or "").strip(),
                "state": (match.group("state") or "").strip(),
            })
    return rows


def diagnose_maskrom_reattach_failure(
    snapshot: str, pending_busids: list[str],
) -> str:
    """Turn the Windows `usbipd list` snapshot into a precise failure reason.

    返回空串表示快照没有给出更精确的结论（沿用通用提示）。可判定场景：
    物理端口枚举失败（0000:0002 描述符读取失败）、RockUSB 枚举到了新
    BUSID、目标 BUSID 完全未重新出现、目标 BUSID 已被其他客户端 attach
    （Ubuntu vhci 残留）、目标 BUSID 存在但未被共享。
    """
    rows = parse_usbipd_connected_rows(snapshot)
    if not rows:
        return ""
    pending = {str(busid or "").strip() for busid in pending_busids or ()}

    pending_rows = [row for row in rows if row["busid"] in pending]
    descriptor_failed = [
        row for row in pending_rows
        if row["vid_pid"] == "0000:0002"
        or "descriptor request failed" in row["device"].lower()
    ]
    if descriptor_failed:
        forced = any("forced" in row["state"].lower() for row in descriptor_failed)
        return (
            "Loader切换期间 Windows 最终未能读取 USB 设备描述符"
            "（0000:0002 Unknown USB Device）；该状态发生在设备重新导出"
            "之前，所以此刻 bind/attach 均无法恢复。若同一 BUSID 此前曾正常"
            "显示 2207:* Shared/Attached，优先排查 usbipd 普通 attach 引起的"
            "驱动切换/端口复位与 Rockchip 重枚举冲突，并在维护窗口人工使用"
            " Shared (forced) 做 A/B 验证；同时仍需检查线材、Hub、供电和"
            "固件 Loader。当前设备请先断电重上电。"
            + (
                "检测到该 BUSID 处于 Shared (forced) 状态；AutoBind 无法自动"
                "重建 forced binding，但该状态本身不能证明是描述符失败的根因。"
                "请停止烧写后确认此设备是否确因 Windows filter driver 必须使用"
                " --force；若不需要，再执行 `usbipd unbind --busid <BUSID>` 并"
                "断电重插（平台代码不会自动使用或撤销 --force）。"
                if forced else ""
            )
        )

    rockchip_rows = [row for row in rows if row["vid_pid"].startswith(f"{ROCKCHIP_USB_VENDOR_ID}:")]
    moved_busids = sorted(
        row["busid"] for row in rockchip_rows if row["busid"] not in pending
    )
    if moved_busids:
        return (
            "RockUSB 设备在重新枚举后出现在新 BUSID（"
            + ", ".join(moved_busids)
            + "），而平台持久记录仍为 "
            + (", ".join(sorted(pending)) or "未知")
            + "。请断开后从设备管理页重新选择该 USB 设备并连接以刷新分配记录。"
        )

    if pending and not pending_rows:
        # 生产日志实录：快照时刻目标 BUSID 尚未回到 Connected 列表，随后
        # 手工 usbipd list 才出现 0000:0002。该场景与 TCP 3240/AutoBind
        # 无关，必须给出枚举层结论而不是通用网络提示。
        return (
            "目标 BUSID（"
            + ", ".join(sorted(pending))
            + "）未在 Windows 上重新出现：MaskROM 新实例未完成 USB 枚举"
            "（或已更换物理端口）。请在 Windows 上用 "
            "`Get-PnpDevice -PresentOnly | ? InstanceId -match 'VID_2207|VID_0000'`"
            " 核对枚举状态，优先检查 USB 线材、Hub、供电与固件内的 Loader；"
            "断电重上电后重试。"
        )

    attached_elsewhere = [
        row for row in pending_rows
        if row["state"].lower().startswith("attached")
    ]
    if attached_elsewhere:
        return (
            "目标 BUSID 在 Windows 上仍显示 Attached（服务端保留旧 attach "
            "会话），而本地 attach 失败：Ubuntu 侧大概率残留旧 vhci 端口。"
            "请在 Ubuntu 执行 `sudo usbip port` 找到 "
            "`usbip://<来源主机>:3240/<BUSID>` 对应端口并仅 "
            "`sudo usbip detach -p <PORT>` 该端口后重试（不要 detach all）。"
        )

    unshared = [row for row in pending_rows if "not shared" in row["state"].lower()]
    if unshared:
        return (
            "目标 BUSID 在 Windows 上处于 Not shared：AutoBind 策略未对"
            " MaskROM 新实例生效。请核对 `usbipd policy list` 中该 BUSID 的"
            " Allow AutoBind 规则及 Windows SSH 账号的管理员权限。"
        )
    return ""


def schedule_usbip_mode_reconnect(device: str, target_protocol: str) -> bool:
    """Rebind a USB/IP device after its USB identity changes."""
    try:
        device_host = usbip_reconnect.usbip_source_host_for_device(device)
        if not device_host:
            return False
        return usbip_reconnect.schedule_usbip_reconnect(
            device_host,
            reason=f"USB/IP {device} switching to {target_protocol}",
            expected_devices=[device],
            accept_transport_only=True,
        )
    except Exception as exc:
        logger.warning("USB/IP mode reconnect schedule failed for %s: %s", device, exc)
        return False


def usbip_firmware_route_devices(routes: list[dict]) -> list[str]:
    return sorted({
        str(device_id or "").strip()
        for route in routes or []
        for device_id in route.get("device_ids") or []
        if str(device_id or "").strip()
    })


async def pause_usbip_firmware_reconnects(
    routes: list[dict], *, timeout: float = FIRMWARE_RECONNECT_STOP_SECONDS,
) -> tuple[list[str], str]:
    """Give firmware flashing exclusive ownership of its USB/IP routes.

    A generic reconnect worker started by the ADB disappearance monitor can
    otherwise keep issuing ``usbip attach`` while ``upgrade_tool`` is changing
    Loader/MaskROM identities.  Pause new workers first, then stop and verify
    every existing source-host worker before the burn is allowed to start.
    """
    devices = usbip_firmware_route_devices(routes)
    device_hosts = sorted({
        str(route.get("device_host") or "").strip()
        for route in routes or []
        if str(route.get("device_host") or "").strip()
    })
    if not devices:
        return [], "USB/IP固件路由缺少目标设备标识，无法建立独占重连保护"

    usbip_reconnect.pause_usbip_reconnect(device_ids=devices)
    logger.info(
        "USB/IP firmware reconnect ownership acquired before mode transition: "
        "devices=%s routes=%s",
        devices,
        [
            f"{route.get('device_host')}/"
            f"{','.join(str(busid) for busid in route.get('busids') or [])}"
            for route in routes or []
        ],
    )
    for device_host in device_hosts:
        await asyncio.to_thread(
            usbip_reconnect.stop_usbip_reconnect_for_host,
            device_host,
            timeout,
        )

    active = set(usbip_reconnect.active_usbip_reconnect_hosts())
    contended = sorted(set(device_hosts) & active)
    if contended:
        return devices, (
            "USB/IP后台重连仍在占用固件物理端口，已拒绝启动烧写: "
            + ", ".join(contended)
        )
    return devices, ""


def resume_usbip_firmware_reconnects(devices: list[str]) -> None:
    usbip_reconnect.resume_usbip_reconnect(device_ids=devices)


def defer_usbip_firmware_reconnects_after_failure(
    devices: list[str],
    *, ttl_seconds: int = FIRMWARE_FAILED_RECONNECT_COOLDOWN_SECONDS,
) -> None:
    """Prevent a descriptor failure from turning into an attach storm."""
    usbip_reconnect.pause_usbip_reconnect(
        device_ids=devices,
        ttl_seconds=ttl_seconds,
    )


async def wait_for_rockusb_loaders(
    ssh, check_cmd: str, expected_count: int, *,
    timeout: float = 120, interval: float = 2,
) -> tuple[bool, str]:
    deadline, last_detail = time.monotonic() + max(1, timeout), ""
    while True:
        output, error, _code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=5
        )
        last_detail = (output or error or "").strip()
        match = ROCKUSB_LOADER_COUNT_RE.search(last_detail)
        if match and int(match.group(1)) >= max(1, expected_count):
            return True, last_detail
        if time.monotonic() >= deadline:
            return False, last_detail
        await asyncio.sleep(max(0.1, interval))


async def wait_for_adb_devices(
    ssh, expected_devices: list[str], *,
    timeout: float = 120, interval: float = 2,
) -> tuple[bool, list[str]]:
    expected = set(expected_devices)
    deadline = time.monotonic() + max(1, timeout)
    observed: list[str] = []
    while True:
        output, _error, _code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, "adb devices", timeout=8
        )
        states = parse_adb_device_states(output)
        observed = sorted(
            serial for serial, state in states.items() if state == "device"
        )
        if expected.issubset(observed):
            return True, observed
        if time.monotonic() >= deadline:
            return False, observed
        await asyncio.sleep(max(0.1, interval))


def _normalized_usbip_host(host: str) -> str:
    value = str(host or "").strip()
    if "@" in value:
        value = value.split("@", 1)[-1]
    return value.strip("[]")


async def _usbip_pair_already_attached(ssh, source_host: str, busid: str) -> bool:
    """核验 (host, busid) 是否已出现在本地 ``usbip port`` 列表。

    attach 客户端超时（execute_command 返回 ``-1``）或重试撞上端口占用时，
    服务端实际可能已完成挂载。以 ``usbip port`` 的结构化条目全等匹配为准
    （host 为空的条目按未匹配处理），避免把慢速成功误判为失败。
    """
    busid = str(busid or "").strip()
    host = _normalized_usbip_host(source_host)
    if not host or not busid:
        return False
    try:
        stdout, _error, code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh, USBIP_PORT_COMMAND, timeout=10,
        )
    except Exception as exc:
        logger.debug(
            "usbip port verification unavailable for %s/%s: %s",
            source_host, busid, exc,
        )
        return False
    if code != 0:
        return False
    return any(
        entry.get("busid") == busid
        and _normalized_usbip_host(entry.get("host") or "") == host
        for entry in parse_usbip_port_entries(stdout or "")
    )


async def _detach_stale_local_usbip_pair(
    ssh, source_host: str, busid: str,
) -> bool:
    """Detach only an exact stale local ``(source_host, busid)`` vhci route.

    This is intentionally target-side only. ``usbipd detach`` on Windows
    resets the physical port and can make a fragile Loader/MaskROM
    re-enumeration fail with ``0000:0002``.  A local vhci route is considered
    stale only after Windows no longer reports the target BUSID.
    """
    host = _normalized_usbip_host(source_host)
    busid = str(busid or "").strip()
    if not host or not busid:
        return False
    try:
        stdout, _error, code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh, USBIP_PORT_COMMAND, timeout=10,
        )
        if code != 0:
            return False
        ports = [
            entry["port"]
            for entry in parse_usbip_port_entries(stdout or "")
            if entry.get("busid") == busid
            and _normalized_usbip_host(entry.get("host") or "") == host
        ]
        detached = False
        for port in ports:
            _out, _err, detach_code = await asyncio.to_thread(
                runtime.ssh_manager.execute_command,
                ssh, f"sudo -n usbip detach -p {shlex.quote(port)}", timeout=10,
            )
            detached = detach_code == 0 or detached
        return detached
    except Exception as exc:
        logger.debug(
            "stale usbip route cleanup unavailable for %s/%s: %s",
            source_host, busid, exc,
        )
        return False


async def _remote_usbip_exports_busid(
    ssh, source_host: str, busid: str,
) -> bool | None:
    """Return whether a successful remote list exports the exact BUSID.

    ``None`` means the probe itself was unavailable, in which case callers
    retain the direct attach fallback.  A successful list that omits the
    BUSID is authoritative and should not be followed by attach spam.
    """
    command = f"usbip list -r {shlex.quote(source_host)}"
    try:
        stdout, _error, code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh, command, timeout=4,
        )
    except Exception:
        return None
    if code != 0:
        return None
    token = re.compile(
        rf"^\s*-?\s*{re.escape(str(busid or '').strip())}\s*:",
        re.MULTILINE,
    )
    return bool(token.search(stdout or ""))


# attach 客户端超时或端口占用时，服务端可能实际已完成挂载；这类失败
# 才值得用 `usbip port` 二次核验。"设备尚未导出"等确定性失败直接重试。
_ATTACH_MAY_HAVE_SUCCEEDED_MARKERS = (
    "timed out",
    "timeout",
    "busy",
    "occupied",
    "already",
    "in use",
    "resource temporarily unavailable",
)


def _attach_may_have_succeeded(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(marker in lowered for marker in _ATTACH_MAY_HAVE_SUCCEEDED_MARKERS)


# MaskROM 新实例到达后 AutoBind 可能未生效。只有 Windows 状态明确为
# Not shared 时才按节流执行普通 `usbipd bind`；不依据 Linux attach 错误
# 猜测源端状态，更不在转换窗口内执行 Windows detach。
ROCKUSB_BIND_RETRY_SECONDS = 2.0
ROCKUSB_SOURCE_POLL_SECONDS = 0.25
# Two distinct ``usbipd state`` samples are enough to reject a transient row
# without spending a fixed two-second delay from upgrade_tool's short MaskROM
# window.  Cached reads do not count as additional samples.
ROCKUSB_STABLE_SAMPLE_COUNT = 2
ROCKUSB_ATTACH_RETRY_SECONDS = 1.0
ROCKUSB_ATTACH_VERIFY_SECONDS = 0.35
# 实机（usbipd-win 5.3.0-54 + Rockchip 351a）验证：刚完成重新枚举的 PnP
# 节点尚未可 claim；在节点到达/离开窗口内发起的 VBoxUsb claim 会崩溃并
# 把端口锁死为 0000:0002（Code 43），此后任何 bind/attach 都无法恢复。
# 身份稳定后仍需等待最短落定时间，才允许第一次 attach。DB 后的 DRAM
# Loader 空闲窗口约 45 秒，6 秒落定 + 2 次采样仍留有充足裕量。
ROCKUSB_ATTACH_SETTLE_SECONDS = 6.0
# watcher 截止后 MaskROM 二次枚举可能仍未完成（生产日志里 0000:0002
# 描述符失败行在超时后才出现）。快照轮询等待目标 BUSID 出现，避免单次
# 抓拍拍在枚举完成前、把枚举失败误报成通用 AutoBind/TCP 3240 提示。
ROCKUSB_SNAPSHOT_WAIT_SECONDS = 10.0
ROCKUSB_SNAPSHOT_INTERVAL_SECONDS = 1.0


async def _poll_usbipd_snapshot(
    ssh_win, busids: set[str], *, wait_seconds: float | None = None,
) -> tuple[str, str]:
    """Poll `usbipd list` until every target BUSID shows up or window ends."""
    wanted = {str(busid or "").strip() for busid in busids or ()}
    snapshot_wait = (
        ROCKUSB_SNAPSHOT_WAIT_SECONDS
        if wait_seconds is None else max(0.0, wait_seconds)
    )
    deadline = time.monotonic() + snapshot_wait
    listing, list_err = "", ""
    while True:
        listing, list_err = await asyncio.to_thread(usbipd_list_via_ssh, ssh_win)
        present = {
            row["busid"]
            for row in parse_usbipd_connected_rows(listing or "")
        }
        if wanted.issubset(present) or time.monotonic() >= deadline:
            return listing, list_err
        await asyncio.sleep(ROCKUSB_SNAPSHOT_INTERVAL_SECONDS)


async def prepare_usbip_firmware_routes(
    devices: list[str],
) -> tuple[list[dict], str]:
    usbip_devices = [
        device for device in devices
        if usbip_reconnect.usbip_source_host_for_device(device)
    ]
    if not usbip_devices:
        return [], ""
    routes = resolve_usbip_flash_routes(usbip_devices)
    routed = {
        str(device or "") for route in routes
        for device in route.get("device_ids") or []
    }
    unresolved = [device for device in usbip_devices if device not in routed]
    if unresolved:
        return [], (
            "USB/IP固件烧写缺少设备到物理BUSID的持久分配记录: "
            + ", ".join(unresolved)
            + "。请断开后从设备管理页重新选择该USB设备并连接。"
        )
    for route in routes:
        result = await asyncio.to_thread(
            ensure_usbip_auto_bind_policies,
            route["device_host"], route["busids"],
        )
        if not result.get("success"):
            return [], str(result.get("error") or "USB/IP AutoBind策略配置失败")
    return routes, ""


async def capture_rockusb_route_baseline(
    routes: list[dict],
) -> tuple[dict[tuple[str, str], dict[str, str]], str]:
    """Capture the attached Loader identity before ``upgrade_tool uf``.

    usbipd-win briefly reports the old Loader as ``Shared`` while tearing down
    the previous USB/IP client.  That row is not the new MaskROM device.  The
    watcher uses this baseline (PnP instance plus VID:PID) to avoid attaching
    the old row and cycling the Windows USB port in the middle of re-enumeration.
    """
    pairs_by_host: dict[str, list[tuple[str, str]]] = {}
    for route in routes:
        device_host = str(route.get("device_host") or "").strip()
        source_host = str(route.get("source_host") or "").strip()
        for raw_busid in route.get("busids") or []:
            busid = str(raw_busid or "").strip()
            if device_host and source_host and busid:
                pairs_by_host.setdefault(device_host, []).append(
                    (source_host, busid)
                )
    if not pairs_by_host:
        return {}, ""

    baseline: dict[tuple[str, str], dict[str, str]] = {}
    for device_host, pairs in pairs_by_host.items():
        ssh_win, ssh_error = await asyncio.to_thread(
            open_usbip_source_ssh, device_host,
        )
        if ssh_win is None:
            return {}, (
                f"无法在烧写前锁定 {device_host} 的 Loader USB 实例: "
                + (ssh_error or "Windows SSH不可用")
            )
        try:
            try:
                states = await asyncio.to_thread(
                    query_usbipd_device_states,
                    runtime.ssh_manager,
                    ssh_win,
                )
            except Exception as exc:
                logger.warning(
                    "Unable to capture structured usbipd state on %s: %s",
                    device_host, exc,
                )
                states = {}

            # usbipd-win >=2.2 exposes JSON state and all supported production
            # versions should take this path.  Retain the list fallback for an
            # actionable preflight error on older/misconfigured sources.
            if states:
                rows = list(states.values())
                instance_ids = {
                    busid: str(state.get("instance_id") or "")
                    for busid, state in states.items()
                }
            else:
                listing, list_error = await asyncio.to_thread(
                    usbipd_list_via_ssh, ssh_win,
                )
                if list_error:
                    return {}, (
                        f"无法在烧写前读取 {device_host} 的 usbipd state: "
                        + list_error
                    )
                rows = parse_usbipd_connected_rows(listing or "")
                try:
                    instance_ids = await asyncio.to_thread(
                        query_usbipd_busid_instance_ids,
                        runtime.ssh_manager,
                        ssh_win,
                    )
                except Exception:
                    instance_ids = {}
            for source_host, busid in pairs:
                row = next(
                    (item for item in rows if item.get("busid") == busid),
                    None,
                )
                if row is None:
                    return {}, (
                        f"烧写前 Windows 上未找到已分配的 Loader BUSID "
                        f"{device_host}/{busid}"
                    )
                vid_pid = str(row.get("vid_pid") or "").lower()
                device_label = str(row.get("device") or "")
                if (
                    vid_pid == "0000:0002"
                    or "descriptor request failed" in device_label.lower()
                ):
                    return {}, (
                        f"Windows 上目标 BUSID {busid} 的 USB 描述符读取失败"
                        "（0000:0002），请给设备断电重上电后重试。"
                    )
                if not vid_pid.startswith(f"{ROCKCHIP_USB_VENDOR_ID}:"):
                    return {}, (
                        f"烧写前 {device_host}/{busid} 不是 RockUSB Loader"
                        f"（当前 {vid_pid or '未知'}）"
                    )
                state = str(row.get("state") or "").lower()
                is_attached = row.get("is_attached")
                if not (
                    is_attached is True
                    or (is_attached is None and state.startswith("attached"))
                ):
                    return {}, (
                        f"烧写前 {device_host}/{busid} 的 Loader USB/IP 会话"
                        f"已不在 Attached 状态（当前 {row.get('state') or '未知'}），"
                        "请重新连接该设备后再试。"
                    )
                baseline[(source_host, busid)] = {
                    "instance_id": str(instance_ids.get(busid) or ""),
                    "vid_pid": vid_pid,
                }
        finally:
            with contextlib.suppress(Exception):
                ssh_win.close()
    return baseline, ""


async def reattach_usbip_after_rockusb_reset(
    ssh, routes: list[dict], *, timeout: float = 30, interval: float = 0.15,
    attach_command_timeout: float = 4,
    baseline: dict[tuple[str, str], dict[str, str]] | None = None,
    allow_identity_transition: bool = False,
    transition_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    force_bind: bool = False,
    require_forced: bool = False,
) -> dict:
    """Reattach Loader ports while upgrade_tool waits for MaskROM.

    ``upgrade_tool`` 自身只等待数秒；窗口需覆盖 Download Boot 下载耗时、
    Windows 端重新枚举并按 AutoBind 策略共享新实例、以及 Linux 端 attach
    握手。实测同一部署上 ADB→Loader 的 USB/IP 恢复最长约 30 秒，15 秒
    窗口会吞掉慢速成功；配合烧写请求内的自动重试（工具先超时、传输随后
    挂回时从 MaskROM 直接重跑），窗口取 30 秒。单次 attach 给足数秒
    （常规连接流程为 15s；被拒绝的请求会立即返回，超时上限只保护慢速
    成功路径，避免 1s 截断杀死即将成功的挂载）。

    watcher 以烧写前的 Loader PnP 实例/VID:PID 为基线，并以结构化
    ``usbipd state`` 为主状态源。Loader→MaskROM/DB 路径应传入
    ``transition_event``：watcher 会提前建立 Windows SSH，调用方在启动
    会触发重枚举的命令前解除等待；基线和稳定身份变化负责防止旧实例误挂。
    ``ready_event`` 会在这些源端 SSH 会话均已预建后置位，供调用方确保
    watcher 布防完成再启动工具。
    ARMED 后同一 BUSID 出现非 Attached 的合法 ``2207:*`` 即构成转换证据，
    不再要求轮询必须碰巧采到 BUSID 消失。合法状态连续出现两个独立样本、
    且身份稳定满 ``ROCKUSB_ATTACH_SETTLE_SECONDS`` 后才允许 attach：结构
    化状态行出现远早于 PnP 节点真正可 claim，过早的 VBoxUsb claim 会崩溃
    并把端口锁死为 0000:0002。结构化模式下目标行缺失同样按物理缺席处理
    （旧实现仅识别人类可读 list 的缺席，会对已消失的 BUSID 连发 claim）。
    未传事件的 ADB→Loader 路径仍以物理缺席为强证据，并可通过
    ``allow_identity_transition=True`` 接受稳定的 PnP/VID 变化。绝不自动
    执行 Windows ``usbipd detach``。
    窗口耗尽仍失败则轮询抓取 ``usbipd list`` 快照（等待目标 BUSID 完成
    二次枚举，最长 10 秒）并附 ``usbipd policy list`` 用于定位。即使
    attach 晚于工具退出才完成，设备也会留在 MaskROM 传输上，直接重试
    烧写即可通过 Loader 预检。
    """
    pending = {
        (str(route.get("source_host") or "").strip(), str(busid).strip())
        for route in routes for busid in route.get("busids") or []
        if str(route.get("source_host") or "").strip() and str(busid).strip()
    }
    if not pending:
        return {"success": True, "attached": []}
    route_by_pair = {
        (str(route.get("source_host") or "").strip(), str(busid).strip()): route
        for route in routes for busid in route.get("busids") or []
    }
    attached, errors = [], {}
    attempts = 0
    baseline_by_pair = {
        (str(pair[0]), str(pair[1])): {
            "instance_id": str((identity or {}).get("instance_id") or ""),
            "vid_pid": str((identity or {}).get("vid_pid") or "").lower(),
        }
        for pair, identity in (baseline or {}).items()
        if isinstance(pair, tuple) and len(pair) == 2
    }
    windows_ssh: dict[str, object] = {}
    windows_ssh_errors: dict[str, str] = {}
    last_bind_at: dict[tuple[str, str], float] = {}
    last_attach_at: dict[tuple[str, str], float] = {}
    cleaned_local_pairs: set[tuple[str, str]] = set()
    absence_seen: set[tuple[str, str]] = set()
    transition_seen: set[tuple[str, str]] = set()
    stable_fingerprints: dict[tuple[str, str], tuple[str, str, str]] = {}
    stable_samples: dict[tuple[str, str], int] = {}
    stable_observed_at: dict[tuple[str, str], float] = {}
    stable_since_at: dict[tuple[str, str], float] = {}

    def _reset_stability(pair: tuple[str, str]) -> None:
        """Drop stable-sample progress so the settle dwell re-applies."""
        stable_fingerprints.pop(pair, None)
        stable_samples.pop(pair, None)
        stable_observed_at.pop(pair, None)
        stable_since_at.pop(pair, None)
    windows_state_cache: dict[
        str, tuple[float, list[dict[str, object]], str, str, dict[str, str]]
    ] = {}
    started = time.monotonic()
    # Event-driven watchers spend their timeout budget only after the caller
    # arms the transition event immediately before the reset-causing command.
    deadline = (
        None if transition_event is not None
        else started + max(0.5, timeout)
    )

    windows_ssh_locks: dict[str, asyncio.Lock] = {}

    async def _windows_session(device_host: str):
        if device_host in windows_ssh:
            return windows_ssh[device_host], windows_ssh_errors.get(device_host, "")
        # 并行尝试的多个 BUSID 可能同时首次命中同一台 Windows 主机；
        # 用锁避免重复建连。
        lock = windows_ssh_locks.setdefault(device_host, asyncio.Lock())
        async with lock:
            if device_host in windows_ssh:
                return windows_ssh[device_host], windows_ssh_errors.get(device_host, "")
            ssh_win, ssh_err = await asyncio.to_thread(
                open_usbip_source_ssh, device_host,
            )
            windows_ssh[device_host] = ssh_win
            if ssh_win is None:
                windows_ssh_errors[device_host] = ssh_err
            return ssh_win, ssh_err

    async def _windows_rows(device_host: str):
        """Poll one Windows source at most four times per second.

        Returns the monotonic timestamp of the real source probe as the last
        value, so repeated cache hits cannot masquerade as stable samples.
        """
        ssh_win, ssh_err = await _windows_session(device_host)
        if ssh_win is None:
            return [], "", ssh_err, {}, 0.0
        lock = windows_ssh_locks.setdefault(device_host, asyncio.Lock())
        async with lock:
            cached = windows_state_cache.get(device_host)
            now = time.monotonic()
            if cached and now - cached[0] < ROCKUSB_SOURCE_POLL_SECONDS:
                return cached[1], cached[2], cached[3], cached[4], cached[0]
            try:
                states = await asyncio.to_thread(
                    query_usbipd_device_states,
                    runtime.ssh_manager,
                    ssh_win,
                )
            except Exception as exc:
                logger.debug(
                    "usbipd state probe unavailable on %s: %s",
                    device_host, exc,
                )
                states = {}
            if states:
                rows = list(states.values())
                instance_ids = {
                    busid: str(state.get("instance_id") or "")
                    for busid, state in states.items()
                }
                listing, list_err = "", ""
            else:
                # Diagnostic compatibility fallback only.  Production
                # automation uses the JSON branch above.
                listing, list_err = await asyncio.to_thread(
                    usbipd_list_via_ssh, ssh_win,
                )
                rows = parse_usbipd_connected_rows(listing or "")
                instance_ids = {}
                if any(
                    identity.get("instance_id")
                    for identity in baseline_by_pair.values()
                ):
                    try:
                        instance_ids = await asyncio.to_thread(
                            query_usbipd_busid_instance_ids,
                            runtime.ssh_manager,
                            ssh_win,
                        )
                    except Exception as exc:
                        logger.debug(
                            "usbipd instance probe unavailable on %s: %s",
                            device_host, exc,
                        )
            windows_state_cache[device_host] = (
                now, rows, listing or "", list_err or "", instance_ids,
            )
            return (
                rows, listing or "", list_err or "", instance_ids, now,
            )

    async def _bind_instance(
        source_host: str, busid: str, device_host: str, *, force: bool = False,
    ) -> bool:
        key = (source_host, busid)
        if (
            time.monotonic() - last_bind_at.get(key, 0.0)
            < ROCKUSB_BIND_RETRY_SECONDS
        ):
            return False
        try:
            ssh_win, ssh_err = await _windows_session(device_host)
            if ssh_win is None:
                detail = f"Windows SSH失败({ssh_err})" if ssh_err else "Windows SSH失败"
                errors[f"{source_host}/{busid}"] = detail
                return False
            lock = windows_ssh_locks.setdefault(device_host, asyncio.Lock())
            async with lock:
                last_bind_at[key] = time.monotonic()
                bind_result = await asyncio.to_thread(
                    bind_usbip_busid_via_ssh, ssh_win, busid,
                    force=force,
                )
                logger.info(
                    "RockUSB USB/IP source %sbind for %s/%s: %s",
                    "forced " if force else "", source_host, busid, bind_result,
                )
                # bind changes the state table; force a fresh list next time.
                windows_state_cache.pop(device_host, None)
            if not bind_result.get("success"):
                errors[f"{source_host}/{busid}"] = (
                    ("Windows目标实例forced bind失败: " if force else
                     "Windows目标实例为Not shared，普通bind失败: ")
                    + str(bind_result.get("detail") or bind_result.get("error") or "未知错误")
                )
                return False
            return True
        except Exception as exc:
            logger.warning(
                "RockUSB USB/IP source bind failed for %s/%s: %s",
                source_host, busid, exc,
            )
            errors[f"{source_host}/{busid}"] = f"Windows bind异常: {exc}"
            return False

    async def _attempt_pair(source_host: str, busid: str) -> None:
        nonlocal attempts
        key = (source_host, busid)
        bound_now = False
        device_host = str(
            (route_by_pair.get(key) or {}).get("device_host") or ""
        ).strip()

        if device_host:
            (
                rows,
                listing,
                list_err,
                instance_ids,
                observed_at,
            ) = await _windows_rows(
                device_host
            )
            target_row = next(
                (row for row in rows if row.get("busid") == busid), None,
            )
            # 结构化 ``usbipd state`` 模式下 ``listing`` 恒为空：目标行缺失
            # 即物理 BUSID 尚未重新枚举（或已离开），绝不能落入下方的
            # attach 分支——对着消失的 BUSID 连发 claim 会在 Windows 枚举
            # 窗口内触发 VBoxUsb 崩溃并把端口锁死为 0000:0002。
            if target_row is None and (listing or rows):
                absence_seen.add(key)
                _reset_stability(key)
                if (
                    key not in cleaned_local_pairs
                    and (
                        transition_event is not None
                        or key in absence_seen
                    )
                ):
                    cleaned_local_pairs.add(key)
                    await _detach_stale_local_usbip_pair(
                        ssh, source_host, busid,
                    )
                errors[f"{source_host}/{busid}"] = (
                    "Windows目标BUSID尚未重新枚举"
                )
                return
            if target_row is not None:
                vid_pid = str(target_row.get("vid_pid") or "").lower()
                device_label = str(target_row.get("device") or "")
                state = str(target_row.get("state") or "").lower()
                is_forced = bool(target_row.get("is_forced"))
                if (
                    vid_pid == "0000:0002"
                    or "descriptor request failed" in device_label.lower()
                ):
                    transition_seen.add(key)
                    errors[f"{source_host}/{busid}"] = (
                        "Windows USB descriptor enumeration failed (0000:0002)"
                    )
                    return
                current_instance = str(instance_ids.get(busid) or "")
                initial = baseline_by_pair.get(key)
                if initial is None:
                    # Direct callers that did not supply a pre-burn baseline
                    # are armed from the first observed row.  Never infer a
                    # transition merely from Attached -> Shared.
                    baseline_by_pair[key] = {
                        "instance_id": current_instance,
                        "vid_pid": vid_pid,
                    }
                    errors[f"{source_host}/{busid}"] = (
                        "已锁定Loader基线，等待MaskROM重新枚举"
                    )
                    return
                initial_instance = str(initial.get("instance_id") or "")
                initial_vid = str(initial.get("vid_pid") or "")
                identity_changed = bool(
                    (current_instance and current_instance != initial_instance)
                    or (vid_pid and vid_pid != initial_vid)
                )
                if transition_event is not None:
                    # The caller's Download Boot phase marker is the
                    # authoritative phase edge.
                    # The old Loader may still be Attached for a few samples;
                    # wait until the transport is released (or a genuinely new
                    # identity is already attached) before accepting the row.
                    if state.startswith("attached") and not identity_changed:
                        errors[f"{source_host}/{busid}"] = (
                            "Download Boot已完成，等待旧Loader传输释放"
                        )
                        return
                elif key not in absence_seen:
                    # 缺席是最强证据；ADB→Loader 的行替换可能发生在两次
                    # 轮询之间（2207:0006→2207:351a 原子换行），此时以
                    # PnP 实例/VID 相对基线的变化 + 稳定窗口作为转换判据。
                    # 同实例同 VID 的旧行残留（拆除期 Shared）不构成转换。
                    if not allow_identity_transition or not identity_changed:
                        errors[f"{source_host}/{busid}"] = (
                            "等待Loader物理BUSID消失或PnP身份变化"
                        )
                        return
                transition_seen.add(key)
                if state.startswith("attached"):
                    if await _usbip_pair_already_attached(
                        ssh, source_host, busid,
                    ):
                        pending.remove(key)
                        attached.append({
                            "source_host": source_host, "busid": busid,
                        })
                        errors.pop(f"{source_host}/{busid}", None)
                    else:
                        errors[f"{source_host}/{busid}"] = (
                            "Windows目标BUSID仍被旧客户端占用(Attached)"
                        )
                    return
                if require_forced and not is_forced:
                    _reset_stability(key)
                    errors[f"{source_host}/{busid}"] = (
                        "目标RockUSB实例尚未预绑定为Shared (forced)；"
                        "为避免在重枚举窗口切换Windows驱动，本次不会执行bind/attach"
                    )
                    return
                if (
                    key not in cleaned_local_pairs
                    and (
                        transition_event is not None
                        or key in absence_seen
                    )
                ):
                    # Source-side transport is no longer Attached, so any
                    # exact local vhci pair can only represent the old USB
                    # instance.  Clean that pair before attaching the new one.
                    cleaned_local_pairs.add(key)
                    await _detach_stale_local_usbip_pair(
                        ssh, source_host, busid,
                    )
                fingerprint = (
                    current_instance.casefold(),
                    vid_pid,
                    state,
                )
                if stable_fingerprints.get(key) != fingerprint:
                    stable_fingerprints[key] = fingerprint
                    stable_samples[key] = 1
                    stable_observed_at[key] = observed_at
                    stable_since_at[key] = time.monotonic()
                    errors[f"{source_host}/{busid}"] = (
                        "MaskROM已出现，等待Windows PnP枚举稳定"
                    )
                    return
                if observed_at != stable_observed_at.get(key):
                    stable_observed_at[key] = observed_at
                    stable_samples[key] = stable_samples.get(key, 1) + 1
                if stable_samples.get(key, 0) < ROCKUSB_STABLE_SAMPLE_COUNT:
                    errors[f"{source_host}/{busid}"] = (
                        "MaskROM已出现，等待Windows PnP枚举稳定"
                    )
                    return
                settle_waited = (
                    time.monotonic() - stable_since_at.get(key, 0.0)
                )
                if settle_waited < ROCKUSB_ATTACH_SETTLE_SECONDS:
                    # 身份稳定 ≠ PnP/驱动落定：立即 claim 会撞上仍在安装
                    # 驱动的节点（VBoxUsb could-not-claim/433），并可能把
                    # 端口打进描述符失败状态。
                    errors[f"{source_host}/{busid}"] = (
                        "新USB实例已稳定，等待PnP落定"
                        f"（{settle_waited:.1f}s/"
                        f"{ROCKUSB_ATTACH_SETTLE_SECONDS:.0f}s）"
                    )
                    return
                needs_force_bind = force_bind and not is_forced
                needs_normal_bind = not force_bind and "not shared" in state
                if needs_force_bind or needs_normal_bind:
                    if not await _bind_instance(
                        source_host, busid, device_host,
                        force=needs_force_bind,
                    ):
                        return
                    # A forced bind deliberately switches the Windows driver;
                    # even a normal bind changes the source state table. Never
                    # attach from the pre-bind sample. Re-observe the instance
                    # and apply the complete stability dwell again.
                    _reset_stability(key)
                    errors[f"{source_host}/{busid}"] = (
                        "forced bind已完成，等待USB实例重新稳定"
                        if needs_force_bind else
                        "普通bind已完成，等待USB实例重新稳定"
                    )
                    return
            elif list_err:
                errors[f"{source_host}/{busid}"] = (
                    f"Windows usbipd list不可用: {list_err}"
                )
                # 未观察到 reset 前不能通过 Linux 侧旧 vhci 猜测转换完成；
                # 等下一轮 Windows 状态探测，避免把 Loader 旧会话误报成功。
                return

        if (
            time.monotonic() - last_attach_at.get(key, 0.0)
            < ROCKUSB_ATTACH_RETRY_SECONDS
        ):
            return
        # When structured `usbipd state` is available, Shared is the
        # authoritative source-side export state; `usbip list -r` may be
        # unavailable on usbipd-win even though attach is valid. Keep the
        # discovery guard for the legacy human-readable fallback.
        if device_host and listing and not bound_now:
            exported = await _remote_usbip_exports_busid(
                ssh, source_host, busid,
            )
            if exported is False:
                errors[f"{source_host}/{busid}"] = "Windows尚未导出目标BUSID"
                return

        command = (
            "sudo usbip attach -r "
            f"{shlex.quote(source_host)} -b {shlex.quote(busid)}"
        )
        last_attach_at[key] = time.monotonic()
        attempts += 1
        try:
            output, error, code = await asyncio.to_thread(
                runtime.ssh_manager.execute_command,
                ssh, command, timeout=attach_command_timeout,
            )
        except Exception as exc:
            # execute_command 超时通常以异常形态抛出，而服务端 attach
            # 仍可能已完成；先核验再判定失败。
            error_text = str(exc)
            if (
                (not device_host or key in transition_seen)
                and _attach_may_have_succeeded(error_text)
                and await _usbip_pair_already_attached(
                    ssh, source_host, busid,
                )
            ):
                pending.remove((source_host, busid))
                attached.append({"source_host": source_host, "busid": busid})
                errors.pop(f"{source_host}/{busid}", None)
                logger.info(
                    "RockUSB USB/IP reattach verified via `usbip port` "
                    "for %s/%s after %s attempt(s)",
                    source_host, busid, attempts,
                )
            else:
                errors[f"{source_host}/{busid}"] = error_text
            return
        if code == 0 and (not device_host or key in transition_seen):
            if device_host:
                await asyncio.sleep(ROCKUSB_ATTACH_VERIFY_SECONDS)
                if not await _usbip_pair_already_attached(
                    ssh, source_host, busid,
                ):
                    errors[f"{source_host}/{busid}"] = (
                        "USB/IP attach返回成功，但vhci会话未稳定"
                    )
                    # attach 成功即节点随后消失：目标正在离开总线，
                    # 同样需要重新等待落定后再试。
                    _reset_stability(key)
                    return
            pending.remove((source_host, busid))
            attached.append({"source_host": source_host, "busid": busid})
            errors.pop(f"{source_host}/{busid}", None)
            logger.info(
                "RockUSB USB/IP reattach succeeded for %s/%s "
                "after %s attempt(s)",
                source_host, busid, attempts,
            )
            return
        failure_text = (
            error or output or f"usbip attach exited with code {code}"
        ).strip()
        # 常见于"端口已被占用"：上一次超时的 attach 实际已挂载成功。
        if (
            (not device_host or key in transition_seen)
            and _attach_may_have_succeeded(failure_text)
            and await _usbip_pair_already_attached(
                ssh, source_host, busid,
            )
        ):
            pending.remove((source_host, busid))
            attached.append({"source_host": source_host, "busid": busid})
            errors.pop(f"{source_host}/{busid}", None)
            logger.info(
                "RockUSB USB/IP reattach verified via `usbip port` "
                "for %s/%s after %s attempt(s)",
                source_host, busid, attempts,
            )
        else:
            errors[f"{source_host}/{busid}"] = failure_text
            lowered_failure = failure_text.lower()
            if any(
                marker in lowered_failure
                for marker in (
                    "device not found",
                    "error state",
                    "could not claim",
                    "file not found",
                    "no such device",
                )
            ):
                # claim 撞上了过渡态 PnP 节点（正在到达或离开）。重置稳定
                # 跟踪，让落定等待重新生效；按秒级间隔继续轰击只会继续
                # 触发 VBoxUsb 崩溃并锁死端口（0000:0002）。
                _reset_stability(key)

    try:
        if transition_event is not None:
            device_hosts = sorted({
                str(route.get("device_host") or "").strip()
                for route in routes
                if str(route.get("device_host") or "").strip()
            })
            # Pre-open source sessions without touching the USB device.  This
            # removes SSH setup latency from the post-Download-Boot window.
            await asyncio.gather(
                *(_windows_session(device_host) for device_host in device_hosts)
            )
            if ready_event is not None:
                ready_event.set()
            for source_host, busid in pending:
                errors[f"{source_host}/{busid}"] = (
                    "watcher已就绪，等待Download Boot阶段标记"
                )
            await transition_event.wait()
            deadline = time.monotonic() + max(0.5, timeout)

        while (
            pending
            and deadline is not None
            and time.monotonic() < deadline
        ):
            # 多 BUSID 并行尝试：串行时单条 attach 最坏占满 4 秒超时，
            # 多设备场景第一轮就会耗尽 MaskROM 等待窗口。
            await asyncio.gather(
                *(_attempt_pair(source_host, busid)
                  for source_host, busid in list(pending))
            )
            if pending:
                await asyncio.sleep(max(0.05, interval))
    finally:
        for device_host, ssh_win in list(windows_ssh.items()):
            if ssh_win is not None:
                with contextlib.suppress(Exception):
                    ssh_win.close()
            windows_ssh.pop(device_host, None)

    source_list = ""
    if pending:
        pending_by_host: dict[str, set[str]] = {}
        for pair in sorted(pending):
            device_host = str(
                (route_by_pair.get(pair) or {}).get("device_host") or ""
            ).strip()
            if device_host:
                pending_by_host.setdefault(device_host, set()).add(pair[1])
        snapshot_parts = []
        for device_host, host_busids in pending_by_host.items():
            ssh_win, ssh_err = await _windows_session(device_host)
            if ssh_win is None:
                snapshot_parts.append(
                    f"[{device_host}]\n{ssh_err or 'Windows SSH不可用'}"
                )
                continue
            try:
                listing, list_err = await _poll_usbipd_snapshot(
                    ssh_win,
                    host_busids,
                    # Event-driven callers have already spent their complete
                    # post-transition budget.  Take one immediate diagnostic
                    # snapshot so `upgrade_tool ld` gets the next 15 seconds.
                    wait_seconds=(
                        0 if transition_event is not None else None
                    ),
                )
                snapshot_parts.append(f"[{device_host}]\n{listing or list_err}")
                # AutoBind 规则命中情况一并入快照：一次失败日志就能判断
                # 是"实例未出现"、"未共享"还是"策略缺失"。诊断自身失败
                # 不能影响快照返回。
                try:
                    policy = await asyncio.to_thread(
                        usbipd_policy_list_via_ssh, ssh_win,
                    )
                except Exception as exc:
                    policy = f"usbipd policy list 不可用: {exc}"
                if policy:
                    snapshot_parts.append(f"[{device_host} policy]\n{policy}")
            finally:
                ssh_win.close()
        source_list = "\n".join(snapshot_parts)
    result = {
        "success": not pending,
        "attached": attached,
        "pending": [
            {"source_host": host, "busid": busid}
            for host, busid in sorted(pending)
        ],
        "errors": errors,
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "source_list": source_list,
    }
    if pending:
        logger.warning(
            "RockUSB USB/IP reattach unfinished after %ss (%s attempt(s)): "
            "pending=%s errors=%s usbipd现场=%s",
            result["elapsed_seconds"], attempts, result["pending"], errors,
            source_list or "不可用",
        )
    return result


def device_flash_protocols(ssh, devices: list[str]) -> dict[str, str]:
    adb_output, _error, _code = runtime.ssh_manager.execute_command(
        ssh, "adb devices", timeout=8
    )
    adb_states = parse_adb_device_states(adb_output)
    fastboot_output, fastboot_error, _code = runtime.ssh_manager.execute_command(
        ssh, "fastboot devices", timeout=8
    )
    fastboot_devices = set(
        DeviceUtils.parse_fastboot_devices(fastboot_output or fastboot_error)
    )
    return {
        serial: (
            "adb" if adb_states.get(serial) == "device"
            else "fastboot" if serial in fastboot_devices else ""
        )
        for serial in devices
    }


def accept_direct_rockusb_loaders(
    protocols: dict[str, str], usbip_routes: list[dict], loader_output: str,
) -> dict[str, str]:
    updated = dict(protocols)
    unavailable = [device for device, protocol in updated.items() if not protocol]
    routed = {
        str(device or "") for route in usbip_routes
        for device in route.get("device_ids") or []
    }
    match = ROCKUSB_LOADER_COUNT_RE.search(loader_output or "")
    if (unavailable and set(unavailable).issubset(routed)
            and match and int(match.group(1)) >= len(unavailable)):
        updated.update({device: "rockusb-loader" for device in unavailable})
    return updated


def partition_devices_by_flash_state(
    ssh, devices: list[str],
) -> tuple[list[str], list[str]]:
    protocols = device_flash_protocols(ssh, devices)
    ready = [serial for serial in devices if protocols.get(serial)]
    return ready, [serial for serial in devices if not protocols.get(serial)]


async def notify_skipped_devices(client_id: str, offline: list[str]) -> None:
    if not offline or client_id not in runtime.global_state.websocket_connections:
        return
    with contextlib.suppress(Exception):
        await runtime.safe_websocket_send(client_id, {
            "type": "log_update",
            "log": (
                "跳过不可烧写设备（未在 ADB/Fastboot 中或状态异常）: "
                + ", ".join(offline)
            ),
            "log_type": "warning",
        })
