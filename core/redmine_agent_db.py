"""Persistence for RedmineAgent nightly triage."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.settings import PROJECT_ROOT


DB_PATH = Path(PROJECT_ROOT) / "data" / "redmine_agent.sqlite3"
DOCS_DIR = Path(PROJECT_ROOT) / "data" / "redmine_agent_docs"
USER_MAP_PATH = Path(PROJECT_ROOT) / "configs" / "redmine_user_map.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


RESOLVED_STATUS_NAMES = {"已关闭", "Closed", "已解决", "Resolved", "关闭", "解决"}
REPORT_ATTACHMENT_RE = (
    "report",
    "test_result",
    "test-result",
    "testresult",
    "tradefed",
    "cts",
    "gts",
    "vts",
    "gms",
    "result",
    "测试报告",
    "测试结果",
)
REPORT_ATTACHMENT_EXTENSIONS = (".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".xml", ".html", ".htm", ".log", ".txt")


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19 if " " in fmt else 10], fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _time_key(value: Any, granularity: str = "day") -> str:
    """Generate date key at given granularity: day/week/month/year."""
    parsed = _parse_dt(value)
    if not parsed:
        return ""
    if granularity == "day":
        return parsed.date().isoformat()
    if granularity == "week":
        iso = parsed.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if granularity == "month":
        return f"{parsed.year}-{parsed.month:02d}"
    if granularity == "year":
        return str(parsed.year)
    return ""


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("@rock-chips.com", "").split())


def _name_keys(value: Any) -> set:
    normalized = _norm_name(value)
    if not normalized:
        return set()
    compact = normalized.replace(" ", "")
    keys = {normalized, compact}
    if "@" in normalized:
        keys.add(normalized.split("@", 1)[0])
    parts = [part for part in normalized.split() if part]
    if len(parts) > 1:
        keys.add(" ".join(reversed(parts)))
        keys.add(" ".join(sorted(parts)))
        keys.add("".join(reversed(parts)))
        keys.add("".join(sorted(parts)))
    return keys


def _identity_compacts(value: Any) -> set:
    normalized = _norm_name(value)
    if not normalized:
        return set()
    values = {normalized}
    for marker in ("（", "(", "【", "["):
        if marker in normalized:
            values.add(normalized.split(marker, 1)[0].strip())
    return {item.replace(" ", "") for item in values if item}


def _name_matches_keys(value: Any, owner_keys: set) -> bool:
    if not owner_keys:
        return True
    value_keys = _name_keys(value)
    if value_keys and value_keys.intersection(owner_keys):
        return True
    compacts = _identity_compacts(value)
    for key in owner_keys:
        compact_key = str(key or "").replace(" ", "")
        if len(compact_key) >= 2 and any(compact_key in compact for compact in compacts):
            return True
    return False


# ------------------------------------------------------------------
# User-map helpers (shared by executor and router)
# ------------------------------------------------------------------

_user_map_cache: tuple = (0.0, [])  # (mtime, parsed_list)


def load_redmine_user_map() -> List[Dict[str, Any]]:
    global _user_map_cache
    if not USER_MAP_PATH.exists():
        _user_map_cache = (0.0, [])
        return []
    try:
        mtime = USER_MAP_PATH.stat().st_mtime
        if _user_map_cache[0] == mtime:
            return _user_map_cache[1]
        payload = json.loads(USER_MAP_PATH.read_text(encoding="utf-8"))
        result = [item for item in payload.get("users") or [] if item.get("id")]
        _user_map_cache = (mtime, result)
        return result
    except Exception:
        return []


def load_user_map_payload() -> Dict[str, Any]:
    """Load the raw user-map JSON payload (for mutation + save round-trips)."""
    if not USER_MAP_PATH.exists():
        return {"users": []}
    try:
        payload = json.loads(USER_MAP_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("users", [])
            return payload
    except Exception:
        pass
    return {"users": []}


def save_user_map_payload(payload: Dict[str, Any]) -> None:
    """Write the raw user-map JSON payload to disk."""
    USER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_MAP_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def display_names_from_mapping(item: Dict[str, Any]) -> List[str]:
    values = [item.get("name") or "", *(item.get("aliases") or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def find_user_mapping(name: str) -> Optional[Dict[str, Any]]:
    keys = _name_keys(name)
    for item in load_redmine_user_map():
        for value in display_names_from_mapping(item):
            if keys.intersection(_name_keys(value)):
                return item
    return None


def _sorted_slice(bucket: Dict[str, int], key_name: str, limit: int) -> List[Dict[str, Any]]:
    """Return [{key_name: k, count: v}] sorted by key ascending, last *limit* items."""
    return [{key_name: k, "count": bucket[k]} for k in sorted(bucket.keys())[-limit:]]


def _looks_like_report_attachment(attachment: Dict[str, Any]) -> bool:
    filename = str(attachment.get("filename") or "").strip().lower()
    if not filename:
        return False
    analysis = attachment.get("analysis_json") or {}
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis or "{}")
        except Exception:
            analysis = {}
    if analysis.get("parsed") or analysis.get("failures") or analysis.get("summary"):
        return True
    has_report_word = any(token in filename for token in REPORT_ATTACHMENT_RE)
    has_report_ext = filename.endswith(REPORT_ATTACHMENT_EXTENSIONS)
    return has_report_word or (has_report_ext and any(token in filename for token in ("log", "result", "report", "cts", "gts", "vts", "gms")))


async def compute_user_overdue_stats(
    client: Any,
    db: "RedmineAgentDB",
    user: Dict[str, Any],
    stale_days: int = 3,
    issue_limit: int = 500,
    window_days: int = 0,
) -> Dict[str, Any]:
    """Compute workload + overdue stats for a single mapped user.

    Shared by the router's department-overdue endpoint, the workload endpoint,
    and the agent executor. Returns a dict with counts and overdue issue lists.

    Args:
        window_days: If > 0, only count stale issues updated within this window.
    """
    owner_names = display_names_from_mapping(user)
    counts = await client.count_issues_by_assignee(int(user["id"]))
    workload = db.get_workload_statistics(
        owner_names=owner_names,
        stale_days=stale_days,
        list_limit=min(issue_limit, 100),
        display_names=owner_names,
        window_days=window_days,
    )
    overdue = list((workload.get("lists") or {}).get("no_reply_3_days") or [])
    now = datetime.now()
    for item in overdue:
        last_dt = _parse_dt(item.get("last_external_reply_at"))
        item["unreplied_days"] = max(0, int((now - last_dt).total_seconds() // 86400)) if last_dt else 0
        item["stale"] = True
    overdue.sort(key=lambda item: (item.get("unreplied_days") or 0, item.get("last_external_reply_at") or ""), reverse=True)
    return {
        "id": user.get("id"),
        "name": user.get("name") or "",
        "aliases": user.get("aliases") or [],
        "total_owned": counts.get("total_owned", 0),
        "open_count": counts.get("open_count", 0),
        "closed_count": counts.get("closed_count", 0),
        "scanned_open_count": workload.get("open_count", 0),
        "waiting_my_reply": workload.get("waiting_my_reply", 0),
        "no_reply_3_days": workload.get("no_reply_3_days", 0),
        "max_unreplied_days": max([item.get("unreplied_days") or 0 for item in overdue] or [0]),
        "overdue_issues": overdue,
        "resolved_daily": workload.get("resolved_daily", []),
        "resolved_weekly": workload.get("resolved_weekly", []),
        "resolved_monthly": workload.get("resolved_monthly", []),
        "resolved_yearly": workload.get("resolved_yearly", []),
        "detail_source": "local_db",
    }


class RedmineAgentDB:
    def __init__(self, db_path: Path = DB_PATH, docs_dir: Path = DOCS_DIR):
        self.db_path = Path(db_path)
        self.docs_dir = Path(docs_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS redmine_agent_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    assigned_to TEXT,
                    window_start TEXT,
                    window_end TEXT,
                    max_issues INTEGER,
                    started_at TEXT,
                    finished_at TEXT,
                    issue_count INTEGER DEFAULT 0,
                    processed_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    error TEXT,
                    report_path TEXT,
                    summary_json TEXT DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_issues (
                    issue_id INTEGER PRIMARY KEY,
                    run_id TEXT,
                    subject TEXT,
                    status_name TEXT,
                    priority_name TEXT,
                    project_name TEXT,
                    tracker_name TEXT,
                    author_name TEXT,
                    assigned_to_name TEXT,
                    created_on TEXT,
                    updated_on TEXT,
                    description TEXT,
                    journals_json TEXT DEFAULT '[]',
                    attachments_json TEXT DEFAULT '[]',
                    failures_json TEXT DEFAULT '[]',
                    references_json TEXT DEFAULT '[]',
                    ai_json TEXT DEFAULT '{}',
                    summary TEXT,
                    reply_draft TEXT,
                    doc_path TEXT,
                    doc_content TEXT,
                    analysis_status TEXT DEFAULT 'pending',
                    error TEXT,
                    last_scanned_at TEXT,
                    error_info TEXT DEFAULT '',
                    error_analysis TEXT DEFAULT '',
                    solution TEXT DEFAULT '',
                    patch_direction TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    is_resolved INTEGER DEFAULT 0,
                    scan_count INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    attachment_id TEXT,
                    filename TEXT,
                    content_type TEXT,
                    filesize INTEGER DEFAULT 0,
                    local_path TEXT,
                    analysis_json TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_references (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    reference_issue_id INTEGER NOT NULL,
                    score REAL DEFAULT 0,
                    similarity_level TEXT DEFAULT '',
                    reason TEXT,
                    match_details_json TEXT DEFAULT '{}',
                    source TEXT DEFAULT '',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS redmine_agent_issue_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id INTEGER NOT NULL,
                    old_status TEXT DEFAULT '',
                    new_status TEXT DEFAULT '',
                    detected_at TEXT
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS redmine_agent_issue_fts USING fts5(
                        issue_id UNINDEXED,
                        subject,
                        description,
                        summary,
                        failures,
                        doc_content
                    )
                    """
                )
            except sqlite3.OperationalError:
                pass

            # --- safe migrations for columns added after initial schema ---
            self._migrate_columns(conn)
            self._migrate_indexes(conn)

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        """Add columns that may not exist in older databases (idempotent)."""
        new_columns = [
            ("redmine_agent_issues", "error_info", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "error_analysis", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "solution", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "patch_direction", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "category", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "is_resolved", "INTEGER DEFAULT 0"),
            ("redmine_agent_issues", "scan_count", "INTEGER DEFAULT 1"),
            ("redmine_agent_issues", "soc_platform", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "android_version", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "fixed_version", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "component", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "start_date", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "due_date", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "closed_on", "TEXT DEFAULT ''"),
            ("redmine_agent_issues", "done_ratio", "INTEGER DEFAULT 0"),
            ("redmine_agent_references", "similarity_level", "TEXT DEFAULT ''"),
            ("redmine_agent_references", "match_details_json", "TEXT DEFAULT '{}'"),
            ("redmine_agent_references", "source", "TEXT DEFAULT ''"),
        ]
        for table, column, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            except sqlite3.OperationalError:
                pass  # already exists

    @staticmethod
    def _migrate_indexes(conn: sqlite3.Connection) -> None:
        """Create query-path indexes for Redmine dashboards (idempotent)."""
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_assignee_status
                ON redmine_agent_issues(assigned_to_name, is_resolved, status_name);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_updated
                ON redmine_agent_issues(updated_on, created_on, issue_id);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_resolved_closed
                ON redmine_agent_issues(is_resolved, closed_on, updated_on);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_issues_run
                ON redmine_agent_issues(run_id, priority_name, issue_id);
            CREATE INDEX IF NOT EXISTS idx_redmine_agent_runs_started
                ON redmine_agent_runs(started_at, finished_at);
            """
        )

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, run_id: str, mode: str, window_start: str, window_end: str, max_issues: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO redmine_agent_runs
                (run_id, status, mode, window_start, window_end, max_issues, started_at)
                VALUES (?, 'running', ?, ?, ?, ?, ?)
                """,
                (run_id, mode, window_start, window_end, max_issues, _now()),
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key}=?" for key in fields)
        values = [self._json_value(value) if key.endswith("_json") else value for key, value in fields.items()]
        with self.connect() as conn:
            conn.execute(f"UPDATE redmine_agent_runs SET {columns} WHERE run_id=?", [*values, run_id])

    def mark_stale_running_runs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE redmine_agent_runs
                SET status='interrupted',
                    finished_at=?,
                    error='Process restarted before this scan finished'
                WHERE status='running'
                """,
                (_now(),),
            )
            return cursor.rowcount

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_runs ORDER BY started_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM redmine_agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode_row(row) if row else None

    def get_latest_run(self) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_agent_runs WHERE status='done' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return self._decode_row(row) if row else None

    def list_run_issues(self, run_id: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_issues WHERE run_id=? ORDER BY priority_name, issue_id DESC",
                (run_id,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def upsert_issue(self, issue: Dict[str, Any]) -> None:
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

    def get_issue(self, issue_id: int) -> Optional[Dict[str, Any]]:
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
    ) -> List[Dict[str, Any]]:
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

    def get_issue_statistics(self) -> Dict[str, Any]:
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
        owner_names: Optional[List[str]] = None,
        stale_days: int = 3,
        list_limit: int = 30,
        display_names: Optional[List[str]] = None,
        window_days: int = 0,
    ) -> Dict[str, Any]:
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
        open_issues: List[Dict[str, Any]] = []
        waiting_my_reply: List[Dict[str, Any]] = []
        stale_my_reply: List[Dict[str, Any]] = []
        missing_test_report: List[Dict[str, Any]] = []
        resolved_counts: Dict[str, Dict[str, int]] = {
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

            if self._is_missing_test_report(issue):
                missing_test_report.append(self._issue_summary(issue))

        waiting_my_reply.sort(key=lambda item: item.get("last_external_reply_at") or item.get("updated_on") or "", reverse=True)
        stale_my_reply.sort(key=lambda item: item.get("last_external_reply_at") or item.get("updated_on") or "")
        missing_test_report.sort(key=lambda item: item.get("updated_on") or "", reverse=True)

        return {
            "total_owned": len(owned_issues),
            "open_count": len(open_issues),
            "closed_count": len(owned_issues) - len(open_issues),
            "waiting_my_reply": len(waiting_my_reply),
            "no_reply_3_days": len(stale_my_reply),
            "missing_test_report": len(missing_test_report),
            "resolved_daily": _sorted_slice(resolved_counts["day"], "date", 90),
            "resolved_weekly": _sorted_slice(resolved_counts["week"], "week", 52),
            "resolved_monthly": _sorted_slice(resolved_counts["month"], "month", 24),
            "resolved_yearly": _sorted_slice(resolved_counts["year"], "year", 10),
            "lists": {
                "waiting_my_reply": waiting_my_reply[:list_limit],
                "no_reply_3_days": stale_my_reply[:list_limit],
                "missing_test_report": missing_test_report[:list_limit],
                "open_issues": [self._issue_summary(item) for item in open_issues[:list_limit]],
            },
            "meta": {
                "owner_names": [n for n in (display_names or owner_names or []) if n],
                "stale_days": stale_days,
                "list_limit": list_limit,
                "generated_at": _now(),
            },
        }

    def list_assignee_names(self) -> List[str]:
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

    def resolve_assignee_names(self, query_names: List[str]) -> Dict[str, List[str]]:
        assignees = self.list_assignee_names()
        assignee_keys = {
            name: _name_keys(name)
            for name in assignees
        }
        resolved: Dict[str, List[str]] = {}
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
    def _is_issue_resolved(issue: Dict[str, Any]) -> bool:
        return bool(issue.get("is_resolved")) or str(issue.get("status_name") or "") in RESOLVED_STATUS_NAMES

    @staticmethod
    def _is_assigned_to_owner(issue: Dict[str, Any], owner_keys: set) -> bool:
        if not owner_keys:
            return True
        return _name_matches_keys(issue.get("assigned_to_name"), owner_keys)

    @staticmethod
    def _last_note_journal(issue: Dict[str, Any]) -> Dict[str, Any]:
        journals = issue.get("journals_json") or []
        note_journals = [j for j in journals if str(j.get("notes") or "").strip()]
        if not note_journals:
            return {}
        return max(note_journals, key=lambda item: _parse_dt(item.get("created_on")) or datetime.min)

    @classmethod
    def _reply_wait_info(cls, issue: Dict[str, Any], owner_keys: set) -> Dict[str, Any]:
        last_note = cls._last_note_journal(issue)
        if not last_note:
            return {"waiting": False, "reason": "no_journal_notes"}
        if _name_matches_keys(last_note.get("user"), owner_keys):
            return {"waiting": False, "reason": "last_reply_is_owner"}
        return {
            "waiting": True,
            "last_external_reply_at": last_note.get("created_on") or issue.get("updated_on") or "",
            "last_external_reply_by": last_note.get("user") or "",
            "last_external_reply": str(last_note.get("notes") or "")[:260],
        }

    @staticmethod
    def _is_missing_test_report(issue: Dict[str, Any]) -> bool:
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
    def _resolved_at_from_journals(issue: Dict[str, Any]) -> str:
        journals = issue.get("journals_json") or []
        for journal in reversed(journals):
            for detail in journal.get("details") or []:
                if str(detail.get("name") or "").lower() == "status":
                    new_value = str(detail.get("new_value") or "")
                    if new_value in RESOLVED_STATUS_NAMES:
                        return journal.get("created_on") or ""
        return ""

    @staticmethod
    def _issue_summary(issue: Dict[str, Any], reply_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
            "attachment_count": len(issue.get("attachments_json") or []),
        }

    def search_issues(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
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
            except Exception:
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

    def get_unresolved_issues(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_agent_issues WHERE is_resolved = 0 ORDER BY updated_on DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    def search_similar(self, query: str, exclude_issue_id: int, limit: int = 5) -> List[Dict[str, Any]]:
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
            except Exception:
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

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def insert_attachment(self, item: Dict[str, Any]) -> None:
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

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    def replace_references(self, issue_id: int, references: List[Dict[str, Any]]) -> None:
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

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def write_issue_doc(self, issue_id: int, content: str) -> str:
        path = self.docs_dir / f"redmine-{issue_id}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def write_run_report(self, run_id: str, content: str) -> str:
        path = self.docs_dir / f"run-{run_id}.md"
        path.write_text(content, encoding="utf-8")
        return str(path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _replace_fts(self, conn: sqlite3.Connection, payload: Dict[str, Any]) -> None:
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
    def _build_issue_where(status: str = "", priority: str = "", category: str = "", search: str = "") -> tuple:
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
        if search:
            like = f"%{search[:80]}%"
            clauses.append("(subject LIKE ? OR description LIKE ? OR error_info LIKE ? OR summary LIKE ?)")
            params.extend([like, like, like, like])
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
    def _decode_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("summary_json", "journals_json", "attachments_json", "failures_json", "references_json", "ai_json", "match_details_json"):
            if key in item:
                try:
                    item[key] = json.loads(item.get(key) or ("[]" if key not in ("ai_json", "summary_json", "match_details_json") else "{}"))
                except Exception:
                    item[key] = [] if key not in ("ai_json", "summary_json", "match_details_json") else {}
        return item
