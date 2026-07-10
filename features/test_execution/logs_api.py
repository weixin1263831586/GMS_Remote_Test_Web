from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from features.devices import get_or_create_user_state, update_user_state_field
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from . import runtime
from .logs import test_logs_manager


logger = logging.getLogger(__name__)
router = APIRouter()


# ==================== Clean Logs ====================

@router.post("/api/test/clean")
async def clean_test_logs(request: Request):
    """Clean current user test logs."""
    try:
        client_id = runtime.get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)
        user_state["logs"] = []
        update_user_state_field(client_id, {"logs": []})
        logger.info(f"[Clean Logs] User {client_id} cleared test logs")
        return success_response(message="Logs cleared")
    except Exception as e:
        logger.error(f"Error cleaning logs: {e}")
        return error_response(f"{e!s}", status_code=500)


# ==================== Get Logs ====================

@router.get("/api/test/logs/get")
async def get_test_logs(request: Request):
    """Get test logs (view or download)."""
    try:
        client_id = runtime.get_client_id_from_request(request)
        log_file = runtime.global_state.last_saved_log_file.get(client_id)

        if not log_file or not os.path.exists(log_file):
            logs_dir = Path(os.path.join(runtime.project_root, "logs"))
            if logs_dir.exists():
                existing_files = [(f, f.stat().st_mtime) for f in logs_dir.glob("*.log") if f.exists()]
                if existing_files:
                    log_file = str(max(existing_files, key=lambda x: x[1])[0])

        if not log_file or not os.path.exists(log_file):
            user_state = get_or_create_user_state(client_id)
            log_file = user_state.get("log_file")

        if not log_file or not os.path.exists(log_file):
            return error_response("No log file available", status_code=404)

        filename = os.path.basename(log_file)
        return FileResponse(log_file, media_type="text/plain", filename=filename)

    except Exception as e:
        logger.error(f"Error getting test logs: {e}")
        return error_response(str(e), status_code=500)


# ==================== Batch Download Logs ====================

@router.post("/api/test/logs/batch")
async def download_test_logs(req: dict):
    """Batch download test logs (ZIP)."""
    try:
        file_paths = req.get("files", [])
        if not file_paths:
            return error_response("No files selected", status_code=400)

        result = test_logs_manager.download_logs(file_paths)
        if result["success"]:
            return FileResponse(
                result["zip_path"],
                media_type="application/zip",
                filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            )
        else:
            return error_response(result["error"], status_code=500)
    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        return error_response(str(e), status_code=500)


# ==================== Save Log ====================

@router.post("/api/test/logs/save")
async def save_current_log(req: dict):
    """Save current log."""
    log_content = req.get("content", "")
    client_id = req.get("client_id", "test_client")
    test_type = req.get("test_type", "").strip()

    if not log_content:
        return error_response("No log content provided", status_code=400)

    try:
        logs_dir = os.path.join(runtime.project_root, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config = runtime.config_manager.load_config()

        display_test_type = "MANUAL" if not test_type or test_type.lower() == "unknown" else test_type.upper()

        if client_id == "test_client":
            user_id = runtime.config_manager.get_ubuntu_user(config)
        else:
            user_id = runtime.parse_client_id(client_id)[0] if "@" in client_id else client_id

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

        runtime.global_state.last_saved_log_file[client_id] = str(log_file)

        return JSONResponse(
            content={
                "success": True,
                "log_file": str(log_file),
                "filename": log_filename,
                "message": f"Log saved: {log_filename}",
            }
        )
    except Exception as e:
        logger.error(f"Error saving log: {e}")
        return error_response(str(e), status_code=500)


# ==================== List Logs ====================

@router.get("/api/test/logs/list")
@handle_api_errors
async def list_test_logs():
    """List test logs."""
    result = test_logs_manager.list_log_files()
    return JSONResponse(content=result)
