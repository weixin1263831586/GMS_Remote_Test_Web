"""Post-flash boot wait and AOA snapshot validation, without retrying input."""
from __future__ import annotations

import hashlib
import json
import time

from foundation.aoa_calibration import mapping_options
from foundation.aoa_hid_mode import profile_hid_options
from foundation.aoa_profile import parse_profile_commands


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
    return {"aoa_commands": commands, "usb_port_path": snapshot["usb_port_path"],
            **hid_options, **mapping_options({**hid_options, **mappings}),
            "wait_adb_seconds": wait_adb_seconds,
            "aoa_metadata": snapshot.get("metadata") or {},
            "post_flash_recovery": snapshot}


def wait_for_boot(snapshot: dict, device_ids: list[str], validate, probe, on_event=None) -> None:
    """Require the original serial and port; loader/fastboot is not Android."""
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
    deadline = time.monotonic() + 180 + delay
    while True:
        validate()
        now = time.monotonic()
        attached = {item["serial"]: item for item in probe()}
        device = attached.get(serial) or {}
        if device and (device.get("properties") or {}).get("usb") != port:
            raise RuntimeError("physical USB identity changed after flash; reconcile required")
        ready = (device.get("transport") != "adb_proxy"
                 and device.get("state") in {"available", "device", "usb_only", "unauthorized"})
        if ready:
            if ready_since is None:
                ready_since = now
            if now - ready_since >= delay:
                return
        else:
            ready_since = None
        if now >= deadline:
            raise RuntimeError("固件已烧写，但未等到原 USB 端口的 Android 设备；请检查设备后单独回放 AOA")
        time.sleep(1)


def validate_preflash(snapshot: dict, devices: list[str], validate, probe) -> None:
    recovery_options(snapshot)
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
    try:
        options = {**recovery_options(snapshot), "_validate_fencing": validate,
                   "suppress_adb_daemon": suppress_adb_daemon,
                   "suppress_adb_daemon_gate": suppress_adb_daemon_gate}
        recovered = execute("aoa_sequence", devices, options, progress_event_callback=on_event)
        if recovered.get("success") is not True or recovered.get("adb_recovered") is not True:
            raise RuntimeError("ADB 尚未恢复或尚未授权；请在设备上确认允许 USB 调试")
        return {**flash_result, "flash_success": True, "stage": "ADB_READY", "aoa_recovery": recovered}
    except Exception as exc:
        return {**flash_result, "success": False, "flash_success": True,
                "stage": "AOA_RECOVERY_FAILED", "reconcile_required": True,
                "error": f"固件烧写已完成，AOA 恢复失败：{exc}。请检查设备并单独调试 AOA，无需重新烧写"}
