"""
File utilities for common file operations
"""
import os
import logging
import zipfile
import io
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class FileUtils:
    """Shared file operation utilities"""

    @staticmethod
    def create_zip_from_directory(
        source_dir: str,
        zip_filename: str,
        base_dir_for_arcnames: str = None
    ) -> Optional[Tuple[bytes, int]]:
        """
        Create ZIP file from directory in memory

        Args:
            source_dir: Source directory path
            zip_filename: Name for the ZIP file
            base_dir_for_arcnames: Base directory for archive names (defaults to source_dir parent)

        Returns:
            (zip_data, file_count) or None if failed
        """
        if not base_dir_for_arcnames:
            base_dir_for_arcnames = os.path.dirname(source_dir)

        zip_buffer = io.BytesIO()
        file_count = 0

        try:
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for root, dirs, filenames in os.walk(source_dir):
                    for filename in filenames:
                        file_path = os.path.join(root, filename)
                        arcname = os.path.relpath(file_path, base_dir_for_arcnames)

                        try:
                            zip_file.write(file_path, arcname)
                            file_count += 1
                        except Exception as e:
                            logger.warning(f"Cannot add file to ZIP: {file_path}, error: {e}")

            if file_count == 0:
                logger.warning(f"No files found in directory: {source_dir}")
                return None

            zip_data = zip_buffer.getvalue()
            logger.info(f"Created ZIP: {zip_filename}, {file_count} files")
            return zip_data, file_count

        except Exception as e:
            logger.error(f"Failed to create ZIP: {e}")
            return None

    @staticmethod
    def create_zip_from_multiple_directories(
        dir_mapping: Dict[str, str],
        zip_filename: str
    ) -> Optional[Tuple[bytes, int]]:
        """
        Create ZIP file from multiple directories with optional path prefixes

        Args:
            dir_mapping: Dict mapping {directory_path: path_prefix_in_zip}
                         e.g., {'/path/to/results': '', '/path/to/logs': 'logs'}
            zip_filename: Name for the ZIP file

        Returns:
            (zip_data, file_count) or None if failed
        """
        zip_buffer = io.BytesIO()
        file_count = 0

        try:
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for source_dir, path_prefix in dir_mapping.items():
                    if not os.path.exists(source_dir):
                        logger.warning(f"Directory does not exist, skipping: {source_dir}")
                        continue

                    for root, dirs, filenames in os.walk(source_dir):
                        for filename in filenames:
                            file_path = os.path.join(root, filename)
                            rel_path = os.path.relpath(file_path, source_dir)

                            # Add path prefix if specified
                            if path_prefix:
                                arcname = os.path.join(path_prefix, rel_path)
                            else:
                                arcname = rel_path

                            try:
                                zip_file.write(file_path, arcname)
                                file_count += 1
                            except Exception as e:
                                logger.warning(f"Cannot add file to ZIP: {file_path}, error: {e}")

            if file_count == 0:
                logger.warning(f"No files found in directories: {list(dir_mapping.keys())}")
                return None

            zip_data = zip_buffer.getvalue()
            logger.info(f"Created ZIP: {zip_filename}, {file_count} files")
            return zip_data, file_count

        except Exception as e:
            logger.error(f"Failed to create ZIP: {e}")
            return None

    @staticmethod
    def list_directory_files(
        directory: str,
        max_files: int = 100,
        relative_to: str = None
    ) -> List[Dict[str, any]]:
        """
        List files in directory with metadata

        Args:
            directory: Directory to scan
            max_files: Maximum number of files to return
            relative_to: Base directory for relative paths (defaults to directory)

        Returns:
            List of file dicts with name, path, relative_path, size
        """
        if not relative_to:
            relative_to = directory

        if not os.path.exists(directory):
            logger.error(f"Directory does not exist: {directory}")
            return []

        files = []
        try:
            for root, dirs, filenames in os.walk(directory):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(file_path, relative_to)

                    try:
                        file_size = os.path.getsize(file_path)
                    except (FileNotFoundError, OSError):
                        file_size = 0

                    files.append({
                        'name': filename,
                        'path': file_path,
                        'relative_path': rel_path,
                        'size': file_size
                    })

                    if len(files) >= max_files:
                        return files

        except Exception as e:
            logger.error(f"Failed to list directory {directory}: {e}")

        return files