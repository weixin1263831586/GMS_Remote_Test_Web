"""Manual burn continuation: keep burn success separate from ADB recovery."""
from __future__ import annotations

import asyncio

from foundation.cluster_port import (
    prepare_local_firmware_recovery,
    run_local_firmware_recovery,
)
from foundation.error_model import ApiError
from foundation.networking import is_local_host

from . import runtime


def prepare_recovery(request, form, client_id, devices, config):
    profile_id = str(form.get("aoa_profile_id") or "").strip()
    if not profile_id:
        return None
    if not is_local_host(runtime.config_manager.get_ubuntu_host(config)):
        raise ApiError.conflict("烧写后 AOA 恢复请使用设备所在 Worker 的烧写入口")
    return prepare_local_firmware_recovery(
        request, client_id, devices, profile_id, form.get("aoa_boot_wait_seconds", 60))


async def recover_after_burn(prepared: dict, client_id: str, results: list) -> dict:
    loop = asyncio.get_running_loop()

    async def emit(event):
        await runtime.safe_websocket_send(client_id, {
            "type": "log_update", "log_type": "info",
            "log": f"AOA {event.get('phase', '')} {event.get('index', 0)}/{event.get('total', 0)} "
                   f"{event.get('command', '')}",
        })

    def report(event):
        asyncio.run_coroutine_threadsafe(emit(event), loop)

    try:
        recovery = await run_local_firmware_recovery(prepared, report)
    except Exception as exc:
        recovery = {"success": False, "error": str(exc), "reconcile_required": True,
                    "replay_completed": False}
    replayed = bool(recovery.get("replay_completed") or
                    (recovery.get("result") or {}).get("replay_completed"))
    results[0].update({"flash_success": True, "aoa_recovery": recovery,
                       "stage": "ADB_READY" if recovery["success"]
                       else ("REPLAY_DONE_ADB_UNAVAILABLE" if replayed
                             else "AOA_RECOVERY_FAILED")})
    if recovery["success"]:
        return recovery
    if replayed:
        detail = (f"固件烧写与 AOA 回放均已完成，但 ADB 恢复未确认：{recovery['error']}。"
                  "输入已生效，无需重新烧写或回放；请重启设备或人工确认 USB 调试后重新验证 ADB")
    else:
        detail = (f"固件烧写已完成，AOA 恢复失败：{recovery['error']}。"
                  "请检查设备并单独调试 AOA，无需重新烧写；若显示 unauthorized，请在设备上确认允许 USB 调试")
    results[0]["success"] = False
    results[0]["error"] = detail
    await runtime.safe_websocket_send(client_id, {
        "type": "firmware_burn_complete", "devices": [prepared["device_id"]],
        "success": False, "flash_success": True, "error": detail, "results": results})
    runtime.store_notification(client_id, "固件已烧写，AOA 恢复未完成", detail[:500],
                               "error", "firmware", {"results": results})
    return {**recovery, "error": detail}
