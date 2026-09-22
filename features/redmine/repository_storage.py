from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from typing import Any

from .users import (
    _now,
    name_keys,
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
            owner_keys.update(name_keys(name))
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
            if owner_keys and not owner_keys.intersection(name_keys(issue.get("assigned_to_name"))):
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

    # 历史检索返回给 agent 的精简字段：solution/patch_direction 是"参考修复"
    # 的核心；不返回 journals/doc 全文，避免撑爆分析上下文。
    HISTORY_RESULT_FIELDS = (
        "issue_id", "subject", "status_name", "is_resolved",
        "category", "component", "soc_platform", "android_version",
        "fixed_version", "solution", "patch_direction", "summary",
        "updated_on", "closed_on",
    )
    HISTORY_TEXT_LIMITS = {"solution": 400, "patch_direction": 200, "summary": 300}
    # 命中词标注（matched_terms / distinctive_matches）扫描的文本字段。
    HISTORY_MATCH_TEXT_KEYS = ("subject", "description", "summary", "solution", "doc_content")
    # 命中词 df/语料总数 ≤ 该比例才可能成为“区分词”。实测语料（101 归档）：
    # RK3576(df=50)、Android16(df=42) 远超此线；RK3588(df=9)、power_ext(df=3)
    # 在线内。比例阈值不随语料规模漂移。
    DISTINCTIVE_DF_RATIO = 0.25
    # SoC 型号 / Android 版本即使占比低也只是背景词（如本例 RK3588 8%），
    # 不计入 distinctive_matches，避免“只命中芯片名”被当成同型问题。
    CONTEXT_TERM_RE = re.compile(r"^(?:rk\d{3,5}\w*|android\d*)$", re.IGNORECASE)

    @staticmethod
    def history_query_tokens(query: str) -> list[str]:
        """与 _fts_query 一致的查询分词（供命中标注与远端结果标注复用）。"""
        return [token for token in query.replace('"', " ").split() if len(token) >= 2][:12]

    def corpus_token_stats(self, tokens: list[str], conn: sqlite3.Connection | None = None) -> dict[str, dict[str, float]]:
        """每个 token 在归档 FTS 语料中的 idf 与 df 占比。

        idf = ln(1 + N / (1 + df))；df_ratio = df / N。FTS 不可用时返回空表，
        调用方按“无区分度信息”处理（结果退化为 bm25 原序）。
        """
        try:
            if conn is None:
                with self.connect() as own:
                    return self.corpus_token_stats(tokens, conn=own)
            total = conn.execute("SELECT count(*) FROM redmine_agent_issue_fts").fetchone()[0]
            if total <= 0:
                return {}
            stats: dict[str, dict[str, float]] = {}
            for token in tokens:
                df = conn.execute(
                    "SELECT count(*) FROM redmine_agent_issue_fts WHERE redmine_agent_issue_fts MATCH ?",
                    (f'"{token}"',),
                ).fetchone()[0]
                stats[token] = {
                    "idf": math.log(1 + total / (1 + df)),
                    "df_ratio": df / total,
                }
            return stats
        except Exception as exc:
            logger.warning("corpus_token_stats failed: %s", exc)
            return {}

    def annotate_match_terms(self, item: dict[str, Any], stats: dict[str, dict[str, float]]) -> None:
        """就地标注 matched_terms / distinctive_matches（命中词透明化）。

        matched_terms：该文档文本里实际出现的查询词；distinctive_matches：
        其中的高区分度词（低 df 占比且非 SoC/Android 版本背景词）。
        """
        haystack = " ".join(
            str(item.get(key) or "") for key in self.HISTORY_MATCH_TEXT_KEYS
        ).lower()
        matched = [token for token in stats if token.lower() in haystack]
        item["matched_terms"] = matched
        item["distinctive_matches"] = [
            token
            for token in matched
            if stats[token]["df_ratio"] <= self.DISTINCTIVE_DF_RATIO
            and not self.CONTEXT_TERM_RE.match(token)
        ]

    def search_history(
        self,
        query: str,
        exclude_issue_id: int = 0,
        limit: int = 8,
        resolved_only: bool = False,
    ) -> list[dict[str, Any]]:
        """跨工单历史检索：查找同题/类似问题的已归档分析与修复方案。

        Daily Brief 相似参考与 gms-rt-redmine-history-search 共用。FTS 优先
        （bm25 相关性），失败降级 LIKE；排序“已解决优先、相关性次之”，
        让“可参考修复”的工单排前面。

        bm25 是 OR 弱匹配：只命中 SoC 型号等背景词（如 RK3588）的短文档可能
        压过命中故障签名词（如 power_ext）的长文档——长文档 bm25 被长度归一化
        稀释。因此按命中词的语料 IDF 加权重排，并为每条结果标注 matched_terms
        与 distinctive_matches，让模型能自行核对弱相关命中。
        """
        query = (query or "").strip()
        if not query:
            return []
        exclude = int(exclude_issue_id or 0)
        limit = max(1, min(int(limit or 8), 20))
        resolved_filter = " AND i.is_resolved = 1" if resolved_only else ""
        query_tokens = self.history_query_tokens(query)
        with self.connect() as conn:
            try:
                stats = self.corpus_token_stats(query_tokens, conn=conn)
                rows = conn.execute(
                    f"""
                    SELECT i.*, bm25(redmine_agent_issue_fts) AS rank
                    FROM redmine_agent_issue_fts f
                    JOIN redmine_agent_issues i ON i.issue_id = f.issue_id
                    WHERE redmine_agent_issue_fts MATCH ? AND i.issue_id != ?{resolved_filter}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (self._fts_query(query), exclude, limit * 3),
                ).fetchall()
            except Exception as exc:
                logger.warning("search_history FTS failed, falling back to LIKE: %s", exc)
                like = f"%{query[:80]}%"
                stats = {}
                rows = conn.execute(
                    f"""
                    SELECT * FROM redmine_agent_issues
                    WHERE issue_id != ? AND (subject LIKE ? OR description LIKE ?
                       OR summary LIKE ? OR solution LIKE ? OR doc_content LIKE ?){resolved_filter.replace("i.", "")}
                    ORDER BY updated_on DESC
                    LIMIT ?
                    """,
                    (exclude, like, like, like, like, like, limit * 3),
                ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            self.annotate_match_terms(item, stats)
            distinctive = item["distinctive_matches"]
            # IDF 加权重排：命中高 idf（区分）词权重高的文档在前，其次比对
            # 上区分度阈值的词数、总命中词数；bm25 rank 作为尾序稳定项。
            # 四元组统一升序 = 最相关在前。stats 为空（LIKE 降级）时退化为
            # 原有 updated_on 序（此处 sort 稳定，不改相对顺序）。
            item["_sort"] = (
                -sum(stats[token]["idf"] for token in item["matched_terms"]),
                -len(distinctive),
                -len(item["matched_terms"]),
                item.get("rank") or 0,
            )
        items.sort(
            key=lambda row: (
                not bool(row.get("is_resolved")),
                not bool(stats),
                row["_sort"],
            )
        )
        trimmed: list[dict[str, Any]] = []
        for row in items[:limit]:
            item = {key: row.get(key) for key in self.HISTORY_RESULT_FIELDS}
            item["is_resolved"] = bool(row.get("is_resolved"))
            item["matched_terms"] = row.get("matched_terms") or []
            item["distinctive_matches"] = row.get("distinctive_matches") or []
            for key, cap in self.HISTORY_TEXT_LIMITS.items():
                text = str(item.get(key) or "")
                item[key] = text if len(text) <= cap else text[:cap] + "…"
            trimmed.append(item)
        return trimmed

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
                    # failures_json 走 _decode_row 后是 list/dict，而 FTS 列是 TEXT：
                    # 直接绑定会以 "Error binding parameter" 抛 InterfaceError，
                    # 而 except 只捕 sqlite3.OperationalError，于是索引未更新、
                    # 整个写事务（含调用方的 UPDATE）回滚。
                    self._json_value(payload.get("failures_json") or ""),
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
