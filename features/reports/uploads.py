"""Bounded staging for report uploads."""

from __future__ import annotations

from fastapi import UploadFile

from foundation.uploads import safe_upload_target_path, save_upload_to_path


MAX_REPORT_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


class ReportUploadTooLargeError(ValueError):
    pass


async def stage_report_uploads(
    files: list[UploadFile],
    temp_dir: str,
    *,
    allow_nested: bool,
) -> list[tuple[str, int]]:
    staged = []
    total = 0
    for uploaded in files:
        if not uploaded.filename:
            continue
        remaining = MAX_REPORT_UPLOAD_BYTES - total
        if remaining <= 0:
            raise ReportUploadTooLargeError('上传文件总大小超过限制')
        path = safe_upload_target_path(
            temp_dir,
            uploaded.filename,
            allow_nested=allow_nested,
        )
        try:
            size = await save_upload_to_path(uploaded, path, remaining)
        except ValueError as exc:
            raise ReportUploadTooLargeError('上传文件总大小超过限制') from exc
        total += size
        staged.append((path, size))
    return staged
