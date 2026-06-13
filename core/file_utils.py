"""File utilities for common file operations."""
import io
import logging
import os
import zipfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _write_entries_to_zip(
    zip_file: zipfile.ZipFile,
    source_dir: str,
    arcname_base: str,
    path_prefix: str = '',
) -> int:
    """Walk *source_dir* and write files into *zip_file*. Returns file count."""
    count = 0
    for root, _dirs, filenames in os.walk(source_dir):
        for filename in filenames:
            file_path = os.path.join(root, filename)
            rel_path = os.path.relpath(file_path, arcname_base)
            arcname = os.path.join(path_prefix, rel_path) if path_prefix else rel_path
            try:
                zip_file.write(file_path, arcname)
                count += 1
            except Exception as e:
                logger.warning(f"Cannot add file to ZIP: {file_path}, error: {e}")
    return count


def _finalize_zip_buffer(
    zip_buffer: io.BytesIO,
    file_count: int,
    zip_filename: str,
    empty_warning: str,
) -> Optional[Tuple[bytes, int]]:
    """Return (zip_data, file_count) or None if no files were written."""
    if file_count == 0:
        logger.warning(empty_warning)
        return None
    zip_data = zip_buffer.getvalue()
    logger.info(f"Created ZIP: {zip_filename}, {file_count} files")
    return zip_data, file_count


class FileUtils:
    """Shared file operation utilities"""

    @staticmethod
    def create_zip_from_directory(
        source_dir: str,
        zip_filename: str,
        base_dir_for_arcnames: str = None,
    ) -> Optional[Tuple[bytes, int]]:
        """Create ZIP file from directory in memory. Returns (zip_data, file_count) or None."""
        base = base_dir_for_arcnames or os.path.dirname(source_dir)
        zip_buffer = io.BytesIO()
        try:
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                count = _write_entries_to_zip(zf, source_dir, base)
            return _finalize_zip_buffer(zip_buffer, count, zip_filename,
                                        f"No files found in directory: {source_dir}")
        except Exception as e:
            logger.error(f"Failed to create ZIP: {e}")
            return None

    @staticmethod
    def create_zip_from_multiple_directories(
        dir_mapping: Dict[str, str],
        zip_filename: str,
    ) -> Optional[Tuple[bytes, int]]:
        """Create ZIP from multiple directories. dir_mapping: {dir_path: prefix_in_zip}."""
        zip_buffer = io.BytesIO()
        try:
            total = 0
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                for source_dir, path_prefix in dir_mapping.items():
                    if not os.path.exists(source_dir):
                        logger.warning(f"Directory does not exist, skipping: {source_dir}")
                        continue
                    total += _write_entries_to_zip(zf, source_dir, source_dir, path_prefix)
            return _finalize_zip_buffer(zip_buffer, total, zip_filename,
                                        f"No files found in directories: {list(dir_mapping.keys())}")
        except Exception as e:
            logger.error(f"Failed to create ZIP: {e}")
            return None

    @staticmethod
    def list_directory_files(
        directory: str,
        max_files: int = 100,
        relative_to: str = None,
    ) -> List[Dict[str, Any]]:
        """List files in directory with metadata. Returns list of dicts with name, path, relative_path, size."""
        base = relative_to or directory
        if not os.path.exists(directory):
            logger.error(f"Directory does not exist: {directory}")
            return []

        files: List[Dict[str, Any]] = []
        try:
            for root, _dirs, filenames in os.walk(directory):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    try:
                        file_size = os.path.getsize(file_path)
                    except (FileNotFoundError, OSError):
                        file_size = 0
                    files.append({
                        'name': filename,
                        'path': file_path,
                        'relative_path': os.path.relpath(file_path, base),
                        'size': file_size,
                    })
                    if len(files) >= max_files:
                        return files
        except Exception as e:
            logger.error(f"Failed to list directory {directory}: {e}")
        return files
