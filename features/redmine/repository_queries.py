from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .users import (
    RESOLVED_STATUS_NAMES,
    _looks_like_report_attachment,
    _looks_like_rk_actor,
    _name_keys,
    _name_matches_keys,
    _norm_name,
    _now,
    _parse_dt,
    _sorted_slice,
    _time_key,
)


def _dedupe_display_names(names: list[str]) -> list[str]:
    """Keep one display name per person, drop emails.

    - emails (含 @) 仅用于查询匹配，不作为显示名；
    - 同一人的多个写法（“卞 金晨”/“卞金晨”）按去空格归一，优先保留带空格的写法；
    - 过滤后人名为空时退回原列表，避免显示成“未识别”。
    """
    fallback = [n for n in names if n]
    persons = [n for n in fallback if "@" not in n]
    if not persons:
        return fallback
    seen: set[str] = set()
    result: list[str] = []
    # 按带空格优先排序：含空格的写法排在前面，作为该人的规范显示名。
    for name in sorted(persons, key=lambda s: (" " not in s, s)):
        compact = name.replace(" ", "")  # 去空格归一键
        if compact in seen:
            continue
        seen.add(compact)
        result.append(name)
    return result or fallback


class RepositoryQueryMixin:
    def upsert_issue(self, issue: dict[str, Any]) -> None:
        payload = dict(issue)
        payload["last_scanned_at"] = payload.get("last_scanned_at") or _now()
        json_fields = {"journals_json", "attachments_json", "failures_json", "references_json", "ai_json"}
        for key in json_fields:
            payload[key] = self._json_value(payload.get(key, [] if key != "ai_json" else {}))
        columns = [
            "issue_id", "run_id", "subject", "status_name", "priority_name", "project_name",
            "tracker_name", "author_name", "assigned_to_name", "created_on", "updated_on",
            "description", "journals_json", "attachments_json", "failures_json",
            "references_json", "ai_json", "summary", "reply_draft", "doc_path",
            "doc_content", "analysis_status", "error", "last_scanned_at",
            "error_info", "error_analysis", "solution", "patch_direction",
            "category", "is_resolved", "scan_count",
            "soc_platform", "android_version", "fixed_version", "component",
            "start_date", "due_date", "closed_on", "done_ratio",
        ]
        values = [payload.get(col) for col in columns]
        updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "issue_id")
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO redmine_agent_issues ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(issue_id) DO UPDATE SET {updates}
                """,
                values,
            )
            self._replace_fts(conn, payload)

    def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM redmine_agent_issues WHERE issue_id=?", (issue_id,)).fetchone()
        return self._decode_row(row) if row else None

    def list_all_issues(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str = "",
        priority: str = "",
        category: str = "",
        search: str = "",
        sort: str = "updated_on",
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        """Paginated listing with optional filters."""
        where, params = self._build_issue_where(status, priority, category, search)
        allowed_sorts = {"updated_on", "created_on", "priority_name", "issue_id", "subject", "analysis_status"}
        sort_col = sort if sort in allowed_sorts else "updated_on"
        order_dir = "DESC" if order.lower() == "desc" else "ASC"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM redmine_agent_issues {where} ORDER BY {sort_col} {order_dir} LIMIT ? OFFSET ?",
                [*params, max(1, min(limit, 100)), max(0, offset)],
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def count_issues(
        self,
        status: str = "",
        priority: str = "",
        category: str = "",
        search: str = "",
    ) -> int:
        where, params = self._build_issue_where(status, priority, category, search)
        with self.connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS cnt FROM redmine_agent_issues {where}", params).fetchone()
        return int(row["cnt"]) if row else 0

    def get_issue_statistics(self) -> dict[str, Any]:
        with self.connect() as conn:
            # Single query for total, unresolved, and all group-by counts
            total = conn.execute("SELECT COUNT(*) AS c FROM redmine_agent_issues").fetchone()["c"]
            unresolved = conn.execute(
                "SELECT COUNT(*) AS c FROM redmine_agent_issues WHERE is_resolved = 0"
            ).fetchone()["c"]
            by_status = conn.execute(
                "SELECT status_name, COUNT(*) AS c FROM redmine_agent_issues GROUP BY status_name"
            ).fetchall()
            by_priority = conn.execute(
                "SELECT priority_name, COUNT(*) AS c FROM redmine_agent_issues GROUP BY priority_name"
            ).fetchall()
            by_analysis = conn.execute(
                "SELECT analysis_status, COUNT(*) AS c FROM redmine_agent_issues GROUP BY analysis_status"
            ).fetchall()
            by_category = conn.execute(
                "SELECT category, COUNT(*) AS c FROM redmine_agent_issues WHERE category != '' GROUP BY category"
            ).fetchall()
        return {
            "total": total,
            "unresolved": unresolved,
            "by_status": {r["status_name"] or "unknown": r["c"] for r in by_status},
            "by_priority": {r["priority_name"] or "unknown": r["c"] for r in by_priority},
            "by_analysis_status": {r["analysis_status"] or "unknown": r["c"] for r in by_analysis},
            "by_category": {r["category"] or "unknown": r["c"] for r in by_category},
        }

    def get_workload_statistics(
        self,
        owner_names: list[str] | None = None,
        stale_days: int = 3,
        list_limit: int = 30,
        display_names: list[str] | None = None,
        window_days: int = 0,
    ) -> dict[str, Any]:
        """Return Redmine workload metrics for the statistics dashboard.

        The database stores Redmine snapshots, so journal-based metrics are as
        fresh as the latest sync/analyze pass that populated journals_json.

        Args:
            window_days: If > 0, only count issues with activity within this many
                         days. Prevents ancient issues from inflating stale counts.
        """
        owner_keys = set()
        for name in owner_names or []:
            owner_keys.update(_name_keys(name))
        stale_after = datetime.now() - timedelta(days=max(1, int(stale_days or 3)))
        list_limit = max(1, min(int(list_limit or 30), 100))
        now = datetime.now()
        window_cutoff = now - timedelta(days=int(window_days)) if int(window_days or 0) > 0 else None

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT issue_id, subject, status_name, priority_name, assigned_to_name,
                       created_on, updated_on, closed_on, description, category,
                       is_resolved, last_scanned_at, journals_json, attachments_json, failures_json
                FROM redmine_agent_issues
                ORDER BY COALESCE(updated_on, created_on) DESC, issue_id DESC
                """
            ).fetchall()

        issues = [self._decode_row(row) for row in rows]
        if not owner_keys:
            owner_keys = {
                key
                for item in issues
                for key in _name_keys(item.get("assigned_to_name"))
            }

        owned_issues = [
            issue for issue in issues
            if self._is_assigned_to_owner(issue, owner_keys)
        ]
        open_issues: list[dict[str, Any]] = []
        waiting_my_reply: list[dict[str, Any]] = []
        stale_my_reply: list[dict[str, Any]] = []
        waiting_customer_reply: list[dict[str, Any]] = []
        stale_customer_reply: list[dict[str, Any]] = []
        stale_rk_colleague_reply: list[dict[str, Any]] = []
        missing_test_report: list[dict[str, Any]] = []
        resolved_counts: dict[str, dict[str, int]] = {
            "day": {}, "week": {}, "month": {}, "year": {},
        }

        for issue in owned_issues:
            if self._is_issue_resolved(issue):
                resolved_at = issue.get("closed_on") or self._resolved_at_from_journals(issue)
                for gran, bucket in resolved_counts.items():
                    key = _time_key(resolved_at, gran)
                    if key:
                        bucket[key] = bucket.get(key, 0) + 1
                continue

            open_issues.append(issue)

            reply_info = self._reply_wait_info(issue, owner_keys)
            if reply_info.get("waiting"):
                summary = self._issue_summary(issue, reply_info=reply_info)
                waiting_my_reply.append(summary)
                last_dt = _parse_dt(reply_info.get("last_external_reply_at"))
                if last_dt and last_dt <= stale_after:
                    in_window = True
                    if window_cutoff:
                        issue_updated = _parse_dt(issue.get("updated_on"))
                        if not issue_updated or issue_updated < window_cutoff:
                            in_window = False
                    if in_window:
                        stale_my_reply.append(summary)
                        if reply_info.get("last_reply_side") == "rk_colleague":
                            stale_rk_colleague_reply.append(summary)
            elif reply_info.get("waiting_customer"):
                summary = self._issue_summary(issue, reply_info=reply_info)
                waiting_customer_reply.append(summary)
                last_dt = _parse_dt(reply_info.get("last_owner_reply_at"))
                if last_dt and last_dt <= stale_after:
                    in_window = True
                    if window_cutoff:
                        issue_updated = _parse_dt(issue.get("updated_on"))
                        if not issue_updated or issue_updated < window_cutoff:
                            in_window = False
                    if in_window:
                        stale_customer_reply.append(summary)

            if self._is_missing_test_report(issue):
                missing_test_report.append(self._issue_summary(issue))

        waiting_my_reply.sort(key=lambda item: item.get("last_external_reply_at") or item.get("updated_on") or "", reverse=True)
        stale_my_reply.sort(key=lambda item: item.get("last_external_reply_at") or item.get("updated_on") or "")
        waiting_customer_reply.sort(key=lambda item: item.get("last_owner_reply_at") or item.get("updated_on") or "", reverse=True)
        stale_customer_reply.sort(key=lambda item: item.get("last_owner_reply_at") or item.get("updated_on") or "")
        stale_rk_colleague_reply.sort(key=lambda item: item.get("last_external_reply_at") or item.get("updated_on") or "")
        missing_test_report.sort(key=lambda item: item.get("updated_on") or "", reverse=True)

        return {
            "total_owned": len(owned_issues),
            "open_count": len(open_issues),
            "closed_count": len(owned_issues) - len(open_issues),
            "waiting_my_reply": len(waiting_my_reply),
            "no_reply_3_days": len(stale_my_reply),
            "rk_no_reply_3_days": len(stale_my_reply),
            "waiting_customer_reply": len(waiting_customer_reply),
            "customer_no_reply_3_days": len(stale_customer_reply),
            "rk_colleague_no_reply_3_days": len(stale_rk_colleague_reply),
            "missing_test_report": len(missing_test_report),
            "resolved_daily": _sorted_slice(resolved_counts["day"], "date", 90),
            "resolved_weekly": _sorted_slice(resolved_counts["week"], "week", 52),
            "resolved_monthly": _sorted_slice(resolved_counts["month"], "month", 24),
            "resolved_yearly": _sorted_slice(resolved_counts["year"], "year", 10),
            "lists": {
                "waiting_my_reply": waiting_my_reply[:list_limit],
                "no_reply_3_days": stale_my_reply[:list_limit],
                "customer_no_reply_3_days": stale_customer_reply[:list_limit],
                "waiting_customer_reply": waiting_customer_reply[:list_limit],
                "rk_colleague_no_reply_3_days": stale_rk_colleague_reply[:list_limit],
                "missing_test_report": missing_test_report[:list_limit],
                "open_issues": [self._issue_summary(item) for item in open_issues[:list_limit]],
            },
            "meta": {
                # 显示名只保留人名，过滤掉邮箱/login 等匹配键（它们只用于查询匹配，
                # 不应出现在“统计身份”里）。同一人的多个写法（如“卞 金晨”与“卞金晨”，
                # 由 _name_display_variants 为匹配而生）按去空格归一，只保留一个，
                # 优先带空格的规范写法。若过滤后为空（例如只有邮箱可用），则退回原列表，
                # 避免显示成“未识别”。
                "owner_names": _dedupe_display_names(list(display_names or owner_names or [])),
                "stale_days": stale_days,
                "list_limit": list_limit,
                "generated_at": _now(),
            },
        }

    def list_assignee_names(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT assigned_to_name, COUNT(*) AS c
                FROM redmine_agent_issues
                WHERE assigned_to_name IS NOT NULL AND assigned_to_name != ''
                GROUP BY assigned_to_name
                ORDER BY c DESC, assigned_to_name
                """
            ).fetchall()
        return [str(row["assigned_to_name"] or "") for row in rows if row["assigned_to_name"]]

    def resolve_assignee_names(self, query_names: list[str]) -> dict[str, list[str]]:
        assignees = self.list_assignee_names()
        assignee_keys = {
            name: _name_keys(name)
            for name in assignees
        }
        resolved: dict[str, list[str]] = {}
        for raw_name in query_names:
            name = str(raw_name or "").strip()
            if not name:
                continue
            query_keys = _name_keys(name)
            compact_query = _norm_name(name).replace(" ", "")
            matches = []
            for assignee, keys in assignee_keys.items():
                compact_assignee = _norm_name(assignee).replace(" ", "")
                if query_keys.intersection(keys) or (compact_query and compact_query in compact_assignee):
                    matches.append(assignee)
            resolved[name] = list(dict.fromkeys(matches)) or [name]
        return resolved

    @staticmethod
    def _is_issue_resolved(issue: dict[str, Any]) -> bool:
        return bool(issue.get("is_resolved")) or str(issue.get("status_name") or "") in RESOLVED_STATUS_NAMES

    @staticmethod
    def _is_assigned_to_owner(issue: dict[str, Any], owner_keys: set) -> bool:
        if not owner_keys:
            return True
        return _name_matches_keys(issue.get("assigned_to_name"), owner_keys)

    @staticmethod
    def _last_note_journal(issue: dict[str, Any]) -> dict[str, Any]:
        journals = issue.get("journals_json") or []
        note_journals = [j for j in journals if str(j.get("notes") or "").strip()]
        if not note_journals:
            return {}
        return max(note_journals, key=lambda item: _parse_dt(item.get("created_on")) or datetime.min)

    @staticmethod
    def _last_activity_journal(issue: dict[str, Any]) -> dict[str, Any]:
        journals = issue.get("journals_json") or []
        activity_journals = [
            j for j in journals
            if str(j.get("notes") or "").strip() or (j.get("details") or [])
        ]
        if not activity_journals:
            return {}
        return max(activity_journals, key=lambda item: _parse_dt(item.get("created_on")) or datetime.min)

    @classmethod
    def _reply_wait_info(cls, issue: dict[str, Any], owner_keys: set) -> dict[str, Any]:
        last_activity = cls._last_activity_journal(issue)
        if not last_activity:
            return {"waiting": False, "reason": "no_journal_notes"}
        last_user = last_activity.get("user") or ""
        if _name_matches_keys(last_user, owner_keys) or _looks_like_rk_actor(last_activity):
            return {
                "waiting": False,
                "reason": "last_reply_is_rk",
                "waiting_customer": True,
                "last_owner_reply_at": last_activity.get("created_on") or issue.get("updated_on") or "",
                "last_owner_reply_by": last_user,
                "last_owner_reply": str(last_activity.get("notes") or "")[:260],
            }
        return {
            "waiting": True,
            "last_reply_side": "customer",
            "last_external_reply_at": last_activity.get("created_on") or issue.get("updated_on") or "",
            "last_external_reply_by": last_user,
            "last_external_reply": str(last_activity.get("notes") or "")[:260],
        }

    @staticmethod
    def _is_missing_test_report(issue: dict[str, Any]) -> bool:
        attachments = issue.get("attachments_json") or []
        if any(_looks_like_report_attachment(att) for att in attachments if isinstance(att, dict)):
            return False
        failures = issue.get("failures_json") or []
        if failures:
            return False
        description = str(issue.get("description") or "").lower()
        if any(token in description for token in ("test_result", "测试报告", "测试结果", "tradefed")):
            return False
        return True

    @staticmethod
    def _resolved_at_from_journals(issue: dict[str, Any]) -> str:
        journals = issue.get("journals_json") or []
        for journal in reversed(journals):
            for detail in journal.get("details") or []:
                if str(detail.get("name") or "").lower() == "status":
                    new_value = str(detail.get("new_value") or "")
                    if new_value in RESOLVED_STATUS_NAMES:
                        return journal.get("created_on") or ""
        return ""

    @staticmethod
    def _issue_summary(issue: dict[str, Any], reply_info: dict[str, Any] | None = None) -> dict[str, Any]:
        reply_info = reply_info or {}
        return {
            "issue_id": issue.get("issue_id"),
            "subject": issue.get("subject") or "",
            "status_name": issue.get("status_name") or "",
            "priority_name": issue.get("priority_name") or "",
            "assigned_to_name": issue.get("assigned_to_name") or "",
            "updated_on": issue.get("updated_on") or "",
            "created_on": issue.get("created_on") or "",
            "closed_on": issue.get("closed_on") or "",
            "last_scanned_at": issue.get("last_scanned_at") or "",
            "last_external_reply_at": reply_info.get("last_external_reply_at") or "",
            "last_external_reply_by": reply_info.get("last_external_reply_by") or "",
            "last_external_reply": reply_info.get("last_external_reply") or "",
            "last_reply_side": reply_info.get("last_reply_side") or "",
            "last_owner_reply_at": reply_info.get("last_owner_reply_at") or "",
            "last_owner_reply_by": reply_info.get("last_owner_reply_by") or "",
            "last_owner_reply": reply_info.get("last_owner_reply") or "",
            "attachment_count": len(issue.get("attachments_json") or []),
        }
