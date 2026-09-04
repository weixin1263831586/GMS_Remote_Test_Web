"""Non-terminal-stream WebSocket helpers used by the system socket."""

from __future__ import annotations

import logging

from fastapi import WebSocket

from features.system.ssh import ssh_manager
from foundation.config import config_manager
from foundation.device_locks import device_lock_manager


logger = logging.getLogger(__name__)


async def refresh_devices_websocket(
    _client_id: str,
    websocket: WebSocket,
) -> None:
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return
        try:
            devices_result = ssh_manager.execute_command(
                ssh, "adb devices", timeout=5
            )
            if devices_result.code != 0:
                return
            devices_info = []
            for line in devices_result.stdout.strip().split("\n")[1:]:
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                device_id, status = parts[:2]
                device_data = {"id": device_id, "status": status}
                lock_status = device_lock_manager.get_lock_status(device_id)
                if lock_status:
                    device_data["locked"] = True
                    device_data["locked_by"] = lock_status["locked_by"]
                devices_info.append(device_data)
            await websocket.send_json(
                {"type": "devices_updated", "devices": devices_info}
            )
        finally:
            ssh_manager.return_connection(ssh)
    except Exception as exc:
        logger.error("Error refreshing devices: %s", exc)
        await websocket.send_json({"type": "error", "message": str(exc)})


async def handle_tradefed_list_results(
    _client_id: str,
    websocket: WebSocket,
    data: dict,
) -> None:
    from features.test_execution import (
        execute_tradefed_command,
        parse_tradefed_list_results,
    )

    ssh = None
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await websocket.send_json(
                {"type": "tradefed_list_results_error", "error": "SSH 连接失败"}
            )
            return
        suite_path = data.get("suite_path", "")
        tradefed_bin = data.get("tradefed_bin", "")
        if not suite_path or not tradefed_bin:
            await websocket.send_json(
                {
                    "type": "tradefed_list_results_error",
                    "error": "缺少参数：suite_path 或 tradefed_bin",
                }
            )
            return
        output, error, code = execute_tradefed_command(
            ssh, suite_path, tradefed_bin
        )
        command = f"cd '{suite_path}' && {tradefed_bin} list results"
        if code == 0:
            parsed = parse_tradefed_list_results(output)
            await websocket.send_json(
                {
                    "type": "tradefed_list_results",
                    "success": True,
                    "output": output,
                    "columns": parsed.get("columns", []),
                    "results": parsed.get("results", []),
                    "count": len(parsed.get("results", [])),
                    "command": command,
                }
            )
        else:
            await websocket.send_json(
                {
                    "type": "tradefed_list_results_error",
                    "success": False,
                    "error": error or f"命令执行失败，退出代码：{code}",
                    "command": command,
                }
            )
    except Exception as exc:
        logger.error("[TRADEFED_LIST_RESULTS] Error: %s", exc)
        await websocket.send_json(
            {
                "type": "tradefed_list_results_error",
                "success": False,
                "error": str(exc),
            }
        )
    finally:
        if ssh:
            ssh_manager.return_connection(ssh)
