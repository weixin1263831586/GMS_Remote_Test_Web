from __future__ import annotations

import io
import os
import zipfile


def create_zip_from_directory(source_dir: str) -> tuple[bytes, int] | None:
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for root, _directories, filenames in os.walk(source_dir):
            for filename in filenames:
                path = os.path.join(root, filename)
                archive.write(path, os.path.relpath(path, os.path.dirname(source_dir)))
                count += 1
    return (buffer.getvalue(), count) if count else None


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
