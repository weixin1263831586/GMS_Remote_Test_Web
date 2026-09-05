from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from features.auth import principal_display_name, principal_owner_id

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .models import TransferComplete
from .repository import utc_now
from .transfer_support import worker_device


router = APIRouter()
_TRANSFER_ID_RE = re.compile(r"^transfer-[a-f0-9]{32}$")


def _transfer_helpers():
    # Imported lazily because transfers_api mounts this router after defining
    # the shared roots and owner checks used by both route groups.
    from . import transfers_api

    return transfers_api


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, "invalid transfer chunk size")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise HTTPException(413, "invalid transfer chunk size")
    return body


@contextmanager
def _locked_transfer_root(transfer_id: str):
    if not _TRANSFER_ID_RE.fullmatch(str(transfer_id or "")):
        raise HTTPException(404, "transfer not found")
    transfer_root = _transfer_helpers()._transfer_root().resolve()
    root = (transfer_root / transfer_id).resolve()
    if not root.is_relative_to(transfer_root):
        raise HTTPException(404, "transfer not found")
    root.mkdir(parents=True, exist_ok=True)
    lock_handle = (root / ".operation.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(409, "transfer operation is already in progress") from exc
        yield transfer_root, root
    finally:
        lock_handle.close()


@router.post("/devices/export")
def create_device_export(
    request: Request,
    worker_id: str = Query(...),
    device_id: str = Query(...),
    path: str = Query(...),
):
    from worker_agent.android_inspection import validate_export_path

    helpers = _transfer_helpers()
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    helpers._online_worker(worker_id)
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
            username=principal_display_name(request),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    lease_tokens = service().repository.claim_fencing_tokens(records, operation_id)
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
    if index < 0 or index >= 100000:
        raise HTTPException(400, "invalid chunk index")
    max_chunk = configured_max_bytes(
        "GMS_CLUSTER_TRANSFER_CHUNK_BYTES",
        8 * 1024 * 1024,
    )
    body = await _read_limited_body(request, max_chunk)
    max_bytes = configured_max_bytes(
        "GMS_CLUSTER_TRANSFER_MAX_BYTES",
        service().config.transfer_max_bytes,
    )
    with _locked_transfer_root(transfer_id) as (_transfer_root, root):
        transfer = service().repository.get_transfer(transfer_id)
        if not transfer or transfer["worker_id"] != worker_id:
            raise HTTPException(404, "transfer not found for worker")
        if transfer["status"] not in {"created", "uploading"}:
            raise HTTPException(409, "transfer no longer accepts chunks")
        chunk_dir = root / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = chunk_dir / f"{index:08d}.part"
        # 配额按累计字节数 O(1) 维护（.received-bytes 记账文件）。
        # 逐块全量 glob+stat 在 20GB/4MB 场景是 O(n²)（约 1300 万次 stat）。
        accounting = root / ".received-bytes"
        try:
            received = int(accounting.read_text() or "0")
        except (OSError, ValueError):
            # 记账文件丢失/损坏时按磁盘现状重建：统计必须"包含"当前
            # chunk 在内的全部 .part，与下方 received - previous_size
            # 的增量公式自洽；排除当前块会重复扣一次。
            received = sum(
                path.stat().st_size
                for path in chunk_dir.glob("*.part")
            )
        previous_size = chunk_path.stat().st_size if chunk_path.exists() else 0
        if received - previous_size + len(body) > max_bytes:
            raise HTTPException(413, "transfer is too large")
        temporary = chunk_dir / f".{index:08d}.{uuid.uuid4().hex}.upload"
        try:
            temporary.write_bytes(body)
            os.replace(temporary, chunk_path)
        finally:
            temporary.unlink(missing_ok=True)
        accounting.write_text(str(received - previous_size + len(body)))
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
    max_bytes = configured_max_bytes(
        "GMS_CLUSTER_TRANSFER_MAX_BYTES", service().config.transfer_max_bytes
    )
    if body.size_bytes > max_bytes:
        raise HTTPException(413, "transfer is too large")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(body.filename).name)
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(400, "invalid transfer filename")
    # Reject unknown transfers before acquiring the lock: _locked_transfer_root
    # creates the transfer directory, and a well-formed but unknown id must not
    # leave orphan storage behind.
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["worker_id"] != worker_id:
        raise HTTPException(404, "transfer not found for worker")
    with _locked_transfer_root(transfer_id) as (transfer_root, root):
        transfer = service().repository.get_transfer(transfer_id)
        if not transfer or transfer["worker_id"] != worker_id:
            raise HTTPException(404, "transfer not found for worker")
        if transfer["status"] == "completed":
            if (
                transfer.get("filename") == safe_name
                and int(transfer.get("size_bytes") or 0) == body.size_bytes
                and transfer.get("sha256") == body.sha256
            ):
                return {"success": True, "transfer": transfer}
            raise HTTPException(409, "transfer completion does not match stored artifact")
        if transfer["status"] not in {"created", "uploading"}:
            raise HTTPException(409, "transfer cannot be completed from its current state")
        chunk_dir = root / "chunks"
        chunks = [chunk_dir / f"{index:08d}.part" for index in range(body.chunk_count)]
        if set(chunk_dir.glob("*.part")) != set(chunks) or not all(
            path.is_file() for path in chunks
        ):
            raise HTTPException(409, "transfer chunks are incomplete or inconsistent")
        destination = root / safe_name
        temporary = root / f".{safe_name}.{uuid.uuid4().hex}.merge"
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("xb") as output:
                for chunk in chunks:
                    with chunk.open("rb") as source:
                        while data := source.read(1024 * 1024):
                            total += len(data)
                            if total > max_bytes:
                                raise HTTPException(413, "transfer is too large")
                            digest.update(data)
                            output.write(data)
            if total != body.size_bytes or digest.hexdigest() != body.sha256:
                raise HTTPException(409, "transfer checksum or size mismatch")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        transfer = service().repository.update_transfer(
            transfer_id,
            status="completed",
            filename=safe_name,
            relative_path=str(destination.relative_to(transfer_root)),
            size_bytes=total,
            sha256=body.sha256,
            completed_at=utc_now(),
        )
        shutil.rmtree(chunk_dir, ignore_errors=True)
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str, request: Request):
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    _transfer_helpers()._require_transfer_access(request, transfer)
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}/download")
def download_transfer(transfer_id: str, request: Request):
    helpers = _transfer_helpers()
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["status"] != "completed":
        raise HTTPException(409, "transfer is not complete")
    helpers._require_transfer_access(request, transfer)
    transfer_root = helpers._transfer_root()
    path = (transfer_root / transfer["relative_path"]).resolve()
    if not path.is_relative_to(transfer_root.resolve()) or not path.is_file():
        raise HTTPException(404, "transfer file not found")
    download_name = transfer["filename"]
    prefix = f"{transfer_id}-"
    if download_name.startswith(prefix):
        download_name = download_name[len(prefix):]
    segments = [
        item for item in str((transfer.get("metadata") or {}).get("path") or "").split("/")
        if item
    ]
    kind = segments[0].lower() if segments else ""
    if kind in {"logs", "results"}:
        stem, dot, extension = download_name.rpartition(".")
        download_name = f"{stem}-{kind}.{extension}" if dot else f"{download_name}-{kind}"
    return FileResponse(path, filename=download_name)


@router.post("/transfers/{transfer_id}/apk-analysis")
def import_transfer_for_apk_analysis(transfer_id: str, request: Request):
    from features.test_execution import runtime as test_runtime

    helpers = _transfer_helpers()
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    helpers._require_transfer_access(request, transfer)
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
    transfer_root = helpers._transfer_root()
    source = (transfer_root / transfer["relative_path"]).resolve()
    if not source.is_relative_to(transfer_root.resolve()) or not source.is_file():
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
                "source_device_id": metadata.get("device_id", ""),
                "source_path": metadata.get("source_path") or metadata.get("path", ""),
                "source_suite_path": metadata.get("suite_path", ""),
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
            "device_id": metadata.get("device_id", ""),
            "transfer_id": transfer_id,
        },
    }


@router.post("/transfers/{transfer_id}/report-analysis")
async def analyze_transferred_log_directory(transfer_id: str, request: Request):
    from features.reports import ArchiveReportAnalyzer

    helpers = _transfer_helpers()
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    helpers._require_transfer_access(request, transfer)
    metadata = transfer.get("metadata") or {}
    if (
        transfer.get("transfer_type") != "suite_export"
        or not metadata.get("directory")
        or transfer.get("status") != "completed"
    ):
        raise HTTPException(409, "suite log directory transfer is not complete")
    transfer_root = helpers._transfer_root()
    archive = (transfer_root / transfer["relative_path"]).resolve()
    if not archive.is_relative_to(transfer_root.resolve()) or not zipfile.is_zipfile(archive):
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
                unsafe = any(
                    not (destination / item.filename).resolve().is_relative_to(destination)
                    for item in members
                )
                if total > max_bytes or unsafe:
                    raise ValueError("log archive is too large or contains an unsafe path")
                bundle.extractall(destination)
            result = ArchiveReportAnalyzer().analyze_log_dir(str(destination))
            if not result:
                raise ValueError("transferred directory contains no analyzable host logs")
            result.setdefault("report_type", "log")
            result.setdefault("report_name", Path(str(metadata.get("path") or "suite log")).name)
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
