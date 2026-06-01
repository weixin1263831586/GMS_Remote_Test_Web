"""Safe file upload helpers."""

import asyncio
import os
from typing import List, Optional

from fastapi import UploadFile


def extract_report_name_from_upload(files: List[UploadFile]) -> str:
    """Extract a display report name from uploaded file names."""
    if not files or not files[0].filename:
        return 'Unknown Report'

    if len(files) == 1:
        return files[0].filename

    first_file = files[0].filename
    return os.path.dirname(first_file) or os.path.basename(first_file)


def normalize_upload_relative_path(filename: Optional[str], allow_nested: bool = True) -> str:
    """Normalize browser-provided upload names and keep them relative."""
    raw_name = (filename or '').replace('\\', '/').strip()
    if not raw_name:
        raise ValueError("文件名无效")

    normalized = os.path.normpath(raw_name).replace('\\', '/')
    if normalized in ('.', '..') or normalized.startswith('../') or os.path.isabs(normalized):
        raise ValueError("非法文件路径")

    if not allow_nested:
        normalized = os.path.basename(normalized)

    return normalized


def safe_upload_target_path(base_dir: str, filename: Optional[str], allow_nested: bool = True) -> str:
    """Build a safe destination path for an uploaded file."""
    relative_path = normalize_upload_relative_path(filename, allow_nested=allow_nested)
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, relative_path))
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError("非法文件路径")
    return target_abs


def copy_fileobj_to_path(source, destination: str, max_size: Optional[int] = None, chunk_size: int = 1024 * 1024) -> int:
    """Copy a file-like object to disk in chunks and return bytes written."""
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as target:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            bytes_written += len(chunk)
            if max_size is not None and bytes_written > max_size:
                raise ValueError(f"文件过大，最大支持 {max_size // (1024 * 1024)}MB")
            target.write(chunk)
    return bytes_written


async def save_upload_to_path(upload_file: UploadFile, destination: str, max_size: Optional[int] = None) -> int:
    """Persist an UploadFile without loading the whole file into memory."""
    await upload_file.seek(0)
    return await asyncio.to_thread(copy_fileobj_to_path, upload_file.file, destination, max_size)


def merge_files_to_path(source_paths: List[str], destination: str, chunk_size: int = 1024 * 1024) -> int:
    """Merge files into destination using bounded memory."""
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as outfile:
        for source_path in source_paths:
            with open(source_path, 'rb') as infile:
                while True:
                    chunk = infile.read(chunk_size)
                    if not chunk:
                        break
                    outfile.write(chunk)
                    bytes_written += len(chunk)
    return bytes_written
