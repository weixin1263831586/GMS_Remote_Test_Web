from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .users import (
    _name_keys,
    _now,
)


logger = logging.getLogger(__name__)


class RepositoryStorageMixin:
    def search_issues(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT i.*, bm25(redmine_agent_issue_fts) AS rank
                    FROM redmine_agent_issue_fts f
                    JOIN redmine_agent_issues i ON i.issue_id = f.issue_id
                    WHERE redmine_agent_issue_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (self._fts_query(query), limit),
                ).fetchall()
            except Exception as exc:
                # FTS index/query failure silently degrades to LIKE, which has
                # far worse recall — log so index corruption or bad queries
                # don't go unnoticed.
                logger.warning("search_issues FTS failed, falling back to LIKE: %s", exc)
                like = f"%{query[:80]}%"
                rows = conn.execute(
                    """
                    SELECT * FROM redmine_agent_issues
                    WHERE subject LIKE ? OR description LIKE ? OR summary LIKE ? OR error_info LIKE ?
                    ORDER BY updated_on DESC
                    LIMIT ?
                    """,
                    (like, like, like, like, limit),
                ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_unresolved_issues(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_issues WHERE is_resolved = 0 ORDER BY updated_on DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_resolved_issues_by_date(
        self,
        owner_names: list[str] | None = None,
        start: str = "",
        end: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """查询某日期范围（按 closed_on，闭区间 [start, end)）内已解决的 issue。

        owner_names 为空时不过滤指派人。用于趋势柱状图点击查看该天/周解决的问题单明细。
        日期范围用 closed_on 的字符串前缀比较（ISO 格式可字典序排序）。
        """
        owner_keys = set()
        for name in owner_names or []:
            owner_keys.update(_name_keys(name))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM redmine_agent_issues
                WHERE is_resolved = 1
                ORDER BY COALESCE(closed_on, updated_on, created_on) DESC, issue_id DESC
                """
            ).fetchall()
        issues = [self._decode_row(row) for row in rows]
        result: list[dict[str, Any]] = []
        max_items = max(1, min(int(limit or 500), 2000))
        for issue in issues:
            if owner_keys and not owner_keys.intersection(_name_keys(issue.get("assigned_to_name"))):
                continue
            resolved_on = issue.get("closed_on") or self._resolved_at_from_journals(issue)
            if start and resolved_on < start:
                continue
            if end and resolved_on >= end:
                continue
            issue["resolved_on"] = resolved_on
            result.append(issue)
            if len(result) >= max_items:
                break
        result.sort(key=lambda item: (item.get("resolved_on") or "", item.get("issue_id") or 0), reverse=True)
        return result

    def search_similar(self, query: str, exclude_issue_id: int, limit: int = 5) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        with self.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT i.*, bm25(redmine_agent_issue_fts) AS rank
                    FROM redmine_agent_issue_fts f
                    JOIN redmine_agent_issues i ON i.issue_id = f.issue_id
                    WHERE redmine_agent_issue_fts MATCH ? AND i.issue_id != ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (self._fts_query(query), exclude_issue_id, limit),
                ).fetchall()
            except Exception as exc:
                logger.warning("search_similar FTS failed, falling back to LIKE: %s", exc)
                like = f"%{query[:80]}%"
                rows = conn.execute(
                    """
                    SELECT *
                    FROM redmine_agent_issues
                    WHERE issue_id != ? AND (subject LIKE ? OR description LIKE ? OR summary LIKE ? OR doc_content LIKE ?)
                    ORDER BY updated_on DESC
                    LIMIT ?
                    """,
                    (exclude_issue_id, like, like, like, like, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def record_status_change(self, issue_id: int, old_status: str, new_status: str) -> None:
        if old_status == new_status:
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO redmine_agent_issue_status_history (issue_id, old_status, new_status, detected_at) VALUES (?, ?, ?, ?)",
                (issue_id, old_status, new_status, _now()),
            )

    # Attachments

    def insert_attachment(self, item: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO redmine_agent_attachments
                (issue_id, attachment_id, filename, content_type, filesize, local_path, analysis_json, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.get("issue_id"),
                    item.get("attachment_id"),
                    item.get("filename"),
                    item.get("content_type"),
                    item.get("filesize") or 0,
                    item.get("local_path"),
                    self._json_value(item.get("analysis_json") or {}),
                    item.get("status") or "pending",
                    item.get("error"),
                ),
            )

    # References

    def replace_references(self, issue_id: int, references: list[dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM redmine_agent_references WHERE issue_id=?", (issue_id,))
            conn.executemany(
                """
                INSERT INTO redmine_agent_references
                (issue_id, reference_issue_id, score, similarity_level, reason, match_details_json, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        issue_id,
                        int(ref.get("issue_id")),
                        float(ref.get("score") or 0),
                        ref.get("similarity_level") or "",
                        ref.get("reason") or "",
                        self._json_value(ref.get("match_details") or {}),
                        ref.get("source") or "",
                        _now(),
                    )
                    for ref in references
                    if ref.get("issue_id")
                ],
            )

    # Documents

    def write_issue_doc(self, issue_id: int, content: str) -> str:
        path = self.docs_dir / f"redmine-{issue_id}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_run_report(self, run_id: str, content: str) -> str:
        path = self.docs_dir / f"run-{run_id}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    # Internal helpers

    def _replace_fts(self, conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
        try:
            conn.execute("DELETE FROM redmine_agent_issue_fts WHERE issue_id=?", (payload.get("issue_id"),))
            conn.execute(
                """
                INSERT INTO redmine_agent_issue_fts
                (issue_id, subject, description, summary, failures, doc_content)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("issue_id"),
                    payload.get("subject") or "",
                    payload.get("description") or "",
                    payload.get("summary") or "",
                    payload.get("failures_json") or "",
                    payload.get("doc_content") or "",
                ),
            )
        except sqlite3.OperationalError:
            pass

    @staticmethod
    def _build_issue_where(status: str = "", priority: str = "", category: str = "", search: str = "", assignee_names: list[str] | None = None) -> tuple:
        clauses = []
        params: list = []
        if status:
            clauses.append("status_name=?")
            params.append(status)
        if priority:
            clauses.append("priority_name=?")
            params.append(priority)
        if category:
            clauses.append("category=?")
            params.append(category)
        # 按姓名或邮箱片段筛选个人工单。
        names = [str(n).strip() for n in (assignee_names or []) if str(n).strip()]
        if names:
            name_clauses = " OR ".join("assigned_to_name LIKE ?" for _ in names)
            clauses.append(f"({name_clauses})")
            params.extend(f"%{n}%" for n in names)
        if search:
            like = f"%{search[:80]}%"
            clauses.append("(CAST(issue_id AS TEXT) LIKE ? OR subject LIKE ? OR description LIKE ? OR error_info LIKE ? OR summary LIKE ?)")
            params.extend([like, like, like, like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = [token for token in query.replace('"', " ").split() if len(token) >= 2][:12]
        return " OR ".join(f'"{token}"' for token in tokens) or '"empty"'

    @staticmethod
    def _json_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("summary_json", "journals_json", "attachments_json", "failures_json", "references_json", "ai_json", "match_details_json"):
            if key in item:
                try:
                    item[key] = json.loads(item.get(key) or ("[]" if key not in ("ai_json", "summary_json", "match_details_json") else "{}"))
                except Exception:
                    item[key] = [] if key not in ("ai_json", "summary_json", "match_details_json") else {}
        return item
