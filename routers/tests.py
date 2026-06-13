"""Tests router - test execution, suite management, and log APIs."""

import os
import re
import json
import shlex
import time
import glob
import uuid
import shutil
import asyncio
import logging
import subprocess
import tarfile
import zipfile
from pathlib import Path
import mimetypes
import urllib.parse
from datetime import datetime
from collections import deque
from typing import Dict, Any, List, Optional, Union

from fastapi import APIRouter, HTTPException, Query, Request, Body
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse, FileResponse

from core.config import config_manager
from core.ssh import ssh_manager
from core.devices import (
    SSHConnection,
    get_or_create_user_state,
    update_user_state_field,
    release_device_locks,
    broadcast_device_lock_update,
    safe_websocket_send,
    ssh_connection_failed_response,
)
from core.error_handling import handle_api_errors
from core.api_help import generate_help_or_continue
from core.test_suite_utils import (
    TRADEFED_BINARY_LIST,
    build_suite_info,
    detect_test_type_from_dir_path,
    detect_test_type_from_suite_path,
    ensure_tradefed_executable,
    get_default_suites_path,
    get_effective_local_server,
    is_config_host_local,
    list_local_test_suites,
)
from core.upload_utils import (
    extract_report_name_from_upload,
    merge_files_to_path,
    safe_upload_target_path,
    save_upload_to_path,
)
from core.archive_utils import (
    derive_suite_dir_name_from_archive,
    is_complete_archive_file,
    safe_extract_member_path,
    sanitize_suite_dir_name,
    sanitize_suite_filename_from_url,
    strip_common_archive_root,
)
from core.schemas import (
    TestParseArgsRequest,
    TestParseArgsResponse,
    TestStartRequest,
    TestSuiteAddLocalRequest,
    TestSuiteDownloadRequest,
    TestSuiteExtractRequest,
    TradefedListResultsRequest,
    SuiteApkAnalyzeRequest,
    SuiteDiagnosisTargetRequest,
)
from core.state import global_state
from core.reports import save_test_report_to_db
from core.api_response import ApiResponse, success_response, error_response
from core.clients import get_client_id_from_request, parse_client_id
from core.notifications import store_notification
from core.settings import (
    APK_MAX_FILE_SIZE,
    APK_MAX_SOURCE_FILE_SIZE,
    APK_UPLOAD_DIR,
    MAX_LOG_ENTRIES,
    PROJECT_ROOT,
)
from modules.device_lock_manager import device_lock_manager
from modules.test_logs_manager import test_logs_manager
from core.test_report_db import test_report_db
from core.enums import LogLevel

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Upload progress constants ---
UPLOAD_PROGRESS_EXPIRATION = 10

# ==================== Parse Args ====================

@router.post("/api/test/parse-args")
async def parse_test_args(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TestParseArgsRequest = Body(None),
):
    """Parse test launch arguments - smart recognition of CLI parameters."""
    if help or req is None:
        help_text = """API: /api/test/parse-args

Function: Smart parse test launch command line arguments

Direct test mode params:
  params: ["DEVICE", "TYPE", "MODULE/SUITE", "CASE/SUITE", "SUITE"]

Retry mode params:
  params: ["--retry", "REPORT_TIMESTAMP", "DEVICE", "TYPE", "SUITE"]

Supported Test Types: CTS, GTS, GTS-ROOT, STS, VTS, APTS, GSI
"""
        return PlainTextResponse(content=help_text, headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"})

    if req is None or not req.params:
        return error_response("Missing params", 400)

    params = req.params
    first_param = params[0] if params else ""

    result = {
        "success": True,
        "device": "",
        "test_type": "",
        "test_module": "",
        "test_case": "",
        "test_suite": "",
        "retry_dir": "",
        "warnings": [],
    }

    if first_param == "--retry":
        if len(params) < 2:
            return error_response("Report timestamp required for retry mode", 400)
        result["retry_dir"] = params[1]
        if len(params) > 2:
            result["device"] = params[2]
        if len(params) > 3:
            third_param = params[3]
            if "/" in third_param:
                result["test_suite"] = third_param
                result["warnings"].append("Test type will be auto-detected from suite path")
            else:
                result["test_type"] = third_param
                if len(params) > 4:
                    fourth_param = params[4]
                    if "/" in fourth_param:
                        result["test_suite"] = fourth_param
                    else:
                        result["warnings"].append(f"Fourth parameter ignored (expected suite path, got: {fourth_param})")
        else:
            result["warnings"].append("Neither test type nor suite specified")
        return TestParseArgsResponse(**result)

    result["device"] = params[0] if len(params) > 0 else ""
    result["test_type"] = params[1] if len(params) > 1 else ""

    param3 = params[2] if len(params) > 2 else ""
    param4 = params[3] if len(params) > 3 else ""
    param5 = params[4] if len(params) > 4 else ""

    if param3:
        if "/" in param3:
            result["test_suite"] = param3
        else:
            result["test_module"] = param3

    if param4:
        if result["test_suite"]:
            result["test_case"] = param4
        else:
            if "/" in param4:
                result["test_suite"] = param4
            else:
                result["test_case"] = param4

    if param5 and not result["test_suite"]:
        if "/" in param5:
            result["test_suite"] = param5
        else:
            if result["test_case"]:
                result["warnings"].append(f"Fifth parameter ignored (unexpected: {param5})")
            else:
                result["test_case"] = param5

    return TestParseArgsResponse(**result)


# ==================== Start Test ====================

@router.post("/api/test/start")
async def start_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TestStartRequest = Body(None),
):
    resp = generate_help_or_continue(help, "POST", "/api/test/start")
    if resp:
        return resp

    if req is None:
        return error_response("Missing request body", 400)

    client_id = get_client_id_from_request(request)

    user_state = get_or_create_user_state(client_id)
    if user_state.get("running", False):
        return error_response("You already have a test running", 400)

    devices = req.devices
    if not devices:
        return error_response("No devices selected", 400)

    config = config_manager.load_config()
    username = config.get("client_username", "unknown")

    locked_devices = []
    failed_devices = []
    for device_id in devices:
        success, message = device_lock_manager.lock_device(device_id, client_id, username)
        if success:
            locked_devices.append(device_id)
        else:
            failed_devices.append({"device_id": device_id, "error": message})

    if failed_devices:
        await release_device_locks(client_id, locked_devices, broadcast=False)
        error_msg = "The following devices are occupied by other users:\n"
        for fail in failed_devices:
            error_msg += f"- {fail['device_id']} ({fail['error']})\n"
        return JSONResponse(
            content={"success": False, "error": error_msg.strip(), "failed_devices": failed_devices},
            status_code=409,
        )

    if locked_devices:
        logger.info(f"[TestStart] Broadcasting device lock for: {locked_devices}")
        await broadcast_device_lock_update(locked_devices)

    test_params = req.model_dump()
    test_params["client_id"] = client_id

    user_state = get_or_create_user_state(client_id)
    logger.info(f"[TestStart] Client state created/loaded: {client_id}")

    logger.info(f"[TestStart] Setting running=True for {client_id}")
    update_user_state_field(client_id, {"running": True, "devices": devices, "test_type": req.test_type, "logs": []})

    task = asyncio.create_task(_run_test_background(config, test_params, client_id, locked_devices))
    global_state.background_tasks.add(task)
    task.add_done_callback(global_state.background_tasks.discard)

    return success_response(message="Test started")


async def _run_test_background(
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    client_id: str,
    locked_devices: List[str],
):
    """Run test in background."""
    from core.tradefed import find_tradefed_binary, execute_tradefed_command

    ssh = None

    async def log_callback(message: str, log_type: Union[LogLevel, str] = LogLevel.INFO):
        timestamp_str = datetime.now().strftime("%H:%M:%S")
        log_str = f"[{timestamp_str}] {message}"

        if isinstance(log_type, str):
            log_type_str = log_type
        else:
            log_type_str = log_type.value

        with global_state.test_logs_lock:
            if client_id not in global_state.test_logs:
                global_state.test_logs[client_id] = deque(maxlen=MAX_LOG_ENTRIES)
            global_state.test_logs[client_id].append(
                {"message": message, "type": log_type_str, "timestamp": datetime.now().isoformat()}
            )

        user_state = get_or_create_user_state(client_id)
        if "logs" not in user_state:
            user_state["logs"] = deque(maxlen=MAX_LOG_ENTRIES)
        user_state["logs"].append(log_str)

        await safe_websocket_send(client_id, {"type": "log_update", "log": message, "log_type": log_type_str})

    try:
        user_state = get_or_create_user_state(client_id)
        if not user_state.get("running", False):
            await log_callback("Test cancelled", "warning")
            return

        process_group_id = f"gms_test_{client_id.replace('@', '_')}_{int(time.time() * 1000)}"
        update_user_state_field(client_id, {"process_group_id": process_group_id})

        await log_callback(f"Process group ID: {process_group_id}", "info")

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await log_callback("SSH connection failed", "error")
            update_user_state_field(client_id, {"running": False})
            await release_device_locks(client_id, locked_devices)
            return

        await log_callback("SSH connection successful", "success")

        local_script = os.path.realpath(
            os.path.join(PROJECT_ROOT, "scripts", "run_GMS_Test_Auto.sh")
        )

        suites_path = config.get("suites_path") or get_default_suites_path(config)
        remote_script = os.path.join(suites_path, "run_GMS_Test_Auto.sh")

        try:
            script_size = os.path.getsize(local_script)
            size_kb = script_size / 1024
            await log_callback(f"Uploading: run_GMS_Test_Auto.sh -> {remote_script} ({size_kb:.2f}KB)", "info")
            with ssh.open_sftp() as sftp:
                sftp.put(local_script, remote_script)
            stdin, stdout, stderr = ssh.exec_command(f"chmod +x '{remote_script}'")
            stdout.read()
            await log_callback(f"Upload complete ({size_kb:.2f}KB)", "success")
        except FileNotFoundError:
            await log_callback("Local script not found, using remote script", "warning")
        except Exception as e:
            await log_callback(f"Script upload failed: {str(e)}", "warning")

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
            await release_device_locks(client_id, locked_devices)
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
        command_full = f"cd {os.path.dirname(remote_script)} && {command}"

        await log_callback(f"Executing command: {command}", "info")

        stdin, stdout, stderr = ssh.exec_command(command_full, get_pty=True)

        while not stdout.channel.exit_status_ready():
            user_state = get_or_create_user_state(client_id)
            if not user_state.get("running", False):
                await log_callback("Test stopped by user", "warning")
                try:
                    ssh.exec_command("pkill -f 'run_GMS_Test_Auto.sh'")
                except Exception:
                    pass
                break

            if stdout.channel.recv_ready():
                try:
                    data = stdout.channel.recv(65536).decode("utf-8", errors="replace")
                    if data:
                        for line in data.split("\n"):
                            if line.strip():
                                await log_callback(line.strip(), "info")
                except Exception as e:
                    logger.error(f"Error reading stdout: {e}")

            if stderr.channel.recv_stderr_ready():
                try:
                    error_data = stderr.channel.recv_stderr(65536).decode("utf-8", errors="replace")
                    if error_data:
                        for line in error_data.split("\n"):
                            if line.strip():
                                await log_callback(line.strip(), "error")
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")

            await asyncio.sleep(0.05)

        exit_code = stdout.channel.recv_exit_status()

        if stdout.channel.recv_ready():
            try:
                remaining_data = stdout.channel.recv(65536).decode("utf-8", errors="replace")
                if remaining_data:
                    for line in remaining_data.split("\n"):
                        if line.strip():
                            await log_callback(line.strip(), "info")
            except Exception as e:
                logger.error(f"Error reading remaining stdout: {e}")

        if stderr.channel.recv_stderr_ready():
            try:
                remaining_error = stderr.channel.recv_stderr(65536).decode("utf-8", errors="replace")
                if remaining_error:
                    for line in remaining_error.split("\n"):
                        if line.strip():
                            await log_callback(line.strip(), "error")
            except Exception as e:
                logger.error(f"Error reading remaining stderr: {e}")

        if exit_code == 0:
            await log_callback(f"Test completed successfully (exit code: {exit_code})", "success")
        else:
            await log_callback(f"Test failed with exit code: {exit_code}", "error")

    except Exception as e:
        logger.error(f"Error in _run_test_background: {e}")
        await log_callback(f"Test execution error: {str(e)}", "error")

    finally:
        try:
            user_state = get_or_create_user_state(client_id)
            user_logs = user_state.get("logs", [])
            report_timestamp = save_test_report_to_db(client_id, config, test_params, user_logs)
            if report_timestamp:
                await log_callback(f"Test report recorded: {report_timestamp}", "success")
        except Exception as e:
            logger.error(f"Failed to save test report: {e}")

        if ssh:
            ssh_manager.return_connection(ssh)

        await release_device_locks(client_id, locked_devices)
        logger.info(f"[Device Lock] Test completed, device unlock broadcast: {locked_devices}")

        update_user_state_field(client_id, {"running": False, "devices": []})

        notification = store_notification(
            client_id,
            "Test task completed",
            "Test execution has completed, please check logs and reports.",
            "info",
            "test",
            {"devices": locked_devices},
        )

        await safe_websocket_send(client_id, {"type": "test_complete", "notification": notification})


# ==================== Stop Test ====================

@router.post("/api/test/stop")
async def stop_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
):
    resp = generate_help_or_continue(help, "POST", "/api/test/stop")
    if resp:
        return resp

    client_id = get_client_id_from_request(request)
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
        for device_id in devices_to_release:
            device_lock_manager.unlock_device(device_id, client_id)

        logger.info(f"[TestStop] Broadcasting device unlock for: {devices_to_release}")
        await broadcast_device_lock_update(devices_to_release)

    update_user_state_field(client_id, {"devices": []})

    config = config_manager.load_config()
    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return error_response("SSH connection failed", 500)

    try:
        killed_count = 0

        if process_group_id:
            find_cmd = f"ps eww -e | grep 'GMS_TEST_PGID={process_group_id}' | grep -v grep | awk '{{print $1}}'"
            user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminating test process group: {process_group_id}...")

            output, error, code = ssh_manager.execute_command(ssh, find_cmd, timeout=10)
            if output.strip():
                pids = output.strip().split("\n")
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                await asyncio.sleep(1)
                user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminated {killed_count} test processes")
                ssh_manager.return_connection(ssh)
                return success_response(message="Test stopped")

            fallback_cmd = f"ps aux | grep -- '--pgid {process_group_id}' | grep -v grep | awk '{{print $2}}'"
            output2, error2, code2 = ssh_manager.execute_command(ssh, fallback_cmd, timeout=10)
            if output2.strip():
                pids = output2.strip().split("\n")
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                await asyncio.sleep(1)
                user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Terminated {killed_count} test processes (command match)")
                ssh_manager.return_connection(ssh)
                return success_response(message="Test stopped")

        user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] No test process found (may have already stopped or manual test)")
        ssh_manager.return_connection(ssh)
        return success_response(message="Test stopped (no running test process found)")

    except Exception as e:
        ssh_manager.return_connection(ssh)
        user_state["logs"].append(f"[{datetime.now().strftime('%H:%M:%S')}] Error stopping test: {str(e)}")
        logger.error(f"Error stopping test: {e}")
        return error_response(str(e), 500)


# ==================== Clean Logs ====================

@router.post("/api/test/clean")
async def clean_test_logs(request: Request):
    """Clean current user test logs."""
    try:
        client_id = get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)
        user_state["logs"] = []
        update_user_state_field(client_id, {"logs": []})
        logger.info(f"[Clean Logs] User {client_id} cleared test logs")
        return success_response(message="Logs cleared")
    except Exception as e:
        logger.error(f"Error cleaning logs: {e}")
        raise HTTPException(status_code=500, detail=f"{str(e)}")


# ==================== Get Logs ====================

@router.get("/api/test/logs/get")
async def get_test_logs(request: Request):
    """Get test logs (view or download)."""
    try:
        client_id = get_client_id_from_request(request)
        log_file = global_state.last_saved_log_file.get(client_id)

        if not log_file or not os.path.exists(log_file):
            logs_dir = Path(os.path.join(PROJECT_ROOT, "logs"))
            if logs_dir.exists():
                existing_files = [(f, f.stat().st_mtime) for f in logs_dir.glob("*.log") if f.exists()]
                if existing_files:
                    log_file = str(max(existing_files, key=lambda x: x[1])[0])

        if not log_file or not os.path.exists(log_file):
            user_state = get_or_create_user_state(client_id)
            log_file = user_state.get("log_file")

        if not log_file or not os.path.exists(log_file):
            raise HTTPException(status_code=404, detail="No log file available")

        filename = os.path.basename(log_file)
        return FileResponse(log_file, media_type="text/plain", filename=filename)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting test logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Batch Download Logs ====================

@router.post("/api/test/logs/batch")
async def download_test_logs(req: dict):
    """Batch download test logs (ZIP)."""
    try:
        file_paths = req.get("files", [])
        if not file_paths:
            raise HTTPException(status_code=400, detail="No files selected")

        result = test_logs_manager.download_logs(file_paths)
        if result["success"]:
            return FileResponse(
                result["zip_path"],
                media_type="application/zip",
                filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            )
        else:
            raise HTTPException(status_code=500, detail=result["error"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Save Log ====================

@router.post("/api/test/logs/save")
async def save_current_log(req: dict):
    """Save current log."""
    log_content = req.get("content", "")
    client_id = req.get("client_id", "test_client")
    test_type = req.get("test_type", "").strip()

    if not log_content:
        raise HTTPException(status_code=400, detail="No log content provided")

    try:
        logs_dir = os.path.join(PROJECT_ROOT, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config = config_manager.load_config()

        display_test_type = "MANUAL" if not test_type or test_type.lower() == "unknown" else test_type.upper()

        if client_id == "test_client":
            user_id = config_manager.get_ubuntu_user(config)
        else:
            user_id = parse_client_id(client_id)[0] if "@" in client_id else client_id

        log_filename = f"{user_id}_{display_test_type}_{timestamp}.log"
        log_path = os.path.join(logs_dir, log_filename)

        log_file = Path(log_path)
        log_file.write_text(
            f"GMS Test Log - {display_test_type}\n"
            f"Saved: {timestamp}\n"
            f"User: {user_id}\n"
            f"Client ID: {client_id}\n"
            f"{'=' * 80}\n\n"
            f"{log_content}",
            encoding="utf-8",
        )

        global_state.last_saved_log_file[client_id] = str(log_file)

        return JSONResponse(
            content={
                "success": True,
                "log_file": str(log_file),
                "filename": log_filename,
                "message": f"Log saved: {log_filename}",
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving log: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== List Logs ====================

@router.get("/api/test/logs/list")
async def list_test_logs():
    """List test logs."""
    try:
        result = test_logs_manager.list_log_files()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error listing logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Suite helpers ====================

def _get_available_test_suites(config: Dict[str, Any], base_path: Optional[str] = None) -> List[Dict[str, str]]:
    """Return all test suites visible to the current host/config."""
    base_path = base_path or config.get("suites_path") or get_default_suites_path(config)
    if is_config_host_local(config):
        return list_local_test_suites(base_path)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        raise RuntimeError("SSH connection failed")

    try:
        find_cmd = f"find {shlex.quote(base_path)} -maxdepth 5 -type f -executable -name '*-tradefed' 2>/dev/null | sort"
        output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=30)
        suites = []
        if output.strip():
            for line in output.strip().split("\n"):
                suite = build_suite_info(line)
                if suite:
                    suites.append(suite)
        return suites
    finally:
        ssh_manager.return_connection(ssh)


def _normalize_suite_match_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _normalize_suite_version_text(version: str) -> str:
    version = (version or "").strip().lower()
    if not version:
        return ""
    for old, new in (("android-", ""), ("cts-verifier", "ctsverifier"), ("cts-v", "ctsv")):
        version = version.replace(old, new)
    return _normalize_suite_match_text(version)


def _score_suite_version_match(suite_version: str, report_version: str) -> int:
    suite_norm = _normalize_suite_version_text(suite_version)
    report_norm = _normalize_suite_version_text(report_version)
    if not suite_norm or not report_norm:
        return 0
    if suite_norm == report_norm:
        return 100
    if report_norm in suite_norm or suite_norm in report_norm:
        return 80
    return 0


def _build_apk_source_path_guess(test_name: str, class_names: Optional[List[str]] = None) -> Dict[str, Any]:
    class_names = [c for c in (class_names or []) if c]
    candidate = class_names[0] if class_names else ""
    if not candidate and test_name and "#" in test_name:
        candidate = test_name.split("#", 1)[0]

    simple_class = (candidate or "").split("$")[0].strip()
    if not simple_class or "." not in simple_class:
        return {"source_path": "", "class_name": simple_class, "line_number": 0}

    return {"source_path": f"{simple_class.replace('.', '/')}.java", "class_name": simple_class, "line_number": 0}


def _build_artifact_candidate(full_path: str, suite_root: str, include_size: bool = False) -> Dict[str, Any]:
    name = os.path.basename(full_path)
    lower = name.lower()
    entry = {"name": name, "path": os.path.relpath(full_path, suite_root), "full_path": full_path, "is_apk": lower.endswith(".apk"), "is_jar": lower.endswith(".jar")}
    if include_size:
        entry["size"] = os.path.getsize(full_path) if os.path.exists(full_path) else 0
    return entry


def _collect_suite_artifact_candidates_local(suite_root: str, max_results: int = 200) -> List[Dict[str, Any]]:
    candidates = []
    if not os.path.isdir(suite_root):
        return candidates
    for root, _, files in os.walk(suite_root):
        for file_name in files:
            lower = file_name.lower()
            if not (lower.endswith(".apk") or lower.endswith(".jar")):
                continue
            full_path = os.path.join(root, file_name)
            candidates.append(_build_artifact_candidate(full_path, suite_root, include_size=True))
            if len(candidates) >= max_results:
                return candidates
    return candidates


def _collect_suite_artifact_candidates_remote(ssh, suite_root: str, max_results: int = 200) -> List[Dict[str, Any]]:
    find_cmd = f"find {shlex.quote(suite_root)} -type f \\( -iname '*.apk' -o -iname '*.jar' \\) 2>/dev/null | sort"
    output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=45)
    candidates = []
    if not output.strip():
        return candidates
    for line in output.strip().split("\n"):
        full_path = line.strip()
        if not full_path:
            continue
        candidates.append(_build_artifact_candidate(full_path, suite_root))
        if len(candidates) >= max_results:
            break
    return candidates


def _collect_preferred_suite_artifact_candidates_local(suite_root: str, module: str, max_results: int = 20) -> List[Dict[str, Any]]:
    candidates = []
    if not module or not os.path.isdir(suite_root):
        return candidates
    seen = set()
    patterns = [os.path.join(suite_root, "**", f"{module}.apk"), os.path.join(suite_root, "**", f"{module}.jar")]
    for pattern in patterns:
        for full_path in glob.glob(pattern, recursive=True):
            if not os.path.isfile(full_path):
                continue
            normalized = os.path.abspath(full_path)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(_build_artifact_candidate(normalized, suite_root, include_size=True))
            if len(candidates) >= max_results:
                return candidates
    return candidates


def _collect_preferred_suite_artifact_candidates_remote(ssh, suite_root: str, module: str, max_results: int = 20) -> List[Dict[str, Any]]:
    candidates = []
    if not module:
        return candidates
    apk_name = shlex.quote(f"{module}.apk")
    jar_name = shlex.quote(f"{module}.jar")
    find_cmd = f"find {shlex.quote(suite_root)} -type f \\( -iname {apk_name} -o -iname {jar_name} \\) 2>/dev/null | sort"
    output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=45)
    if not output.strip():
        return candidates
    seen = set()
    for line in output.strip().split("\n"):
        full_path = line.strip()
        if not full_path:
            continue
        normalized = os.path.abspath(full_path)
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(_build_artifact_candidate(normalized, suite_root))
        if len(candidates) >= max_results:
            break
    return candidates


def _score_suite_artifact_candidate(candidate: Dict[str, Any], search_terms: List[str], suite_version: str = "", test_type: str = "", module: str = "", source_path: str = "") -> Dict[str, Any]:
    haystack = _normalize_suite_match_text(" ".join([candidate.get("name", ""), candidate.get("path", ""), suite_version, test_type]))
    score = 0
    reasons = []
    candidate_path = (candidate.get("path", "") or "").replace("\\", "/").lower()
    candidate_name = (candidate.get("name", "") or "").lower()
    module_name = (module or "").strip()
    module_norm = _normalize_suite_match_text(module_name)

    if candidate.get("is_apk"):
        score += 5
        reasons.append("apk")
    if candidate.get("is_jar"):
        score += 3
        reasons.append("jar")

    for term in search_terms:
        norm = _normalize_suite_match_text(term)
        if not norm:
            continue
        if norm in haystack:
            score += 25 + min(len(norm), 20)
            reasons.append(term)
            continue
        token_parts = [part for part in re.split(r"[^a-zA-Z0-9]+", term) if len(part) >= 4]
        if any(_normalize_suite_match_text(part) in haystack for part in token_parts):
            score += 10
            reasons.append(term)

    if module_norm:
        if module_norm in _normalize_suite_match_text(candidate_name) or module_norm in haystack:
            score += 35
            reasons.append(f"module:{module_name}")
        if candidate_path.endswith(f"/{module_name.lower()}.apk") or candidate_path.endswith(f"/{module_name.lower()}.jar"):
            score += 60
            reasons.append("module-binary")
        elif f"/{module_name.lower()}/" in candidate_path:
            score += 45
            reasons.append("module-path")

    if source_path:
        source_norm = _normalize_suite_match_text(source_path)
        if source_norm and source_norm in haystack:
            score += 20
            reasons.append("source-path")

    if "testcase" in candidate_name or "testcases" in candidate_name:
        score += 10
    if "android" in candidate.get("path", "").lower():
        score += 2

    scored = dict(candidate)
    scored["score"] = score
    scored["reasons"] = list(dict.fromkeys(reasons))[:8]
    return scored


_SUITE_TYPE_ALIASES: Dict[str, set] = {"cts": {"cts", "cts-v"}, "gts-root": {"gts"}}


def _canonical_suite_types(test_type: str) -> set:
    test_type = (test_type or "").strip().lower()
    if test_type in _SUITE_TYPE_ALIASES:
        return _SUITE_TYPE_ALIASES[test_type]
    return {test_type}


def _make_empty_suite_target(test_type: str = "", suite_version: str = "", suite_path: str = "", suite_root: str = "", suite_name: str = "", test_name: str = "", class_names: Optional[List[str]] = None, match_notes: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "test_type": test_type, "suite_version": suite_version, "suite_path": suite_path,
        "suite_root": suite_root, "suite_name": suite_name, "suite_candidates": [],
        "artifact": None, "artifact_confidence": 0, "artifact_candidates": [],
        "source_guess": _build_apk_source_path_guess(test_name, class_names),
        "match_notes": match_notes or [],
    }


def _extract_suite_artifact_terms(source_path: str = "", module: str = "", test_name: str = "", class_names: Optional[List[str]] = None) -> List[str]:
    terms: List[str] = []
    def add(term: str):
        term = (term or "").strip()
        if term and term not in terms:
            terms.append(term)
    add(module)
    add(test_name)
    for class_name in (class_names or [])[:3]:
        add(class_name)
    normalized_source = (source_path or "").replace("\\", "/").strip("/")
    if not normalized_source:
        return terms
    source_parts = [part for part in normalized_source.split("/") if part]
    if not source_parts:
        return terms
    file_name = source_parts[-1]
    file_stem = os.path.splitext(file_name)[0]
    add(file_name)
    add(file_stem)
    package_parts = source_parts[:-1]
    if len(package_parts) >= 2:
        tail_parts = package_parts[-3:]
        for size in range(2, len(tail_parts) + 1):
            suffix = tail_parts[-size:]
            add("".join(suffix))
            add("".join(reversed(suffix)))
    return terms


def _resolve_suite_diagnosis_target(config: Dict[str, Any], *, test_type: str = "", suite_version: str = "", module: str = "", test_name: str = "", class_names: Optional[List[str]] = None, suite_path: str = "", source_path: str = "") -> Dict[str, Any]:
    available_suites = _get_available_test_suites(config)
    class_names = [c for c in (class_names or []) if c]
    source_path = (source_path or "").strip() or _build_apk_source_path_guess(test_name, class_names).get("source_path", "")
    search_terms = _extract_suite_artifact_terms(source_path, module, test_name, class_names)
    normalized_type = (test_type or "").strip().lower()
    normalized_version = (suite_version or "").strip()
    normalized_suite_path = (suite_path or "").strip()
    canonical_types = _canonical_suite_types(normalized_type) if normalized_type else set()

    def suite_matches(suite: Dict[str, Any]) -> bool:
        if normalized_suite_path and suite.get("tools_path") != normalized_suite_path:
            return False
        suite_type = (suite.get("test_type") or "").strip().lower()
        if normalized_type and suite_type not in canonical_types:
            return False
        if normalized_version and _score_suite_version_match(suite.get("version", ""), normalized_version) <= 0 and normalized_suite_path:
            return False
        return True

    filtered_suites = [suite for suite in available_suites if suite_matches(suite)]
    if not filtered_suites:
        filtered_suites = available_suites[:20]

    suite_ranked = []
    for suite in filtered_suites:
        score = 0
        reasons = []
        suite_type = (suite.get("test_type") or "").strip().lower()
        version_score = _score_suite_version_match(suite.get("version", ""), normalized_version)
        if normalized_type and suite_type == normalized_type:
            score += 60
            reasons.append(f"type:{normalized_type}")
        elif normalized_type and suite_type in canonical_types:
            score += 50
            reasons.append(f"type:{suite_type}")
        if version_score:
            score += version_score
            reasons.append(f"version:{suite.get('version', '')}")
        suite_text = " ".join([suite.get("full_path", ""), suite.get("tools_path", ""), suite.get("binary", "")])
        if module and _normalize_suite_match_text(module) in _normalize_suite_match_text(suite_text):
            score += 45
            reasons.append(f"module:{module}")
        if normalized_suite_path and suite.get("tools_path") == normalized_suite_path:
            score += 100
            reasons.append("suite_path")
        suite_ranked.append((score, reasons, suite))

    suite_ranked.sort(key=lambda item: (item[0], item[2].get("version", ""), item[2].get("full_path", "")), reverse=True)
    best_suite = suite_ranked[0][2] if suite_ranked else None
    best_tools_path = best_suite.get("tools_path", "") if best_suite else ""
    best_suite_root = ""
    if best_tools_path:
        best_suite_root = best_tools_path[:-len("/tools")] if best_tools_path.endswith("/tools") else best_tools_path

    target = _make_empty_suite_target(
        test_type=normalized_type, suite_version=normalized_version,
        suite_path=best_tools_path, suite_root=best_suite_root,
        suite_name=(best_suite.get("version") or best_suite.get("binary") or best_tools_path) if best_suite else "",
        test_name=test_name, class_names=class_names,
    )
    target["suite_candidates"] = [item[2] for item in suite_ranked[:5]]
    if not best_suite:
        target["match_notes"].append("No matching test suite found")
        return target

    target["match_notes"].append(f"Selected suite: {best_tools_path}")
    if best_suite.get("version"):
        target["match_notes"].append(f"Suite version: {best_suite.get('version')}")

    preferred_artifact_candidates = []
    is_local = is_config_host_local(config)
    ssh = None if is_local else ssh_manager.get_connection(config)
    try:
        if module:
            if is_local:
                preferred_artifact_candidates = _collect_preferred_suite_artifact_candidates_local(best_suite_root, module)
            elif ssh:
                preferred_artifact_candidates = _collect_preferred_suite_artifact_candidates_remote(ssh, best_suite_root, module)
        if is_local:
            artifact_candidates = _collect_suite_artifact_candidates_local(best_suite_root)
        elif ssh:
            artifact_candidates = _collect_suite_artifact_candidates_remote(ssh, best_suite_root)
        else:
            raise RuntimeError("SSH connection failed")
    except Exception as e:
        logger.warning(f"[TestSuites] Artifact search failed: {e}")
        target["match_notes"].append(f"Artifact search failed: {e}")
        artifact_candidates = []
        preferred_artifact_candidates = []
    finally:
        if ssh:
            ssh_manager.return_connection(ssh)

    if preferred_artifact_candidates:
        preferred_map = {item.get("full_path", ""): item for item in preferred_artifact_candidates if item.get("full_path")}
        merged_candidates = list(preferred_artifact_candidates)
        for candidate in artifact_candidates:
            full_path = candidate.get("full_path", "")
            if full_path and full_path in preferred_map:
                continue
            merged_candidates.append(candidate)
        artifact_candidates = merged_candidates

    ranked_candidates = [
        _score_suite_artifact_candidate(candidate, search_terms, suite_version=best_suite.get("version", ""), test_type=best_suite.get("test_type", ""), module=module, source_path=source_path)
        for candidate in artifact_candidates
    ]
    ranked_candidates.sort(key=lambda item: (item.get("score", 0), item.get("size", 0)), reverse=True)
    target["artifact_candidates"] = ranked_candidates[:10]
    if ranked_candidates:
        target["artifact_confidence"] = int(ranked_candidates[0].get("score", 0))
        if target["artifact_confidence"] >= 50:
            target["artifact"] = ranked_candidates[0]
            target["match_notes"].append(f"Artifact: {ranked_candidates[0].get('path', '')}")
        else:
            target["match_notes"].append("No high-confidence APK/JAR artifact found, please locate manually in suite directory")
    else:
        target["match_notes"].append("No APK/JAR artifact candidates found")

    return target


# ==================== List Suites ====================

@router.get("/api/test/suites")
async def list_suites(base_path: str = None):
    """List all available test suites."""
    try:
        config = config_manager.load_config()
        base_path = base_path or config.get("suites_path") or get_default_suites_path(config)
        suites = _get_available_test_suites(config, base_path)
        return JSONResponse(content={
            "success": True, "suites": suites, "count": len(suites),
            "base_path": base_path, "source": "local" if is_config_host_local(config) else "ssh",
        })
    except Exception as e:
        logger.error(f"Error listing suites: {e}")
        return error_response(str(e), 500)


# ==================== Diagnose Target ====================

@router.post("/api/test/suites/diagnose-target")
@handle_api_errors
async def diagnose_suite_target(req: SuiteDiagnosisTargetRequest):
    """Locate the most likely suite artifact and source path for a report failure."""
    try:
        target = await asyncio.to_thread(
            _resolve_suite_diagnosis_target,
            config_manager.load_config(),
            test_type=req.test_type, suite_version=req.suite_version,
            module=req.module, test_name=req.test_name,
            class_names=req.class_names, suite_path=req.suite_path,
        )
        return ApiResponse.success(target)
    except Exception as e:
        logger.error(f"[TestSuites] Diagnosis target failed: {e}", exc_info=True)
        return ApiResponse.error(f"Targeting failed: {e}", status_code=500)


# ==================== Suite File Browsing ====================

_SUITE_SCRIPT_PREAMBLE = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
if target != root and not target.startswith(root + os.sep):
    emit({"success": False, "error": "Illegal path"})
    sys.exit(0)
"""

SUITE_FILE_LIST_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isdir(target):
    emit({"success": False, "error": "Directory not found"})
    sys.exit(0)
items = []
for name in sorted(os.listdir(target), key=lambda n: n.lower()):
    full_path = os.path.join(target, name)
    try:
        real_path = os.path.realpath(full_path)
        if real_path != root and not real_path.startswith(root + os.sep):
            continue
        st = os.stat(full_path)
        is_dir = os.path.isdir(full_path)
        rel = os.path.relpath(full_path, root)
        items.append({"name": name, "path": "" if rel == "." else rel, "type": "directory" if is_dir else "file", "size": 0 if is_dir else st.st_size, "modified": int(st.st_mtime), "is_apk": (not is_dir) and name.lower().endswith(".apk"), "is_jar": (not is_dir) and name.lower().endswith(".jar")})
    except OSError:
        continue
items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
emit({"success": True, "path": "" if target == root else os.path.relpath(target, root), "root": root, "items": items})
"""

SUITE_FILE_INFO_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isfile(target):
    emit({"success": False, "error": "File not found"})
    sys.exit(0)
st = os.stat(target)
name_lower = target.lower()
emit({"success": True, "real_path": target, "name": os.path.basename(target), "size": st.st_size, "modified": int(st.st_mtime), "is_apk": name_lower.endswith(".apk"), "is_jar": name_lower.endswith(".jar")})
"""


def _normalize_suite_relative_path(path: Optional[str]) -> str:
    rel_path = (path or "").replace("\\", "/").strip().strip("/")
    if not rel_path:
        return ""
    parts = [part for part in rel_path.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("Illegal path")
    return "/".join(parts)


def _get_suite_root_from_path(suite_path: str, config: Dict[str, Any]) -> str:
    raw_path = (suite_path or "").replace("\\", "/").strip().rstrip("/")
    if not raw_path or not raw_path.startswith("/"):
        raise ValueError("Invalid test suite path")
    suite_root = raw_path[:-len("/tools")] if raw_path.endswith("/tools") else raw_path
    suite_root = suite_root.rstrip("/")
    if not suite_root:
        raise ValueError("Invalid test suite path")
    base_path = (config.get("suites_path") or "").replace("\\", "/").strip().rstrip("/")
    if base_path.startswith("/") and not (suite_root == base_path or suite_root.startswith(base_path + "/")):
        raise ValueError("Test suite not in configured suites directory")
    return suite_root


def _build_suite_remote_path(suite_path: str, path: Optional[str], config: Dict[str, Any]) -> tuple:
    suite_root = _get_suite_root_from_path(suite_path, config)
    rel_path = _normalize_suite_relative_path(path)
    remote_path = suite_root if not rel_path else f"{suite_root}/{rel_path}"
    return suite_root, rel_path, remote_path


def _run_suite_file_script(ssh, script: str, suite_root: str, remote_path: str, timeout: int = 20) -> Dict[str, Any]:
    cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(suite_root)} {shlex.quote(remote_path)}"
    output, error, code = ssh_manager.execute_command(ssh, cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(error.strip() or output.strip() or "Remote file operation failed")
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Remote file response parse failed: {e}")


@router.get("/api/test/suites/files")
@handle_api_errors
async def list_suite_files(suite_path: str = Query(...), path: str = Query("")):
    """Browse test suite directory files."""
    config = config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        payload = _run_suite_file_script(ssh, SUITE_FILE_LIST_SCRIPT, suite_root, remote_path)
        if not payload.get("success"):
            return ApiResponse.error(payload.get("error", "Directory read failed"), status_code=400)
        return ApiResponse.success({"suite_path": suite_path, "suite_root": suite_root, "path": payload.get("path", rel_path), "items": payload.get("items", [])})
    finally:
        ssh_manager.return_connection(ssh)


@router.get("/api/test/suites/download")
@handle_api_errors
async def download_suite_file(suite_path: str = Query(...), path: str = Query(...)):
    """Download a specified file from test suite directory."""
    config = config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get("success"):
            ssh_manager.return_connection(ssh)
            return ApiResponse.error(info.get("error", "File not found"), status_code=404)

        sftp = ssh.open_sftp()
        remote_file = sftp.open(info["real_path"], "rb")
    except Exception:
        ssh_manager.return_connection(ssh)
        raise

    filename = info.get("name") or os.path.basename(remote_path) or "download"
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download"
    quoted_filename = urllib.parse.quote(filename)
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def iter_remote_file():
        try:
            while True:
                chunk = remote_file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            finally:
                try:
                    sftp.close()
                finally:
                    ssh_manager.return_connection(ssh)

    return StreamingResponse(
        iter_remote_file(),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}',
            "Content-Length": str(info.get("size", 0)),
        },
    )


@router.post("/api/test/suites/apk/analyze")
@handle_api_errors
async def create_suite_apk_analysis_task(req: SuiteApkAnalyzeRequest):
    """Copy an APK from test suite for APK analysis."""
    from core.apk import _create_apk_task, _normalize_apk_filename, _safe_join, _cleanup_files

    config = config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(req.suite_path, req.path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    task_id = str(uuid.uuid4())
    sftp = None
    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get("success"):
            return ApiResponse.error(info.get("error", "File not found"), status_code=404)
        if not (info.get("is_apk") or info.get("is_jar")):
            return ApiResponse.error("Only APK/JAR files supported for decompilation", status_code=400)
        if int(info.get("size", 0)) > APK_MAX_FILE_SIZE:
            return ApiResponse.error(f"File too large, max {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

        filename = _normalize_apk_filename(info.get("name") or os.path.basename(remote_path))
        task_dir = _safe_join(APK_UPLOAD_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        apk_path = _safe_join(task_dir, filename)

        sftp = ssh.open_sftp()
        await asyncio.to_thread(sftp.get, info["real_path"], apk_path)

        if os.path.getsize(apk_path) > APK_MAX_FILE_SIZE:
            _cleanup_files([apk_path])
            return ApiResponse.error(f"File too large, max {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

        _create_apk_task(task_id, apk_path, filename)
        return ApiResponse.success({"task_id": task_id, "filename": filename, "size": os.path.getsize(apk_path), "source_path": req.path})
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        ssh_manager.return_connection(ssh)


# ==================== Suite Download Tasks ====================

def _update_suite_task(tasks_dict: dict, lock, task_id: str, **updates):
    with lock:
        task = tasks_dict.get(task_id)
        if task:
            task.update(updates)
            task["updated_at"] = time.time()


def _update_suite_download_task(task_id: str, **updates):
    _update_suite_task(global_state.suite_download_tasks, global_state.suite_download_tasks_lock, task_id, **updates)


def _update_suite_extract_task(task_id: str, **updates):
    _update_suite_task(global_state.suite_extract_tasks, global_state.suite_extract_tasks_lock, task_id, **updates)


_SUITE_TASK_TTL = 3600
_last_suite_cleanup = 0.0


def _cleanup_old_suite_tasks():
    global _last_suite_cleanup
    now = time.time()
    if now - _last_suite_cleanup < 60:
        return
    _last_suite_cleanup = now
    cutoff = now - _SUITE_TASK_TTL
    for tasks, lock in (
        (global_state.suite_download_tasks, global_state.suite_download_tasks_lock),
        (global_state.suite_extract_tasks, global_state.suite_extract_tasks_lock),
    ):
        with lock:
            stale = [tid for tid, t in tasks.items() if t.get("status") in ("completed", "error") and t.get("updated_at", 0) < cutoff]
            for tid in stale:
                del tasks[tid]


def _parse_curl_size(s: str) -> float:
    s = s.rstrip(",").lower()
    if s.endswith("g"):
        return float(s[:-1]) * 1024 ** 3
    if s.endswith("m"):
        return float(s[:-1]) * 1024 ** 2
    if s.endswith("k"):
        return float(s[:-1]) * 1024
    return float(s)


async def _run_suite_download_task(task_id: str, url: str, archive_path: str):
    part_path = archive_path + ".part"
    filename = os.path.basename(archive_path)
    cmd = ["curl", "-L", "-C", "-", "--connect-timeout", "30", "--max-time", "7200", "--retry", "3", "--retry-delay", "5", "-o", part_path, url]
    _update_suite_download_task(task_id, status="downloading", progress=0, message=f"Downloading: {filename}")

    process = None
    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buffer = ""
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="ignore")
            while "\r" in buffer:
                line, buffer = buffer.split("\r", 1)
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        pct = float(parts[0])
                        total = _parse_curl_size(parts[1])
                        speed = _parse_curl_size(parts[-1])
                        downloaded = total * pct / 100
                        progress = pct if pct > 0 else 0
                        _update_suite_download_task(task_id, progress=min(progress, 99.0), downloaded_size=int(downloaded), total_size=int(total), speed_bps=int(speed))
                    except (ValueError, ZeroDivisionError):
                        pass

        await process.wait()
        if process.returncode != 0:
            if os.path.exists(part_path):
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            _update_suite_download_task(task_id, status="error", progress=0, error=f"Download failed (exit code {process.returncode})")
            return

        os.replace(part_path, archive_path)
        file_size = os.path.getsize(archive_path)
        _update_suite_download_task(task_id, status="completed", progress=100, downloaded_size=file_size, archive_path=archive_path, speed_bps=0, message=f"Download complete: {filename}")
    except Exception as e:
        if process and process.returncode is None:
            process.kill()
        _update_suite_download_task(task_id, status="error", error=f"Download error: {str(e)}")


def _extract_archive_local_with_progress(archive_path: str, extract_dir: str, target_dir_name: str, task_id: Optional[str] = None) -> Dict[str, Any]:
    target_extract_dir = os.path.join(extract_dir, target_dir_name) if target_dir_name else extract_dir
    if target_dir_name:
        os.makedirs(target_extract_dir, exist_ok=True)

    files_count = 0
    _last_pct = -1

    def progress(done: int, total: int):
        nonlocal _last_pct
        if not task_id:
            return
        pct = int(done / total * 100) if total else 0
        if pct == _last_pct:
            return
        _last_pct = pct
        _update_suite_extract_task(task_id, status="extracting", progress=min(float(pct), 99.0), extracted_count=done, total_count=total)

    def _chmod_tradefed(path: str, name: str):
        if name.endswith("-tradefed"):
            ensure_tradefed_executable(path)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zip_ref:
            names = zip_ref.namelist()
            if target_dir_name:
                _, mapped_names = strip_common_archive_root(names)
                total = len([item for item in mapped_names if item[1] and not item[0].endswith("/")])
                for source_name, relative_name in mapped_names:
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(target_extract_dir, relative_name)
                    if source_name.endswith("/"):
                        os.makedirs(target_path, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zip_ref.open(source_name) as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    _chmod_tradefed(target_path, os.path.basename(target_path))
                    files_count += 1
                    progress(files_count, total)
            else:
                total = len(names)
                for member_name in names:
                    zip_ref.extract(member_name, extract_dir)
                    files_count += 0 if member_name.endswith("/") else 1
                    progress(files_count, total)
    elif archive_path.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2")):
        mode = "r:gz" if archive_path.endswith((".tar.gz", ".tgz")) else ("r:bz2" if archive_path.endswith(".tar.bz2") else "r")
        with tarfile.open(archive_path, mode) as tar_ref:
            members = tar_ref.getmembers()
            if target_dir_name:
                names = [m.name for m in members]
                _, mapped_names = strip_common_archive_root(names)
                name_map = dict(mapped_names)
                total = len([m for m in members if m.isfile()])
                for member in members:
                    relative_name = name_map.get(member.name, member.name)
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(target_extract_dir, relative_name)
                    if member.isdir():
                        os.makedirs(target_path, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        src = tar_ref.extractfile(member)
                        if src:
                            with src, open(target_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            _chmod_tradefed(target_path, os.path.basename(target_path))
                            files_count += 1
                            progress(files_count, total)
            else:
                total = len([m for m in members if m.isfile()])
                for member in members:
                    safe_extract_member_path(extract_dir, member.name)
                    tar_ref.extract(member, extract_dir)
                    if member.isfile():
                        extracted = os.path.join(extract_dir, member.name)
                        _chmod_tradefed(extracted, os.path.basename(extracted))
                        files_count += 1
                        progress(files_count, total)
    else:
        cmd = ["tar", "-xf", archive_path, "-C", target_extract_dir if target_dir_name else extract_dir]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "tar extraction failed")

    extracted_name = target_dir_name or derive_suite_dir_name_from_archive(archive_path)
    return {"message": f"Extraction complete: {extracted_name}", "extracted_path": os.path.join(extract_dir, extracted_name), "files_count": files_count, "extract_method": "local"}


async def _run_suite_extract_task(task_id: str, archive_path: str, extract_dir: str, target_dir_name: str):
    try:
        _update_suite_extract_task(task_id, status="extracting", progress=0, message="Extracting...")
        result = await asyncio.to_thread(_extract_archive_local_with_progress, archive_path, extract_dir, target_dir_name, task_id)
        _update_suite_extract_task(task_id, status="completed", progress=100, **result)
    except Exception as e:
        logger.error(f"[Suite Extract] Failed: {e}")
        _update_suite_extract_task(task_id, status="error", error=f"Extraction failed: {str(e)}")


@router.post("/api/test/suites/add-local")
@handle_api_errors
async def add_local_test_suite(req: TestSuiteAddLocalRequest):
    """Add a local test suite path to config."""
    config = config_manager.load_config()
    if not req.path:
        return error_response("Path cannot be empty", 400)

    if is_config_host_local(config):
        if not os.path.exists(req.path):
            return error_response(f"Path not found: {req.path}", 404)
        if not os.path.isdir(req.path):
            return error_response(f"Not a directory: {req.path}", 400)
    else:
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()
        check_cmd = f"[ -d '{req.path}' ] && echo 'exists' || echo 'not_exists'"
        output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=10)
        if output.strip() != "exists":
            return error_response(f"Path not found: {req.path}", 404)

    return JSONResponse(content={"success": True, "message": f"Added local path: {os.path.basename(req.path.rstrip('/'))}", "path": req.path})


@router.post("/api/test/suites/result")
async def list_tradefed_results(
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TradefedListResultsRequest = Body(None),
    force_refresh: bool = Query(False),
):
    """Execute tradefed list results and return test results."""
    from core.tradefed import find_tradefed_binary, execute_tradefed_command, parse_tradefed_list_results

    resp = generate_help_or_continue(help, "POST", "/api/test/suites/result")
    if resp:
        return resp

    if req is None:
        return error_response("Missing request body", 400)

    try:
        config = config_manager.load_config()
        suite_path = req.suite_path
        tradefed_bin = req.tradefed_bin
        logger.info(f"Querying test suite results for {suite_path}")

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            if not tradefed_bin:
                tradefed_bin = find_tradefed_binary(ssh, suite_path)
                if not tradefed_bin:
                    ssh_manager.return_connection(ssh)
                    return error_response(f"No tradefed binary found in {suite_path}", 404)

            output, error, code = execute_tradefed_command(ssh, suite_path, tradefed_bin)
            ssh_manager.return_connection(ssh)

            if code != 0:
                return JSONResponse(content={"success": False, "error": error or f"Command failed with exit code: {code}", "raw_output": output}, status_code=500)

            results = parse_tradefed_list_results(output)
            return JSONResponse(content={"success": True, "results": results, "count": len(results), "raw_output": output, "cached": False})
        except Exception:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing tradefed results: {e}")
        return error_response(str(e), 500)


@router.post("/api/test/suites/download-url")
@handle_api_errors
async def download_test_suite_from_url(req: TestSuiteDownloadRequest):
    """Download test suite from URL."""
    config = config_manager.load_config()
    if not req.url:
        return error_response("Download URL cannot be empty", 400)

    save_dir = req.save_dir or get_default_suites_path(config)
    os.makedirs(save_dir, exist_ok=True)

    filename = sanitize_suite_filename_from_url(req.url)
    archive_path = os.path.join(save_dir, filename)

    if is_config_host_local(config):
        with global_state.suite_download_tasks_lock:
            for existing_task in global_state.suite_download_tasks.values():
                if existing_task.get("archive_path") == archive_path and existing_task.get("status") in {"queued", "downloading"}:
                    return JSONResponse(content={"success": True, "message": f"Download task exists: {filename}", "task_id": existing_task.get("task_id"), "archive_path": archive_path, "file_size": 0, "download_method": "local_async_existing"})

        if is_complete_archive_file(archive_path):
            file_size = os.path.getsize(archive_path)
            return JSONResponse(content={"success": True, "message": f"File already exists: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "local_existing"})
        if os.path.exists(archive_path):
            return error_response(f"Incomplete or corrupt archive exists: {archive_path}", 409)

        task_id = str(uuid.uuid4())
        with global_state.suite_download_tasks_lock:
            global_state.suite_download_tasks[task_id] = {
                "task_id": task_id, "status": "queued", "progress": 0,
                "url": req.url, "filename": filename, "archive_path": archive_path,
                "downloaded_size": 0, "total_size": 0, "message": f"Preparing download: {filename}",
                "created_at": time.time(), "updated_at": time.time(),
            }
        task = asyncio.create_task(_run_suite_download_task(task_id, req.url, archive_path))
        global_state.background_tasks.add(task)
        task.add_done_callback(global_state.background_tasks.discard)
        return JSONResponse(content={"success": True, "message": f"Download started: {filename}", "task_id": task_id, "archive_path": archive_path, "file_size": 0, "download_method": "local_async"})
    else:
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()
        try:
            cmd = f"curl -L -o '{archive_path}' '{req.url}' 2>&1"
            output, exit_code, _ = ssh_manager.execute_command(ssh, cmd, timeout=600)
            if exit_code != 0:
                return error_response(f"Download failed: {output}", 500)
            size_cmd = f"stat -c%s '{archive_path}' 2>/dev/null || stat -f%z '{archive_path}' 2>/dev/null || echo 0"
            size_output, _, _ = ssh_manager.execute_command(ssh, size_cmd, timeout=10)
            file_size = int(size_output.strip())
            return JSONResponse(content={"success": True, "message": f"Download complete: {filename}", "archive_path": archive_path, "file_size": file_size, "download_method": "ssh"})
        finally:
            ssh_manager.return_connection(ssh)


@router.get("/api/test/suites/download-status/{task_id}")
async def get_test_suite_download_status(task_id: str):
    _cleanup_old_suite_tasks()
    with global_state.suite_download_tasks_lock:
        task = dict(global_state.suite_download_tasks.get(task_id) or {})
    if not task:
        return error_response("Download task not found", 404)
    return JSONResponse(content={"success": True, "task": task})


@router.get("/api/test/suites/archives")
async def list_test_suite_archives():
    config = config_manager.load_config()
    base_path = get_default_suites_path(config)
    archive_exts = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar")

    if is_config_host_local(config):
        archives = []
        if os.path.isdir(base_path):
            for name in sorted(os.listdir(base_path), reverse=True):
                path = os.path.join(base_path, name)
                if os.path.isfile(path) and name.endswith(archive_exts):
                    stat = os.stat(path)
                    archives.append({"name": name, "path": path, "size": stat.st_size, "mtime": stat.st_mtime, "default_dir_name": derive_suite_dir_name_from_archive(path)})
        return JSONResponse(content={"success": True, "archives": archives, "base_path": base_path})

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()
    try:
        find_cmd = f"find {shlex.quote(base_path)} -maxdepth 1 -type f \\( -name '*.zip' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.tar.bz2' -o -name '*.tar' \\) -printf '%T@\\t%s\\t%f\\t%p\\n' 2>/dev/null | sort -nr"
        output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=20)
        archives = []
        for line in output.splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                mtime, size, name, path = parts
                archives.append({"name": name, "path": path, "size": int(float(size)) if size else 0, "mtime": float(mtime) if mtime else 0, "default_dir_name": derive_suite_dir_name_from_archive(path)})
        return JSONResponse(content={"success": True, "archives": archives, "base_path": base_path})
    finally:
        ssh_manager.return_connection(ssh)


@router.post("/api/test/suites/extract-start")
@handle_api_errors
async def start_test_suite_extract(req: TestSuiteExtractRequest):
    config = config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path))

    if not is_config_host_local(config):
        return error_response("Background extraction only supports local host mode", 400)

    if not os.path.exists(req.archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)
    if not is_complete_archive_file(req.archive_path):
        return error_response(f"Incomplete or unsupported archive: {req.archive_path}", 400)

    os.makedirs(extract_dir, exist_ok=True)
    task_id = str(uuid.uuid4())
    with global_state.suite_extract_tasks_lock:
        global_state.suite_extract_tasks[task_id] = {
            "task_id": task_id, "status": "queued", "progress": 0,
            "archive_path": req.archive_path, "extract_dir": extract_dir,
            "target_dir_name": target_dir_name, "extracted_count": 0, "total_count": 0,
            "message": f"Preparing extraction: {os.path.basename(req.archive_path)}",
            "created_at": time.time(), "updated_at": time.time(),
        }

    task = asyncio.create_task(_run_suite_extract_task(task_id, req.archive_path, extract_dir, target_dir_name))
    global_state.background_tasks.add(task)
    task.add_done_callback(global_state.background_tasks.discard)
    return JSONResponse(content={"success": True, "task_id": task_id, "message": "Extraction started", "archive_path": req.archive_path, "target_dir_name": target_dir_name})


@router.get("/api/test/suites/extract-status/{task_id}")
async def get_test_suite_extract_status(task_id: str):
    _cleanup_old_suite_tasks()
    with global_state.suite_extract_tasks_lock:
        task = dict(global_state.suite_extract_tasks.get(task_id) or {})
    if not task:
        return error_response("Extract task not found", 404)
    return JSONResponse(content={"success": True, "task": task})


@router.post("/api/test/suites/extract")
@handle_api_errors
async def extract_test_suite_archive(req: TestSuiteExtractRequest):
    """Extract test suite archive."""
    config = config_manager.load_config()
    if not req.archive_path:
        return error_response("Archive path cannot be empty", 400)

    extract_dir = req.extract_dir or get_default_suites_path(config)
    target_dir_name = sanitize_suite_dir_name(req.target_dir_name, derive_suite_dir_name_from_archive(req.archive_path)) if req.target_dir_name else ""

    if is_config_host_local(config) and not os.path.exists(req.archive_path):
        return error_response(f"Archive not found: {req.archive_path}", 404)

    os.makedirs(extract_dir, exist_ok=True)

    if is_config_host_local(config):
        try:
            result = await asyncio.to_thread(_extract_archive_local_with_progress, req.archive_path, extract_dir, target_dir_name)
            return JSONResponse(content={"success": True, **result})
        except Exception as e:
            return error_response(f"Extraction failed: {str(e)}", 500)
    else:
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()
        try:
            remote_extract_dir = os.path.join(extract_dir, target_dir_name) if target_dir_name else extract_dir
            mkdir_cmd = f"mkdir -p {shlex.quote(remote_extract_dir)}"
            ssh_manager.execute_command(ssh, mkdir_cmd, timeout=20)
            cmd = f"tar -xf {shlex.quote(req.archive_path)} -C {shlex.quote(remote_extract_dir)} 2>&1"
            output, exit_code, _ = ssh_manager.execute_command(ssh, cmd, timeout=300)

            if exit_code != 0:
                return error_response(f"Extraction failed: {output}", 500)

            extracted_name = target_dir_name or derive_suite_dir_name_from_archive(req.archive_path)
            extracted_path = os.path.join(extract_dir, extracted_name)
            return JSONResponse(content={"success": True, "message": f"Extraction complete: {extracted_name}", "extracted_path": extracted_path, "extract_method": "ssh"})
        finally:
            ssh_manager.return_connection(ssh)


# ==================== Test Status ====================

@router.get("/api/test/status")
async def get_status(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
):
    """Get test status."""
    resp = generate_help_or_continue(help, "GET", "/api/test/status")
    if resp:
        return resp

    try:
        # Handle USB event queue if available
        try:
            import queue as _queue
            if hasattr(request.app.state, "usb_event_queue"):
                try:
                    while True:
                        event = request.app.state.usb_event_queue.get_nowait()

                        async def _send_usb_event(cid, ws, usb_event=event):
                            try:
                                await ws.send_json(usb_event)
                            except Exception:
                                pass

                        await asyncio.gather(*[_send_usb_event(cid, ws) for cid, ws in list(global_state.websocket_connections.items())])
                except _queue.Empty:
                    pass
        except Exception:
            pass

        client_id = get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)

        logger.info(f"[Status] Client {client_id} running={user_state.get('running', False)}")

        since = request.query_params.get("since")
        include_logs = request.query_params.get("logs", "true").lower() == "true"

        response = {"running": user_state.get("running", False), "devices": user_state.get("devices", [])}

        try:
            from core.usb_monitor import get_usb_monitor
            usb_monitor = get_usb_monitor()
            if usb_monitor:
                response["usb_monitor"] = {"mode": usb_monitor.mode, "running": usb_monitor.is_running, "pyudev_available": usb_monitor.pyudev_available}
        except Exception:
            pass

        if include_logs:
            logs = user_state.get("logs", [])
            if since is not None and since.isdigit():
                since_int = int(since)
                if 0 <= since_int < len(logs):
                    response["logs"] = logs[since_int:]
                    response["log_count"] = len(logs)
                else:
                    response["logs"] = logs
                    response["log_count"] = len(logs)
            else:
                response["logs"] = logs
                response["log_count"] = len(logs)

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Log Stream ====================

@router.get("/api/test/logs/stream")
async def stream_test_logs(request: Request):
    """Stream test logs (plain text format)."""
    client_id = get_client_id_from_request(request)

    async def log_stream():
        try:
            last_log_count = 0
            while True:
                user_state = get_or_create_user_state(client_id)
                running = user_state.get("running", False)
                logs = user_state.get("logs", [])
                current_log_count = len(logs)

                if current_log_count > last_log_count:
                    for i in range(last_log_count, current_log_count):
                        log_entry = logs[i]
                        yield f"{log_entry}\n"
                    last_log_count = current_log_count

                if not running and last_log_count > 0:
                    yield "=== Test complete ===\n"
                    break

                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error in stream: {e}")
            yield f"Error: {str(e)}\n"

    return StreamingResponse(
        log_stream(),
        media_type="text/plain",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "Access-Control-Allow-Origin": "*", "X-Accel-Buffering": "no"},
    )
