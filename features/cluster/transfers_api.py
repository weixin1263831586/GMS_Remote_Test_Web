from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from features.auth import (
    principal_owner_id,
    require_human_principal,
    require_resource_owner,
)
from features.reports import (
    can_access_report,
    test_report_db,
    tradefed_result_folder_name,
)
from foundation.error_model import ApiError

from .api import _authenticate, _require_cluster_enabled, service
from .config import configured_max_bytes
from .models import ReportCopyCreate
from .repository import utc_now
from .transfer_ingest_api import router as transfer_ingest_router
from .transfer_support import require_export_read
from .transfers_staging import router as transfers_staging_router


router = APIRouter()
def _transfer_root() -> Path:
    return service().repository.db_path.parent / "transfers"


def _firmware_root() -> Path:
    return service().repository.db_path.parent / "firmware"


def _report_copy_local_root() -> Path:
    return service().repository.db_path.parent / "report-copy-local"


def _require_report_copy_access(
    request: Request,
    source_worker_id: str,
    source_suite_path: str,
    report_name: str,
) -> None:
    """校验报告复制请求的来源报告归属（features/reports/access.py 同一边界）。

    普通账号只能复制自己名下报告：平台报告按 ADR 0010 以 account
    ``owner_id`` 分区（列表/下载/删除均如此），复制入口此前仅校验登录与
    套件存在性，多账号共享同一 Worker 套件目录时可能凭已知目录名读走
    他人报告。这里按 ``worker + suite_path + 报告目录名`` 在报告索引内做
    归属匹配——report_name 是秒级 Tradefed 时间戳，不同 Worker 同套件
    同秒运行会产生同名目录，必须绑定来源 Worker 才能排除借用他机索引
    记录过闸的边界。未收录的报告 fail-closed 拒绝（经 report-copy 导入的
    报告目前不会回写索引，需重新索引后才可再导出；遗留索引行缺
    worker_id 时同样拒绝，重新索引即恢复）。admin 不受限。
    """
    principal = require_human_principal(request)

    suite_root = str(source_suite_path or "").replace("\\", "/").rstrip("/")
    if suite_root.endswith("/tools"):
        suite_root = suite_root[: -len("/tools")]
    reports = test_report_db.get_reports(
        limit=500,
        owner_id=None if principal.role == "admin" else principal.resource_owner_id,
        include_all=principal.role == "admin",
        worker_id=source_worker_id,
    )
    for report in reports:
        if not can_access_report(request, report):
            continue
        report_suite = str(report.get("suite_path") or "").replace("\\", "/").rstrip("/")
        if report_suite.endswith("/tools"):
            report_suite = report_suite[: -len("/tools")]
        if report_suite != suite_root:
            continue
        if report_name in {
            str(report.get("report_name") or ""),
            tradefed_result_folder_name(
                report.get("source_timestamp"), report.get("timestamp")
            ),
        }:
            return
    raise ApiError.not_found("来源报告不存在或无权访问")


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
    metadata = transfer.get("metadata") or {}
    require_export_read(
        request, str(transfer.get("worker_id") or ""),
        device_id=(str(metadata.get("device_id") or "")
                   if transfer.get("transfer_type") == "device_export" else ""),
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
    require_export_read(request, worker_id)
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
    require_human_principal(request)
    local_worker_id = service().config.local_worker_id
    source_path = PurePosixPath(body.source_suite_path)
    target_path = PurePosixPath(body.target_suite_path)
    source_root = source_path.parent if source_path.name == "tools" else source_path
    target_root = target_path.parent if target_path.name == "tools" else target_path
    if body.source_worker_id == body.target_worker_id and source_root == target_root:
        raise ApiError.malformed_request("同一主机内请选择不同的来源套件和目标套件")
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
    # 报告归属边界（features/reports/access.py 同一规则）：来源报告必须
    # 属于当前账号（admin 不受限），防止多账号共享 Worker 套件目录时
    # 凭已知目录名读走他人报告。
    _require_report_copy_access(
        request, body.source_worker_id, body.source_suite_path, body.report_name,
    )

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
    require_human_principal(request)
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer.get("transfer_type") != "report_copy":
        raise HTTPException(404, "report copy not found")
    _require_transfer_access(request, transfer)
    if transfer.get("status") != "completed":
        raise HTTPException(409, transfer.get("error") or "report export is not complete")

    metadata = transfer.get("metadata") or {}
    # 已完成导入的幂等回显优先于归属检查：报告在导出后被删除/重新索引
    # 时，Transfer 属主仍可查询既成事实的导入结果（跨账号已被
    # _require_transfer_access 挡在 404）。
    if metadata.get("import_status") == "completed":
        return {
            "success": True,
            "copy_id": transfer_id,
            "status": "completed",
            "result": metadata.get("import_result") or {},
            "deduplicated": True,
        }
    target_worker_id = str(metadata.get("target_worker_id") or "")
    target_suite_path = str(metadata.get("target_suite_path") or "")
    _known_suite(target_worker_id, target_suite_path)
    # 导入侧同样校验来源报告归属： Transfer 元数据可被同账号继续使用，
    # 但换账号（或篡改请求头）重新触发导入时必须重新过归属检查。
    _require_report_copy_access(
        request,
        str(metadata.get("source_worker_id") or ""),
        str(metadata.get("source_suite_path") or ""),
        str(metadata.get("report_name") or ""),
    )
    local_worker_id = service().config.local_worker_id

    existing_command_id = str(metadata.get("target_command_id") or "")
    existing_command = (
        service().repository.get_command(existing_command_id)
        if existing_command_id
        else None
    )
    if existing_command and existing_command.get("status") not in {"failed", "cancelled"}:
        # 在途或其它终态命令：幂等透传当前状态（数据库命令是权威状态源），
        # 防止并发请求重复派发；failed/cancelled 允许走下面的重试新 attempt。
        return {
            "success": True,
            "copy_id": transfer_id,
            "status": existing_command.get("status", "accepted"),
            "command_id": existing_command_id,
            "result": existing_command.get("result") or {},
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
    # 同一 Transfer 的远端导入在数据库层按 operation_id 原子去重
    # （repository_commands.create_command：BEGIN IMMEDIATE 内查重并返回既有
    # 命令）：并发请求与“命令已建、Transfer 元数据未更新”的崩溃窗口都
    # 收敛到同一条 report_import 命令，消除重复派发。
    operation_id = f"report-import:{transfer_id}"
    if existing_command:
        # 上一条命令已 failed 的重试：Worker 端 import_suite_report 以
        # “staging 提取 + 末尾单次 rename 发布”保证失败不残留目标目录，
        # 重试不破坏无覆盖语义；operation_id 追加上条命令 id，使并发重试
        # 也收敛到同一条新命令。
        operation_id = f"{operation_id}:{existing_command_id}"
    command = service().repository.create_command({
        "worker_id": target_worker_id,
        "command_type": "report_import",
        "operation_id": operation_id,
        "payload": {
            "transfer_id": transfer_id,
            "target_suite_path": target_suite_path,
            "report_name": metadata.get("report_name", ""),
            "size_bytes": transfer.get("size_bytes", 0),
            "sha256": transfer.get("sha256", ""),
            "owner_id": owner_id,
        },
    })
    if str(metadata.get("target_command_id") or "") == command["id"]:
        # 命中既有命令（并发败者或元数据滞后于命令创建）：直接回当前状态。
        return {
            "success": True,
            "copy_id": transfer_id,
            "status": command.get("status", "accepted"),
            "command_id": command["id"],
            "result": command.get("result") or {},
            "deduplicated": True,
        }
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
        # 自愈路径可能命中已完成/在途的既有命令：回显数据库权威状态而非
        # 写死 accepted。
        "status": command.get("status", "accepted"),
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
