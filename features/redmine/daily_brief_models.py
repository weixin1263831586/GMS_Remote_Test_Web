"""Daily Brief 数据模型（纯数据契约，无 I/O）。

Schema 常量与 dataclass 供 repository / service / analyzer / UI 共享；
当前分析契约见 docs/architecture/adr/0013-daily-brief-batch-matches-single-issue-diagnosis.md
（ADR 0008 已被 0013 supersede，仅作历史背景）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .daily_brief_result import (
    CONFIDENCE_HUMAN_REVIEW_THRESHOLD as CONFIDENCE_HUMAN_REVIEW_THRESHOLD,
)
from .daily_brief_result import (
    ISSUE_RESULT_REQUIRED_FIELDS as ISSUE_RESULT_REQUIRED_FIELDS,
)
from .daily_brief_result import (
    ISSUE_RESULT_SCHEMA as ISSUE_RESULT_SCHEMA,
)
from .daily_brief_result import (
    MAX_SIMILAR_ISSUES as MAX_SIMILAR_ISSUES,
)
from .daily_brief_result import (
    RISK_LEVELS as RISK_LEVELS,
)
from .daily_brief_result import (
    ROOT_CAUSE_TYPES as ROOT_CAUSE_TYPES,
)
from .daily_brief_result import (
    SIMILARITY_LEVELS as SIMILARITY_LEVELS,
)
from .daily_brief_result import (
    confidence_below_review_threshold as confidence_below_review_threshold,
)
from .daily_brief_result import (
    validate_issue_result as validate_issue_result,
)


BRIEF_MODES = ("nightly", "delta", "manual")
RUN_STATUSES = (
    "pending", "snapshotting", "analyzing", "completed", "partial", "failed", "cancelled",
)
ISSUE_STATUSES = ("pending", "running", "completed", "failed", "cancelled", "stale")
# Data freshness and execution status are independent dimensions:
# execution_status（completed/partial/failed/cancelled）描述 AI 分析本身；
# data_quality 描述输入数据可信程度——"晨报完成"不等于"数据是新的"。
DATA_QUALITY_STATUSES = ("fresh", "stale", "sync_failed", "unknown")
# 快照年龄超过该值（秒）即使 sync 成功也标记 stale。
DATA_FRESH_LIMIT_SECONDS = 24 * 3600


def derive_data_quality(
    source_sync_status: str, snapshot_at: str, now: datetime | None = None
) -> str:
    """从同步状态 + 快照时间推导数据新鲜度。

    - sync_failed → sync_failed（本地镜像兜底，数据可能落后）；
    - skipped / 未知 → unknown；
    - synced → 快照生成时间距今 <= 24h 为 fresh，否则 stale。
    """
    status = str(source_sync_status or "").strip().lower()
    if status == "sync_failed":
        return "sync_failed"
    if status != "synced":
        return "unknown"
    if not str(snapshot_at or "").strip():
        return "unknown"
    try:
        # fromisoformat may yield an aware datetime; mixing it with the naive
        # local ``now`` raises TypeError, so fall back to "unknown" like an
        # unparseable stamp instead of crashing the caller.
        generated = datetime.fromisoformat(str(snapshot_at))
        now = now or datetime.now()
        age = (now - generated).total_seconds()
    except (TypeError, ValueError):
        return "unknown"
    return "fresh" if 0 <= age <= DATA_FRESH_LIMIT_SECONDS else "stale"

# 优先级由规则生成 base score，AI 只做有限调整；排序必须 deterministic。
PRIORITY_SCORE_BASE = {
    "no_reply_3_days": 10,
    "waiting_my_reply": 30,
}
PRIORITY_SCORE_URGENT = 50
PRIORITY_SCORE_HIGH = 30

PRIORITY_LABELS = ("P1", "P2", "P3")


def priority_from_score(score: int) -> str:
    """规则分到 P1/P2/P3 的确定性映射（先于 AI 分析使用）。"""
    if score >= 70:
        return "P1"
    if score >= 40:
        return "P2"
    return "P3"


def base_priority_score(entry: dict[str, Any]) -> int:
    """按规则计算 issue 的优先级 base score。

    输入为 snapshot entry（含 buckets/unreplied_days/priority_name 等），
    不得引入任何 AI 判断。
    """
    score = 0
    buckets = {str(b) for b in (entry.get("buckets") or [])}
    if "no_reply_3_days" in buckets:
        score += PRIORITY_SCORE_BASE["no_reply_3_days"]
    if "waiting_my_reply" in buckets:
        score += PRIORITY_SCORE_BASE["waiting_my_reply"]
    priority = str(entry.get("priority_name") or "").strip().lower()
    if priority == "urgent":
        score += PRIORITY_SCORE_URGENT
    elif priority == "high":
        score += PRIORITY_SCORE_HIGH
    try:
        days = float(entry.get("unreplied_days") or 0.0)
        if days >= 7:
            score += 5
    except (TypeError, ValueError):
        pass
    return min(100, max(0, int(score)))


@dataclass
class DailyBriefRun:
    """一次晨报运行（nightly/delta/manual）。

    snapshot_json / snapshot_hash 在 snapshotting 阶段冻结；分析阶段一律
    以冻结快照为准，避免 Redmine 数据变化改变分析基准。
    """

    owner_id: str
    brief_date: str
    mode: str
    run_id: str
    status: str = "pending"
    started_at: str = ""
    finished_at: str = ""
    snapshot_at: str = ""
    snapshot_hash: str = ""
    source_sync_status: str = ""
    # 数据新鲜度（独立于 execution status）：fresh/stale/sync_failed/unknown。
    data_quality: str = ""
    # 最近一次成功同步 Redmine 的时刻（快照生成时间即同步时刻；失败时留空，
    # 由 snapshot_at 表达本地镜像的时间点）。
    last_sync_at: str = ""
    issue_count: int = 0
    waiting_my_reply_count: int = 0
    no_reply_3_days_count: int = 0
    urgent_count: int = 0
    analysis_backend: str = ""
    model_name: str = ""
    # Standalone analysis may bind one current-host ADB device for read-only
    # runtime evidence.  It is persisted with the run for reproducibility.
    device_serial: str = ""
    # Optional observation supplied by the operator for one standalone issue
    # analysis. It is kept with the run so queued work has the same context.
    analysis_hint: str = ""
    prompt_version: str = ""
    report_json: dict[str, Any] = field(default_factory=dict)
    report_markdown: str = ""
    error: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "brief_date": self.brief_date,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "snapshot_at": self.snapshot_at,
            "snapshot_hash": self.snapshot_hash,
            "source_sync_status": self.source_sync_status,
            "data_quality": self.data_quality,
            "last_sync_at": self.last_sync_at,
            "issue_count": self.issue_count,
            "waiting_my_reply_count": self.waiting_my_reply_count,
            "no_reply_3_days_count": self.no_reply_3_days_count,
            "urgent_count": self.urgent_count,
            "analysis_backend": self.analysis_backend,
            "model_name": self.model_name,
            "device_serial": self.device_serial,
            "analysis_hint": self.analysis_hint,
            "prompt_version": self.prompt_version,
            "report_json": self.report_json,
            "report_markdown": self.report_markdown,
            "error": self.error,
        }


@dataclass
class DailyBriefIssue:
    """快照中单个 issue 的分析状态与结果。"""

    run_id: str
    issue_id: int
    buckets: list[str]
    priority: str = "P3"
    priority_score: int = 0
    fingerprint: str = ""
    subject: str = ""
    status: str = "pending"
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    attempt_count: int = 0
    error: str = ""
    error_type: str = ""
    raw_response: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "issue_id": self.issue_id,
            "buckets": self.buckets,
            "priority": self.priority,
            "priority_score": self.priority_score,
            "fingerprint": self.fingerprint,
            "subject": self.subject,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "attempt_count": self.attempt_count,
            "error": self.error,
            "error_type": self.error_type,
            "raw_response": self.raw_response,
            "result": self.result,
        }
