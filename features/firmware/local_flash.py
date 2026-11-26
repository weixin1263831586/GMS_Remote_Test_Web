"""Safe batch flashing for devices attached to one Linux Worker."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex

from features.devices import parse_adb_device_states
from foundation.cluster_port import (
    FirmwareRecoveryResult,
    PreparedFirmwareRecovery,
    prepare_local_firmware_recovery,
    run_local_firmware_recovery,
)
from foundation.error_model import ApiError
from foundation.networking import is_local_host
from foundation.ssh_executor import ssh_executor

from . import runtime
from .usbip_transport import (
    wait_for_rockusb_loader_exit,
    wait_for_single_rockusb_loader,
)


_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


async def run_local_firmware_batch(
    *, ssh, devices: list[str], protocols: dict[str, str], suite_dir: str,
    remote_tool: str, remote_firmware: str, client_id: str,
) -> tuple[list[dict], str | None]:
    """Flash local devices safely as a verified batch.

    ``upgrade_tool uf`` has no serial selector. Only one selected device is
    therefore put in Loader at a time; this prevents a second board from being
    reported successful when the tool actually operated on the first loader.
    """
    command_prefix = f"cd {shlex.quote(suite_dir)} && {shlex.quote(remote_tool)} uf "
    results: list[dict] = []
    for device in devices:
        if protocols.get(device) != "rockusb-loader":
            reboot = await asyncio.to_thread(
                runtime.ssh_manager.execute_command, ssh,
                f"adb -s {shlex.quote(device)} reboot loader", timeout=10,
            )
            if not reboot.ok:
                results.append({
                    "device": device, "success": False,
                    "stage": "ENTER_LOADER", "exit_code": reboot.code,
                    "error": (reboot.stderr or reboot.stdout or "").strip(),
                })
                break
        ready, detail = await wait_for_single_rockusb_loader(ssh, device)
        if not ready:
            # 协议快照过期场景：快照时设备是 rockusb-loader，执行前已
            # 自行恢复 adb（例如上一轮烧写实际已完成的迟到回报）。此时
            # 卡满 120s 超时只会得到误导性的「唯一 Loader」错误；先复查
            # adb 再定性，让上层能选择直接重试而非排查烧写链路。
            adb_probe = await asyncio.to_thread(
                runtime.ssh_manager.execute_command, ssh, "adb devices", timeout=8,
            )
            if adb_probe.ok and device in parse_adb_device_states(adb_probe.stdout):
                wait_error = (
                    "目标设备未停留在 Loader（已恢复 ADB，"
                    "设备协议快照可能已过期，请刷新设备列表后重试）"
                )
            else:
                wait_error = "未能确认目标设备是唯一 Loader"
            results.append({
                "device": device, "success": False, "stage": "WAIT_LOADER",
                "error": wait_error,
                "loader_output": detail[-1000:],
            })
            break

        # 门禁通过到 ``uf`` 执行之间存在时间窗：共享 Worker 上其他任务
        # 可能在此窗口让第二块板进入 Loader。``upgrade_tool uf`` 无串号
        # 选择器，窗口内出现歧义必须中止而不是赌目标板被选中。
        reverified, reverify_detail = await wait_for_single_rockusb_loader(
            ssh, device, timeout=2, interval=0.2,
        )
        if not reverified:
            results.append({
                "device": device, "success": False, "stage": "RECONFIRM_LOADER",
                "error": (
                    "Loader 唯一性复核失败：烧写启动前检测到其他设备同时"
                    "处于 Loader，为避免错烧已中止"
                ),
                "loader_output": reverify_detail[-1000:],
            })
            break

        output_buffer: list[str] = []

        async def on_line(
            line: str, log_type: str, *, _device=device, _buffer=output_buffer,
        ) -> None:
            clean = _strip_ansi(line).strip()
            if not clean:
                return
            _buffer.append(clean)
            if client_id in runtime.global_state.websocket_connections:
                with contextlib.suppress(Exception):
                    await runtime.safe_websocket_send(client_id, {
                        "type": "log_update", "log": f"[{_device}] {clean}",
                        "log_type": log_type,
                    })

        burn = await ssh_executor.run_stream(
            ssh, command_prefix + shlex.quote(remote_firmware), on_line,
            timeout=1800, get_pty=True,
        )
        output = "\n".join(output_buffer)
        if burn.code != 0:
            results.append({
                "device": device, "success": False, "stage": "FLASHING",
                "exit_code": burn.code,
                "error": (output or burn.stderr or "")[-20000:],
            })
            break
        exited_loader, loader_detail = await wait_for_rockusb_loader_exit(
            ssh, device, timeout=180, interval=2,
        )
        if not exited_loader:
            results.append({
                "device": device, "success": False, "stage": "VERIFY",
                "exit_code": burn.code,
                "error": "烧写命令返回成功但设备仍停留在 Loader",
                "loader_output": loader_detail[-1000:],
                "output": output[-20000:],
            })
            break
        results.append({
            "device": device, "success": True, "stage": "SUCCEEDED",
            "exit_code": burn.code, "output": output[-20000:],
        })

    completed = {str(item.get("device")) for item in results}
    for device in devices:
        if device not in completed:
            results.append({
                "device": device, "success": False, "stage": "SKIPPED",
                "error": "previous device failed",
            })
    failed = next((item for item in results if not item["success"]), None)
    return results, (failed.get("error") if failed else None)


def prepare_recovery(request, form, client_id, devices, config) -> PreparedFirmwareRecovery | None:
    profile_id = str(form.get("aoa_profile_id") or "").strip()
    if not profile_id:
        return None
    if not is_local_host(runtime.config_manager.get_ubuntu_host(config)):
        raise ApiError.conflict("烧写后 AOA 恢复请使用设备所在 Worker 的烧写入口")
    return prepare_local_firmware_recovery(
        request, client_id, devices, profile_id, form.get("aoa_boot_wait_seconds", 60))


async def recover_after_burn(prepared: PreparedFirmwareRecovery, client_id: str, results: list) -> FirmwareRecoveryResult:
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
