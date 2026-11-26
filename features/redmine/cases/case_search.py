"""基于全文索引和案例属性评分搜索相似 Redmine 工单。"""

from __future__ import annotations

import logging
from typing import Any

from features.retrieval import fuse_rankings

from ..knowledge.repository import RedmineKnowledgeDB
from ..utils import detect_android_version, detect_soc_platform, similarity_level
from .case_extractor import RedmineCaseExtractor
from .semantic import CaseSemantic, rerank_case_candidates


# Resolved-ish statuses that boost confidence in a candidate.
_CONFIRMED_STATUSES = {"closed", "confirmed", "已关闭", "已解决", "resolved"}
logger = logging.getLogger(__name__)


class RedmineCaseSearch:
    """Search similar case facts in the knowledge base."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB):
        self.db = knowledge_db

    def search_similar(self, issue_or_text: dict[str, Any] | str, *, limit: int = 10, exclude_issue_id: int = 0) -> list[dict[str, Any]]:
        probe = self._probe_from(issue_or_text)
        query_text = probe["query_text"]
        if not query_text.strip():
            return []

        # 扩大全文检索候选池，再按评分规则重排。
        budget = min(100, max(limit * 5, 30))
        pool = self.db.search_case_facts(query_text, limit=budget)
        lexical_ids = [int(row["issue_id"]) for row in pool if row.get("issue_id")]
        dense = []
        try:
            dense = CaseSemantic(self.db).search(query_text, limit=budget)
        except Exception as exc:
            logger.warning("Case semantic retrieval unavailable: %s", type(exc).__name__)
        dense_scores = {int(row["issue_id"]): score for row, score in dense}
        candidates = {int(row["issue_id"]): row for row in pool if row.get("issue_id")}
        for row, _ in dense:
            candidates.setdefault(int(row["issue_id"]), row)
        results: list[dict[str, Any]] = []
        pool = list(candidates.values())
        for fact in pool:
            issue_id = int(fact.get("issue_id") or 0)
            if exclude_issue_id and issue_id == exclude_issue_id:
                continue
            score, breakdown = self._score(probe, fact, lexical_hit=issue_id in lexical_ids)
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
                "similarity_level": similarity_level(score),
                "match_details": breakdown,
                "retrieval_channels": (["lexical"] if issue_id in lexical_ids else [])
                                      + (["semantic"] if issue_id in dense_scores else []),
                "semantic_similarity": round(dense_scores[issue_id], 4) if issue_id in dense_scores else None,
            })
        results.sort(key=lambda item: item["score"], reverse=True)
        if dense:
            eligible = {row["issue_id"]: row for row in results}
            exact = [row["issue_id"] for row in results if row["match_details"].get("signature")]
            rankings = [lexical_ids, list(dense_scores), list(eligible)]
            try:
                reranked = rerank_case_candidates(query_text, [candidates[key] for key in eligible])
                if reranked:
                    rankings.append(reranked)
            except Exception as exc:
                logger.warning("Case semantic reranker unavailable: %s", type(exc).__name__)
            keys = fuse_rankings([[key for key in rank if key in eligible] for rank in rankings],
                                 limit=max(1, min(limit, 50)), exact_keys=exact)
            return [eligible[key] for key in keys]
        return results[: max(1, min(limit, 50))]

    @staticmethod
    def _probe_from(issue_or_text: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(issue_or_text, str):
            signature = RedmineCaseExtractor._detect_error_signature(issue_or_text)
            return {
                "query_text": issue_or_text,
                "chip_platform": detect_soc_platform(issue_or_text, "", ""),
                "android_version": detect_android_version(issue_or_text, "", "", major_only=True),
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

    @staticmethod
    def _score(probe: dict[str, Any], fact: dict[str, Any], *, lexical_hit=True) -> tuple[float, dict[str, Any]]:
        breakdown: dict[str, float] = {}
        score = 0.0

        if lexical_hit:
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
