from __future__ import annotations

import asyncio
import os
import posixpath
import tempfile
from contextlib import suppress
from pathlib import Path

from fastapi import UploadFile


def extract_report_name_from_upload(files: list[UploadFile]) -> str:
    if not files or not files[0].filename:
        return 'Unknown Report'
    if len(files) == 1:
        return files[0].filename
    first_file = files[0].filename
    return os.path.dirname(first_file) or os.path.basename(first_file)


def normalize_upload_relative_path(
    filename: str | None,
    allow_nested: bool = True,
) -> str:
    raw_name = (filename or '').replace('\\', '/').strip()
    if not raw_name:
        raise ValueError('文件名无效')
    normalized = os.path.normpath(raw_name).replace('\\', '/')
    if (
        normalized in {'.', '..'}
        or normalized.startswith('../')
        or os.path.isabs(normalized)
    ):
        raise ValueError('非法文件路径')
    return normalized if allow_nested else os.path.basename(normalized)


def safe_upload_target_path(
    base_dir: str,
    filename: str | None,
    allow_nested: bool = True,
) -> str:
    relative_path = normalize_upload_relative_path(filename, allow_nested)
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, relative_path))
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError('非法文件路径')
    return target_abs


def upload_temp_root(namespace: str = "gms_uploads") -> str:
    """Return the local temp root used for transient upload state.

    Prefers GMS_UPLOAD_DIR, then data_root/uploads, then OS temp dir.
    """
    safe_namespace = os.path.basename(str(namespace or "gms_uploads").strip()) or "gms_uploads"
    override = os.getenv("GMS_UPLOAD_DIR", "").strip()
    if override:
        base = Path(override)
    else:
        try:
            from foundation.config import settings

            base = settings.data_root / "uploads"
        except Exception:
            base = Path(tempfile.gettempdir())
    return str(base / safe_namespace)


def remote_home_file_path(username: str, filename: str) -> str:
    """Build a remote SFTP path under an SSH user's home directory."""
    safe_username = str(username or "").strip()
    if not safe_username or "/" in safe_username or safe_username in {".", ".."}:
        raise ValueError("Invalid remote username")
    safe_filename = os.path.basename(str(filename or "").replace("\\", "/"))
    if not safe_filename or safe_filename in {".", ".."}:
        raise ValueError("Invalid remote filename")
    return posixpath.join("/home", safe_username, safe_filename)


def copy_fileobj_to_path(
    source,
    destination: str,
    max_size: int | None = None,
    chunk_size: int = 1024 * 1024,
) -> int:
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    try:
        with open(destination, 'wb') as target:
            while chunk := source.read(chunk_size):
                bytes_written += len(chunk)
                if max_size is not None and bytes_written > max_size:
                    raise ValueError(
                        f'文件过大，最大支持 {max_size // (1024 * 1024)}MB'
                    )
                target.write(chunk)
    except BaseException:
        with suppress(FileNotFoundError):
            os.remove(destination)
        raise
    return bytes_written


async def save_upload_to_path(
    upload_file: UploadFile,
    destination: str,
    max_size: int | None = None,
) -> int:
    await upload_file.seek(0)
    return await asyncio.to_thread(
        copy_fileobj_to_path,
        upload_file.file,
        destination,
        max_size,
    )


def merge_files_to_path(
    source_paths: list[str],
    destination: str,
    chunk_size: int = 1024 * 1024,
) -> int:
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as output:
        for source_path in source_paths:
            with open(source_path, 'rb') as source:
                while chunk := source.read(chunk_size):
                    output.write(chunk)
                    bytes_written += len(chunk)
    return bytes_written
