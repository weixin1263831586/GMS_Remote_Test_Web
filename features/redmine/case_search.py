"""Similarity search over the Redmine knowledge base case facts.

Scoring (Redmine.txt §5.5):

    FTS命中            0–30  (scaled by bm25 rank proximity)
    平台一致           +15
    Android 版本一致    +10
    认证类型一致        +10
    模块一致           +15
    错误签名一致        +25
    Closed/Confirmed    +5
    有 solution         +5

The search decouples from the issue scan store — it operates entirely on the
``case_facts`` table in the knowledge base.
"""

from __future__ import annotations

from typing import Any

from .case_extractor import RedmineCaseExtractor
from .knowledge_repository import RedmineKnowledgeDB

# Resolved-ish statuses that boost confidence in a candidate.
_CONFIRMED_STATUSES = {"closed", "confirmed", "已关闭", "已解决", "resolved"}


class RedmineCaseSearch:
    """Search similar case facts in the knowledge base."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB):
        self.db = knowledge_db

    def search_similar(self, issue_or_text: dict[str, Any] | str, *, limit: int = 10, exclude_issue_id: int = 0) -> list[dict[str, Any]]:
        probe = self._probe_from(issue_or_text)
        query_text = probe["query_text"]
        if not query_text.strip():
            return []

        # Pull a generous FTS candidate pool, then re-rank with the scoring rules.
        pool = self.db.search_case_facts(query_text, limit=max(limit * 5, 30))
        results: list[dict[str, Any]] = []
        for fact in pool:
            issue_id = int(fact.get("issue_id") or 0)
            if exclude_issue_id and issue_id == exclude_issue_id:
                continue
            score, breakdown = self._score(probe, fact)
            results.append({
                "issue_id": issue_id,
                "subject": fact.get("subject") or "",
                "status_name": fact.get("status_name") or "",
                "module": fact.get("module") or "",
                "error_signature": fact.get("error_signature") or "",
                "chip_platform": fact.get("chip_platform") or "",
                "android_version": fact.get("android_version") or "",
                "certification_type": fact.get("certification_type") or "",
                "problem_summary": fact.get("problem_summary") or "",
                "root_cause": fact.get("root_cause") or "",
                "solution": fact.get("solution") or "",
                "reply_template": fact.get("reply_template") or "",
                "doc_excerpt": fact.get("doc_excerpt") or "",
                "score": round(score, 1),
                "similarity_level": self._level(score),
                "match_details": breakdown,
            })
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[: max(1, min(limit, 50))]

    # ------------------------------------------------------------------
    # Probe construction
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_from(issue_or_text: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(issue_or_text, str):
            signature = RedmineCaseExtractor._detect_error_signature(issue_or_text)
            return {
                "query_text": issue_or_text,
                "chip_platform": RedmineCaseExtractor._detect_chip_platform(issue_or_text, "", ""),
                "android_version": RedmineCaseExtractor._detect_android_version(issue_or_text, "", ""),
                "certification_type": RedmineCaseExtractor._detect_certification_type(issue_or_text),
                "module": RedmineCaseExtractor._detect_module(issue_or_text, []),
                "error_signature": signature,
            }
        # It's an issue row — extract its own structured fields as the probe.
        fact = RedmineCaseExtractor.extract(issue_or_text)
        structured_text = " ".join(filter(None, [
            issue_or_text.get("subject"),
            issue_or_text.get("description"),
            issue_or_text.get("summary"),
            issue_or_text.get("problem_summary"),
            issue_or_text.get("error_info"),
            issue_or_text.get("error_analysis"),
            issue_or_text.get("root_cause"),
            issue_or_text.get("solution"),
            issue_or_text.get("doc_excerpt"),
            issue_or_text.get("error_signature"),
            issue_or_text.get("module"),
            fact.get("error_signature"),
            fact.get("module"),
        ]))
        return {
            "query_text": structured_text,
            "chip_platform": fact["chip_platform"],
            "android_version": fact["android_version"],
            "certification_type": fact["certification_type"],
            "module": fact["module"],
            "error_signature": fact["error_signature"],
        }

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _score(probe: dict[str, Any], fact: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        breakdown: dict[str, float] = {}
        score = 0.0

        # FTS hit base (these candidates all matched FTS; give a flat base).
        breakdown["fts_hit"] = 20.0
        score += 20.0

        if probe["chip_platform"] and probe["chip_platform"] == fact.get("chip_platform"):
            breakdown["platform"] = 15.0
            score += 15.0
        if probe["android_version"] and probe["android_version"] == fact.get("android_version"):
            breakdown["android"] = 10.0
            score += 10.0
        if probe["certification_type"] and probe["certification_type"] == fact.get("certification_type"):
            breakdown["cert"] = 10.0
            score += 10.0
        if probe["module"] and probe["module"] == fact.get("module"):
            breakdown["module"] = 15.0
            score += 15.0
        if probe["error_signature"] and probe["error_signature"] == fact.get("error_signature"):
            breakdown["signature"] = 25.0
            score += 25.0
        status = str(fact.get("status_name") or "").lower()
        if any(s in status for s in _CONFIRMED_STATUSES):
            breakdown["confirmed"] = 5.0
            score += 5.0
        if fact.get("solution"):
            breakdown["solution"] = 5.0
            score += 5.0

        return score, breakdown

    @staticmethod
    def _level(score: float) -> str:
        if score >= 70:
            return "high"
        if score >= 40:
            return "medium"
        return "low"
