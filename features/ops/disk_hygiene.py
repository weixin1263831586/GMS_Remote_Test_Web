"""Disk hygiene: reclaim rebuildable/temporary files under data_root.

Boundary (ADR 0030): only files the pipeline can recreate —

- ``data/automation/artifact-stage/<run_id>/`` staged firmware, and only
  when the owning run reached a terminal state (referenced images are
  skipped fail-closed);
- leftover ``.part`` temp files of interrupted artifact downloads;
- ``foundation.uploads.upload_temp_root()`` transient upload state whose
  session and nested files all stayed below the cutoff.

Reports, cluster artifacts (owner retention owns them) and APK upload
sessions (firmware owns their lifecycle) are never touched. A journal
entry is emitted only when something was actually freed, so hourly sweeps
stay quiet.
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


def _dir_fresh_mtime(directory: Path, *, cutoff: float) -> bool:
    """True when the directory itself or any nested file is recent.

    Directory mtime only updates when direct children change, so a long
    chunked upload writing deep inside an old session dir would otherwise
    look abandoned (review P0: active uploads were removed whole).
    """
    try:
        if directory.stat().st_mtime > cutoff:
            return True
        for item in directory.rglob("*"):
            try:
                if item.stat().st_mtime > cutoff:
                    return True
            except OSError:
                continue
    except OSError:
        return True  # unreadable → fail closed, keep the data
    return False


def _rmtree_stale_dirs(directory: Path, *, max_age_seconds: float) -> tuple[int, int]:
    """Remove stale session dirs under ``<root>/.../<session>``.

    Real layouts are ``<root>/<client>/<upload_id>`` (firmware chunks) and
    ``<root>/<namespace>/<owner_key>/<upload_id>`` (terminal uploads), so
    session dirs may sit at depth two or three — walk the tree instead of
    guessing the depth. A session is only removed when neither the session
    directory nor any nested file was modified within the cutoff window;
    fresh uploads keep writing chunks and stay untouched (review P0: active
    uploads were removed whole). Grouping directories are never rmtree'd by
    mtime: only verified-empty ones are pruned with rmdir(), so a
    stale-looking owner dir cannot take active sessions with it.
    """
    if not directory.is_dir():
        return 0, 0
    cutoff = time.time() - max_age_seconds
    freed = removed = 0

    def _contains_dir(path: Path) -> bool:
        try:
            return any(item.is_dir() for item in path.iterdir())
        except OSError:
            return True  # unreadable → fail closed, treat as grouping dir

    def _sweep(parent: Path) -> None:
        nonlocal freed, removed
        try:
            subdirs = [item for item in parent.iterdir() if item.is_dir()]
        except OSError:
            subdirs = []
        for subdir in subdirs:
            _sweep(subdir)
        # A level whose children still contain directories is a grouping
        # level (owner/client), never a session level: prune empties only.
        if any(_contains_dir(subdir) for subdir in subdirs):
            return
        if parent == directory:
            return
        for session_dir in subdirs:
            if _dir_fresh_mtime(session_dir, cutoff=cutoff):
                continue
            try:
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
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    _sweep(directory)
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


def _protected_stage_run_ids() -> set[str] | None:
    """Run IDs whose staged firmware must never be swept.

    A staged image can still be consumed by a queued/in-flight run's replay
    or artifact step; mtime alone cannot prove that. Reads the active-run
    provider from ``foundation.automation_port`` (ops must not import the
    automation feature directly). ``None`` means the lookup itself failed
    or the port is unconfigured — callers must treat every candidate as
    protected (fail closed) instead of assuming nothing is referenced.
    """
    from foundation.automation_port import active_run_ids

    return active_run_ids()


def _stage_candidates(
    stage_root: Path, *, stage_max_age_days: int
) -> list[dict[str, Any]]:
    """Shared candidate list for preview, dry-run and actual cleanup."""
    stage_cutoff = time.time() - stage_max_age_days * 86400
    candidates: list[dict[str, Any]] = []
    if not stage_root.is_dir():
        return candidates
    try:
        run_dirs = sorted(stage_root.iterdir())[:200]
    except OSError:
        return candidates
    protected = _protected_stage_run_ids()
    if protected is None:
        # fail closed：引用查询失败 → 所有候选标记不可删除。
        for run_dir in run_dirs:
            if not run_dir.is_dir():
                continue
            for item in run_dir.iterdir():
                try:
                    if not item.is_file() or item.stat().st_mtime > stage_cutoff:
                        continue
                    candidates.append({
                        "path": str(item.relative_to(settings.data_root)),
                        "size_gb": round(item.stat().st_size / 1024 ** 3, 3),
                        "age_days": round(
                            (time.time() - item.stat().st_mtime) / 86400, 1
                        ),
                        "run_id": run_dir.name,
                        "deletable": False,
                        "protection_reason": "reference_lookup_failed",
                    })
                except OSError:
                    continue
        return candidates
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            continue
        for item in run_dir.iterdir():
            try:
                if not item.is_file() or item.stat().st_mtime > stage_cutoff:
                    continue
                age_days = (time.time() - item.stat().st_mtime) / 86400
                candidates.append({
                    "path": str(item.relative_to(settings.data_root)),
                    "size_gb": round(item.stat().st_size / 1024 ** 3, 3),
                    "age_days": round(age_days, 1),
                    "run_id": run_dir.name,
                    "deletable": run_dir.name not in protected,
                })
            except OSError:
                continue
    return candidates


def preview_cleanup(
    *, stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS
) -> dict[str, Any]:
    stage_root = settings.data_root / STAGE_SUBDIR
    staged = _stage_candidates(stage_root, stage_max_age_days=stage_max_age_days)
    staged.sort(key=lambda item: item["size_gb"], reverse=True)
    deletable = [item for item in staged if item["deletable"]]
    free = disk_free_gb()
    return {
        "disk_free_gb": round(free, 2) if free is not None else None,
        "candidates": staged[:20],
        "candidate_count": len(staged),
        "candidate_size_gb": round(sum(item["size_gb"] for item in staged), 3),
        "deletable_count": len(deletable),
        "protected_count": len(staged) - len(deletable),
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
    an explicit admin action (``include_stage=True``) and still skips any
    ``artifact-stage/<run_id>`` whose run is not terminal (queued/in-flight
    runs may replay the image — fail closed, review P1).
    """
    stage_root = settings.data_root / STAGE_SUBDIR
    if dry_run:
        preview = preview_cleanup(stage_max_age_days=stage_max_age_days)
        return {**preview, "dry_run": True, "freed_bytes": 0, "removed_files": 0}

    freed = removed = 0
    if include_stage:
        # Preview and execution share _stage_candidates, so the admin always
        # sees the same set that will be deleted (review P1). Protection
        # status is re-checked right before deletion: a run could have left
        # the terminal state between preview and execution.
        candidates = [
            item for item in _stage_candidates(
                stage_root, stage_max_age_days=stage_max_age_days
            )
            if item["deletable"]
        ]
        # Protection is re-checked right before deletion: a run could have
        # left the terminal state between candidate building and removal.
        protected_now = _protected_stage_run_ids()
        if protected_now is None:
            candidates = []  # fail closed: lookup failed, delete nothing
        for item in candidates:
            if item["run_id"] in protected_now:
                continue
            freed += _remove_file(settings.data_root / item["path"])
            removed += 1
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
