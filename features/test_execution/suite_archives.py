"""Safe local extraction for downloaded test-suite archives."""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from typing import Any

from foundation.archives import (
    copy_archive_member,
    derive_suite_dir_name_from_archive,
    safe_extract_member_path,
    strip_common_archive_root,
)

from .suites import ensure_tradefed_executable


def extract_archive_local_with_progress(
    archive_path: str,
    extract_dir: str,
    target_dir_name: str,
    task_id: str | None = None,
    progress_callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    target_extract_dir = (
        os.path.join(extract_dir, target_dir_name)
        if target_dir_name
        else extract_dir
    )
    if target_dir_name:
        os.makedirs(target_extract_dir, exist_ok=True)

    files_count = 0
    extraction_budget: dict[str, int] = {}
    last_percentage = -1

    def progress(done: int, total: int) -> None:
        nonlocal last_percentage
        if not task_id or progress_callback is None:
            return
        percentage = int(done / total * 100) if total else 0
        if percentage == last_percentage:
            return
        last_percentage = percentage
        progress_callback(
            task_id,
            status="extracting",
            progress=min(float(percentage), 99.0),
            extracted_count=done,
            total_count=total,
        )

    def chmod_tradefed(path: str, name: str) -> None:
        if name.endswith("-tradefed"):
            ensure_tradefed_executable(path)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as archive:
            names = archive.namelist()
            if target_dir_name:
                _, mapped_names = strip_common_archive_root(names)
                total = len(
                    [
                        item
                        for item in mapped_names
                        if item[1] and not item[0].endswith("/")
                    ]
                )
                for source_name, relative_name in mapped_names:
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(
                        target_extract_dir, relative_name
                    )
                    if source_name.endswith("/"):
                        os.makedirs(target_path, exist_ok=True)
                        continue
                    member = archive.getinfo(source_name)
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError(
                            f"压缩包包含不安全符号链接: {source_name}"
                        )
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with (
                        archive.open(source_name) as source,
                        open(target_path, "wb") as destination,
                    ):
                        copy_archive_member(
                            source, destination, extraction_budget
                        )
                    chmod_tradefed(target_path, os.path.basename(target_path))
                    files_count += 1
                    progress(files_count, total)
            else:
                total = len(names)
                for member_name in names:
                    member = archive.getinfo(member_name)
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError(
                            f"压缩包包含不安全符号链接: {member_name}"
                        )
                    target_path = safe_extract_member_path(
                        extract_dir, member_name
                    )
                    if member_name.endswith("/"):
                        os.makedirs(target_path, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with (
                        archive.open(member_name) as source,
                        open(target_path, "wb") as destination,
                    ):
                        copy_archive_member(
                            source, destination, extraction_budget
                        )
                    chmod_tradefed(target_path, os.path.basename(target_path))
                    files_count += 1
                    progress(files_count, total)
    elif archive_path.endswith((".tar.gz", ".tgz", ".tar", ".tar.bz2")):
        mode = (
            "r:gz"
            if archive_path.endswith((".tar.gz", ".tgz"))
            else "r:bz2"
            if archive_path.endswith(".tar.bz2")
            else "r"
        )
        with tarfile.open(archive_path, mode) as archive:
            members = archive.getmembers()
            if target_dir_name:
                _, mapped_names = strip_common_archive_root(
                    [member.name for member in members]
                )
                name_map = dict(mapped_names)
                total = len([member for member in members if member.isfile()])
                for member in members:
                    relative_name = name_map.get(member.name, member.name)
                    if not relative_name:
                        continue
                    target_path = safe_extract_member_path(
                        target_extract_dir, relative_name
                    )
                    if member.isdir():
                        os.makedirs(target_path, exist_ok=True)
                    elif member.isfile():
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        source = archive.extractfile(member)
                        if source:
                            with source, open(target_path, "wb") as destination:
                                copy_archive_member(
                                    source, destination, extraction_budget
                                )
                            chmod_tradefed(
                                target_path, os.path.basename(target_path)
                            )
                            files_count += 1
                            progress(files_count, total)
            else:
                total = len([member for member in members if member.isfile()])
                for member in members:
                    target_path = safe_extract_member_path(
                        extract_dir, member.name
                    )
                    if member.isdir():
                        os.makedirs(target_path, exist_ok=True)
                        continue
                    if member.isfile():
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        source = archive.extractfile(member)
                        if source:
                            with source, open(target_path, "wb") as destination:
                                copy_archive_member(
                                    source, destination, extraction_budget
                                )
                        chmod_tradefed(target_path, os.path.basename(target_path))
                        files_count += 1
                        progress(files_count, total)
    else:
        command = [
            "tar",
            "-xf",
            archive_path,
            "-C",
            target_extract_dir if target_dir_name else extract_dir,
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr or result.stdout or "tar extraction failed"
            )

    extracted_name = target_dir_name or derive_suite_dir_name_from_archive(
        archive_path
    )
    return {
        "message": f"Extraction complete: {extracted_name}",
        "extracted_path": os.path.join(extract_dir, extracted_name),
        "files_count": files_count,
        "extract_method": "local",
    }
