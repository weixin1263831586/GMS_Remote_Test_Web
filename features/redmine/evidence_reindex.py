"""Repair legacy budget-truncated indexes from the owner's saved originals."""

from __future__ import annotations

from .evidence_archives import extract_system_archive_text
from .evidence_text import derive_text_file
from .evidence_zip import extract_zip_derived_text


def repair_budget_indexes(store, snapshot):
    repaired = False
    for artifact in store.list_artifacts(snapshot["snapshot_id"]):
        error = str(artifact.get("error") or "")
        if (artifact.get("status") != "ready"
                or artifact.get("derived_status") not in {"partial", "missing"}
                or not any(marker in error for marker in ("超阈值", "上限", "超过阈值", "派生预算"))):
            continue
        with store._connect() as connection:
            row = connection.execute(
                "SELECT stored_path FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (artifact["artifact_id"],),
            ).fetchone()
        if not row or not row["stored_path"]:
            continue
        try:
            source = store.resolve_internal(row["stored_path"])
            # Stored paths are evidence-root-relative; resolve_internal does
            # not expose or permit files belonging to a different owner.
            relative = source.parent.parent.parent.relative_to(store.evidence_dir).as_posix()
            if artifact["kind"] in {"text", "log"}:
                derived, note = derive_text_file(store, artifact["artifact_id"], relative, source)
            elif artifact["kind"] == "archive":
                with source.open("rb") as handle:
                    is_zip = handle.read(2) == b"PK"
                extract = extract_zip_derived_text if is_zip else extract_system_archive_text
                derived, note = extract(store, None, artifact["artifact_id"], relative, source)
            else:
                continue
        except (OSError, ValueError):
            continue
        preserved = [part for part in error.split("; ")
                     if not any(marker in part for marker in ("超阈值", "上限", "超过阈值"))]
        status = "ready" if derived else ("empty" if artifact["kind"] == "archive" else "")
        if note:
            status = "partial" if derived else "missing"
        store.update_artifact(
            artifact["artifact_id"], derived_text_path=derived, derived_status=status,
            error="; ".join([*preserved, *([note] if note else [])]),
        )
        repaired = True
    if repaired:
        current = store.get_snapshot(snapshot["snapshot_id"]) or snapshot
        errors = [item for item in current.get("errors", []) if item.get("stage") != "derive"]
        errors.extend(
            {"stage": "derive", "message": f"attachment {item['attachment_id']}: {item.get('error') or '文本未完整提取'}"}
            for item in store.list_artifacts(snapshot["snapshot_id"])
            if item.get("derived_status") in {"partial", "missing"}
        )
        store.update_snapshot(snapshot["snapshot_id"], error_json=errors)
