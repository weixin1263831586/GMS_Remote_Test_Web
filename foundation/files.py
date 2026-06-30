from __future__ import annotations

import io
import logging
import os
import zipfile
from typing import Any


logger = logging.getLogger(__name__)


def _safe_zip_arcname(path: str, arcname_base: str, path_prefix: str = '') -> str:
    path_real = os.path.realpath(path)
    base_real = os.path.realpath(arcname_base)
    if path_real != base_real and not path_real.startswith(base_real + os.sep):
        raise ValueError(f'ZIP source path escapes base: {path}')

    rel_path = os.path.relpath(path_real, base_real)
    arcname = os.path.normpath(os.path.join(path_prefix, rel_path) if path_prefix else rel_path)
    if arcname == '.' or arcname.startswith('..' + os.sep) or os.path.isabs(arcname):
        raise ValueError(f'Unsafe ZIP archive path: {arcname}')
    return arcname.replace(os.sep, '/')


def _write_entries_to_zip(
    zip_file: zipfile.ZipFile,
    source_dir: str,
    arcname_base: str,
    path_prefix: str = '',
) -> int:
    count = 0
    for root, _directories, filenames in os.walk(source_dir):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                if os.path.islink(path):
                    logger.warning("Skipping symlink while creating ZIP: %s", path)
                    continue
                arcname = _safe_zip_arcname(path, arcname_base, path_prefix)
                zip_file.write(path, arcname)
                count += 1
            except Exception as exc:
                logger.warning("Cannot add file to ZIP: %s, error: %s", path, exc)
    return count


def _finalize_zip_buffer(
    buffer: io.BytesIO,
    file_count: int,
    zip_filename: str,
    empty_warning: str,
) -> tuple[bytes, int] | None:
    if file_count == 0:
        logger.warning(empty_warning)
        return None
    logger.info("Created ZIP: %s, %s files", zip_filename, file_count)
    return buffer.getvalue(), file_count


def create_zip_from_directory(
    source_dir: str,
    zip_filename: str = 'archive.zip',
    base_dir_for_arcnames: str | None = None,
) -> tuple[bytes, int] | None:
    buffer = io.BytesIO()
    base = base_dir_for_arcnames or os.path.dirname(source_dir)
    try:
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            count = _write_entries_to_zip(archive, source_dir, base)
        return _finalize_zip_buffer(
            buffer,
            count,
            zip_filename,
            f"No files found in directory: {source_dir}",
        )
    except Exception as exc:
        logger.error("Failed to create ZIP: %s", exc)
        return None


def create_zip_from_multiple_directories(
    dir_mapping: dict[str, str],
    zip_filename: str = 'archive.zip',
) -> tuple[bytes, int] | None:
    buffer = io.BytesIO()
    try:
        total = 0
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for source_dir, path_prefix in dir_mapping.items():
                if not os.path.exists(source_dir):
                    logger.warning("Directory does not exist, skipping: %s", source_dir)
                    continue
                total += _write_entries_to_zip(archive, source_dir, source_dir, path_prefix)
        return _finalize_zip_buffer(
            buffer,
            total,
            zip_filename,
            f"No files found in directories: {list(dir_mapping.keys())}",
        )
    except Exception as exc:
        logger.error("Failed to create ZIP: %s", exc)
        return None


def list_directory_files(
    directory: str,
    max_files: int = 100,
    relative_to: str | None = None,
) -> list[dict]:
    if not os.path.exists(directory):
        return []
    base = relative_to or directory
    result = []
    for root, _directories, filenames in os.walk(directory):
        for filename in filenames:
            path = os.path.join(root, filename)
            if os.path.islink(path):
                continue
            result.append(
                {
                    'name': filename,
                    'path': path,
                    'relative_path': os.path.relpath(path, base),
                    'size': os.path.getsize(path),
                }
            )
            if len(result) >= max_files:
                return result
    return result


class FileUtils:
    create_zip_from_directory = staticmethod(create_zip_from_directory)
    create_zip_from_multiple_directories = staticmethod(create_zip_from_multiple_directories)

    @staticmethod
    def list_directory_files(
        directory: str,
        max_files: int = 100,
        relative_to: str | None = None,
    ) -> list[dict[str, Any]]:
        return list_directory_files(directory, max_files=max_files, relative_to=relative_to)
