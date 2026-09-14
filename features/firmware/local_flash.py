"""Safe batch flashing for devices attached to one Linux Worker."""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex

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
            results.append({
                "device": device, "success": False, "stage": "WAIT_LOADER",
                "error": "未能确认目标设备是唯一 Loader",
                "loader_output": detail[-1000:],
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
