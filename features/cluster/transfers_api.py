from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from features.auth import (
    CurrentUser,
    principal_owner_id,
    require_elevated_admin_when_auth_required,
    require_resource_owner,
)

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .models import ReportCopyCreate
from .repository import utc_now
from .transfer_ingest_api import router as transfer_ingest_router
from .transfer_support import operation_claim_for_request, worker_device


router = APIRouter()
def _transfer_root() -> Path:
    return service().repository.db_path.parent / "transfers"


def _firmware_root() -> Path:
    return service().repository.db_path.parent / "firmware"


def _report_copy_local_root() -> Path:
    return service().repository.db_path.parent / "report-copy-local"


def _online_worker(worker_id: str) -> dict:
    worker = service().repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    if not service().has_command_agent(worker_id):
        raise HTTPException(409, "operation requires a Worker agent")
    return worker


def _require_transfer_access(request: Request, transfer: dict) -> None:
    require_resource_owner(
        request,
        transfer.get("owner_id"),
        not_found_detail="transfer not found",
    )


def _known_suite(worker_id: str, suite_path: str) -> dict:
    suites = service().repository.list_suites(worker_id)
    if worker_id == service().config.local_worker_id and not suites:
        from .local_bridge import _scan_suites, _suite_roots

        suites = _scan_suites(_suite_roots())
    suite = next(
        (
            item
            for item in suites
            if item.get("tools_path") == suite_path and bool(item.get("available", 1))
        ),
        None,
    )
    if suite is None:
        raise HTTPException(409, "Selected suite is not available on the Worker")
    return suite


def _local_worker_config(data_root: Path):
    from worker_agent.config import WorkerConfig

    from .local_bridge import _suite_roots

    config = WorkerConfig.__new__(WorkerConfig)
    config.suite_roots = _suite_roots()
    config.data_root = data_root
    return config


def _complete_local_report_export(transfer: dict) -> dict:
    from worker_agent.inventory import prepare_suite_export

    metadata = transfer.get("metadata") or {}
    config = _local_worker_config(_report_copy_local_root())
    archive = None
    temporary = False
    destination = None
    destination_root = None
    try:
        archive, temporary = prepare_suite_export(
            config,
            {
                "transfer_id": transfer["id"],
                "suite_path": metadata["source_suite_path"],
                "path": f"results/{metadata['report_name']}",
                "directory": True,
            },
        )
        max_bytes = configured_max_bytes(
            "GMS_CLUSTER_TRANSFER_MAX_BYTES", service().config.transfer_max_bytes
        )
        size_bytes = archive.stat().st_size
        if size_bytes <= 0 or size_bytes > max_bytes:
            raise ValueError("report archive is empty or exceeds the transfer limit")

        destination_root = _transfer_root() / transfer["id"]
        destination_root.mkdir(parents=True, exist_ok=False)
        destination = destination_root / f"{transfer['id']}-{metadata['report_name']}.zip"
        digest = hashlib.sha256()
        with archive.open("rb") as source, destination.open("xb") as output:
            while block := source.read(4 * 1024 * 1024):
                digest.update(block)
                output.write(block)
        return service().repository.update_transfer(
            transfer["id"],
            status="completed",
            filename=destination.name,
            relative_path=str(destination.relative_to(_transfer_root())),
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
            completed_at=utc_now(),
        ) or transfer
    except Exception as exc:
        if destination is not None:
            destination.unlink(missing_ok=True)
        if destination_root is not None:
            shutil.rmtree(destination_root, ignore_errors=True)
        service().repository.update_transfer(
            transfer["id"], status="failed", error=str(exc)
        )
        raise
    finally:
        if temporary and archive is not None:
            archive.unlink(missing_ok=True)


def _import_report_to_local_worker(transfer: dict) -> dict:
    from worker_agent.inventory import import_suite_report

    metadata = transfer.get("metadata") or {}
    source = (_transfer_root() / str(transfer.get("relative_path") or "")).resolve()
    if not source.is_relative_to(_transfer_root().resolve()) or not source.is_file():
        raise ValueError("report copy archive is unavailable")

    data_root = _report_copy_local_root()
    staging = data_root / "report-copies" / transfer["id"]
    staging.mkdir(parents=True, exist_ok=False)
    archive = staging / "report.zip"
    try:
        shutil.copy2(source, archive)
        digest = hashlib.sha256()
        with archive.open("rb") as copied:
            while block := copied.read(4 * 1024 * 1024):
                digest.update(block)
        if (
            archive.stat().st_size != int(transfer.get("size_bytes") or 0)
            or digest.hexdigest() != str(transfer.get("sha256") or "")
        ):
            raise ValueError("report archive checksum mismatch")
        return import_suite_report(
            _local_worker_config(data_root),
            archive,
            str(metadata.get("target_suite_path") or ""),
            str(metadata.get("report_name") or ""),
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _report_copy_archive(transfer: dict) -> Path:
    path = (_transfer_root() / str(transfer.get("relative_path") or "")).resolve()
    if not path.is_relative_to(_transfer_root().resolve()) or not path.is_file():
        raise HTTPException(404, "report copy archive not found")
    return path


@router.post("/firmware/stage")
async def stage_worker_firmware(
    request: Request,
    worker_id: str = Form(...),
    devices: str = Form(...),
    reservation_id: str = Form(default=""),
    automation_run_id: str = Form(default=""),
    firmware_file: UploadFile = File(...),
):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    device_id = worker_device(
        worker_id,
        devices,
        "firmware flashing",
        reservation_id,
        automation_run_id,
        require_local_usb=True,
    )
    if automation_run_id:
        existing = service().repository.find_correlated_command(
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
    directory = _firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / filename
    digest = hashlib.sha256()
    total = 0
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", service().config.firmware_max_bytes
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
        command = service().repository.create_command({
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
            service().repository.claims.release(
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
    _authenticate(worker_id, authorization)
    if not re.fullmatch(r"fw-[a-f0-9]{32}", stage_id):
        raise HTTPException(400, "invalid firmware stage")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(filename).name)
    path = (_firmware_root() / stage_id / safe_name).resolve()
    if not path.is_relative_to(_firmware_root().resolve()) or not path.is_file():
        raise HTTPException(404, "staged firmware not found")
    return FileResponse(path, filename=safe_name)


@router.post("/gsi/stage")
async def stage_worker_gsi(
    request: Request,
    worker_id: str = Form(...),
    devices: str = Form(...),
    system_file: UploadFile | None = File(default=None),
    vendor_file: UploadFile | None = File(default=None),
):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
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
    directory = _firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    files = []
    combined_size = 0
    limit = configured_max_bytes(
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", service().config.firmware_max_bytes
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
        command = service().repository.create_command({
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
        service().repository.claims.release(
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
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    source_worker_id = source_worker_id.strip()
    system_path = system_path.strip()
    vendor_path = vendor_path.strip()
    if not system_path and not vendor_path and vendor_file is None:
        raise HTTPException(400, "at least one GSI image path or file is required")
    if (system_path or vendor_path) and source_worker_id != service().config.local_worker_id:
        # Controller 本机文件由 Controller 直接读取，无需 Worker agent。
        _online_worker(source_worker_id)
    device_id = worker_device(
        worker_id,
        devices,
        "GSI flashing",
        require_local_usb=True,
    )
    local_worker_id = service().config.local_worker_id
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
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _firmware_root() / stage_id
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
        command = service().repository.create_command({
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
        service().repository.claims.release(
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
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _firmware_root() / stage_id
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
        command = service().repository.create_command({
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
        service().repository.claims.release(
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
        transfer = service().repository.create_transfer(
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
        command = service().repository.create_command({
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
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    requested_ids = [item.strip() for item in transfer_ids.split(",") if item.strip()]
    if not requested_ids:
        raise HTTPException(400, "no source transfers provided")
    transfers = []
    for transfer_id in requested_ids:
        transfer = service().repository.get_transfer(transfer_id)
        if not transfer or transfer.get("status") != "completed":
            raise HTTPException(409, f"transfer {transfer_id} is not complete")
        _require_transfer_access(request, transfer)
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
        "GMS_CLUSTER_FIRMWARE_MAX_BYTES", service().config.firmware_max_bytes
    )
    stage_id = "fw-" + os.urandom(16).hex()
    claim_payload = operation_claim_for_request(request, worker_id, device_id, stage_id)
    directory = _firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    transfer_root = _transfer_root().resolve()
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
        command = service().repository.create_command({
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
        service().repository.claims.release(
            claim_payload["claim_source_id"], status="failed"
        )
        raise
    for transfer in transfers:
        shutil.rmtree(_transfer_root() / str(transfer["id"]), ignore_errors=True)
        service().repository.update_transfer(str(transfer["id"]), status="consumed")
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


def cleanup_staged_firmware(command: dict) -> None:
    """Remove Controller staging after a terminal flash acknowledgement."""
    if command.get("command_type") not in {"flash_firmware", "flash_gsi"}:
        return
    if command.get("status") not in {"completed", "failed", "cancelled"}:
        return
    stage_id = str((command.get("payload") or {}).get("stage_id") or "")
    if re.fullmatch(r"fw-[a-f0-9]{32}", stage_id):
        shutil.rmtree(_firmware_root() / stage_id, ignore_errors=True)


@router.post("/suites/export")
def create_suite_export(
    request: Request,
    worker_id: str = Query(...),
    suite_path: str = Query(...),
    path: str = Query(...),
    directory: bool = Query(default=False),
):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    transfer = service().repository.create_transfer(
        worker_id,
        owner_id=principal_owner_id(request),
        metadata={"suite_path": suite_path, "path": path, "directory": directory},
    )
    command = service().repository.create_command({
        "worker_id": worker_id,
        "command_type": "suite_export",
        "payload": {
            "transfer_id": transfer["id"],
            "suite_path": suite_path,
            "path": path,
            "directory": directory,
        },
    })
    return {"success": True, "transfer": transfer, "command_id": command["id"]}


@router.post("/suites/report-copies")
async def create_report_copy(
    body: ReportCopyCreate,
    request: Request,
):
    """Export one results/<timestamp> directory for a cross-Worker copy."""
    local_worker_id = service().config.local_worker_id
    if body.source_worker_id == body.target_worker_id:
        raise HTTPException(400, "source and target Workers must be different")
    _require_cluster_enabled(
        remote=(
            body.source_worker_id != local_worker_id
            or body.target_worker_id != local_worker_id
        )
    )
    source_worker = service().repository.get_worker(body.source_worker_id)
    target_worker = service().repository.get_worker(body.target_worker_id)
    if not source_worker or source_worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "source Worker is not online")
    if not target_worker or target_worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "target Worker is not online")
    if body.source_worker_id != local_worker_id:
        _online_worker(body.source_worker_id)
    if body.target_worker_id != local_worker_id:
        _online_worker(body.target_worker_id)
    _known_suite(body.source_worker_id, body.source_suite_path)
    _known_suite(body.target_worker_id, body.target_suite_path)

    owner_id = principal_owner_id(request)
    metadata = {
        "source_worker_id": body.source_worker_id,
        "source_suite_path": body.source_suite_path,
        "path": f"results/{body.report_name}",
        "directory": True,
        "report_name": body.report_name,
        "target_worker_id": body.target_worker_id,
        "target_suite_path": body.target_suite_path,
    }
    transfer = service().repository.create_transfer(
        body.source_worker_id,
        transfer_type="report_copy",
        owner_id=owner_id,
        metadata=metadata,
    )
    if body.source_worker_id == local_worker_id:
        try:
            transfer = await asyncio.to_thread(_complete_local_report_export, transfer)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"failed to prepare local report: {exc}") from exc
        return {
            "success": True,
            "copy_id": transfer["id"],
            "transfer": transfer,
            "export_status": "completed",
        }

    command = service().repository.create_command({
        "worker_id": body.source_worker_id,
        "command_type": "suite_export",
        "payload": {
            "transfer_id": transfer["id"],
            "suite_path": body.source_suite_path,
            "path": f"results/{body.report_name}",
            "directory": True,
            "owner_id": owner_id,
        },
    })
    return {
        "success": True,
        "copy_id": transfer["id"],
        "transfer": transfer,
        "export_status": "created",
        "export_command_id": command["id"],
    }


@router.post("/suites/report-copies/{transfer_id}/import")
async def import_report_copy(transfer_id: str, request: Request):
    """Import a completed report-copy transfer into its locked target suite."""
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer.get("transfer_type") != "report_copy":
        raise HTTPException(404, "report copy not found")
    _require_transfer_access(request, transfer)
    if transfer.get("status") != "completed":
        raise HTTPException(409, transfer.get("error") or "report export is not complete")

    metadata = transfer.get("metadata") or {}
    target_worker_id = str(metadata.get("target_worker_id") or "")
    target_suite_path = str(metadata.get("target_suite_path") or "")
    _known_suite(target_worker_id, target_suite_path)
    local_worker_id = service().config.local_worker_id

    existing_command_id = str(metadata.get("target_command_id") or "")
    if existing_command_id:
        command = service().repository.get_command(existing_command_id)
        if command:
            return {
                "success": True,
                "copy_id": transfer_id,
                "status": command.get("status", "accepted"),
                "command_id": existing_command_id,
                "result": command.get("result") or {},
                "deduplicated": True,
            }
    if metadata.get("import_status") == "completed":
        return {
            "success": True,
            "copy_id": transfer_id,
            "status": "completed",
            "result": metadata.get("import_result") or {},
            "deduplicated": True,
        }

    if target_worker_id == local_worker_id:
        try:
            result = await asyncio.to_thread(_import_report_to_local_worker, transfer)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"failed to import local report: {exc}") from exc
        updated_metadata = {
            **metadata,
            "import_status": "completed",
            "import_result": result,
        }
        service().repository.update_transfer(
            transfer_id,
            metadata_json=json.dumps(updated_metadata, separators=(",", ":")),
        )
        return {
            "success": True,
            "copy_id": transfer_id,
            "status": "completed",
            "result": result,
        }

    _online_worker(target_worker_id)
    owner_id = str(transfer.get("owner_id") or "")
    command = service().repository.create_command({
        "worker_id": target_worker_id,
        "command_type": "report_import",
        "payload": {
            "transfer_id": transfer_id,
            "target_suite_path": target_suite_path,
            "report_name": metadata.get("report_name", ""),
            "size_bytes": transfer.get("size_bytes", 0),
            "sha256": transfer.get("sha256", ""),
            "owner_id": owner_id,
        },
    })
    updated_metadata = {
        **metadata,
        "import_status": "accepted",
        "target_command_id": command["id"],
    }
    service().repository.update_transfer(
        transfer_id,
        metadata_json=json.dumps(updated_metadata, separators=(",", ":")),
    )
    return {
        "success": True,
        "copy_id": transfer_id,
        "status": "accepted",
        "command_id": command["id"],
    }


@router.get("/workers/{worker_id}/report-copies/{transfer_id}")
def download_report_copy_for_worker(
    worker_id: str,
    transfer_id: str,
    authorization: str | None = Header(default=None),
):
    """Allow only the locked target Worker to fetch a completed report archive."""
    _authenticate(worker_id, authorization)
    transfer = service().repository.get_transfer(transfer_id)
    if (
        not transfer
        or transfer.get("transfer_type") != "report_copy"
        or transfer.get("status") != "completed"
    ):
        raise HTTPException(404, "report copy not found")
    metadata = transfer.get("metadata") or {}
    if metadata.get("target_worker_id") != worker_id:
        raise HTTPException(404, "report copy not found")
    return FileResponse(
        _report_copy_archive(transfer),
        filename=f"{metadata.get('report_name') or 'report'}.zip",
        media_type="application/zip",
    )


router.include_router(transfer_ingest_router)
