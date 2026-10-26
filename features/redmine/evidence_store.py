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

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .users import _now, owner_redmine_root


SNAPSHOT_STATUSES = ("queued", "fetching", "downloading", "ready", "partial", "failed")
ARTIFACT_STATUSES = ("metadata", "downloading", "ready", "partial", "rejected", "failed")
# 派生文本状态与原件 status 正交：原件字节已完整下载即为 ready；派生
# （OCR/zip 索引/转码）是否可用由 derived_status 表达，避免一次 OCR
# 失败把"原件可用"误报成"证据不完整"。
#   ready   派生文本完整可用
#   partial 派生文本已生成但索引不完整（如 zip 成员超限被跳过）
#   missing 派生失败且无文本产出（OCR 依赖缺失/超时、解包失败）
#   empty   派生正常执行但零文本（无文字截图/照片），非证据缺口
# 空字符串表示该附件未参与派生（text/log 原件即 UTF-8、rejected/failed）。
DERIVED_STATUSES = ("ready", "partial", "missing", "empty")


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
                    derived_status TEXT DEFAULT '',
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
            self._migrate_derived_status(conn)

    def _migrate_derived_status(self, conn: sqlite3.Connection) -> None:
        """Idempotent cross-process migration: add derived_status, backfill.

        Process-safe per the repo architecture rule: BEGIN IMMEDIATE before
        reading the schema, then ALTER TABLE. Legacy rows marked partial
        because of a derivation failure are backfilled so historical
        snapshots keep accurate index warnings.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(redmine_evidence_artifacts)"
                )
            }
            if "derived_status" not in columns:
                conn.execute(
                    "ALTER TABLE redmine_evidence_artifacts "
                    "ADD COLUMN derived_status TEXT DEFAULT ''"
                )
                # 旧语义里 status=partial 只由派生失败产生（下载失败是
                # failed），回填为 partial 保留检索索引警告的准确性。
                conn.execute(
                    "UPDATE redmine_evidence_artifacts SET derived_status = 'partial' "
                    "WHERE status = 'partial' AND derived_text_path != ''"
                )
                conn.execute(
                    "UPDATE redmine_evidence_artifacts SET derived_status = 'missing' "
                    "WHERE status = 'partial' AND derived_text_path = ''"
                )
                self._restore_legacy_partial_originals(conn)
        except Exception:
            conn.rollback()
            raise
        conn.commit()

    def _restore_legacy_partial_originals(self, conn: sqlite3.Connection) -> None:
        """旧语义 status=partial 记录的原件重判定（评审 P2）。

        新契约是"原件保存成功即 ready"，派生失败只落在 derived_status；
        历史 partial 记录必须先通过磁盘原件校验才能恢复 ready——
        摘要不符或原件缺失的记录保持 partial，绝不无条件翻绿。
        旧记录 error 字段里的派生错误原样保留，与新版"派生告警独立于
        原件状态"的语义一致。
        """
        rows = conn.execute(
            "SELECT artifact_id, stored_path, sha256, size_bytes "
            "FROM redmine_evidence_artifacts WHERE status = 'partial' AND stored_path != ''"
        ).fetchall()
        for row in rows:
            path = self.evidence_dir / str(row["stored_path"])
            if not self._original_file_verified(path, row):
                continue
            conn.execute(
                "UPDATE redmine_evidence_artifacts SET status = 'ready' "
                "WHERE artifact_id = ?",
                (str(row["artifact_id"]),),
            )

    @staticmethod
    def _original_file_verified(path: Path, row: sqlite3.Row) -> bool:
        """磁盘原件校验：优先 SHA-256，旧记录无摘要时退回字节数核对。"""
        if not path.is_file():
            return False
        expected_digest = str(row["sha256"] or "")
        if expected_digest:
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                return False
            return digest.hexdigest() == expected_digest
        expected_size = int(row["size_bytes"] or 0)
        if expected_size <= 0:
            return False
        try:
            return path.stat().st_size == expected_size
        except OSError:
            return False

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

    def list_snapshots_by_status(self, statuses: set[str]) -> list[dict[str, Any]]:
        """全部处于给定状态的快照（启动中断对账用）。"""

        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM redmine_evidence_snapshots
                WHERE status IN ({placeholders})
                """,
                [str(item) for item in statuses],
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
            "derived_status",
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

    def get_artifact_by_attachment_id(self, attachment_id: str) -> dict[str, Any] | None:
        """Map a numeric Redmine attachment id to its latest artifact row.

        Models occasionally pass the Redmine numeric id where an ``art_*``
        evidence id is expected; resolving the alias server-side saves a
        wasted round trip (2026-09-24 brief run). Multiple snapshots of the
        same issue can register the same attachment id: the newest row wins.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM redmine_evidence_artifacts
                WHERE attachment_id = ?
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT 1
                """,
                (str(attachment_id or ""),),
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
        item["text_available"] = bool(item.get("derived_text_path")) or (
            item.get("kind") in {"text", "log"} and item.get("status") in {"ready", "partial"}
        )
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
