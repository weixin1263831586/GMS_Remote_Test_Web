"""CRUD layer for the internal Redmine knowledge base.

Composes with :class:`KnowledgeSchemaMixin`. All JSON columns are stored with
``ensure_ascii=False`` and decoded back to Python objects on read.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .knowledge_schema import KnowledgeSchemaMixin
from .users import _now


class RedmineKnowledgeDB(KnowledgeSchemaMixin):
    """SQLite repository for the Redmine knowledge base."""

    # case_facts

    def upsert_case_fact(self, payload: dict[str, Any]) -> None:
        issue_id = int(payload.get("issue_id") or 0)
        if not issue_id:
            raise ValueError("case_fact requires issue_id")
        now = _now()
        existing = self.get_case_fact(issue_id)
        created_at = (existing or {}).get("created_at") or now
        fields = {
            "issue_id": issue_id,
            "subject": payload.get("subject") or "",
            "status_name": payload.get("status_name") or "",
            "assigned_to_name": payload.get("assigned_to_name") or "",
            "project_name": payload.get("project_name") or "",
            "category": payload.get("category") or "",
            "chip_platform": payload.get("chip_platform") or "",
            "android_version": payload.get("android_version") or "",
            "certification_type": payload.get("certification_type") or "",
            "module": payload.get("module") or "",
            "product_form": payload.get("product_form") or "",
            "region": payload.get("region") or "",
            "error_signature": payload.get("error_signature") or "",
            "problem_summary": payload.get("problem_summary") or "",
            "symptoms_json": self._json_value(payload.get("symptoms") or payload.get("symptoms_json") or []),
            "root_cause": payload.get("root_cause") or "",
            "solution": payload.get("solution") or "",
            "verification": payload.get("verification") or "",
            "reply_template": payload.get("reply_template") or "",
            "keywords_json": self._json_value(payload.get("keywords") or payload.get("keywords_json") or []),
            "evidence_json": self._json_value(payload.get("evidence") or payload.get("evidence_json") or {}),
            "doc_excerpt": payload.get("doc_excerpt") or "",
            "confidence": float(payload.get("confidence") or 0),
            "source_quality": payload.get("source_quality") or "",
            "created_at": created_at,
            "updated_at": now,
        }
        columns = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        with self.connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO redmine_case_facts ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            self._replace_fts(conn, fields)

    def _replace_fts(self, conn: sqlite3.Connection, fields: dict[str, Any]) -> None:
        try:
            conn.execute("DELETE FROM redmine_case_facts_fts WHERE issue_id=?", (fields["issue_id"],))
            conn.execute(
                """
                INSERT INTO redmine_case_facts_fts
                (issue_id, subject, problem_summary, error_signature, root_cause, solution, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["issue_id"],
                    fields["subject"],
                    fields["problem_summary"],
                    fields["error_signature"],
                    fields["root_cause"],
                    fields["solution"],
                    " ".join(self._decode_list(fields["keywords_json"])),
                ),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _decode_list(value: Any) -> list:
        if isinstance(value, list):
            return value
        try:
            data = json.loads(value) if value else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_case_fact(self, issue_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_case_facts WHERE issue_id=?", (int(issue_id),)
            ).fetchone()
        return self._decode_row(row) if row else None

    def get_case_facts_for_issue_ids(self, issue_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Batch fetch case facts keyed by issue_id (single query).

        Replaces N per-row ``get_case_fact`` calls on list endpoints (N+1 → 1).
        """
        ids = [int(i) for i in (issue_ids or []) if i]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM redmine_case_facts WHERE issue_id IN ({placeholders})",
                ids,
            ).fetchall()
        return {
            int((decoded := self._decode_row(row))["issue_id"]): decoded
            for row in rows
        }

    def list_case_facts(self, limit: int = 50, offset: int = 0, module: str = "", search: str = "") -> list[dict[str, Any]]:
        clauses, params = self._build_facts_where(module, search)
        sql = f"SELECT * FROM redmine_case_facts {clauses} ORDER BY issue_id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_row(row) for row in rows]

    def count_case_facts(self, module: str = "", search: str = "") -> int:
        clauses, params = self._build_facts_where(module, search)
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM redmine_case_facts {clauses}", params).fetchone()
        return int(row["c"] if row else 0)

    def _build_facts_where(self, module: str, search: str) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if module:
            clauses.append("module=?")
            params.append(module)
        if search:
            like = f"%{search[:80]}%"
            clauses.append("(subject LIKE ? OR error_signature LIKE ? OR problem_summary LIKE ? OR keywords_json LIKE ?)")
            params.extend([like, like, like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def search_case_facts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT f.*, bm25(redmine_case_facts_fts) AS rank
                    FROM redmine_case_facts_fts t
                    JOIN redmine_case_facts f ON f.issue_id = t.issue_id
                    WHERE redmine_case_facts_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (self._fts_query(query), limit),
                ).fetchall()
            except Exception:
                like = f"%{query[:80]}%"
                rows = conn.execute(
                    """
                    SELECT * FROM redmine_case_facts
                    WHERE subject LIKE ? OR error_signature LIKE ? OR problem_summary LIKE ?
                       OR root_cause LIKE ? OR solution LIKE ?
                    ORDER BY issue_id DESC LIMIT ?
                    """,
                    (like, like, like, like, like, limit),
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    # mature_cases

    def upsert_mature_case(self, payload: dict[str, Any]) -> int:
        case_id = payload.get("case_id")
        now = _now()
        fields = {
            "title": payload.get("title") or "",
            "status": payload.get("status") or "draft",
            "canonical_error_signature": payload.get("canonical_error_signature") or "",
            "chip_platform": payload.get("chip_platform") or "",
            "android_version": payload.get("android_version") or "",
            "certification_type": payload.get("certification_type") or "",
            "module": payload.get("module") or "",
            "product_form": payload.get("product_form") or "",
            "region": payload.get("region") or "",
            "problem_summary": payload.get("problem_summary") or "",
            "scope_json": self._json_value(payload.get("scope") or payload.get("scope_json") or {}),
            "symptoms_json": self._json_value(payload.get("symptoms") or payload.get("symptoms_json") or []),
            "root_cause": payload.get("root_cause") or "",
            "solution_json": self._json_value(payload.get("solution") if isinstance(payload.get("solution"), dict) else (payload.get("solution_json") or {})),
            "notes_json": self._json_value(payload.get("notes") or payload.get("notes_json") or []),
            "rules_json": self._json_value(payload.get("rules") or payload.get("rules_json") or []),
            "reply_template": payload.get("reply_template") or "",
            "source_issue_ids_json": self._json_value(payload.get("source_issue_ids") or payload.get("source_issue_ids_json") or []),
            "evidence_json": self._json_value(payload.get("evidence") or payload.get("evidence_json") or {}),
            "keywords_json": self._json_value(payload.get("keywords") or payload.get("keywords_json") or []),
            "confidence": float(payload.get("confidence") or 0),
            "updated_at": now,
        }
        with self.connect() as conn:
            if case_id:
                existing = conn.execute(
                    "SELECT created_at FROM redmine_mature_cases WHERE case_id=?", (int(case_id),)
                ).fetchone()
                fields["created_at"] = (existing["created_at"] if existing else now)
                columns = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE redmine_mature_cases SET {columns} WHERE case_id=?",
                    (*fields.values(), int(case_id)),
                )
            else:
                fields["created_at"] = now
                columns = ", ".join(fields.keys())
                placeholders = ", ".join("?" for _ in fields)
                cur = conn.execute(
                    f"INSERT INTO redmine_mature_cases ({columns}) VALUES ({placeholders})",
                    tuple(fields.values()),
                )
                case_id = cur.lastrowid
        return int(case_id or 0)

    def get_mature_case(self, case_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_mature_cases WHERE case_id=?", (int(case_id),)
            ).fetchone()
        return self._decode_row(row, default_obj=True) if row else None

    def list_mature_cases(self, limit: int = 50, offset: int = 0, status: str = "", search: str = "") -> list[dict[str, Any]]:
        clauses, params = self._build_cases_where(status, search)
        sql = f"SELECT * FROM redmine_mature_cases {clauses} ORDER BY case_id DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(limit, 500)), max(0, offset)])
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode_row(row, default_obj=True) for row in rows]

    def count_mature_cases(self, status: str = "", search: str = "") -> int:
        clauses, params = self._build_cases_where(status, search)
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM redmine_mature_cases {clauses}", params).fetchone()
        return int(row["c"] if row else 0)

    def _build_cases_where(self, status: str, search: str) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            like = f"%{search[:80]}%"
            clauses.append("(title LIKE ? OR problem_summary LIKE ? OR canonical_error_signature LIKE ? OR keywords_json LIKE ?)")
            params.extend([like, like, like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def approve_mature_case(self, case_id: int, approved_by: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE redmine_mature_cases
                SET status='approved', approved_by=?, approved_at=?, updated_at=?
                WHERE case_id=?
                """,
                (approved_by, _now(), _now(), int(case_id)),
            )
            return cur.rowcount > 0

    # case_issue_links

    def link_case_issue(self, case_id: int, issue_id: int, score: float = 0, reason: str = "", evidence: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO redmine_case_issue_links (case_id, issue_id, score, reason, evidence_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id, issue_id) DO UPDATE SET
                    score=excluded.score, reason=excluded.reason, evidence_json=excluded.evidence_json
                """,
                (int(case_id), int(issue_id), float(score or 0), reason, self._json_value(evidence or {}), _now()),
            )

    def list_links_for_case(self, case_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_case_issue_links WHERE case_id=? ORDER BY score DESC",
                (int(case_id),),
            ).fetchall()
        return [self._decode_row(row, default_obj=True) for row in rows]

    def list_links_for_issue(self, issue_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_case_issue_links WHERE issue_id=? ORDER BY score DESC",
                (int(issue_id),),
            ).fetchall()
        return [self._decode_row(row, default_obj=True) for row in rows]

    # reference_outputs

    def insert_reference_output(self, issue_id: int, source: str, payload: dict[str, Any]) -> int:
        now = _now()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO redmine_reference_outputs
                (issue_id, source, title, markdown, structured_json, raw_output, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(issue_id),
                    source,
                    payload.get("title") or "",
                    payload.get("markdown") or "",
                    self._json_value(payload.get("structured_json") or payload.get("structured") or {}),
                    payload.get("raw_output") or "",
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid or 0)

    def get_reference_outputs(self, issue_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_reference_outputs WHERE issue_id=? ORDER BY id DESC",
                (int(issue_id),),
            ).fetchall()
        return [self._decode_row(row, default_obj=True) for row in rows]

    # case_evaluations

    def insert_case_evaluation(self, issue_id: int, payload: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO redmine_case_evaluations
                (issue_id, internal_case_json, reference_case_json, score,
                 missing_fields_json, mismatch_fields_json, suggestions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(issue_id),
                    self._json_value(payload.get("internal_case") or payload.get("internal_case_json") or {}),
                    self._json_value(payload.get("reference_case") or payload.get("reference_case_json") or {}),
                    float(payload.get("score") or 0),
                    self._json_value(payload.get("missing_fields") or payload.get("missing_fields_json") or []),
                    self._json_value(payload.get("mismatch_fields") or payload.get("mismatch_fields_json") or []),
                    self._json_value(payload.get("suggestions") or payload.get("suggestions_json") or []),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)

    def get_latest_case_evaluation(self, issue_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_case_evaluations WHERE issue_id=? ORDER BY id DESC LIMIT 1",
                (int(issue_id),),
            ).fetchone()
        return self._decode_row(row, default_obj=True) if row else None

    # internal_issue_links

    def insert_internal_issue_link(self, payload: dict[str, Any]) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO redmine_internal_issue_links
                (source_issue_id, case_id, internal_issue_id, created_by, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("source_issue_id"),
                    payload.get("case_id"),
                    int(payload.get("internal_issue_id") or 0),
                    payload.get("created_by") or "",
                    self._json_value(payload.get("payload") or payload.get("payload_json") or {}),
                    _now(),
                ),
            )
            return int(cur.lastrowid or 0)
