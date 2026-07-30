"""Display-only helpers for report list metadata."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any


_RESULT_DIRECTORY_RE = re.compile(r"RESULT DIRECTORY\s*:\s*(\S+)")


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
