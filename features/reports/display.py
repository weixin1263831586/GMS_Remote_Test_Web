"""Display-only helpers for report list metadata."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_RESULT_DIRECTORY_RE = re.compile(r"RESULT DIRECTORY\s*:\s*(\S+)")
_TRADEFED_RESULT_FOLDER_RE = re.compile(
    r"^\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}(?:\.\d+)?(?:_\d+)?$"
)


def tradefed_result_folder_name(*values: object) -> str:
    """Return the first value that names a Tradefed results directory."""
    for value in values:
        text = str(value or "").strip().replace("\\", "/").rstrip("/")
        name = text.rsplit("/", 1)[-1] if text else ""
        if _TRADEFED_RESULT_FOLDER_RE.fullmatch(name):
            return name
    return ""


def report_name_from_result_dir(result_dir: str) -> str:
    """Return the original Tradefed results folder name when it is available."""
    stdout_path = Path(str(result_dir or "")) / "stdout.log"
    if not stdout_path.is_file():
        return ""
    try:
        with stdout_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 4 * 1024 * 1024))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    matches = _RESULT_DIRECTORY_RE.findall(text)
    return Path(matches[-1]).name if matches else ""


def report_display_name(report: dict[str, Any]) -> str:
    """Return a stable user-facing name while keeping internal IDs private."""
    stored_name = str(report.get("report_name") or "").strip()
    if stored_name and not stored_name.startswith("cluster-job-"):
        return stored_name
    detected_name = report_name_from_result_dir(
        str(report.get("result_dir") or "")
    )
    return (
        detected_name
        or tradefed_result_folder_name(
            report.get("source_timestamp"),
            report.get("timestamp"),
        )
        or stored_name
        or str(report.get("timestamp") or "").strip()
        or "report"
    )


def report_download_filename(report: dict[str, Any]) -> str:
    """Return an ASCII-safe ZIP filename derived from the display name."""
    stem = report_display_name(report)
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return f"{safe_stem or 'report'}.zip"


def report_client_display_id(
    report: dict[str, Any],
    fallback_display_id: str = "",
) -> str:
    """Prefer the test execution host, then resolve the report owner."""
    worker_id = str(report.get("worker_id") or "").strip()
    if worker_id:
        try:
            from features.cluster import get_cluster_service

            worker = get_cluster_service().repository.get_worker(worker_id) or {}
            capabilities = worker.get("capabilities") or {}
            ssh_user = str(capabilities.get("ssh_user") or "").strip()
            address = str(
                worker.get("address") or worker.get("hostname") or ""
            ).strip()
            if ssh_user and address:
                return f"{ssh_user}@{address}"
        except Exception:
            pass
        if worker_id == "worker-local":
            try:
                from foundation.config import config_manager

                config = config_manager.load_config()
                ssh_user = str(config.get("ubuntu_user") or "").strip()
                address = str(config.get("ubuntu_host") or "").strip()
                if ssh_user and address:
                    return f"{ssh_user}@{address}"
            except Exception:
                pass

    from features.users import resolve_client_display_id

    owner_id = str(report.get("owner_id") or "")
    stored_display = str(
        report.get("display_client_id") or report.get("client_name") or ""
    ).strip()
    return resolve_client_display_id(
        owner_id,
        stored_display
        if stored_display and stored_display != owner_id
        else fallback_display_id,
    )
