"""Durable, owner-scoped firmware chunk staging."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time

from fastapi.responses import JSONResponse

from foundation.responses import error_response
from foundation.uploads import merge_files_to_path, safe_upload_target_path, save_upload_to_path


logger = logging.getLogger(__name__)

MAX_FIRMWARE_UPLOAD_BYTES = 32 * 1024 * 1024 * 1024
MAX_FIRMWARE_CHUNK_BYTES = 128 * 1024 * 1024
MAX_FIRMWARE_CHUNKS = 10_000
MERGE_LOCK_STALE_SECONDS = 60 * 60
LEGACY_STAGED_FILENAME = "merged_firmware.bin"


def safe_upload_token(value: str) -> str:
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:96] or "default"
    if cleaned != raw:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"{cleaned}_{digest}"
    return cleaned


def upload_session_dir(root: str, client_id: str, upload_id: str) -> str:
    return os.path.join(root, safe_upload_token(client_id), safe_upload_token(upload_id))


def cleanup_expired_upload_sessions(root: str, client_id: str, max_age: int) -> None:
    client_dir = os.path.join(root, safe_upload_token(client_id))
    if not os.path.isdir(client_dir):
        return
    cutoff = time.time() - max_age
    with contextlib.suppress(OSError):
        for entry in os.scandir(client_dir):
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    shutil.rmtree(entry.path)
            except OSError:
                logger.debug("Failed to clean expired firmware upload %s", entry.path)


def _metadata_path(session_dir: str) -> str:
    return os.path.join(session_dir, "upload_metadata.json")


def _read_metadata(session_dir: str) -> dict | None:
    try:
        with open(_metadata_path(session_dir), encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, TypeError, json.JSONDecodeError):
        return None


def _read_uploaded_chunks(session_dir: str) -> set[int]:
    try:
        with open(os.path.join(session_dir, "uploaded_chunks.json"), encoding="utf-8") as handle:
            return {int(item) for item in json.load(handle)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def _write_uploaded_chunks(session_dir: str, uploaded_chunks: set[int]) -> None:
    target = os.path.join(session_dir, "uploaded_chunks.json")
    temporary = f"{target}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(sorted(uploaded_chunks), handle)
    os.replace(temporary, target)


def _validate_metadata(
    session_dir: str,
    *,
    file_name: str,
    total_chunks: int,
    file_size: int,
    content_fingerprint: str = "",
) -> str | None:
    if total_chunks <= 0 or total_chunks > MAX_FIRMWARE_CHUNKS:
        return f"total_chunks must be between 1 and {MAX_FIRMWARE_CHUNKS}"
    if file_size <= 0 or file_size > MAX_FIRMWARE_UPLOAD_BYTES:
        return f"file_size must be between 1 and {MAX_FIRMWARE_UPLOAD_BYTES}"
    metadata = {
        "file_name": os.path.basename(file_name),
        "total_chunks": total_chunks,
        "file_size": file_size,
    }
    if content_fingerprint:
        metadata["content_fingerprint"] = content_fingerprint
    path = _metadata_path(session_dir)
    try:
        existing = _read_metadata(session_dir) if os.path.exists(path) else None
        if existing is not None:
            return None if existing == metadata else "Chunk metadata does not match the existing upload session"
        if os.path.exists(path):
            return "Upload session metadata is invalid"
        temporary = f"{path}.{threading.get_ident()}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_metadata(session_dir) != metadata:
                return "Chunk metadata does not match the existing upload session"
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(temporary)
    except OSError:
        return "Upload session metadata is invalid"
    return None


def _chunk_paths(session_dir: str, total_chunks: int) -> list[str]:
    return [os.path.join(session_dir, f"chunk_{idx:05d}") for idx in range(total_chunks)]


def _staged_path(session_dir: str, file_name: str) -> str:
    # Rockchip upgrade_tool uses the filename extension to distinguish a
    # complete update image from a standalone Loader. Keep the source
    # extension instead of publishing every merged upload as a .bin file.
    staged_name = f"staged-{os.path.basename(file_name)}"
    return safe_upload_target_path(session_dir, staged_name, allow_nested=False)


def load_staged_upload(root: str, client_id: str, upload_id: str) -> tuple[dict | None, str | None]:
    if not str(upload_id or "").strip():
        return None, "upload_id is required"
    session_dir = upload_session_dir(root, client_id, upload_id)
    metadata = _read_metadata(session_dir)
    if not metadata:
        return None, "Firmware upload session was not found"
    try:
        expected_size = int(metadata["file_size"])
        file_name = os.path.basename(str(metadata["file_name"]))
        staged_path = _staged_path(session_dir, file_name)
        legacy_path = safe_upload_target_path(
            session_dir,
            LEGACY_STAGED_FILENAME,
            allow_nested=False,
        )
    except (KeyError, TypeError, ValueError):
        return None, "Firmware upload session metadata is invalid"
    if file_name and not os.path.isfile(staged_path) and os.path.isfile(legacy_path):
        # Migrate durable uploads created by the regression without copying or
        # requiring the browser to upload multi-gigabyte firmware again.
        try:
            os.replace(legacy_path, staged_path)
        except OSError:
            return None, "Failed to migrate staged firmware filename"
    if not file_name or not os.path.isfile(staged_path):
        return None, "Firmware upload is not staged"
    if os.path.getsize(staged_path) != expected_size:
        return None, "Staged firmware size mismatch"
    return {
        "path": staged_path,
        "name": file_name,
        "size": expected_size,
        "upload_id": str(upload_id),
        "session_dir": session_dir,
    }, None


def clear_upload_progress(global_state, client_id: str, upload_id: str) -> None:
    with global_state.firmware_upload_progress_lock:
        current = global_state.firmware_upload_progress.get(client_id) or {}
        if not upload_id or current.get("upload_id") == upload_id:
            global_state.firmware_upload_progress.pop(client_id, None)


def remove_staged_upload(staged: dict) -> None:
    session_dir = str(staged.get("session_dir") or "")
    if session_dir:
        with contextlib.suppress(OSError):
            shutil.rmtree(session_dir)


def acquire_burn_lock(staged: dict) -> str | None:
    lock_path = os.path.join(staged["session_dir"], ".burn.lock")
    with contextlib.suppress(OSError):
        if time.time() - os.path.getmtime(lock_path) > MERGE_LOCK_STALE_SECONDS:
            os.remove(lock_path)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
        return lock_path
    except FileExistsError:
        return None


def release_burn_lock(lock_path: str | None) -> None:
    if lock_path:
        with contextlib.suppress(FileNotFoundError):
            os.remove(lock_path)


def _parse_chunk_shape(form) -> tuple[int, int, int, str | None]:
    try:
        total_chunks = int(form.get("total_chunks") or 0)
        file_size = int(form.get("file_size") or 0)
        chunk_size = int(form.get("chunk_size") or 0)
    except (TypeError, ValueError):
        return 0, 0, 0, "Invalid chunk metadata"
    if chunk_size < 0 or chunk_size > MAX_FIRMWARE_CHUNK_BYTES:
        return total_chunks, file_size, chunk_size, "Invalid chunk_size"
    if chunk_size and math.ceil(file_size / chunk_size) != total_chunks:
        return total_chunks, file_size, chunk_size, "Chunk size does not match total_chunks"
    return total_chunks, file_size, chunk_size, None


def _staged_response(staged: dict, total_chunks: int) -> JSONResponse:
    return JSONResponse(content={
        "success": True,
        "upload_complete": True,
        "staged": True,
        "uploaded_chunks": list(range(total_chunks)),
        "chunks_uploaded": total_chunks,
        "total_chunks": total_chunks,
        "progress": 100,
        "uploaded_size": staged["size"],
        "total_size": staged["size"],
        "upload_id": staged["upload_id"],
        "file_name": staged["name"],
    })


async def handle_chunk_upload(form, client_id: str, root: str, global_state, max_age: int):
    upload_id = str(form.get("upload_id") or "").strip()
    file_name = str(form.get("file_name") or "").strip()
    if not upload_id or not file_name:
        return error_response("upload_id and file_name are required for chunk upload", 400), None

    await asyncio.to_thread(cleanup_expired_upload_sessions, root, client_id, max_age)
    session_dir = upload_session_dir(root, client_id, upload_id)
    os.makedirs(session_dir, exist_ok=True)
    total_chunks, file_size, chunk_size, shape_error = _parse_chunk_shape(form)
    if shape_error:
        return error_response(shape_error, 400), None
    content_fingerprint = str(form.get("content_fingerprint") or "").strip().lower()
    if content_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", content_fingerprint):
        return error_response("Invalid content_fingerprint", 400), None
    metadata_error = _validate_metadata(
        session_dir,
        file_name=file_name,
        total_chunks=total_chunks,
        file_size=file_size,
        content_fingerprint=content_fingerprint,
    )
    if metadata_error:
        return error_response(metadata_error, 400), None

    if str(form.get("check_chunks") or "").strip().lower() in {"1", "true", "yes"}:
        staged, _error = load_staged_upload(root, client_id, upload_id)
        if staged:
            return _staged_response(staged, total_chunks), None
        paths = _chunk_paths(session_dir, total_chunks)
        uploaded_chunks = {idx for idx, path in enumerate(paths) if os.path.isfile(path)}
        uploaded_size = sum(os.path.getsize(paths[idx]) for idx in uploaded_chunks)
        return JSONResponse(content={
            "success": True,
            "uploaded_chunks": sorted(uploaded_chunks),
            "chunks_uploaded": len(uploaded_chunks),
            "total_chunks": total_chunks,
            "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2),
            "uploaded_size": uploaded_size,
            "total_size": file_size,
            "upload_id": upload_id,
        }), None

    try:
        chunk_index = int(form.get("chunk_index"))
    except (TypeError, ValueError):
        return error_response("Invalid chunk metadata", 400), None
    if chunk_index < 0 or chunk_index >= total_chunks:
        return error_response("Invalid chunk index", 400), None
    upload_file = form.get("file") or form.get("firmware_file")
    if upload_file is None:
        return error_response("No chunk file provided", 400), None

    chunk_path = os.path.join(session_dir, f"chunk_{chunk_index:05d}")
    temporary = f"{chunk_path}.{threading.get_ident()}.upload"
    expected_size = min(chunk_size, file_size - chunk_index * chunk_size) if chunk_size else 0
    try:
        written = await save_upload_to_path(
            upload_file,
            temporary,
            expected_size or min(MAX_FIRMWARE_CHUNK_BYTES, file_size),
        )
        if expected_size and written != expected_size:
            return error_response("Firmware chunk size mismatch", 400), None
        os.replace(temporary, chunk_path)
    except ValueError as exc:
        return error_response(str(exc), 413), None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(temporary)

    chunk_paths = _chunk_paths(session_dir, total_chunks)
    present = {idx: path for idx, path in enumerate(chunk_paths) if os.path.isfile(path)}
    _write_uploaded_chunks(session_dir, set(present))
    uploaded_size = sum(os.path.getsize(path) for path in present.values())
    with global_state.firmware_upload_progress_lock:
        global_state.firmware_upload_progress[client_id] = {
            "progress": round((len(present) / total_chunks) * 100, 2),
            "filename": file_name,
            "uploaded_size": uploaded_size,
            "total_size": file_size,
            "timestamp": time.time(),
            "stage": "uploading_to_server",
            "upload_id": upload_id,
        }
    if len(present) < total_chunks:
        return JSONResponse(content={
            "success": True,
            "upload_complete": False,
            "chunk_index": chunk_index,
            "chunks_uploaded": len(present),
            "total_chunks": total_chunks,
            "progress": round((len(present) / total_chunks) * 100, 2),
            "upload_id": upload_id,
        }), None

    merge_lock_path = os.path.join(session_dir, ".merge.lock")
    with contextlib.suppress(OSError):
        if time.time() - os.path.getmtime(merge_lock_path) > MERGE_LOCK_STALE_SECONDS:
            os.remove(merge_lock_path)
    try:
        descriptor = os.open(merge_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError:
        return JSONResponse(content={
            "success": True, "upload_complete": False, "merging": True,
            "chunks_uploaded": total_chunks, "total_chunks": total_chunks,
            "progress": 100, "upload_id": upload_id,
        }), None

    merged_path = _staged_path(session_dir, file_name)
    merge_temp = f"{merged_path}.{threading.get_ident()}.merge"
    try:
        await asyncio.to_thread(merge_files_to_path, chunk_paths, merge_temp)
        if os.path.getsize(merge_temp) != file_size:
            return error_response("Merged firmware size mismatch", 400), None
        os.replace(merge_temp, merged_path)
        for path in chunk_paths:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(session_dir, "uploaded_chunks.json"))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(merge_temp)
        with contextlib.suppress(FileNotFoundError):
            os.remove(merge_lock_path)

    staged, staged_error = load_staged_upload(root, client_id, upload_id)
    if not staged:
        return error_response(staged_error or "Firmware staging failed", 500), None
    if str(form.get("stage_only") or "").strip().lower() in {"1", "true", "yes"}:
        clear_upload_progress(global_state, client_id, upload_id)
        return _staged_response(staged, total_chunks), None
    return None, staged
