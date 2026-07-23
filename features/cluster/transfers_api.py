from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from features.auth import (
    principal_owner_id,
    require_resource_owner,
)

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .models import TransferComplete
from .repository import utc_now
from .transfer_support import operation_claim_for_request, worker_device


router = APIRouter()
def _transfer_root() -> Path:
    return service().repository.db_path.parent / "transfers"


def _firmware_root() -> Path:
    return service().repository.db_path.parent / "firmware"
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
        worker_id, devices, "firmware flashing", reservation_id, automation_run_id
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
    system_file: UploadFile = File(...),
    vendor_file: UploadFile | None = File(default=None),
):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    device_id = worker_device(worker_id, devices, "GSI flashing")
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


@router.post("/devices/export")
def create_device_export(
    request: Request,
    worker_id: str = Query(...),
    device_id: str = Query(...),
    path: str = Query(...),
):
    from worker_agent.android_inspection import validate_export_path

    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    _online_worker(worker_id)
    device = worker_device(worker_id, device_id, "device file export")
    try:
        safe_path = validate_export_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    owner_id = principal_owner_id(request)
    operation_id = f"device-export-{uuid.uuid4().hex}"
    claim_source = f"operation:{operation_id}"
    try:
        records = service().repository.acquire_device_operation_claim(
            worker_id, [device], owner_id=owner_id,
            source_type="cluster-device-export", source_id=claim_source,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    lease_tokens = service().repository.claim_fencing_tokens(
        records, operation_id
    )
    request.state.device_lease_tokens = [
        {**token, "owner_id": owner_id} for token in lease_tokens
    ]
    try:
        transfer = service().repository.create_transfer(
            worker_id,
            transfer_type="device_export",
            owner_id=owner_id,
            metadata={"device_id": device, "source_path": safe_path},
        )
        command = service().repository.create_command({
            "worker_id": worker_id,
            "command_type": "device_export",
            "operation_id": operation_id,
            "payload": {
                "transfer_id": transfer["id"], "devices": [device],
                "path": safe_path, "owner_id": owner_id,
                "claim_source_id": claim_source,
                "release_claim_on_terminal": True,
                "lease_tokens": lease_tokens,
            },
        })
    except Exception:
        service().repository.claims.release(claim_source, status="failed")
        raise
    return {"success": True, "transfer": transfer, "command_id": command["id"]}


@router.put("/transfers/{transfer_id}/chunks/{index}")
async def upload_transfer_chunk(
    transfer_id: str,
    index: int,
    request: Request,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["worker_id"] != worker_id:
        raise HTTPException(404, "transfer not found for worker")
    if transfer["status"] not in {"created", "uploading"}:
        raise HTTPException(409, "transfer no longer accepts chunks")
    if index < 0 or index > 100000:
        raise HTTPException(400, "invalid chunk index")
    body = await request.body()
    max_chunk = int(
        os.getenv("GMS_CLUSTER_TRANSFER_CHUNK_BYTES", str(8 * 1024 * 1024))
    )
    if not body or len(body) > max_chunk:
        raise HTTPException(413, "invalid transfer chunk size")
    chunk_dir = _transfer_root() / transfer_id / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / f"{index:08d}.part").write_bytes(body)
    service().repository.update_transfer(transfer_id, status="uploading")
    return {"success": True, "index": index, "size_bytes": len(body)}


@router.post("/transfers/{transfer_id}/complete")
def complete_transfer(
    transfer_id: str,
    body: TransferComplete,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["worker_id"] != worker_id:
        raise HTTPException(404, "transfer not found for worker")
    if transfer["status"] not in {"created", "uploading"}:
        raise HTTPException(409, "transfer cannot be completed from its current state")
    max_bytes = configured_max_bytes(
        "GMS_CLUSTER_TRANSFER_MAX_BYTES", service().config.transfer_max_bytes
    )
    if body.size_bytes > max_bytes:
        raise HTTPException(413, "transfer is too large")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(body.filename).name)
    if not safe_name:
        raise HTTPException(400, "invalid transfer filename")
    root = _transfer_root() / transfer_id
    chunks = [
        root / "chunks" / f"{index:08d}.part"
        for index in range(body.chunk_count)
    ]
    if not all(path.is_file() for path in chunks):
        raise HTTPException(409, "transfer chunks are incomplete")
    destination = root / safe_name
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as output:
        for chunk in chunks:
            data = chunk.read_bytes()
            total += len(data)
            if total > max_bytes:
                destination.unlink(missing_ok=True)
                raise HTTPException(413, "transfer is too large")
            digest.update(data)
            output.write(data)
    if total != body.size_bytes or digest.hexdigest() != body.sha256:
        destination.unlink(missing_ok=True)
        raise HTTPException(409, "transfer checksum or size mismatch")
    for chunk in chunks:
        chunk.unlink(missing_ok=True)
    transfer = service().repository.update_transfer(
        transfer_id,
        status="completed",
        filename=safe_name,
        relative_path=str(destination.relative_to(_transfer_root())),
        size_bytes=total,
        sha256=body.sha256,
        completed_at=utc_now(),
    )
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str, request: Request):
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    _require_transfer_access(request, transfer)
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}/download")
def download_transfer(transfer_id: str, request: Request):
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["status"] != "completed":
        raise HTTPException(409, "transfer is not complete")
    _require_transfer_access(request, transfer)
    path = (_transfer_root() / transfer["relative_path"]).resolve()
    if not path.is_relative_to(_transfer_root().resolve()) or not path.is_file():
        raise HTTPException(404, "transfer file not found")
    # 下载文件名：去掉 transfer_id 前缀；logs/results 目录导出追加类型后缀
    download_name = transfer["filename"]
    prefix = f"{transfer_id}-"
    if download_name.startswith(prefix):
        download_name = download_name[len(prefix):]
    segments = [s for s in str((transfer.get("metadata") or {}).get("path") or "").split("/") if s]
    kind = segments[0].lower() if segments else ""
    if kind in {"logs", "results"}:
        stem, dot, ext = download_name.rpartition(".")
        download_name = f"{stem}-{kind}.{ext}" if dot else f"{download_name}-{kind}"
    return FileResponse(path, filename=download_name)


@router.post("/transfers/{transfer_id}/apk-analysis")
def import_transfer_for_apk_analysis(transfer_id: str, request: Request):
    from features.test_execution import runtime as test_runtime

    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    _require_transfer_access(request, transfer)
    metadata = transfer.get("metadata") or {}
    allowed_type = transfer.get("transfer_type") in {"device_export", "suite_export"}
    suite_file = transfer.get("transfer_type") != "suite_export" or not metadata.get("directory")
    if not allowed_type or not suite_file or transfer.get("status") != "completed":
        raise HTTPException(409, "APK/JAR transfer is not complete")
    required = (
        test_runtime.create_apk_task,
        test_runtime.normalize_apk_filename,
        test_runtime.safe_join,
        test_runtime.cleanup_files,
    )
    if not all(required) or not test_runtime.apk_upload_dir:
        raise HTTPException(500, "APK analysis dependencies are not initialized")
    source = (_transfer_root() / transfer["relative_path"]).resolve()
    if not source.is_relative_to(_transfer_root().resolve()) or not source.is_file():
        raise HTTPException(404, "transferred device file not found")
    if source.stat().st_size > test_runtime.apk_max_file_size:
        raise HTTPException(413, "device APK/JAR exceeds APK analysis size limit")
    task_id = str(uuid.uuid4())
    try:
        filename = test_runtime.normalize_apk_filename(transfer["filename"])
        task_dir = test_runtime.safe_join(test_runtime.apk_upload_dir, task_id)
        os.makedirs(task_dir, exist_ok=False)
        target = test_runtime.safe_join(task_dir, filename)
        shutil.copy2(source, target)
        owner_id = principal_owner_id(request)
        test_runtime.create_apk_task(task_id, target, filename, owner_id)
        with test_runtime.global_state.apk_analysis_tasks_lock:
            task = test_runtime.global_state.apk_analysis_tasks.get(task_id, {})
            task.update({
                "source_type": (
                    "cluster_device" if transfer["transfer_type"] == "device_export"
                    else "cluster_suite"
                ),
                "source_worker_id": transfer["worker_id"],
                "source_device_id": (transfer.get("metadata") or {}).get("device_id", ""),
                "source_path": (
                    (transfer.get("metadata") or {}).get("source_path")
                    or (transfer.get("metadata") or {}).get("path", "")
                ),
                "source_suite_path": (transfer.get("metadata") or {}).get("suite_path", ""),
                "source_transfer_id": transfer_id,
            })
    except ValueError as exc:
        test_runtime.cleanup_files([locals().get("target", "")])
        shutil.rmtree(locals().get("task_dir", ""), ignore_errors=True)
        raise HTTPException(429, str(exc)) from exc
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "filename": filename,
            "size": source.stat().st_size,
            "worker_id": transfer["worker_id"],
            "device_id": (transfer.get("metadata") or {}).get("device_id", ""),
            "transfer_id": transfer_id,
        },
    }


@router.post("/transfers/{transfer_id}/report-analysis")
async def analyze_transferred_log_directory(transfer_id: str, request: Request):
    from features.reports import ArchiveReportAnalyzer

    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    _require_transfer_access(request, transfer)
    metadata = transfer.get("metadata") or {}
    if (
        transfer.get("transfer_type") != "suite_export"
        or not metadata.get("directory")
        or transfer.get("status") != "completed"
    ):
        raise HTTPException(409, "suite log directory transfer is not complete")
    archive = (_transfer_root() / transfer["relative_path"]).resolve()
    if not archive.is_relative_to(_transfer_root().resolve()) or not zipfile.is_zipfile(archive):
        raise HTTPException(400, "transferred log archive is invalid")

    def extract_and_analyze() -> dict:
        with tempfile.TemporaryDirectory(prefix="gms-cluster-log-") as temp_dir:
            destination = Path(temp_dir).resolve()
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if len(members) > 100_000:
                    raise ValueError("log archive contains too many files")
                total = sum(max(0, item.file_size) for item in members)
                max_bytes = configured_max_bytes(
                    "GMS_CLUSTER_LOG_ANALYSIS_MAX_BYTES",
                    service().config.log_analysis_max_bytes,
                )
                if total > max_bytes or any(
                    not (destination / item.filename).resolve().is_relative_to(destination)
                    for item in members
                ):
                    raise ValueError("log archive is too large or contains an unsafe path")
                bundle.extractall(destination)
            result = ArchiveReportAnalyzer().analyze_log_dir(str(destination))
            if not result:
                raise ValueError("transferred directory contains no analyzable host logs")
            result.setdefault("report_type", "log")
            result.setdefault(
                "report_name", Path(str(metadata.get("path") or "suite log")).name
            )
            result["provenance"] = {
                "worker_id": transfer["worker_id"],
                "suite_path": metadata.get("suite_path", ""),
                "path": metadata.get("path", ""),
                "transfer_id": transfer_id,
            }
            return result

    try:
        result = await asyncio.to_thread(extract_and_analyze)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"success": True, "data": result, "mode": "cluster_suite_log_dir"}
