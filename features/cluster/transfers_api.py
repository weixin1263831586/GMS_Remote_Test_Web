from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from features.auth import (
    principal_owner_id,
    require_resource_owner,
)

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .models import ReportCopyCreate
from .repository import utc_now
from .transfer_ingest_api import router as transfer_ingest_router
from .transfers_staging import router as transfers_staging_router


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


router.include_router(transfers_staging_router)


def cleanup_staged_firmware(command: dict) -> None:
    """Re-exported for commands_api compatibility (moved to transfers_staging)."""
    from .transfers_staging import cleanup_staged_firmware as _cleanup

    _cleanup(command)
