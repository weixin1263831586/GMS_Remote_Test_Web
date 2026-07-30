"""Controller-local test-suite file browsing helpers."""

from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
import zipfile
from contextlib import suppress
from typing import Any

from fastapi.responses import StreamingResponse


def resolve_local_suite_target(suite_root: str, target_path: str) -> str:
    """Resolve a controller-local suite target without allowing symlink escape."""
    root = os.path.realpath(os.path.expanduser(suite_root))
    target = os.path.realpath(os.path.expanduser(target_path))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("Illegal path")
    return target


def local_suite_file_info(suite_root: str, target_path: str) -> dict[str, Any]:
    target = resolve_local_suite_target(suite_root, target_path)
    if not os.path.isfile(target):
        raise FileNotFoundError("File not found")
    entry_stat = os.stat(target)
    lower = target.lower()
    return {
        "real_path": target,
        "name": os.path.basename(target),
        "size": entry_stat.st_size,
        "modified": int(entry_stat.st_mtime),
        "is_apk": lower.endswith(".apk"),
        "is_jar": lower.endswith(".jar"),
    }


def local_suite_file_response(
    suite_root: str, target_path: str, inline: bool
) -> StreamingResponse:
    info = local_suite_file_info(suite_root, target_path)
    filename = info["name"]
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download"
    quoted_filename = urllib.parse.quote(filename)
    disposition = (
        "inline" if inline
        else f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}'
    )

    def iter_file():
        with open(info["real_path"], "rb") as local_file:
            while chunk := local_file.read(1024 * 1024):
                yield chunk

    import mimetypes
    return StreamingResponse(
        iter_file(),
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
        headers={"Content-Disposition": disposition, "Content-Length": str(info["size"])},
    )


def run_folder_suffix(rel_path: str) -> str:
    """Return '-results' or '-logs' when rel_path is inside a results/logs folder.

    rel_path is relative to the suite root (e.g. "results/2026.06.25_10.57.05"
    or "android-cts/results/2026.06.25_10.57.05"). The first segment named
    exactly "results" or "logs" determines the suffix.
    """
    for seg in (rel_path or "").split("/"):
        kind = seg.lower()
        if kind in {"results", "logs"}:
            return f"-{kind}"
    return ""


def local_suite_directory_response(
    suite_root: str, target_path: str, rel_path: str = ""
) -> StreamingResponse:
    target = resolve_local_suite_target(suite_root, target_path)
    if not os.path.isdir(target):
        raise FileNotFoundError("Directory not found")
    fd, zip_path = tempfile.mkstemp(suffix=".zip", prefix="suite_dl_")
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for current, dirs, files in os.walk(target):
                dirs[:] = [name for name in dirs if _inside_suite(suite_root, current, name)]
                for name in files:
                    full_path = os.path.join(current, name)
                    try:
                        real_path = resolve_local_suite_target(suite_root, full_path)
                    except ValueError:
                        continue
                    archive.write(real_path, os.path.relpath(full_path, target))
        zip_size = os.path.getsize(zip_path)
    except Exception:
        with suppress(OSError):
            os.remove(zip_path)
        raise

    folder_name = os.path.basename(target) or "download"
    suffix = run_folder_suffix(rel_path or target_path)
    filename = f"{folder_name}{suffix}.zip"
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "download.zip"
    quoted_filename = urllib.parse.quote(filename)

    def iter_directory():
        try:
            with open(zip_path, "rb") as local_file:
                while chunk := local_file.read(1024 * 1024):
                    yield chunk
        finally:
            with suppress(OSError):
                os.remove(zip_path)

    return StreamingResponse(
        iter_directory(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}',
            "Content-Length": str(zip_size),
        },
    )


def _inside_suite(suite_root: str, current: str, name: str) -> bool:
    try:
        resolve_local_suite_target(suite_root, os.path.join(current, name))
        return True
    except ValueError:
        return False


def list_suite_files_local(suite_root: str, target_path: str) -> dict[str, Any]:
    root = os.path.realpath(os.path.expanduser(suite_root))
    target = os.path.realpath(os.path.expanduser(target_path))
    if target != root and not target.startswith(root + os.sep):
        return {"success": False, "error": "Illegal path"}
    if not os.path.isdir(target):
        return {"success": False, "error": "Directory not found"}

    items: list[dict[str, Any]] = []
    for name in sorted(os.listdir(target), key=str.lower):
        full_path = os.path.join(target, name)
        try:
            real_path = os.path.realpath(full_path)
            if real_path != root and not real_path.startswith(root + os.sep):
                continue
            st = os.stat(full_path)
        except OSError:
            continue
        is_dir = os.path.isdir(full_path)
        rel = os.path.relpath(full_path, root)
        lower = name.lower()
        items.append({
            "name": name,
            "path": "" if rel == "." else rel,
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "modified": int(st.st_mtime),
            "is_apk": not is_dir and lower.endswith(".apk"),
            "is_jar": not is_dir and lower.endswith(".jar"),
        })
    items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
    return {
        "success": True,
        "path": "" if target == root else os.path.relpath(target, root),
        "root": root,
        "items": items,
    }


def search_suite_files_local(
    suite_root: str, query: str, limit: int
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    query_lower = query.lower()
    for current, dirs, files in os.walk(suite_root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in sorted(dirs, key=str.lower):
            if query_lower and query_lower not in name.lower():
                continue
            full_path = os.path.join(current, name)
            matches.append({
                "name": name, "path": os.path.relpath(full_path, suite_root),
                "type": "directory", "size": 0,
                "modified": int(os.path.getmtime(full_path)),
            })
            if len(matches) >= limit:
                return matches
        for name in sorted(files, key=str.lower):
            if query_lower and query_lower not in name.lower():
                continue
            full_path = os.path.join(current, name)
            try:
                entry_stat = os.stat(full_path)
            except OSError:
                continue
            lower = name.lower()
            matches.append({
                "name": name, "path": os.path.relpath(full_path, suite_root),
                "type": "file", "size": entry_stat.st_size,
                "modified": int(entry_stat.st_mtime),
                "is_apk": lower.endswith(".apk"), "is_jar": lower.endswith(".jar"),
            })
            if len(matches) >= limit:
                return matches
    return matches
