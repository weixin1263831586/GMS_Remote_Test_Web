"""Daily Brief 数据模型（纯数据契约，无 I/O）。

Schema 常量与 dataclass 供 repository / service / analyzer / UI 共享；
对应 docs/plans/redmine-ai-daily-brief.md 的 Phase 1/2/14。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BRIEF_MODES = ("nightly", "delta", "manual")
RUN_STATUSES = ("pending", "snapshotting", "analyzing", "completed", "partial", "failed")
ISSUE_STATUSES = ("pending", "running", "completed", "failed", "stale")

# 优先级由规则生成 base score，AI 只做有限调整；排序必须 deterministic。
PRIORITY_SCORE_BASE = {
    "no_reply_3_days": 50,
    "waiting_my_reply": 20,
}
PRIORITY_SCORE_URGENT = 50
PRIORITY_SCORE_HIGH = 30
PRIORITY_SCORE_PER_STALE_DAY = 5
PRIORITY_SCORE_MISSING_LOG = -5
PRIORITY_SCORE_BLOCKER = 10

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
        score += max(0.0, float(entry.get("unreplied_days") or 0.0)) * PRIORITY_SCORE_PER_STALE_DAY
    except (TypeError, ValueError):
        pass
    if not (entry.get("attachment_count") or 0):
        score += PRIORITY_SCORE_MISSING_LOG
    return int(score)


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
    issue_count: int = 0
    waiting_my_reply_count: int = 0
    no_reply_3_days_count: int = 0
    urgent_count: int = 0
    analysis_backend: str = ""
    model_name: str = ""
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
            "issue_count": self.issue_count,
            "waiting_my_reply_count": self.waiting_my_reply_count,
            "no_reply_3_days_count": self.no_reply_3_days_count,
            "urgent_count": self.urgent_count,
            "analysis_backend": self.analysis_backend,
            "model_name": self.model_name,
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


# AI 单 issue 输出的必填字段（analyzer 校验 + UI 渲染依赖）。
ISSUE_RESULT_REQUIRED_FIELDS = (
    "problem_summary",
    "customer_request",
    "recommended_actions",
    "suggested_solution",
    "evidence",
    "confidence",
)
ROOT_CAUSE_TYPES = ("confirmed", "likely", "possible", "unknown")
RISK_LEVELS = ("high", "medium", "low")


def validate_issue_result(result: dict[str, Any]) -> list[str]:
    """校验 AI 输出 schema，返回错误列表（空列表 = 合法）。

    缺字段/类型不符一律判 invalid_ai_output，不做 regex 猜测修复。
    """
    errors: list[str] = []
    if not isinstance(result, dict):
        return ["result is not an object"]
    for key in ISSUE_RESULT_REQUIRED_FIELDS:
        if key not in result or result[key] in (None, ""):
            errors.append(f"missing field: {key}")
    root_cause_type = str(result.get("root_cause_type") or "unknown")
    if root_cause_type not in ROOT_CAUSE_TYPES:
        errors.append(f"invalid root_cause_type: {root_cause_type}")
    risk = str(result.get("risk") or "")
    if risk and risk not in RISK_LEVELS:
        errors.append(f"invalid risk: {risk}")
    try:
        confidence = float(result.get("confidence"))
        if not 0.0 <= confidence <= 1.0:
            errors.append(f"confidence out of range: {confidence}")
    except (TypeError, ValueError):
        errors.append(f"invalid confidence: {result.get('confidence')!r}")
    if not isinstance(result.get("evidence") or [], list):
        errors.append("evidence must be a list")
    if not isinstance(result.get("recommended_actions") or [], list):
        errors.append("recommended_actions must be a list")
    if confidence_below_review_threshold(result):
        result["needs_human_review"] = True
    return errors


CONFIDENCE_HUMAN_REVIEW_THRESHOLD = 0.6


def confidence_below_review_threshold(result: dict[str, Any]) -> bool:
    try:
        return float(result.get("confidence")) < CONFIDENCE_HUMAN_REVIEW_THRESHOLD
    except (TypeError, ValueError):
        return True
