"""Disk hygiene: reclaim rebuildable/temporary files under data_root.

Boundary (ADR 0030): only files the pipeline can recreate —

- ``data/automation/artifact-stage/<run_id>/`` staged firmware (rebuildable
  from the build server or re-downloadable from Jenkins);
- leftover ``.part`` temp files of interrupted artifact downloads;
- ``foundation.uploads.upload_temp_root()`` transient upload state.

Reports, cluster artifacts (owner retention owns them) and APK upload
sessions (firmware owns their lifecycle) are never touched. Every removal is
mtime-based; a journal entry is emitted only when something was actually
freed, so hourly sweeps stay quiet.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from foundation.config import settings


STAGE_SUBDIR = "automation/artifact-stage"
STALE_PART_SECONDS = 24 * 3600
DEFAULT_STAGE_MAX_AGE_DAYS = 7
DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS = 7


def disk_free_gb() -> float | None:
    try:
        return shutil.disk_usage(str(settings.data_root)).free / 1024 ** 3
    except OSError:
        return None


def largest_dirs_under(root: Path, *, limit: int = 5) -> list[dict[str, Any]]:
    """Best-effort top-level size overview (one level deep)."""
    entries: list[dict[str, Any]] = []
    try:
        iterator = list(root.iterdir())[:200]
    except OSError:
        return entries
    for child in iterator:
        if not child.is_dir():
            continue
        try:
            total = sum(
                item.stat().st_size
                for item in child.rglob("*")
                if item.is_file()
            )
        except OSError:
            continue
        entries.append({"path": child.name, "size_gb": round(total / 1024 ** 3, 3)})
    entries.sort(key=lambda item: item["size_gb"], reverse=True)
    return entries[:limit]


def _remove_file(path: Path) -> int:
    try:
        size = path.stat().st_size
        path.unlink()
        return size
    except OSError:
        return 0


def _sweep_directory(
    directory: Path, *, max_age_seconds: float, part_only: bool = False
) -> tuple[int, int]:
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - max_age_seconds
    freed = removed = 0
    try:
        candidates = list(directory.rglob("*"))
    except OSError:
        return 0, 0
    for item in candidates:
        try:
            if not item.is_file():
                continue
            if part_only and not item.name.endswith(".part"):
                continue
            if item.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        freed += _remove_file(item)
        removed += 1
    return freed, removed


def _rmtree_stale_dirs(directory: Path, *, max_age_seconds: float) -> tuple[int, int]:
    """Remove stale session dirs under ``<root>/<client>/<session>``.

    An active upload keeps adding entries, so the session mtime stays
    fresh; abandoned sessions age past the cutoff and are removed whole.
    Client directories that end up empty (or are themselves stale) are
    pruned afterwards.
    """
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - max_age_seconds
    freed = removed = 0
    try:
        client_dirs = [item for item in directory.iterdir() if item.is_dir()]
    except OSError:
        return 0, 0
    for client_dir in client_dirs:
        try:
            sessions = [item for item in client_dir.iterdir() if item.is_dir()]
        except OSError:
            continue
        for session_dir in sessions:
            try:
                if session_dir.stat().st_mtime > cutoff:
                    continue
                size = sum(
                    item.stat().st_size
                    for item in session_dir.rglob("*")
                    if item.is_file()
                )
                shutil.rmtree(session_dir)
                freed += size
                removed += 1
            except OSError:
                continue
        try:
            if not any(client_dir.iterdir()) or client_dir.stat().st_mtime <= cutoff:
                shutil.rmtree(client_dir)
        except OSError:
            continue
    return freed, removed


def _prune_empty_stage_dirs(stage_root: Path) -> None:
    if not stage_root.is_dir():
        return
    try:
        for run_dir in stage_root.iterdir():
            if run_dir.is_dir() and not any(run_dir.iterdir()):
                run_dir.rmdir()
    except OSError:
        pass


def preview_cleanup(
    *, stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS
) -> dict[str, Any]:
    stage_root = settings.data_root / STAGE_SUBDIR
    stage_cutoff = time.time() - stage_max_age_days * 86400
    staged: list[dict[str, Any]] = []
    if stage_root.is_dir():
        try:
            for run_dir in sorted(stage_root.iterdir())[:100]:
                if not run_dir.is_dir():
                    continue
                for item in run_dir.iterdir():
                    try:
                        if not item.is_file() or item.stat().st_mtime > stage_cutoff:
                            continue
                        staged.append({
                            "path": str(item.relative_to(settings.data_root)),
                            "size_gb": round(item.stat().st_size / 1024 ** 3, 3),
                            "age_days": round((time.time() - item.stat().st_mtime) / 86400, 1),
                        })
                    except OSError:
                        continue
        except OSError:
            pass
    staged.sort(key=lambda item: item["size_gb"], reverse=True)
    free = disk_free_gb()
    return {
        "disk_free_gb": round(free, 2) if free is not None else None,
        "candidates": staged[:20],
        "candidate_count": len(staged),
        "candidate_size_gb": round(sum(item["size_gb"] for item in staged), 3),
        "largest_dirs": largest_dirs_under(settings.data_root),
    }


def run_cleanup(
    *,
    stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS,
    upload_temp_max_age_days: int = DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
    include_stage: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Sweep rebuildable files.

    ``include_stage=False`` (the default) keeps staged firmware untouched —
    the hourly unattended pass runs in that mode; staged firmware removal is
    an explicit admin action (``include_stage=True``) because a staged image
    may still be referenced by a queued replay.
    """
    stage_root = settings.data_root / STAGE_SUBDIR
    if dry_run:
        preview = preview_cleanup(stage_max_age_days=stage_max_age_days)
        return {**preview, "dry_run": True, "freed_bytes": 0, "removed_files": 0}

    freed = removed = 0
    if include_stage:
        amount, count = _sweep_directory(
            stage_root, max_age_seconds=stage_max_age_days * 86400
        )
        freed += amount
        removed += count
        _prune_empty_stage_dirs(stage_root)
    # Interrupted-download leftovers are safe at any age past one day.
    amount, count = _sweep_directory(stage_root, max_age_seconds=STALE_PART_SECONDS, part_only=True)
    freed += amount
    removed += count

    from foundation.uploads import upload_temp_root

    amount, count = _rmtree_stale_dirs(
        Path(upload_temp_root()),
        max_age_seconds=upload_temp_max_age_days * 86400,
    )
    freed += amount
    removed += count

    return {
        "dry_run": False,
        "include_stage": include_stage,
        "freed_bytes": freed,
        "removed_files": removed,
        "disk_free_gb": round(disk_free_gb(), 2) if disk_free_gb() is not None else None,
    }
