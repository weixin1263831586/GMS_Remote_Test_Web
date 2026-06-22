"""Terminal router - SSH terminal info and file push/upload APIs."""

import json
import logging
import os
import shutil
import tempfile

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from features.system.ssh import ssh_manager
from foundation.config import config_manager
from foundation.responses import error_response
from foundation.uploads import merge_files_to_path, safe_upload_target_path, save_upload_to_path


logger = logging.getLogger(__name__)

router = APIRouter()


# ==================== SSH Terminal Info ====================

@router.get("/api/terminal/open")
async def get_ssh_terminal_info():
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
    except Exception as e:
        logger.error(f"Error getting SSH terminal info: {e}")
        return error_response(str(e), 500)


# ==================== File Upload ====================

@router.post("/api/terminal/push")
@router.head("/api/terminal/push")
async def upload_file(
    request: Request,
    file: UploadFile | None = File(None),
    path: str = Form(""),
    chunk_index: int | None = Form(None),
    total_chunks: int | None = Form(None),
    upload_id: str | None = Form(None),
    file_name: str | None = Form(None),
    file_size: int | None = Form(None),
    resume: str | None = Form(None),
    check_chunks: str | None = Form(None),
):
    """File upload - supports chunked upload and resume."""
    # HEAD request: check uploaded chunks for resume
    if check_chunks and upload_id:
        session_dir = os.path.join(tempfile.gettempdir(), "gms_uploads", upload_id)
        chunks_file = os.path.join(session_dir, "uploaded_chunks.json")

        if os.path.exists(chunks_file):
            with open(chunks_file) as f:
                uploaded_chunks = json.load(f)
            return JSONResponse(content={"success": True, "uploaded_chunks": uploaded_chunks})
        else:
            return JSONResponse(content={"success": True, "uploaded_chunks": []})

    # Check file parameter for chunk upload
    if chunk_index is not None and not file:
        return error_response("No file provided for chunk upload", 400)

    # Chunked upload mode
    if chunk_index is not None and total_chunks is not None:
        return await _upload_file_chunk(file, chunk_index, total_chunks, upload_id, file_name or file.filename, file_size, resume)

    # Normal upload mode
    try:
        if not file or file.filename == "":
            return error_response("No file selected", 400)

        config = config_manager.load_config()

        upload_dir = os.path.join(tempfile.gettempdir(), "gms_uploads")
        os.makedirs(upload_dir, exist_ok=True)

        try:
            temp_path = safe_upload_target_path(upload_dir, file.filename, allow_nested=False)
        except ValueError as e:
            return error_response(str(e), 400)

        safe_filename = os.path.basename(temp_path)
        await save_upload_to_path(file, temp_path)

        try:
            with ssh_manager.optional_connection(config) as ssh:
                if not ssh:
                    os.remove(temp_path)
                    return error_response("SSH connection failed", 500)

                # Determine target path and upload via SFTP
                default_remote = f"/home/{config['ubuntu_user']}/{safe_filename}"

                if path and path.strip():
                    target_dir = path.rstrip("/")
                    try:
                        with ssh.open_sftp() as sftp:
                            ssh_manager.optimize_sftp_performance(sftp)
                            try:
                                sftp.stat(target_dir)
                            except OSError:
                                sftp.mkdir(target_dir)
                            remote_path = f"{target_dir}/{safe_filename}"
                            sftp.put(temp_path, remote_path)
                    except Exception as e:
                        logger.error(f"Failed to upload to specified path: {e}")
                        remote_path = default_remote
                        with ssh.open_sftp() as sftp:
                            ssh_manager.optimize_sftp_performance(sftp)
                            sftp.put(temp_path, remote_path)
                else:
                    remote_path = default_remote
                    with ssh.open_sftp() as sftp:
                        ssh_manager.optimize_sftp_performance(sftp)
                        sftp.put(temp_path, remote_path)

                os.remove(temp_path)

                return JSONResponse(content={"success": True, "remote_path": remote_path, "message": f"File uploaded to {remote_path}"})
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise e


    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return error_response(str(e), status_code=500)


async def _upload_file_chunk(
    file: UploadFile,
    chunk_index: int,
    total_chunks: int,
    upload_id: str,
    file_name: str,
    file_size: int | None = None,
    resume: str | None = None,
):
    """Handle chunked file upload."""
    try:
        if not upload_id or not file_name:
            return error_response("upload_id and file_name are required for chunk upload", 400)

        import time
        start_time = time.time()
        logger.info(f"[ChunkUpload] Received chunk {chunk_index}/{total_chunks} for {upload_id}")

        session_dir = os.path.join(tempfile.gettempdir(), "gms_uploads", upload_id)
        os.makedirs(session_dir, exist_ok=True)

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
            merge_files_to_path(chunk_paths, merged_file)

            merge_time = time.time() - merge_start
            logger.info(f"[ChunkUpload] Merged {total_chunks} chunks in {merge_time:.2f}s")

            config = config_manager.load_config()
            with ssh_manager.optional_connection(config) as ssh:

                if not ssh:
                    try:
                        os.remove(merge_lock_path)
                    except OSError:
                        pass
                    return JSONResponse(content={
                        "success": False, "error": "SSH connection failed",
                        "chunks_uploaded": len(uploaded_chunks), "total_chunks": total_chunks,
                    }, status_code=500)

                try:
                    remote_filename = os.path.basename(merged_file)
                    remote_path = f"/home/{config['ubuntu_user']}/{remote_filename}"
                    upload_start = time.time()

                    with ssh.open_sftp() as sftp:
                        ssh_manager.optimize_sftp_performance(sftp)
                        sftp.put(merged_file, remote_path, confirm=True)

                    upload_time = time.time() - upload_start
                    file_size_mb = os.path.getsize(merged_file) / (1024 * 1024)
                    upload_speed = file_size_mb / upload_time if upload_time > 0 else 0
                    logger.info(f"[ChunkUpload] Uploaded {file_size_mb:.2f}MB to remote in {upload_time:.2f}s ({upload_speed:.2f} MB/s)")

                    shutil.rmtree(session_dir)

                    return JSONResponse(content={
                        "success": True, "upload_complete": True,
                        "remote_path": remote_path, "message": f"File uploaded to {remote_path}",
                    })
                except Exception as e:
                    try:
                        os.remove(merge_lock_path)
                    except OSError:
                        pass
                    logger.error(f"Error uploading merged file: {e}")
                    return JSONResponse(content={
                        "success": False, "error": f"Upload failed: {e!s}",
                        "chunks_uploaded": len(uploaded_chunks), "total_chunks": total_chunks,
                    }, status_code=500)

        return JSONResponse(content={
            "success": True, "chunk_index": chunk_index,
            "chunks_uploaded": len(uploaded_chunks), "total_chunks": total_chunks,
            "upload_complete": False, "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2),
        })

    except Exception as e:
        if "merge_lock_path" in locals():
            try:
                os.remove(merge_lock_path)
            except OSError:
                pass
        logger.error(f"Error uploading chunk {chunk_index}: {e}")
        return error_response(str(e), status_code=500, chunk_index=chunk_index)
