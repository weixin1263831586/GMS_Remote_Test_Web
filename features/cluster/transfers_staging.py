"""Firmware/GSI staging routes (split from transfers_api, 2026-09 review).

上传/下载 staging 镜像、GSI stage-from-source/transfer、固件清理。
共享 helper（_transfer_root/_online_worker 等）经函数内延迟 import
自 transfers_api，避免循环依赖。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse

from features.auth import (
    CurrentUser,
    principal_owner_id,
    require_elevated_admin_when_auth_required,
)

from .config import configured_max_bytes
from .transfer_support import operation_claim_for_request, worker_device


router = APIRouter()


def _shared():
    """延迟引用 transfers_api 的共享 helper（避免循环 import）。"""
    from . import transfers_api

    return transfers_api


@router.post("/firmware/stage")
async def stage_worker_firmware(
    request: Request,
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
    worker_id: str = Form(...),
    devices: str = Form(...),
    reservation_id: str = Form(default=""),
    automation_run_id: str = Form(default=""),
    firmware_file: UploadFile = File(...),
):
    _shared()._require_cluster_enabled(remote=worker_id != _shared().service().config.local_worker_id)
    _shared()._online_worker(worker_id)
    device_id = worker_device(
        worker_id,
        devices,
        "firmware flashing",
        reservation_id,
        automation_run_id,
        require_local_usb=True,
    )
    if automation_run_id:
        existing = _shared().service().repository.find_correlated_command(
            worker_id, "flash_firmware", "automation_run_id", automation_run_id
        )
        if existing:
            return {
                "success": True,
                "stage_id": (existing.get("payload") or {}).get("stage_id", ""),
                "command_id": existing["id"],
                "size_bytes": (existing.get("payload") or {}).get("size_bytes", 0),
                "deduplicated": True,
            }
    filename = re.sub(
        r"[^A-Za-z0-9._+-]",
        "_",
        Path(firmware_file.filename or "firmware.img").name,
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(
        request,
        worker_id,
        device_id,
        stage_id,
        reservation_id=reservation_id,
    )
    directory = _shared()._firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / filename
    digest = hashlib.sha256()
    total = 0
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", _shared().service().config.firmware_max_bytes
    )
    try:
        with target.open("wb") as output:
            while chunk := await firmware_file.read(4 * 1024 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, "firmware image is too large")
                digest.update(chunk)
                output.write(chunk)
        if not total:
            raise HTTPException(400, "firmware image is empty")
        command = _shared().service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "flash_firmware",
            "payload": {
                "stage_id": stage_id,
                "filename": filename,
                "sha256": digest.hexdigest(),
                "size_bytes": total,
                "devices": [device_id],
                "reservation_id": reservation_id,
                "automation_run_id": automation_run_id,
                **claim_payload,
            },
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        if claim_payload["release_claim_on_terminal"]:
            _shared().service().repository.claims.release(
                claim_payload["claim_source_id"], status="failed"
            )
        raise
    return {
        "success": True,
        "stage_id": stage_id,
        "command_id": command["id"],
        "size_bytes": total,
    }


@router.get("/workers/{worker_id}/firmware/{stage_id}")
def download_staged_firmware(
    worker_id: str,
    stage_id: str,
    filename: str = Query(...),
    authorization: str | None = Header(default=None),
):
    _shared()._authenticate(worker_id, authorization)
    if not re.fullmatch(r"fw-[a-f0-9]{32}", stage_id):
        raise HTTPException(400, "invalid firmware stage")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(filename).name)
    path = (_shared()._firmware_root() / stage_id / safe_name).resolve()
    if not path.is_relative_to(_shared()._firmware_root().resolve()) or not path.is_file():
        raise HTTPException(404, "staged firmware not found")
    return FileResponse(path, filename=safe_name)


@router.post("/gsi/stage")
async def stage_worker_gsi(
    request: Request,
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
    worker_id: str = Form(...),
    devices: str = Form(...),
    system_file: UploadFile | None = File(default=None),
    vendor_file: UploadFile | None = File(default=None),
):
    _shared()._require_cluster_enabled(remote=worker_id != _shared().service().config.local_worker_id)
    _shared()._online_worker(worker_id)
    if system_file is None and vendor_file is None:
        raise HTTPException(400, "at least one GSI image is required")
    device_id = worker_device(
        worker_id,
        devices,
        "GSI flashing",
        require_local_usb=True,
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(
        request, worker_id, device_id, stage_id
    )
    directory = _shared()._firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    files = []
    combined_size = 0
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", _shared().service().config.firmware_max_bytes
    )
    try:
        for kind, upload in (("system", system_file), ("vendor", vendor_file)):
            if upload is None:
                continue
            name = f"{kind}.img"
            target = directory / name
            digest = hashlib.sha256()
            total = 0
            with target.open("wb") as output:
                while chunk := await upload.read(4 * 1024 * 1024):
                    total += len(chunk)
                    combined_size += len(chunk)
                    if combined_size > limit:
                        raise HTTPException(413, "GSI images are too large")
                    digest.update(chunk)
                    output.write(chunk)
            if not total:
                raise HTTPException(400, f"{kind} image is empty")
            files.append({
                "kind": kind,
                "filename": name,
                "size_bytes": total,
                "sha256": digest.hexdigest(),
            })
        command = _shared().service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "flash_gsi",
            "payload": {
                "stage_id": stage_id,
                "files": files,
                "devices": [device_id],
                **claim_payload,
            },
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        _shared().service().repository.claims.release(
            claim_payload["claim_source_id"], status="failed"
        )
        raise
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


async def _write_staged_image(
    upload: UploadFile, directory: Path, kind: str, limit: int
) -> dict:
    name = f"{kind}.img"
    target = directory / name
    digest = hashlib.sha256()
    total = 0
    with target.open("wb") as output:
        while chunk := await upload.read(4 * 1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, "GSI images are too large")
            digest.update(chunk)
            output.write(chunk)
    if not total:
        raise HTTPException(400, f"{kind} image is empty")
    return {
        "kind": kind,
        "filename": name,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
    }


def _copy_controller_file_into(source: Path, target: Path, limit: int) -> dict:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while block := reader.read(4 * 1024 * 1024):
            total += len(block)
            if total > limit:
                raise HTTPException(413, "GSI images are too large")
            digest.update(block)
            writer.write(block)
    if not total:
        raise HTTPException(400, "GSI image is empty")
    return {
        "kind": Path(target).stem,
        "filename": Path(target).name,
        "size_bytes": total,
        "sha256": digest.hexdigest(),
    }


@router.post("/gsi/stage-from-source")
async def stage_worker_gsi_from_source(
    request: Request,
    worker_id: str = Form(...),
    devices: str = Form(...),
    source_worker_id: str = Form(...),
    system_path: str = Form(default=""),
    vendor_path: str = Form(default=""),
    vendor_file: UploadFile | None = File(default=None),
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """Stage GSI images that already live on a Worker/Controller host.

    - source == target Worker: one flash_gsi command with local_sources
      (the Worker copies the image into its own staging).
    - source == Controller local worker: images are staged on the Controller.
    - other remote Worker: creates per-image transfers + file_transfer
      commands; the caller finishes with /gsi/stage-from-transfer.
    """
    _shared()._require_cluster_enabled(remote=worker_id != _shared().service().config.local_worker_id)
    _shared()._online_worker(worker_id)
    source_worker_id = source_worker_id.strip()
    system_path = system_path.strip()
    vendor_path = vendor_path.strip()
    if not system_path and not vendor_path and vendor_file is None:
        raise HTTPException(400, "at least one GSI image path or file is required")
    if (system_path or vendor_path) and source_worker_id != _shared().service().config.local_worker_id:
        # Controller 本机文件由 Controller 直接读取，无需 Worker agent。
        _shared()._online_worker(source_worker_id)
    device_id = worker_device(
        worker_id,
        devices,
        "GSI flashing",
        require_local_usb=True,
    )
    local_worker_id = _shared().service().config.local_worker_id
    if source_worker_id == worker_id:
        return await _stage_gsi_from_local_sources(
            request, worker_id, device_id, system_path, vendor_path, vendor_file
        )
    if source_worker_id == local_worker_id:
        return await _stage_gsi_from_controller_files(
            request, worker_id, device_id, system_path, vendor_path, vendor_file
        )
    return _stage_gsi_pull_from_worker(
        request, worker_id, device_id, source_worker_id, system_path, vendor_path
    )


async def _stage_gsi_from_local_sources(
    request: Request,
    worker_id: str,
    device_id: str,
    system_path: str,
    vendor_path: str,
    vendor_file: UploadFile | None,
):
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", _shared().service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _shared()._firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    try:
        files = []
        if vendor_file is not None:
            files.append(await _write_staged_image(vendor_file, directory, "vendor", limit))
        local_sources = [
            {"kind": kind, "path": path_text}
            for kind, path_text in (("system", system_path), ("vendor", vendor_path))
            if path_text
        ]
        command = _shared().service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "flash_gsi",
            "payload": {
                "stage_id": stage_id,
                "files": files,
                "local_sources": local_sources,
                "devices": [device_id],
                **claim_payload,
            },
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        _shared().service().repository.claims.release(
            claim_payload["claim_source_id"], status="failed"
        )
        raise
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


async def _stage_gsi_from_controller_files(
    request: Request,
    worker_id: str,
    device_id: str,
    system_path: str,
    vendor_path: str,
    vendor_file: UploadFile | None,
):
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", _shared().service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _shared()._firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    try:
        files = []
        if vendor_file is not None:
            files.append(await _write_staged_image(vendor_file, directory, "vendor", limit))
        for kind, path_text in (("system", system_path), ("vendor", vendor_path)):
            if not path_text:
                continue
            if any(item["kind"] == kind for item in files):
                raise HTTPException(409, f"duplicate {kind} image source")
            source = Path(path_text).expanduser().resolve()
            if not source.is_file():
                raise HTTPException(400, f"file not found on Controller: {path_text}")
            files.append(_copy_controller_file_into(source, directory / f"{kind}.img", limit))
        command = _shared().service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "flash_gsi",
            "payload": {
                "stage_id": stage_id,
                "files": files,
                "devices": [device_id],
                **claim_payload,
            },
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        _shared().service().repository.claims.release(
            claim_payload["claim_source_id"], status="failed"
        )
        raise
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


def _stage_gsi_pull_from_worker(
    request: Request,
    worker_id: str,
    device_id: str,
    source_worker_id: str,
    system_path: str,
    vendor_path: str,
):
    pulls = []
    for kind, path_text in (("system", system_path), ("vendor", vendor_path)):
        if not path_text:
            continue
        transfer = _shared().service().repository.create_transfer(
            source_worker_id,
            transfer_type="gsi_pull",
            owner_id=principal_owner_id(request),
            metadata={
                "target_worker_id": worker_id,
                "device_id": device_id,
                "kind": kind,
                "source_path": path_text,
            },
        )
        command = _shared().service().repository.create_command({
            "worker_id": source_worker_id,
            "command_type": "file_transfer",
            "payload": {
                "transfer_id": transfer["id"],
                "source_path": path_text,
                "owner_id": principal_owner_id(request),
            },
        })
        pulls.append({
            "kind": kind,
            "worker_id": source_worker_id,
            "transfer_id": transfer["id"],
            "command_id": command["id"],
        })
    return {"success": True, "pulls": pulls}


@router.post("/gsi/stage-from-transfer")
async def finalize_worker_gsi_from_transfer(
    request: Request,
    worker_id: str = Form(...),
    devices: str = Form(...),
    transfer_ids: str = Form(...),
    vendor_file: UploadFile | None = File(default=None),
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """Stage pulled cross-Worker GSI images and create the flash command."""
    _shared()._require_cluster_enabled(remote=worker_id != _shared().service().config.local_worker_id)
    _shared()._online_worker(worker_id)
    requested_ids = [item.strip() for item in transfer_ids.split(",") if item.strip()]
    if not requested_ids:
        raise HTTPException(400, "no source transfers provided")
    transfers = []
    for transfer_id in requested_ids:
        transfer = _shared().service().repository.get_transfer(transfer_id)
        if not transfer or transfer.get("status") != "completed":
            raise HTTPException(409, f"transfer {transfer_id} is not complete")
        _shared()._require_transfer_access(request, transfer)
        metadata = transfer.get("metadata") or {}
        if metadata.get("target_worker_id") != worker_id:
            raise HTTPException(409, "transfer does not target this worker")
        transfers.append(transfer)
    device_id = worker_device(
        worker_id,
        devices,
        "GSI flashing",
        require_local_usb=True,
    )
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", _shared().service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _shared()._firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    transfer_root = _shared()._transfer_root().resolve()
    files = []
    try:
        if vendor_file is not None:
            files.append(await _write_staged_image(vendor_file, directory, "vendor", limit))
        for transfer in transfers:
            metadata = transfer.get("metadata") or {}
            kind = str(metadata.get("kind") or "")
            if kind not in {"system", "vendor"}:
                raise HTTPException(400, "invalid GSI transfer kind")
            if any(item["kind"] == kind for item in files):
                raise HTTPException(409, f"duplicate {kind} image source")
            source = (transfer_root / str(transfer.get("relative_path") or "")).resolve()
            if not source.is_relative_to(transfer_root) or not source.is_file():
                raise HTTPException(404, "transferred GSI image not found")
            staged = _copy_controller_file_into(source, directory / f"{kind}.img", limit)
            if staged["sha256"] != str(transfer.get("sha256") or ""):
                raise HTTPException(409, "transferred GSI image checksum mismatch")
            files.append(staged)
        command = _shared().service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "flash_gsi",
            "payload": {
                "stage_id": stage_id,
                "files": files,
                "devices": [device_id],
                **claim_payload,
            },
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        _shared().service().repository.claims.release(
            claim_payload["claim_source_id"], status="failed"
        )
        raise
    for transfer in transfers:
        shutil.rmtree(_shared()._transfer_root() / str(transfer["id"]), ignore_errors=True)
        _shared().service().repository.update_transfer(str(transfer["id"]), status="consumed")
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


def cleanup_staged_firmware(command: dict) -> None:
    """Remove Controller staging after a terminal flash acknowledgement."""
    if command.get("command_type") not in {"flash_firmware", "flash_gsi"}:
        return
    if command.get("status") not in {"completed", "failed", "cancelled"}:
        return
    stage_id = str((command.get("payload") or {}).get("stage_id") or "")
    if re.fullmatch(r"fw-[a-f0-9]{32}", stage_id):
        shutil.rmtree(_shared()._firmware_root() / stage_id, ignore_errors=True)


