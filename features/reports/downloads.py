"""Build report downloads from the suite's Tradefed results and logs."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .display import report_display_name, tradefed_result_folder_name


@dataclass(frozen=True)
class ReportBundle:
    path: Path
    file_count: int
    sources: tuple[str, ...]


def report_suite_location(report: dict[str, Any]) -> tuple[Path, str]:
    """Resolve the suite root and validated Tradefed run folder."""
    suite_path = Path(str(report.get("suite_path") or "")).expanduser()
    if not suite_path.is_absolute():
        raise FileNotFoundError("Report suite path is missing")
    suite_path = suite_path.resolve()
    suite_root = suite_path.parent if suite_path.name == "tools" else suite_path
    run_folder = tradefed_result_folder_name(
        report_display_name(report),
        report.get("source_timestamp"),
        report.get("timestamp"),
    )
    if not run_folder:
        raise FileNotFoundError("Report result folder is missing")
    return suite_root, run_folder


def local_report_directories(report: dict[str, Any]) -> dict[str, Path]:
    """Return existing local results/logs directories for a report."""
    suite_root, run_folder = report_suite_location(report)
    directories: dict[str, Path] = {}
    for kind in ("results", "logs"):
        target = (suite_root / kind / run_folder).resolve()
        if target.is_relative_to(suite_root) and target.is_dir():
            directories[kind] = target
    return directories


def _temporary_zip_path() -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix="gms-report-download-",
        suffix=".zip",
    )
    os.close(descriptor)
    return Path(raw_path)


def _safe_archive_member(name: str) -> PurePosixPath | None:
    member = PurePosixPath(str(name or ""))
    if not name or member.is_absolute() or ".." in member.parts:
        return None
    return member


def create_local_report_bundle(report: dict[str, Any]) -> ReportBundle | None:
    """Create a ZIP containing the real local results and logs trees."""
    directories = local_report_directories(report)
    if not directories:
        return None
    _suite_root, run_folder = report_suite_location(report)
    archive_path = _temporary_zip_path()
    count = 0
    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            for kind, source in directories.items():
                for current, dirnames, filenames in os.walk(source):
                    current_path = Path(current)
                    dirnames[:] = [
                        name for name in dirnames
                        if not (current_path / name).is_symlink()
                    ]
                    for filename in filenames:
                        path = current_path / filename
                        if path.is_symlink() or not path.is_file():
                            continue
                        relative = path.relative_to(source)
                        archive.write(
                            path,
                            (PurePosixPath(kind) / run_folder / PurePosixPath(relative.as_posix())).as_posix(),
                        )
                        count += 1
        if not count:
            archive_path.unlink(missing_ok=True)
            return None
        return ReportBundle(archive_path, count, tuple(directories))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def merge_remote_report_exports(
    exports: dict[str, Path],
) -> ReportBundle | None:
    """Merge trusted Worker directory exports beneath results/ and logs/."""
    if not exports:
        return None
    archive_path = _temporary_zip_path()
    count = 0
    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as output:
            for kind, source_archive in exports.items():
                with zipfile.ZipFile(source_archive) as source:
                    for info in source.infolist():
                        member = _safe_archive_member(info.filename)
                        mode = (info.external_attr >> 16) & 0o170000
                        if info.is_dir() or member is None or mode == 0o120000:
                            continue
                        target_info = zipfile.ZipInfo(
                            (PurePosixPath(kind) / member).as_posix(),
                            date_time=info.date_time,
                        )
                        target_info.compress_type = zipfile.ZIP_DEFLATED
                        target_info.external_attr = info.external_attr
                        with source.open(info) as input_file, output.open(target_info, "w") as output_file:
                            shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                        count += 1
        if not count:
            archive_path.unlink(missing_ok=True)
            return None
        return ReportBundle(archive_path, count, tuple(exports))
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


async def create_remote_report_bundle(
    report: dict[str, Any],
    *,
    owner_id: str,
    timeout_seconds: float = 300,
) -> ReportBundle | None:
    """Export results/logs through the existing authenticated Worker channel."""
    from features.cluster import get_cluster_service

    cluster = get_cluster_service()
    worker_id = str(report.get("worker_id") or "")
    if not worker_id or worker_id == cluster.config.local_worker_id:
        return None
    worker = cluster.repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise RuntimeError("Report Worker is not online")
    if not cluster.has_command_agent(worker_id):
        raise RuntimeError("Report download requires an online Worker agent")

    _suite_root, run_folder = report_suite_location(report)
    pending: dict[str, tuple[str, str]] = {}
    for kind in ("results", "logs"):
        relative_path = f"{kind}/{run_folder}"
        transfer = cluster.repository.create_transfer(
            worker_id,
            owner_id=owner_id,
            metadata={
                "suite_path": str(report.get("suite_path") or ""),
                "path": relative_path,
                "directory": True,
                "report_id": str(report.get("report_id") or ""),
            },
        )
        command = cluster.repository.create_command({
            "worker_id": worker_id,
            "command_type": "suite_export",
            "payload": {
                "transfer_id": transfer["id"],
                "suite_path": str(report.get("suite_path") or ""),
                "path": relative_path,
                "directory": True,
            },
        })
        pending[kind] = (transfer["id"], command["id"])

    exports: dict[str, Path] = {}
    errors: list[str] = []
    deadline = time.monotonic() + max(1, timeout_seconds)
    while pending and time.monotonic() < deadline:
        for kind, (transfer_id, command_id) in list(pending.items()):
            transfer = cluster.repository.get_transfer(transfer_id) or {}
            command = cluster.repository.get_command(command_id) or {}
            if transfer.get("status") == "completed":
                path = (
                    cluster.repository.db_path.parent
                    / "transfers"
                    / str(transfer.get("relative_path") or "")
                ).resolve()
                transfer_root = (cluster.repository.db_path.parent / "transfers").resolve()
                if not path.is_relative_to(transfer_root) or not path.is_file():
                    errors.append(f"{kind}: transferred archive is missing")
                else:
                    exports[kind] = path
                pending.pop(kind, None)
            elif command.get("status") in {"failed", "cancelled"}:
                error = str(command.get("error") or f"{kind} export failed")
                cluster.repository.update_transfer(
                    transfer_id,
                    status="failed",
                    error=error,
                )
                errors.append(f"{kind}: {error}")
                pending.pop(kind, None)
        if pending:
            await asyncio.sleep(0.25)

    if pending:
        for kind, (transfer_id, _command_id) in pending.items():
            cluster.repository.update_transfer(
                transfer_id,
                status="failed",
                error="report export timed out",
            )
            errors.append(f"{kind}: export timed out")
    if not exports:
        raise RuntimeError("; ".join(errors) or "Report results and logs are unavailable")
    return await asyncio.to_thread(merge_remote_report_exports, exports)


def remove_report_bundle(path: Path | str) -> None:
    Path(path).unlink(missing_ok=True)
