"""Evidence snapshot/artifact persistence (per-owner SQLite).

Evidence data is
kept strictly separate from the dashboard tables (``redmine_agent_issues``):
the dashboard keeps its summarized/2,000-char-truncated semantics, while this
store keeps byte-faithful raw evidence for the agent analysis chain.

Storage layout under ``owner_redmine_root(owner_id)``:

    evidence.sqlite3                                  (this store)
    evidence/<issue_id>/<snapshot_id>/issue.json      (raw Redmine JSON bytes)
    evidence/<issue_id>/<snapshot_id>/manifest.json   (normalized manifest)
    evidence/<issue_id>/<snapshot_id>/attachments/<artifact_id>/<name>
    evidence/<issue_id>/<snapshot_id>/derived/<artifact_id>.txt

Paths stored in the DB are relative to the owner evidence root; they are
server-internal and must never be returned to clients.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .users import _now, owner_redmine_root


SNAPSHOT_STATUSES = ("queued", "fetching", "downloading", "ready", "partial", "failed")
ARTIFACT_STATUSES = ("metadata", "downloading", "ready", "partial", "rejected", "failed")


def new_snapshot_id() -> str:
    return "ev_" + uuid.uuid4().hex


def new_artifact_id() -> str:
    return "art_" + uuid.uuid4().hex


class EvidenceStore:
    """SQLite persistence for one owner's evidence snapshots/artifacts."""

    def __init__(self, owner_root: Path):
        self.owner_root = Path(owner_root)
        self.evidence_dir = self.owner_root / "evidence"
        self.db_path = self.owner_root / "evidence.sqlite3"
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ schema

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS redmine_evidence_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    issue_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_updated_on TEXT DEFAULT '',
                    fetched_at TEXT DEFAULT '',
                    raw_json_path TEXT DEFAULT '',
                    manifest_json TEXT DEFAULT '{}',
                    content_sha256 TEXT DEFAULT '',
                    journal_count INTEGER DEFAULT 0,
                    attachment_count INTEGER DEFAULT 0,
                    downloaded_count INTEGER DEFAULT 0,
                    download_policy TEXT DEFAULT 'none',
                    error_json TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_issue
                    ON redmine_evidence_snapshots(issue_id, created_at);

                CREATE TABLE IF NOT EXISTS redmine_evidence_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    attachment_id TEXT DEFAULT '',
                    filename TEXT DEFAULT '',
                    original_filename TEXT DEFAULT '',
                    content_type TEXT DEFAULT '',
                    detected_content_type TEXT DEFAULT '',
                    size_bytes INTEGER DEFAULT 0,
                    declared_size INTEGER DEFAULT 0,
                    sha256 TEXT DEFAULT '',
                    stored_path TEXT DEFAULT '',
                    derived_text_path TEXT DEFAULT '',
                    kind TEXT DEFAULT 'unknown',
                    status TEXT DEFAULT 'metadata',
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_snapshot
                    ON redmine_evidence_artifacts(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_artifacts_attachment
                    ON redmine_evidence_artifacts(attachment_id);
                """
            )

    # --------------------------------------------------------------- snapshots

    def create_snapshot(
        self,
        *,
        issue_id: int,
        download_policy: str = "none",
    ) -> dict[str, Any]:
        snapshot_id = new_snapshot_id()
        created = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO redmine_evidence_snapshots (
                    snapshot_id, issue_id, status, download_policy, created_at
                ) VALUES (?, ?, 'queued', ?, ?)
                """,
                (snapshot_id, int(issue_id), str(download_policy or "none"), created),
            )
        return self.get_snapshot(snapshot_id) or {"snapshot_id": snapshot_id, "status": "queued"}

    def update_snapshot(self, snapshot_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "source_updated_on",
            "fetched_at",
            "raw_json_path",
            "manifest_json",
            "content_sha256",
            "journal_count",
            "attachment_count",
            "downloaded_count",
            "download_policy",
            "error_json",
        }
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("manifest_json", "error_json") and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            updates[key] = value
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*list(updates.values()), snapshot_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE redmine_evidence_snapshots SET {assignments} WHERE snapshot_id = ?",
                values,
            )

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_evidence_snapshots WHERE snapshot_id = ?",
                (str(snapshot_id or ""),),
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def latest_snapshot_for_issue(self, issue_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM redmine_evidence_snapshots
                WHERE issue_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (int(issue_id),),
            ).fetchone()
        if row is None:
            return None
        return self._snapshot_from_row(row)

    def list_snapshots_for_issue(
        self, issue_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """某 issue 的快照列表，最新在前（latest 端点用）。"""

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM redmine_evidence_snapshots
                WHERE issue_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (int(issue_id), int(limit)),
            ).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    @staticmethod
    def _snapshot_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["manifest"] = json.loads(item.pop("manifest_json") or "{}")
        except ValueError:
            item["manifest"] = {}
        try:
            item["errors"] = json.loads(item.pop("error_json") or "[]")
        except ValueError:
            item["errors"] = []
        # raw_json_path is server-internal; drop it before any API exposure.
        item.pop("raw_json_path", None)
        return item

    # --------------------------------------------------------------- artifacts

    def create_artifact(
        self,
        *,
        snapshot_id: str,
        attachment_id: str,
        filename: str,
        original_filename: str,
        content_type: str,
        kind: str,
        declared_size: int = 0,
    ) -> dict[str, Any]:
        artifact_id = new_artifact_id()
        created = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO redmine_evidence_artifacts (
                    artifact_id, snapshot_id, attachment_id, filename,
                    original_filename, content_type, kind, declared_size,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'metadata', ?)
                """,
                (
                    artifact_id,
                    str(snapshot_id),
                    str(attachment_id or ""),
                    str(filename or ""),
                    str(original_filename or ""),
                    str(content_type or ""),
                    str(kind or "unknown"),
                    int(declared_size or 0),
                    created,
                ),
            )
        return self.get_artifact(artifact_id) or {"artifact_id": artifact_id, "status": "metadata"}

    def update_artifact(self, artifact_id: str, **fields: Any) -> None:
        allowed = {
            "filename",
            "content_type",
            "detected_content_type",
            "size_bytes",
            "declared_size",
            "sha256",
            "stored_path",
            "derived_text_path",
            "kind",
            "status",
            "error",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*list(updates.values()), str(artifact_id)]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE redmine_evidence_artifacts SET {assignments} WHERE artifact_id = ?",
                values,
            )

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_evidence_artifacts WHERE artifact_id = ?",
                (str(artifact_id or ""),),
            ).fetchone()
        if row is None:
            return None
        return self._artifact_from_row(row)

    def list_artifacts(self, snapshot_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM redmine_evidence_artifacts
                WHERE snapshot_id = ?
                ORDER BY created_at, artifact_id
                """,
                (str(snapshot_id or ""),),
            ).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        # Server-internal absolute paths never leave the Controller.
        item.pop("stored_path", None)
        item.pop("derived_text_path", None)
        return item

    # -------------------------------------------------------------- file paths

    def snapshot_dir(self, snapshot: dict[str, Any]) -> Path:
        return self.evidence_dir / str(snapshot["issue_id"]) / str(snapshot["snapshot_id"])

    def resolve_internal(self, relative_path: str) -> Path:
        """Resolve a stored internal path, refusing escapes from evidence_dir."""
        base = self.evidence_dir.resolve()
        candidate = (base / str(relative_path or "")).resolve()
        if candidate != base and base not in candidate.parents:
            raise ValueError("evidence path escapes owner evidence directory")
        return candidate


_STORE_CACHE: dict[str, EvidenceStore] = {}
_STORE_LOCK = threading.Lock()


def owner_evidence_store(owner_id: str) -> EvidenceStore:
    """Return (and cache) the evidence store for one platform owner."""
    key = str(owner_id or "anonymous")
    with _STORE_LOCK:
        store = _STORE_CACHE.get(key)
        if store is None:
            store = EvidenceStore(owner_redmine_root(key))
            _STORE_CACHE[key] = store
        return store
