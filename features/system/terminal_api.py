"""Terminal router - SSH terminal info and file push/upload APIs."""

import asyncio
import json
import logging
import os
import shutil

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from features.auth import CurrentUser, require_elevated_admin
from features.system.ssh import ssh_manager
from features.system.terminal_service import resolve_authorized_terminal_target
from foundation.config import config_manager
from foundation.error_model import ApiError, record_internal_error
from foundation.responses import error_response
from foundation.uploads import (
    merge_files_to_path,
    safe_upload_target_path,
    save_upload_to_path,
    upload_temp_root,
)

from .terminal_upload_sessions import bind_upload_session, push_terminal_upload, upload_binding


logger = logging.getLogger(__name__)

router = APIRouter()

# 分片索引/总数来自表单：负值、倒置或天文数字的 total_chunks 会触发
# range(total_chunks) 全量 os.path.exists 轮询与巨型 chunk 文件名集合。
_MAX_UPLOAD_CHUNKS = 100_000


# ==================== SSH Terminal Info ====================

@router.get("/api/terminal/open")
async def get_ssh_terminal_info(
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    """Get SSH terminal connection info."""
    try:
        config = config_manager.load_config()

        ssh_host = config_manager.get_ubuntu_host(config) or "localhost"
        ssh_user = config_manager.get_ubuntu_user(config)
        connection_string = f"ssh {ssh_user}@{ssh_host}"

        return JSONResponse(content={
            "success": True,
            "host": ssh_host,
            "user": ssh_user,
            "port": 22,
            "connection_command": connection_string,
            "instructions": [
                f"1. Copy connection command: {connection_string}",
                "2. Paste and execute in terminal",
                "3. Enter password or use SSH key authentication",
                "4. You will have terminal access to the test host",
            ],
        })
    except Exception:
        message = record_internal_error(
            logger, "读取 SSH 终端信息", "Error getting SSH terminal info"
        )
        return ApiError.internal(message).to_response()


# ==================== File Upload ====================

@router.post("/api/terminal/push")
@router.head("/api/terminal/push")
async def upload_file(
    request: Request,
    _admin: CurrentUser = Depends(require_elevated_admin),
    file: UploadFile | None = File(None),
    path: str = Form(""),
    chunk_index: int | None = Form(None),
    total_chunks: int | None = Form(None),
    upload_id: str | None = Form(None),
    file_name: str | None = Form(None),
    file_size: int | None = Form(None),
    resume: str | None = Form(None),
    check_chunks: str | None = Form(None),
    worker_id: str = Form(""),
    host: str = Form(""),
    user: str = Form(""),
):
    """File upload - supports chunked upload and resume."""
    if not (check_chunks and upload_id) and not file:
        return error_response("No file provided", 400)
    try:
        config = await _upload_target_config(worker_id)
    except ValueError as exc:
        return ApiError.malformed_request(str(exc)).to_response()
    except RuntimeError:
        return ApiError.dependency_unavailable("终端目标服务不可用").to_response()

    if check_chunks and upload_id:
        try:
            session_dir = bind_upload_session(
                upload_temp_root(), _admin.resource_owner_id, upload_id,
                upload_binding(config, path, file_name, total_chunks, file_size),
            )
        except ApiError as exc:
            return exc.to_response()
        chunks_file = session_dir / "uploaded_chunks.json"
        try:
            uploaded_chunks = json.loads(chunks_file.read_text()) if chunks_file.exists() else []
        except (OSError, ValueError):
            uploaded_chunks = []
        return JSONResponse(content={"success": True, "uploaded_chunks": uploaded_chunks})

    if chunk_index is not None and total_chunks is not None:
        return await _upload_file_chunk(
            file, chunk_index, total_chunks, upload_id, file_name or file.filename,
            file_size, resume, config=config, path=path, owner_id=_admin.resource_owner_id,
        )

    # Normal upload mode
    try:
        if not file or file.filename == "":
            return error_response("No file selected", 400)

        upload_dir = upload_temp_root()
        os.makedirs(upload_dir, exist_ok=True)

        try:
            temp_path = safe_upload_target_path(upload_dir, file.filename, allow_nested=False)
        except ValueError as e:
            return error_response(str(e), 400)

        await save_upload_to_path(file, temp_path)

        try:
            async with ssh_manager.async_optional_connection(config) as ssh:
                if not ssh:
                    os.remove(temp_path)
                    return ApiError.upstream_failure(
                        "SSH connection failed",
                        service="ssh",
                        next_actions=({"action": "检查目标主机 SSH 服务与凭据"},),
                    ).to_response()

                remote_path = await asyncio.to_thread(
                    push_terminal_upload, ssh, temp_path, path, config["username"],
                    ssh_manager.optimize_sftp_performance,
                )

                os.remove(temp_path)

                return JSONResponse(content={"success": True, "remote_path": remote_path, "message": f"File uploaded to {remote_path}"})
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e


    except OSError:
        return ApiError.upstream_failure("终端文件传输失败，请检查目标目录和 SSH/SFTP 服务").to_response()
    except Exception:
        message = record_internal_error(
            logger, "上传终端文件", "Error uploading file"
        )
        return ApiError.internal(message).to_response()


async def _upload_target_config(worker_id: str) -> dict:
    worker, host, user, password, _serial = await asyncio.to_thread(
        resolve_authorized_terminal_target, worker_id, mode="ssh"
    )
    return {
        **config_manager.load_config(), "worker_id": worker,
        "ubuntu_host": host, "ubuntu_user": user, "ubuntu_pswd": password,
        "host": host, "username": user, "password": password,
    }


async def _upload_file_chunk(
    file: UploadFile,
    chunk_index: int,
    total_chunks: int,
    upload_id: str,
    file_name: str,
    file_size: int | None = None,
    resume: str | None = None,
    *, config: dict | None = None, path: str = "", owner_id: str = "",
):
    """Handle chunked file upload."""
    try:
        if not upload_id or not file_name:
            return error_response("upload_id and file_name are required for chunk upload", 400)

        if (
            chunk_index < 0
            or total_chunks < 1
            or chunk_index >= total_chunks
            or total_chunks > _MAX_UPLOAD_CHUNKS
        ):
            return error_response(
                "chunk_index/total_chunks out of allowed range "
                f"(0 <= chunk_index < total_chunks <= {_MAX_UPLOAD_CHUNKS})",
                400,
            )

        if config is None:
            raise ApiError.forbidden("上传分片缺少已授权的终端目标")
        session_dir = str(bind_upload_session(
            upload_temp_root(), owner_id, upload_id,
            upload_binding(config, path, file_name, total_chunks, file_size),
        ))

        import time
        start_time = time.time()
        logger.info("[ChunkUpload] Received chunk %s/%s", chunk_index, total_chunks)

        chunk_filename = f"chunk_{chunk_index:05d}"
        chunk_path = os.path.join(session_dir, chunk_filename)

        saved_size = await save_upload_to_path(file, chunk_path)

        elapsed = time.time() - start_time
        speed = saved_size / elapsed / (1024 * 1024) if elapsed > 0 else 0
        logger.info(f"[ChunkUpload] Saved chunk {chunk_index} ({saved_size} bytes) in {elapsed:.2f}s ({speed:.2f} MB/s)")

        chunks_file = os.path.join(session_dir, "uploaded_chunks.json")
        uploaded_chunks = set()

        if os.path.exists(chunks_file):
            try:
                with open(chunks_file) as f:
                    uploaded_chunks = set(json.load(f))
                if resume:
                    logger.info(f"[ChunkUpload] Resuming with {len(uploaded_chunks)} chunks already uploaded")
            except (OSError, json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[ChunkUpload] Ignoring invalid chunk state for {upload_id}: {e}")
                uploaded_chunks = set()

        uploaded_chunks.add(chunk_index)
        uploaded_chunks.update(
            idx
            for idx in range(total_chunks)
            if os.path.exists(os.path.join(session_dir, f"chunk_{idx:05d}"))
        )

        with open(chunks_file, "w") as f:
            json.dump(list(uploaded_chunks), f)

        chunk_paths = [os.path.join(session_dir, f"chunk_{i:05d}") for i in range(total_chunks)]

        if len(uploaded_chunks) == total_chunks and all(os.path.exists(path) for path in chunk_paths):
            merge_lock_path = os.path.join(session_dir, ".merge.lock")
            try:
                merge_lock_fd = os.open(merge_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(merge_lock_fd)
            except FileExistsError:
                return JSONResponse(content={
                    "success": True, "chunk_index": chunk_index,
                    "chunks_uploaded": len(uploaded_chunks), "total_chunks": total_chunks,
                    "merging": True, "progress": 100,
                })

            merge_start = time.time()
            logger.info(f"[ChunkUpload] All chunks received for {upload_id}, merging...")

            merged_file = safe_upload_target_path(session_dir, file_name, allow_nested=False)
            # 合并是纯磁盘 I/O，大文件分块场景会阻塞事件循环——与下方
            # SFTP 上传一样移出事件循环执行。
            await asyncio.to_thread(merge_files_to_path, chunk_paths, merged_file)

            merge_time = time.time() - merge_start
            logger.info(f"[ChunkUpload] Merged {total_chunks} chunks in {merge_time:.2f}s")

            async with ssh_manager.async_optional_connection(config) as ssh:

                if not ssh:
                    try:
                        os.remove(merge_lock_path)
                    except OSError:
                        pass
                    return ApiError.upstream_failure(
                        "SSH connection failed",
                        service="ssh",
                        details={
                            "chunks_uploaded": len(uploaded_chunks),
                            "total_chunks": total_chunks,
                        },
                        next_actions=({"action": "检查目标主机 SSH 服务与凭据"},),
                    ).to_response()

                try:
                    upload_start = time.time()
                    remote_path = await asyncio.to_thread(
                        push_terminal_upload, ssh, merged_file, path, config["username"],
                        ssh_manager.optimize_sftp_performance,
                    )

                    upload_time = time.time() - upload_start
                    file_size_mb = os.path.getsize(merged_file) / (1024 * 1024)
                    upload_speed = file_size_mb / upload_time if upload_time > 0 else 0
                    logger.info(f"[ChunkUpload] Uploaded {file_size_mb:.2f}MB to remote in {upload_time:.2f}s ({upload_speed:.2f} MB/s)")

                    shutil.rmtree(session_dir)

                    return JSONResponse(content={
                        "success": True, "upload_complete": True,
                        "remote_path": remote_path, "message": f"File uploaded to {remote_path}",
                    })
                except Exception:
                    try:
                        os.remove(merge_lock_path)
                    except OSError:
                        pass
                    message = record_internal_error(
                        logger,
                        "上传合并文件",
                        "Error uploading merged file",
                    )
                    return ApiError.upstream_failure(
                        message,
                        service="ssh",
                        details={
                            "chunks_uploaded": len(uploaded_chunks),
                            "total_chunks": total_chunks,
                        },
                        next_actions=({"action": "检查目标主机 SSH/SFTP 服务"},),
                    ).to_response()

        return JSONResponse(content={
            "success": True, "chunk_index": chunk_index,
            "chunks_uploaded": len(uploaded_chunks), "total_chunks": total_chunks,
            "upload_complete": False, "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2),
        })

    except ApiError as exc:
        return exc.to_response()
    except Exception:
        if "merge_lock_path" in locals():
            try:
                os.remove(merge_lock_path)
            except OSError:
                pass
        message = record_internal_error(
            logger,
            "上传文件分片",
            "Error uploading chunk",
            context={"chunk_index": chunk_index},
        )
        return ApiError.internal(
            message, details={"chunk_index": chunk_index}
        ).to_response()
