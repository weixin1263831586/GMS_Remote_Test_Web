from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import shlex
import time
from collections import deque
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from features.devices import device_lock_manager, get_or_create_user_state, update_user_state_field
from features.reports import save_test_report_to_db, test_report_db
from features.users import get_client_display_id_from_request, get_client_username_from_request
from foundation.responses import error_response, success_response

from . import runtime
from .api_support import LogLevel
from .models import TestStartRequest
from .process_control import (
    build_process_group_id,
    find_arg_pgid_command,
    find_env_pgid_command,
    kill_pid_tree_commands,
    parse_pid_lines,
)
from .suites import (
    TRADEFED_BINARY_LIST,
    detect_test_type_from_dir_path,
    detect_test_type_from_suite_path,
    get_default_suites_path,
    get_effective_local_server,
)


logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Start Test ====================

@router.post("/api/test/start")
async def start_test(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
    req: TestStartRequest = Body(None),
):
    resp = runtime.generate_help_or_continue(help, "POST", "/api/test/start")
    if resp:
        return resp

    if req is None:
        return error_response("Missing request body", 400)

    client_id = runtime.get_client_id_from_request(request)

    devices = req.devices
    if not devices:
        return error_response("No devices selected", 400)

    if req.worker_id and req.worker_id != "worker-local":
        return runtime.start_cluster_test(req, runtime.get_client_id_from_request(request))

    config = runtime.config_manager.load_config()
    username = get_client_username_from_request(request)

    user_state = get_or_create_user_state(client_id)
    with runtime.global_state.user_states_lock:
        if user_state.get("running", False) or user_state.get("starting", False):
            return error_response("You already have a test running", 400)
        # Reserve this client's execution slot before awaiting device locks.
        # Without this flag, concurrent requests can both observe running=False.
        user_state["starting"] = True

    try:
        locked_devices, failed_devices = await runtime.acquire_test_devices(
            client_id=client_id,
            username=username,
            devices=devices,
        )
    except Exception as exc:
        with runtime.global_state.user_states_lock:
            user_state["starting"] = False
        logger.exception("Failed to acquire devices for %s", client_id)
        return error_response(f"Failed to acquire devices: {exc}", 500)

    if failed_devices:
        with runtime.global_state.user_states_lock:
            user_state["starting"] = False
        error_msg = "The following devices are occupied by other users:\n"
        for fail in failed_devices:
            error_msg += f"- {fail['device_id']} ({fail['error']})\n"
        return JSONResponse(
            content={"success": False, "error": error_msg.strip(), "failed_devices": failed_devices},
            status_code=409,
        )

    try:
        test_params = req.model_dump()
        test_params["client_id"] = client_id
        test_params["display_client_id"] = get_client_display_id_from_request(request)

        user_state = get_or_create_user_state(client_id)
        logger.info(f"[TestStart] Client state created/loaded: {client_id}")

        logger.info(f"[TestStart] Setting running=True for {client_id}")
        update_user_state_field(
            client_id,
            {
                "running": True,
                "starting": False,
                "devices": devices,
                "test_type": req.test_type,
                "logs": [],
                "test_outcome": "running",
                "report_timestamp": "",
            },
        )
    except Exception:
        # Any failure between acquiring devices and flipping running=True must
        # release the reservation + device locks, otherwise the starting flag
        # sticks True and the client can never start another test.
        with runtime.global_state.user_states_lock:
            user_state["starting"] = False
        await runtime.release_test_devices(client_id, locked_devices)
        logger.exception("Failed to prepare test start for %s", client_id)
        return error_response("Failed to start test", 500)

    task = asyncio.create_task(_run_test_background(config, test_params, client_id, locked_devices))
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)

    return success_response(message="Test started")


async def _run_test_background(
    config: dict[str, Any],
    test_params: dict[str, Any],
    client_id: str,
    locked_devices: list[str],
):
    """Execute the GMS test over SSH, stream logs, and record the report on completion."""

    ssh = None
    test_outcome = "failed"
    report_timestamp = ""
    last_lock_refresh = time.monotonic()

    async def log_callback(
        message: str,
        log_type: LogLevel | str = LogLevel.INFO,
        source: str = "system",
    ):
        """Append a log entry.

        source: "system" for operational/status logs, "module" for the actual
        CTS/VTS/GTS/STS module-test output (SSH stdout/stderr lines).
        """
        timestamp_str = datetime.now().strftime("%H:%M:%S")

        if isinstance(log_type, str):
            log_type_str = log_type
        else:
            log_type_str = log_type.value

        log_entry = {
            "message": message,
            "type": log_type_str,
            "timestamp": datetime.now().isoformat(),
            "source": source,
        }

        with runtime.global_state.test_logs_lock:
            if client_id not in runtime.global_state.test_logs:
                runtime.global_state.test_logs[client_id] = deque(maxlen=runtime.max_log_entries)
            runtime.global_state.test_logs[client_id].append(log_entry)

        user_state = get_or_create_user_state(client_id)
        if "logs" not in user_state:
            user_state["logs"] = deque(maxlen=runtime.max_log_entries)
        user_state["logs"].append(
            {"t": timestamp_str, "msg": message, "type": log_type_str, "source": source}
        )

        await runtime.safe_websocket_send(
            client_id,
            {"type": "log_update", "log": message, "log_type": log_type_str, "source": source},
        )

    try:
        user_state = get_or_create_user_state(client_id)
        if not user_state.get("running", False):
            await log_callback("Test cancelled", "warning")
            return

        process_group_id = build_process_group_id(client_id, int(time.time() * 1000))
        update_user_state_field(client_id, {"process_group_id": process_group_id})

        await log_callback(f"Process group ID: {process_group_id}", "info")

        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                await log_callback("SSH connection failed", "error")
                update_user_state_field(client_id, {"running": False})
                await runtime.release_test_devices(client_id, locked_devices)
                return

            await log_callback("SSH connection successful", "success")

            local_script = os.path.realpath(
                os.path.join(runtime.project_root, "scripts", "run_GMS_Test_Auto.sh")
            )

            suites_path = config.get("suites_path") or get_default_suites_path(config)
            remote_script = os.path.join(suites_path, "run_GMS_Test_Auto.sh")

            try:
                script_size = os.path.getsize(local_script)
                size_kb = script_size / 1024
                await log_callback(f"Uploading: run_GMS_Test_Auto.sh -> {remote_script} ({size_kb:.2f}KB)", "info")
                def _upload_and_chmod():
                    with ssh.open_sftp() as sftp:
                        sftp.put(local_script, remote_script)
                    _, stdout, _stderr = ssh.exec_command(f"chmod +x {shlex.quote(remote_script)}")
                    stdout.read()
                await asyncio.to_thread(_upload_and_chmod)
                await log_callback(f"Upload complete ({size_kb:.2f}KB)", "success")
            except FileNotFoundError:
                await log_callback("Local script not found, using remote script", "warning")
            except Exception as e:
                await log_callback(f"Script upload failed: {e!s}", "warning")

            test_type = test_params.get("test_type", "")
            test_module = test_params.get("test_module", "")
            test_case = test_params.get("test_case", "")
            retry_dir = test_params.get("retry_dir", "")
            test_suite = test_params.get("test_suite", "")

            test_type_lower = test_type.lower() if test_type else ""

            if test_suite and "testcases" in test_suite:
                test_suite_tools = test_suite.replace("/testcases", "/tools")
            else:
                test_suite_tools = test_suite

            if not test_type_lower and test_suite_tools:
                test_type_lower = detect_test_type_from_suite_path(test_suite_tools)
                if test_type_lower:
                    await log_callback(f"Detected test type from suite path: {test_type_lower}", "info")

            local_server = get_effective_local_server(client_id, test_params.get("local_server", ""))
            devices = test_params.get("devices", [])

            if not retry_dir and not test_suite_tools:
                await log_callback("Missing test suite path", "error")
                await log_callback("Please use --test-suite parameter to specify test suite path", "info")
                update_user_state_field(client_id, {"running": False})
                await runtime.release_test_devices(client_id, locked_devices)
                return

            suites_path = config.get("suites_path") or get_default_suites_path(config)
            remote_script = os.path.join(suites_path, "run_GMS_Test_Auto.sh")

            cmd_parts = [remote_script]

            if retry_dir:
                timestamp = os.path.basename(retry_dir.strip().rstrip("/"))

                if not test_type_lower and test_suite_tools:
                    await log_callback("Detecting test type from test_suite path...", "info")
                    test_type_lower = detect_test_type_from_suite_path(test_suite_tools)
                    if test_type_lower:
                        await log_callback(f"Detected test type: {test_type_lower}", "info")

                if not test_type_lower:
                    await log_callback(f"Looking up report {timestamp} test type from DB...", "info")
                    try:
                        report = test_report_db.get_report_by_timestamp(timestamp)
                        if report and report.get("test_type"):
                            test_type_lower = report["test_type"].lower()
                            await log_callback(f"Detected test type from report: {test_type_lower}", "info")
                        else:
                            await log_callback(f"Report {timestamp} not found in DB, trying directory name", "warning")
                    except Exception as e:
                        await log_callback(f"DB read failed: {e}, trying directory name", "warning")

                if not test_type_lower and retry_dir:
                    await log_callback("Detecting test type from directory name...", "info")
                    test_type_lower = detect_test_type_from_dir_path(retry_dir)
                    if test_type_lower:
                        await log_callback(f"Detected {test_type_lower.upper()} test from path", "info")

                if not test_type_lower:
                    test_type_lower = ""
                    await log_callback("Test type not detected, will be auto-detected by script", "warning")

                cmd_parts.extend([test_type_lower, "retry", timestamp])
                await log_callback(f"Retry mode: test_type={test_type_lower or '(auto)'}, timestamp={timestamp}", "info")

                if not test_suite_tools and test_type_lower:
                    await log_callback(f"Auto-searching for {test_type_lower.upper()} test suite...", "info")
                    try:
                        suite_pattern = os.path.join(suites_path, f"android-{test_type_lower}-*")
                        suite_dirs = [d for d in await asyncio.to_thread(glob.glob, suite_pattern) if os.path.isdir(d)]

                        if suite_dirs:
                            suite_dir = max(suite_dirs, key=os.path.getmtime)
                            await log_callback(f"Found test suite directory: {suite_dir}", "info")
                            possible_tools_dirs = [
                                os.path.join(suite_dir, f"android-{test_type_lower}", "tools"),
                                os.path.join(suite_dir, "tools"),
                                suite_dir,
                            ]

                            for tools_dir in possible_tools_dirs:
                                if os.path.isdir(tools_dir):
                                    tradefed_path = os.path.join(tools_dir, f"{test_type_lower}-tradefed")
                                    if os.path.exists(tradefed_path):
                                        test_suite_tools = tools_dir
                                        await log_callback(f"Found tools directory: {test_suite_tools}", "info")
                                        break
                                    has_tradefed = any(
                                        os.path.exists(os.path.join(tools_dir, tf))
                                        for tf in TRADEFED_BINARY_LIST
                                    )
                                    if has_tradefed or os.path.exists(os.path.join(tools_dir, "test.xml")):
                                        test_suite_tools = tools_dir
                                        await log_callback(f"Found tools directory: {test_suite_tools}", "info")
                                        break

                            if not test_suite_tools:
                                await log_callback(f"Valid tools directory not found, tried: {possible_tools_dirs}", "warning")
                        else:
                            await log_callback(f"{test_type_lower.upper()} test suite not found", "warning")
                    except Exception as e:
                        await log_callback(f"Test suite search failed: {e}", "error")
            else:
                cmd_parts.append(test_type_lower)
                if test_module:
                    cmd_parts.append(test_module)
                if test_case:
                    cmd_parts.append(test_case)

            if devices:
                device_args_list = []
                if len(devices) > 1:
                    device_args_list.extend(["--shard-count", str(len(devices))])
                for device in devices:
                    device_args_list.extend(["-s", device])

                device_args_str = " ".join(device_args_list)
                cmd_parts.extend(["--device-args", device_args_str])

            if test_suite_tools:
                cmd_parts.extend(["--test-suite", test_suite_tools])

            if local_server:
                cmd_parts.extend(["--local-server", local_server])
            else:
                await log_callback("local_server is empty, test may fail", "warning")

            if process_group_id:
                cmd_parts.extend(["--pgid", process_group_id])

            command = " ".join(shlex.quote(part) for part in cmd_parts)

            # SSH exec_command runs a non-interactive shell, so user exports in
            # ~/.bashrc or ~/.profile are otherwise missing. GTS needs
            # APE_API_KEY; prefer the configured value, then fall back to the
            # remote user's shell startup files.
            prefix_parts = [
                "if [ -f ~/.profile ]; then . ~/.profile; fi",
                "if [ -f ~/.bashrc ]; then . ~/.bashrc; fi",
            ]
            ape_api_key = (config.get("tradefed") or {}).get("ape_api_key")
            if ape_api_key:
                prefix_parts.append(f"export APE_API_KEY={shlex.quote(str(ape_api_key))}")
            prefix_parts.append(f"cd {shlex.quote(os.path.dirname(remote_script))}")
            shell_command = " && ".join(prefix_parts) + f" && {command}"
            command_full = f"bash -lic {shlex.quote(shell_command)}"

            await log_callback(f"Executing command: {command}", "info")

            _stdin, stdout, stderr = await asyncio.to_thread(
                lambda: ssh.exec_command(command_full, get_pty=True)
            )

            while not stdout.channel.exit_status_ready():
                if time.monotonic() - last_lock_refresh >= 300:
                    renewed = await asyncio.to_thread(device_lock_manager.refresh_locks, client_id, locked_devices)
                    if renewed != len(locked_devices):
                        await log_callback("One or more device locks could not be renewed", "warning")
                    last_lock_refresh = time.monotonic()
                user_state = get_or_create_user_state(client_id)
                if not user_state.get("running", False):
                    await log_callback("Test stopped by user", "warning")
                    with contextlib.suppress(Exception):
                        if process_group_id:
                            def _kill_pgid():
                                find_cmd = find_env_pgid_command(process_group_id)
                                _stdin, pid_stdout, _stderr = ssh.exec_command(find_cmd)
                                pids = parse_pid_lines(pid_stdout.read().decode("utf-8", errors="replace"))
                                for pid in pids:
                                    for kill_cmd in kill_pid_tree_commands(pid):
                                        ssh.exec_command(kill_cmd)
                            await asyncio.to_thread(_kill_pgid)
                    break

                if stdout.channel.recv_ready():
                    try:
                        data = (await asyncio.to_thread(stdout.channel.recv, 65536)).decode("utf-8", errors="replace")
                        if data:
                            for line in data.split("\n"):
                                if line.strip():
                                    await log_callback(line.strip(), "info", source="module")
                    except Exception as e:
                        logger.error(f"Error reading stdout: {e}")

                if stderr.channel.recv_stderr_ready():
                    try:
                        error_data = (await asyncio.to_thread(stderr.channel.recv_stderr, 65536)).decode("utf-8", errors="replace")
                        if error_data:
                            for line in error_data.split("\n"):
                                if line.strip():
                                    await log_callback(line.strip(), "error", source="module")
                    except Exception as e:
                        logger.error(f"Error reading stderr: {e}")

                await asyncio.sleep(0.05)

            exit_code = await asyncio.to_thread(stdout.channel.recv_exit_status)

            if stdout.channel.recv_ready():
                try:
                    remaining_data = (await asyncio.to_thread(stdout.channel.recv, 65536)).decode("utf-8", errors="replace")
                    if remaining_data:
                        for line in remaining_data.split("\n"):
                            if line.strip():
                                await log_callback(line.strip(), "info", source="module")
                except Exception as e:
                    logger.error(f"Error reading remaining stdout: {e}")

            if stderr.channel.recv_stderr_ready():
                try:
                    remaining_error = (await asyncio.to_thread(stderr.channel.recv_stderr, 65536)).decode("utf-8", errors="replace")
                    if remaining_error:
                        for line in remaining_error.split("\n"):
                            if line.strip():
                                await log_callback(line.strip(), "error", source="module")
                except Exception as e:
                    logger.error(f"Error reading remaining stderr: {e}")

            if exit_code == 0:
                test_outcome = "completed"
                await log_callback(f"Test completed successfully (exit code: {exit_code})", "success")
            else:
                test_outcome = "failed"
                await log_callback(f"Test failed with exit code: {exit_code}", "error")

    except Exception as e:
        logger.error(f"Error in _run_test_background: {e}")
        await log_callback(f"Test execution error: {e!s}", "error")

    finally:
        try:
            user_state = get_or_create_user_state(client_id)
            user_logs = user_state.get("logs", [])
            report_timestamp = save_test_report_to_db(client_id, config, test_params, user_logs) or ""
            if report_timestamp:
                await log_callback(f"Test report recorded: {report_timestamp}", "success")
        except Exception as e:
            logger.error(f"Failed to save test report: {e}")

        await runtime.release_test_devices(client_id, locked_devices)
        logger.info(f"[Device Lock] Test completed, device unlock broadcast: {locked_devices}")

        update_user_state_field(
            client_id,
            {
                "running": False,
                "devices": [],
                "test_outcome": test_outcome,
                "report_timestamp": report_timestamp,
            },
        )

        notification = runtime.store_notification(
            client_id,
            "Test task completed",
            "Test execution has completed, please check logs and reports.",
            "info",
            "test",
            {"devices": locked_devices},
        )

        await runtime.safe_websocket_send(client_id, {"type": "test_complete", "notification": notification})


# ==================== Stop Test ====================

@router.post("/api/test/stop")
async def stop_test(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
):
    resp = runtime.generate_help_or_continue(help, "POST", "/api/test/stop")
    if resp:
        return resp

    client_id = runtime.get_client_id_from_request(request)
    user_state = get_or_create_user_state(client_id)
    process_group_id = user_state.get("process_group_id")

    running = user_state.get("running", False)
    devices_to_release = user_state.get("devices", [])

    if not running and not devices_to_release:
        return error_response("No test running", 400)

    update_user_state_field(client_id, {"running": False})

    timestamp_str = datetime.now().strftime("%H:%M:%S")
    log_str = f"[{timestamp_str}] User requested test stop..."
    if "logs" not in user_state:
        user_state["logs"] = []
    user_state["logs"].append(log_str)

    if devices_to_release:
        logger.info(f"[TestStop] Releasing device locks for: {devices_to_release}")
        await runtime.release_test_devices(client_id, devices_to_release)

    update_user_state_field(client_id, {"devices": []})

    config = runtime.config_manager.load_config()
    async with runtime.ssh_manager.async_optional_connection(config) as ssh:
        if not ssh:
            return error_response("SSH connection failed", 500)

        try:
            killed_count = 0

            if process_group_id:
                find_cmd = find_env_pgid_command(process_group_id)
                user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminating test process group: {process_group_id}...")

                def _find_and_kill(find_command: str) -> int:
                    out, _e, _c = runtime.ssh_manager.execute_command(ssh, find_command, timeout=10)
                    found_pids = parse_pid_lines(out)
                    for pid in found_pids:
                        for command in kill_pid_tree_commands(pid):
                            runtime.ssh_manager.execute_command(ssh, command)
                    return len(found_pids)

                killed = await asyncio.to_thread(_find_and_kill, find_cmd)
                if killed:
                    killed_count += killed

                    await asyncio.sleep(1)
                    user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminated {killed_count} test processes")
                    return success_response(message="Test stopped")

                fallback_cmd = find_arg_pgid_command(process_group_id)
                killed = await asyncio.to_thread(_find_and_kill, fallback_cmd)
                if killed:
                    killed_count += killed

                    await asyncio.sleep(1)
                    user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminated {killed_count} test processes (command match)")
                    return success_response(message="Test stopped")

            user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] No test process found (may have already stopped or manual test)")
            return success_response(message="Test stopped (no running test process found)")

        except Exception as e:
            user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Error stopping test: {e!s}")
            logger.error(f"Error stopping test: {e}")
            return error_response(str(e), 500)
