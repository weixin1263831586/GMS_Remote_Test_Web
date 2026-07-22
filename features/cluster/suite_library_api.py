"""Controller suite-library listing and authenticated Worker downloads."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse

from features.auth import CurrentUser, require_role_when_auth_required

from .api import _authenticate
from .local_bridge import _suite_roots


router = APIRouter()


def controller_suite_archives() -> list[Path]:
    """Return archives directly inside configured Controller suite roots."""

    extensions = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
    archives: list[Path] = []
    for root in _suite_roots():
        resolved = root.expanduser().resolve()
        if resolved.is_dir():
            archives.extend(
                path
                for path in resolved.iterdir()
                if path.is_file() and path.name.lower().endswith(extensions)
            )
    return archives


def _archive(filename: str) -> Path:
    if Path(filename).name != filename:
        raise HTTPException(400, "invalid archive filename")
    path = next(
        (item for item in controller_suite_archives() if item.name == filename),
        None,
    )
    if path is None:
        raise HTTPException(404, "suite archive not found")
    return path


def _download(path: Path) -> FileResponse:
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
    )


@router.get("/suite-library")
def controller_suite_library(
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    archives = []
    for path in controller_suite_archives():
        stat = path.stat()
        archives.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            },
        )
    archives.sort(key=lambda item: item["modified"], reverse=True)
    return {"success": True, "archives": archives}


@router.get("/suite-library/{filename}")
def download_controller_suite_archive(
    filename: str,
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    return _download(_archive(filename))


@router.get("/suite-library-download/{safe_filename}/{filename}")
def download_controller_suite_archive_named(
    safe_filename: str,
    filename: str,
    worker_id: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", safe_filename):
        raise HTTPException(400, "invalid archive filename")
    return _download(_archive(filename))
