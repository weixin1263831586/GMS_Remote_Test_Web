"""Local/remote Tradefed result collection behind one service boundary."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from . import runtime
from .suites import get_default_suites_path, is_config_host_local
from .tradefed import (
    execute_tradefed_command,
    execute_tradefed_command_local,
    find_tradefed_binary,
    find_tradefed_binary_local,
    parse_tradefed_list_results,
)


def _within(path: str, root: str, label: str) -> str:
    resolved_root = os.path.realpath(os.path.expanduser(root))
    resolved = os.path.realpath(os.path.expanduser(path))
    if os.path.commonpath([resolved_root, resolved]) != resolved_root:
        raise ValueError(f"{label} must stay inside suites_path")
    return resolved


def _payload(output: str) -> dict[str, Any]:
    parsed = parse_tradefed_list_results(output)
    results = parsed.get("results", [])
    return {
        "success": True,
        "columns": parsed.get("columns", []),
        "results": results,
        "count": len(results),
        "raw_output": output,
        "cached": False,
    }


async def collect_tradefed_results(
    config: dict[str, Any],
    suite_path: str,
    tradefed_bin: str | None = None,
) -> dict[str, Any]:
    if is_config_host_local(config):
        suite_path = _within(
            suite_path, get_default_suites_path(config), "suite_path"
        )
        launcher = (
            _within(tradefed_bin, suite_path, "tradefed_bin")
            if tradefed_bin
            else find_tradefed_binary_local(suite_path)
        )
        if not launcher:
            return {
                "success": False,
                "status_code": 404,
                "error": f"No tradefed binary found in {suite_path}",
            }
        output, error, code = await asyncio.to_thread(
            execute_tradefed_command_local, suite_path, launcher
        )
    else:
        ssh = await asyncio.to_thread(runtime.ssh_manager.get_connection, config)
        if not ssh:
            return {"success": False, "status_code": 500, "error": "SSH connection failed"}
        try:
            launcher = tradefed_bin or find_tradefed_binary(ssh, suite_path)
            if not launcher:
                return {
                    "success": False,
                    "status_code": 404,
                    "error": f"No tradefed binary found in {suite_path}",
                }
            output, error, code = await asyncio.to_thread(
                execute_tradefed_command, ssh, suite_path, launcher
            )
        finally:
            await asyncio.to_thread(runtime.ssh_manager.return_connection, ssh)

    if code != 0:
        return {
            "success": False,
            "status_code": 500,
            "error": error or f"Command failed with exit code: {code}",
            "raw_output": output,
        }
    return _payload(output)
