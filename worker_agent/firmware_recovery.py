"""Post-flash boot wait and AOA snapshot validation, without retrying input."""
from __future__ import annotations

import hashlib
import json
import time

from foundation.aoa_calibration import mapping_options
from foundation.aoa_hid_mode import profile_hid_options
from foundation.aoa_profile import parse_profile_commands
from foundation.aoa_recovery_policy import firmware_compatibility, require_firmware_digest, require_verified_recovery
from foundation.aoa_recovery_status import recovery_failure_reason


def recovery_options(snapshot: dict) -> dict:
    commands = snapshot.get("commands") or []
    digest = hashlib.sha256(json.dumps(
        commands, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if digest != snapshot.get("commands_sha256"):
        raise ValueError("AOA recovery snapshot changed; inspect before retrying")
    parse_profile_commands(commands)
    if not snapshot.get("usb_port_path"):
        raise ValueError("AOA recovery has no frozen physical USB port")
    wait_adb_seconds = int(snapshot["wait_adb_seconds"])
    if not 0 <= wait_adb_seconds <= 1800:
        # 与 boot_wait_seconds 同款上界：可信快照之外的数据不得把
        # 命令线程卡在 _wait_adb_state 任意久。
        raise ValueError("AOA recovery wait_adb_seconds out of range")
    hid_options = profile_hid_options(snapshot.get("metadata"))
    mappings = snapshot.get("mapping_options") or {
        "aoa_touch_mapping": snapshot.get("touch_mapping"),
        "aoa_source_touch_mapping": ((snapshot.get("metadata") or {}).get("calibration") or {}).get("mapping"),
    }
    checked_mappings = mapping_options({**hid_options, **mappings})
    require_verified_recovery(commands, checked_mappings, snapshot.get("metadata") or {})
    if snapshot.get("firmware_compatibility") != firmware_compatibility(snapshot.get("metadata") or {}, commands):
        raise ValueError("AOA firmware compatibility snapshot changed")
    return {"aoa_commands": commands, "usb_port_path": snapshot["usb_port_path"],
            **hid_options, **checked_mappings,
            "wait_adb_seconds": wait_adb_seconds,
            "aoa_metadata": snapshot.get("metadata") or {},
            "post_flash_recovery": snapshot}


# 放行集合（_PASSABLE_STATES）：判据是「Android 已启动且物理在位」，
# 与 adbd 健康度解耦——usb_only（user 固件无 adb）、unresponsive（adbd
# 半死）、offline 均放行回放；available/device 为常规就绪态，unauthorized 不阻塞。
# adb_proxy 传输另行排除；唯一拦截是幽灵条目（端口不符，循环内显式抛错）。
_PASSABLE_STATES = {"available", "device", "usb_only", "unauthorized",
                    "unresponsive", "offline"}
# 等待 Android 启动的目的：确认系统已起来、稳定在可预期的界面，随后回放
# （pyusb accessory 通道，不依赖 adbd）——user 固件 USB 调试关闭时本就
# 没有 adbd（usb_only），adbd 半死（unresponsive）或 offline 都不阻止
# 回放。真正的陷阱只有一种：adb server 里挂着同 serial 的旧缓存条目，
# 但设备不在冻结的物理端口上——回放会打到别的设备/端口，必须拦截。
# adbd 半死（unresponsive）或 offline 都不阻塞回放，但半死时仍做有界
# ``adb reconnect`` 踢腿：趁 settle 窗口（默认 60s）内首踢，间隔 45s，
# 最多 3 次——踢活则终验一次通过，踢不活不影响回放继续。
_KICK_AFTER_SECONDS = 30
_KICK_INTERVAL_SECONDS = 45
_KICK_MAX = 3
_PROGRESS_INTERVAL_SECONDS = 30


def _boot_blocker_text(device: dict, state: str | None, port: str) -> str:
    if not device:
        return f"尚未在原 USB 端口 {port} 上检测到设备"
    if state == "unresponsive":
        return f"设备已在原端口 {port}（adbd 握手成功但 I/O 未就绪）"
    if state == "offline":
        return "设备 adb 离线（offline）"
    if state == "fastboot":
        return "设备停留在 bootloader/fastboot（非 Android 就绪态）"
    return f"设备状态为 {state or '未知'}"


def _boot_timeout_error(device_present: bool, state: str | None, port: str,
                        waited: int, kicked: int) -> str:
    if not device_present:
        return "固件已烧写，但未等到原 USB 端口的 Android 设备；请检查设备后单独回放 AOA"
    if state == "unresponsive":
        detail = (f"固件已烧写，设备已回到原 USB 端口 {port}，但 Android 启动等待 {waited} 秒"
                  "仍未就绪：adbd 握手成功但 I/O 一直关闭（unresponsive）；"
                  "请给设备断电重启，adb 恢复后再单独回放 AOA")
        if kicked:
            detail += f"（等待期间已自动尝试 {kicked} 次 adb reconnect）"
        return detail
    return (f"固件已烧写，设备已回到原 USB 端口 {port}，但 Android 未在 {waited} 秒内进入就绪状态"
            f"（最后状态：{state or '未知'}）；请检查设备后单独回放 AOA")


def wait_for_boot(snapshot: dict, device_ids: list[str], validate, probe, on_event=None,
                  *, kick=None) -> None:
    """Require the original serial and port; loader/fastboot is not Android.

    ``kick(serial)`` 可选注入：adbd 半死（unresponsive）时重建 adb 传输的
    非破坏性尝试。Worker 侧传 ``_adb_reconnect``；抑制 adb daemon 的会话
    内必须传 None（reconnect 会拉起被压制的 server）。就绪判定不变。
    """
    recovery_options(snapshot)
    if not callable(validate) or len(device_ids) != 1:
        raise RuntimeError("firmware AOA recovery requires one fenced device")
    delay = int(snapshot["boot_wait_seconds"])
    if not 0 <= delay <= 600:
        raise ValueError("invalid AOA boot wait")
    serial = str(device_ids[0]).split(":", 1)[-1]
    port = snapshot["usb_port_path"]
    if on_event:
        on_event({"phase": "waiting_boot", "index": 0, "total": len(snapshot["commands"]),
                  "command": f"固件已烧写，等待 Android 启动（至少 {delay} 秒）"})
    ready_since = None
    halfdead_since = None
    kick_count = 0
    last_kick_at = None
    timeout = 180 + delay
    deadline = time.monotonic() + timeout
    started = deadline - timeout
    # 事件模型：状态迁移（出现/消失/半死/离线…）即时上报；状态无变化时
    # 每 30s 一次心跳。初始锚定 absent（烧写后设备必然缺席）——进入时
    # 不打"已等待 0 秒"噪音；心跳锚点从 started 起算，纯等待也有周期可见性。
    last_situation = "absent"
    last_progress_at = started
    while True:
        validate()
        now = time.monotonic()
        attached = {item["serial"]: item for item in probe()}
        entry = attached.get(serial) or {}
        usb_port = (entry.get("properties") or {}).get("usb")
        if entry and usb_port != port:
            raise RuntimeError("physical USB identity changed after flash; reconcile required")
        device = entry if usb_port == port else {}
        state = device.get("state")
        ready = (device.get("transport") != "adb_proxy"
                 and state in _PASSABLE_STATES)
        if state == "unresponsive":
            if halfdead_since is None:
                halfdead_since = now
        else:
            halfdead_since = None
        # 半死态不阻塞回放，但趁 settle 窗口先踢 adbd：踢活则终验一次通过。
        # 必须排在 settle 返回之前，否则 60s settle 先到、踢腿永远轮空。
        if (callable(kick) and state == "unresponsive" and kick_count < _KICK_MAX
                and now - halfdead_since >= _KICK_AFTER_SECONDS
                and (last_kick_at is None or now - last_kick_at >= _KICK_INTERVAL_SECONDS)):
            kick_ok = bool(kick(serial))
            kick_count += 1
            last_kick_at = now
            if on_event:
                text = (f"adbd I/O 未就绪，已自动尝试 adb reconnect"
                        f"（第 {kick_count}/{_KICK_MAX} 次）")
                if not kick_ok:
                    text += "（adb 命令下发失败，adb server 可能不可用）"
                on_event({"phase": "waiting_boot", "index": 0, "total": len(snapshot["commands"]),
                          "command": text})
                last_progress_at = now
        if ready:
            if ready_since is None:
                ready_since = now
            if now - ready_since >= delay:
                return
        else:
            ready_since = None
        if now >= deadline:
            raise RuntimeError(_boot_timeout_error(bool(device), state, port,
                                                   int(now - started), kick_count))
        if on_event:
            situation = state or "absent"
            elapsed = int(now - started)
            if situation == "absent":
                text = (f"设备从原端口 {port} 消失（重枚举中），继续等待"
                        if last_situation != "absent" else
                        f"已等待 {elapsed} 秒，设备尚未回到原 USB 端口 {port}")
            elif ready:
                text = f"Android 已就绪，保持 {int(now - ready_since)}/{delay} 秒后开始回放"
                if state == "unresponsive":
                    text = (f"Android 已启动（adbd 半死，回放不依赖 adbd），"
                            f"保持 {int(now - ready_since)}/{delay} 秒后开始回放")
            else:
                text = (f"已等待 {elapsed} 秒仍未就绪："
                        f"{_boot_blocker_text(device, state, port)}")
            if situation != last_situation:
                last_situation = situation
                last_progress_at = now
                on_event({"phase": "waiting_boot", "index": 0,
                          "total": len(snapshot["commands"]), "command": text})
            elif now - last_progress_at >= _PROGRESS_INTERVAL_SECONDS:
                last_progress_at = now
                on_event({"phase": "waiting_boot", "index": 0,
                          "total": len(snapshot["commands"]), "command": text})
        time.sleep(1)


def validate_preflash(snapshot: dict, devices: list[str], validate, probe, *, firmware_sha256="") -> None:
    recovery_options(snapshot)
    require_firmware_digest(snapshot.get("firmware_compatibility"), firmware_sha256)
    validate()
    if len(devices) != 1:
        raise ValueError("firmware AOA recovery requires exactly one device")
    serial = str(devices[0]).split(":", 1)[-1]
    device = next((item for item in probe() if item["serial"] == serial), {})
    if (not device or device.get("transport") == "adb_proxy"
            or (device.get("properties") or {}).get("usb") != snapshot["usb_port_path"]):
        raise ValueError("physical USB identity changed before flash")


def recover_after_flash(flash_result: dict, snapshot: dict, devices: list[str],
                       validate, execute, on_event=None, *, suppress_adb_daemon=False,
                       suppress_adb_daemon_gate=None) -> dict:
    if not flash_result.get("success"):
        return flash_result
    recovered = {}
    try:
        options = {**recovery_options(snapshot), "_validate_fencing": validate,
                   "suppress_adb_daemon": suppress_adb_daemon,
                   "suppress_adb_daemon_gate": suppress_adb_daemon_gate}
        recovered = execute("aoa_sequence", devices, options, progress_event_callback=on_event)
        if recovered.get("success") is not True or recovered.get("adb_recovered") is not True:
            raise RuntimeError(recovery_failure_reason(recovered))
        return {**flash_result, "flash_success": True, "stage": "ADB_READY", "aoa_recovery": recovered}
    except Exception as exc:
        replayed = bool(recovered.get("replay_completed"))
        stage = "REPLAY_DONE_ADB_UNAVAILABLE" if replayed else "AOA_RECOVERY_FAILED"
        if replayed:
            error = (f"固件烧写与 AOA 回放均已完成，但 ADB 恢复未确认：{exc}。"
                     "输入已生效，无需重新烧写或回放；请重启设备或人工确认 USB 调试后重新验证 ADB")
        else:
            error = (f"固件烧写已完成，AOA 恢复失败：{exc}。"
                     "请检查设备并单独调试 AOA，无需重新烧写")
        return {**flash_result, "success": False, "flash_success": True,
                "stage": stage, "replay_completed": replayed, "reconcile_required": True,
                "aoa_recovery": getattr(exc, "aoa_result", None) or recovered,
                "error": error}
