from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from features.auth import require_authenticated_user_when_auth_required
from features.devices import get_or_create_user_state, update_user_state_field
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from . import runtime
from .logs import test_logs_manager


logger = logging.getLogger(__name__)
router = APIRouter()


def _request_log_scope(request: Request) -> tuple[str, bool]:
    """Resolve the log owner and admin flag.

    Uses an authenticated user when available; in dev/anonymous deployments
    (authentication_required() == False), falls back to the stable anonymous
    client id so log save/get/list still work without a session.
    """
    current_user = require_authenticated_user_when_auth_required(request)
    if current_user:
        return current_user.id, current_user.role == "admin"
    return runtime.get_client_id_from_request(request), False


# ==================== Clean Logs ====================

@router.post("/api/test/clean")
async def clean_test_logs(request: Request):
    """Clean current user test logs."""
    client_id, _is_admin = _request_log_scope(request)
    try:
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
    client_id, is_admin = _request_log_scope(request)
    try:
        log_file = runtime.global_state.last_saved_log_file.get(client_id)

        if not log_file:
            user_state = get_or_create_user_state(client_id)
            log_file = user_state.get("log_file")

        if not log_file:
            return error_response("No log file available", status_code=404)

        try:
            resolved = test_logs_manager.resolve_log_path(
                log_file,
                owner_id=client_id,
                is_admin=is_admin,
            )
        except ValueError:
            return error_response("No log file available", status_code=404)
        if not resolved.is_file():
            return error_response("No log file available", status_code=404)
        return FileResponse(resolved, media_type="text/plain", filename=resolved.name)

    except Exception as e:
        logger.error(f"Error getting test logs: {e}")
        return error_response(str(e), status_code=500)


# ==================== Batch Download Logs ====================

@router.post("/api/test/logs/batch")
async def download_test_logs(req: dict, request: Request):
    """Batch download test logs (ZIP)."""
    owner_id, is_admin = _request_log_scope(request)
    try:
        log_ids = req.get("log_ids", req.get("files", []))
        if not log_ids:
            return error_response("No files selected", status_code=400)

        result = test_logs_manager.download_logs(
            log_ids,
            owner_id=owner_id,
            is_admin=is_admin,
        )
        if result["success"]:
            return FileResponse(
                result["zip_path"],
                media_type="application/zip",
                filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            )
        else:
            status_code = 404 if "不属于当前用户" in result.get("error", "") else 400
            return error_response("Log file not found", status_code=status_code)
    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        return error_response(str(e), status_code=500)


# ==================== Save Log ====================

@router.post("/api/test/logs/save")
async def save_current_log(req: dict, request: Request):
    """Save current log."""
    client_id, _is_admin = _request_log_scope(request)
    log_content = req.get("content", "")
    test_type = req.get("test_type", "").strip()

    if not log_content:
        return error_response("No log content provided", status_code=400)

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        display_test_type = "MANUAL" if not test_type or test_type.lower() == "unknown" else test_type.upper()
        result = test_logs_manager.save_current_log(
            f"GMS Test Log - {display_test_type}\n"
            f"Saved: {timestamp}\n"
            f"User: {client_id}\n"
            f"Client ID: {client_id}\n"
            f"{'=' * 80}\n\n"
            f"{log_content}",
            client_id,
        )
        if not result.get("success"):
            return error_response(result.get("error", "Failed to save log"), status_code=500)

        log_path = result["file_path"]
        log_filename = result["filename"]
        log_id = test_logs_manager.log_id_for_path(log_path)
        runtime.global_state.last_saved_log_file[client_id] = log_path
        update_user_state_field(client_id, {"log_file": log_path})

        return JSONResponse(
            content={
                "success": True,
                "log_id": log_id,
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
async def list_test_logs(request: Request):
    """List test logs."""
    owner_id, is_admin = _request_log_scope(request)
    result = test_logs_manager.list_log_files(
        owner_id=owner_id,
        is_admin=is_admin,
    )
    return JSONResponse(content=result)
