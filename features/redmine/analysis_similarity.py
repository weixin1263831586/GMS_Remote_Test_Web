"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any

from features.redmine.client import RedmineClient
from features.redmine.utils import to_iso8601


logger = logging.getLogger(__name__)

# Enhanced error patterns for structured extraction
_ERROR_LINE_PATTERNS = [
    # Stack traces
    r"at\s+[\w.$]+\([^)]*\.\w+:\d+\)",
    r"Caused by:\s*[\w.]+(?:Exception|Error)",
    r"java\.\w+\.\w+(?:Exception|Error)",
    # JUnit / Android test
    r"junit\.framework\.(?:AssertionFailedError|ComparisonFailure)",
    r"android\.os\.ServiceSpecificException",
    r"android\.hardware\.\w+",
    r"com\.android\.\w+\.(?:Exception|Error)",
    # GMS / certification
    r"not certified|attestation|integrity|KeyMint|RKPD|STRONGBOX",
    r"Cannot add more profiles|config_user_types|config_multiuserMaximumUsers",
    # General
    r"\bFAIL(?:URE)?:\b",
    r"\bASSUMPTION_FAILURE:\b",
    r"\bFATAL\b",
    r"\bdenied\b",
    r"\bError:\s",
]
_ERROR_LINE_RE = re.compile("|".join(_ERROR_LINE_PATTERNS), re.IGNORECASE)

AI_MODEL_TIMEOUT = 120          # seconds for AI model HTTP request
AI_MODEL_MAX_TOKENS = 2400      # max tokens for AI model response
MAX_FAILURE_LINES = 30          # max error lines to extract
MAX_ERROR_BLOCKS = 5            # max grouped error blocks
SIMILARITY_THRESHOLD_HIGH = 70  # score >= this → "high" similarity
SIMILARITY_THRESHOLD_MEDIUM = 40  # score >= this → "medium" similarity
MAX_REFERENCES = 5              # max similar references to return
TOP_CANDIDATES_FOR_AI = 8       # top candidates sent to AI semantic scoring


SYNC_PRESERVE_FIELDS = {
    "failures_json",
    "references_json",
    "ai_json",
    "summary",
    "reply_draft",
    "doc_path",
    "doc_content",
    "error",
    "error_info",
    "error_analysis",
    "solution",
    "patch_direction",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(value: Any) -> str:
    # Delegate to the shared normalizer so all mixins format Redmine
    # timestamps identically (handles datetime + space-separated strings).
    return to_iso8601(value)


def _obj_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", "") or value)


def _obj_email(value: Any) -> str:
    if value is None:
        return ""
    return str(
        getattr(value, "mail", "")
        or getattr(value, "email", "")
        or getattr(value, "login", "")
        or ""
    )


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"



class SimilarityAnalysisMixin:
    async def _find_similar_references(self, client: RedmineClient, issue_payload: dict[str, Any], failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
        terms = self._similar_terms(issue_payload, failures)
        candidates: dict[int, dict[str, Any]] = {}

        # 1. Local DB FTS search
        for term in terms[:8]:
            for row in self.db.search_similar(term, int(issue_payload["issue_id"]), limit=8):
                ref_id = int(row["issue_id"])
                score, reason, details = self._score_reference(issue_payload, failures, row)
                if score <= 0:
                    continue
                current = candidates.get(ref_id)
                if not current or score > current["score"]:
                    candidates[ref_id] = {
                        "issue_id": ref_id,
                        "subject": row.get("subject") or "",
                        "score": round(score, 2),
                        "reason": reason,
                        "match_details": details,
                        "source": "local_db",
                    }

        # 2. Redmine subject search
        for term in self._redmine_search_terms(issue_payload, failures):
            try:
                rows = await client.search_issues_by_subject(term, project_id="fae", limit=10, status_id="*")
            except Exception as exc:
                logger.warning("[RedmineAgent] Redmine subject search failed for %s: %s", term, exc)
                continue
            for row in rows:
                ref_id = int(row.get("issue_id") or 0)
                if not ref_id or ref_id == int(issue_payload["issue_id"]):
                    continue
                search_row = {**row, "source": "redmine_subject", "matched_term": term}
                score, reason, details = self._score_reference(issue_payload, failures, search_row)
                score += 25  # bonus for Redmine direct match
                reason = "；".join(dict.fromkeys([reason, f"Redmine主题搜索命中 {term}"]))[:500]
                current = candidates.get(ref_id)
                if not current or score > current["score"]:
                    candidates[ref_id] = {
                        "issue_id": ref_id,
                        "subject": row.get("subject") or "",
                        "score": round(score, 2),
                        "reason": reason,
                        "match_details": details,
                        "source": "redmine_subject",
                        "updated_on": row.get("updated_on") or "",
                    }

        # 3. 对高分候选执行可选的 AI 语义匹配。
        top_candidates = sorted(candidates.values(), key=lambda item: item["score"], reverse=True)[:TOP_CANDIDATES_FOR_AI]
        if top_candidates and failures:
            semantic_scores = await self._ai_semantic_similarity(issue_payload, top_candidates)
            for ref in top_candidates:
                ref_id = ref["issue_id"]
                ai_score = semantic_scores.get(ref_id, 0)
                if ai_score > 0:
                    old_score = candidates[ref_id]["score"]
                    # Blend: weighted average of rule-based (80%) + AI semantic (20%)
                    blended = old_score * 0.8 + ai_score * 20  # ai_score 0-1 → 0-20
                    candidates[ref_id]["score"] = round(blended, 2)
                    candidates[ref_id]["match_details"]["ai_semantic_score"] = round(ai_score, 3)
                    candidates[ref_id]["reason"] = (candidates[ref_id].get("reason") or "") + f" | AI语义相似 {ai_score:.2f}"

        # 4. Assign similarity levels
        for ref in candidates.values():
            s = ref["score"]
            if s >= SIMILARITY_THRESHOLD_HIGH:
                ref["similarity_level"] = "high"
            elif s >= SIMILARITY_THRESHOLD_MEDIUM:
                ref["similarity_level"] = "medium"
            else:
                ref["similarity_level"] = "low"
            # Filter out low similarity
        return sorted(
            [ref for ref in candidates.values() if ref["similarity_level"] != "low"],
            key=lambda item: item["score"],
            reverse=True,
        )[:MAX_REFERENCES]

    def _similar_terms(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]]) -> list[str]:
        terms = [issue_payload.get("subject", "")]
        for failure in failures[:5]:
            for key in ("module", "name"):
                if failure.get(key):
                    terms.append(str(failure[key]))
            reason = str(failure.get("reason") or "")
            tokens = re.findall(r"[A-Za-z0-9_.#$-]{8,}", reason)
            if tokens:
                terms.append(" ".join(tokens[:5]))
        desc_tokens = re.findall(r"[A-Za-z0-9_.#$-]{8,}", issue_payload.get("description") or "")
        if desc_tokens:
            terms.append(" ".join(desc_tokens[:8]))
        return [term for term in terms if term.strip()]

    def _redmine_search_terms(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]]) -> list[str]:
        terms: list[str] = []
        source_text = " ".join([
            issue_payload.get("subject") or "",
            issue_payload.get("description") or "",
            " ".join(str(failure.get(key) or "") for failure in failures[:5] for key in ("module", "name", "reason")),
        ])
        patterns = [
            r"\bCts[A-Za-z0-9_]+TestCases\b",
            r"\b[A-Za-z0-9_]*HostTest\b",
            r"\b[A-Za-z0-9_]*Test\b",
            r"\b[A-Za-z0-9_]*ManagedProfile[A-Za-z0-9_]*\b",
            r"\bconfig_[A-Za-z0-9_]+\b",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, source_text):
                if len(match) >= 8:
                    terms.append(match)
        for failure in failures[:5]:
            name = str(failure.get("name") or "")
            if "." in name:
                parts = [part for part in re.split(r"[.#]", name) if len(part) >= 8]
                terms.extend(parts[-3:])
            module = str(failure.get("module") or "")
            if module:
                terms.append(module)
        deduped = []
        seen = set()
        for term in terms:
            term = term.strip("._- ")
            if not term or term.lower() in seen:
                continue
            if term.islower() and "_" not in term:
                continue
            seen.add(term.lower())
            deduped.append(term)
        return deduped[:10]

    def _score_reference(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], row: dict[str, Any]) -> tuple:
        """Multi-dimension similarity scoring (total 100).

        Returns (score, reason, match_details).
        """
        score = 0.0
        reasons = []
        details: dict[str, Any] = {}
        row_text = " ".join(str(row.get(key) or "") for key in ("subject", "description", "summary", "doc_content"))

        def _first_match_score(key: str, max_score: float, label: str) -> float:
            """Find first failure field `key` that appears in row_text."""
            for failure in failures[:5]:
                value = failure.get(key) or ""
                if value and value in row_text:
                    reasons.append(f"{label} {value[:60]}")
                    return max_score
            return 0.0

        # Dimension 1: Same test case name (0-30)
        test_case_score = _first_match_score("name", 30, "同失败用例")
        score += test_case_score
        details["same_test_case"] = test_case_score > 0

        # Dimension 2: Same module (0-20)
        module_score = _first_match_score("module", 20, "同模块")
        score += module_score
        details["same_module"] = module_score > 0

        # Dimension 3: Error keyword overlap (0-15)
        keyword_score = 0.0
        matched_keywords = []
        for failure in failures[:5]:
            for token in re.findall(r"[A-Za-z0-9_.#$-]{12,}", failure.get("reason") or "")[:5]:
                if token in row_text:
                    keyword_score += 3
                    matched_keywords.append(token[:40])
        keyword_score = min(keyword_score, 15)
        score += keyword_score
        details["keyword_overlap"] = round(keyword_score / 15, 2) if keyword_score > 0 else 0
        if matched_keywords:
            reasons.append(f"错误关键词 {', '.join(matched_keywords[:3])}")

        # Dimension 4: Title keyword Jaccard similarity (0-15)
        subject_words = set(re.findall(r"[A-Za-z0-9_一-鿿]{2,}", issue_payload.get("subject") or ""))
        ref_words = set(re.findall(r"[A-Za-z0-9_一-鿿]{2,}", row.get("subject") or ""))
        if subject_words and ref_words:
            intersection = subject_words & ref_words
            union = subject_words | ref_words
            jaccard = len(intersection) / len(union) if union else 0
            title_score = round(jaccard * 15, 1)
        else:
            title_score = 0
        score += title_score
        details["title_similarity"] = round(title_score / 15, 2) if title_score > 0 else 0
        if title_score > 0:
            reasons.append("标题关键词相似")

        return score, "；".join(dict.fromkeys(reasons))[:500], details

    async def _ai_semantic_similarity(self, issue_payload: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[int, float]:
        """Ask the local model to score semantic similarity between the issue and each candidate.

        Returns {issue_id: score_0_to_1}.
        """
        config = self._load_ai_config()
        if self.ai_analyzer_factory is None:
            return {}
        analyzer = self.ai_analyzer_factory(config)
        provider_name = analyzer.get_primary_provider()
        if not provider_name:
            return {}
        provider = config.get("providers", {}).get(provider_name, {})
        if not provider.get("base_url") or not provider.get("model"):
            return {}

        issue_desc = _truncate(issue_payload.get("description") or issue_payload.get("subject") or "", 500)
        issue_failures = _truncate(
            "\n".join(str(f.get("name", "")) + ": " + str(f.get("reason", ""))[:200] for f in (issue_payload.get("failures_json") or [])[:3]),
            500,
        )

        results: dict[int, float] = {}
        # Batch candidates into a single prompt to reduce API calls
        candidate_descriptions = []
        for i, c in enumerate(candidates[:5]):
            candidate_descriptions.append(f"[{i}] #{c['issue_id']} {c.get('subject', '')}")

        prompt = f"""评估以下 Redmine 问题之间的相似度（0-1分）。

当前问题: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}
描述摘要: {issue_desc}
关键失败: {issue_failures}

候选参考单:
{chr(10).join(candidate_descriptions)}

请返回纯JSON（不要markdown标记）:
{{"scores": [{",".join(f'{{"id": {c["issue_id"]}, "score": 0.XX}}' for c in candidates[:5])}]}}

评分标准:
- 0.8-1.0: 同模块同失败原因
- 0.5-0.7: 同模块不同失败或相似问题
- 0.2-0.4: 同大类问题但不同模块
- 0.0-0.1: 基本不相关
"""

        try:
            resp_text = await asyncio.to_thread(self._call_model_raw, analyzer, provider_name, provider, prompt)
            match = re.search(r'\{.*\}', resp_text, re.S)
            if match:
                parsed = json.loads(match.group(0))
                for item in parsed.get("scores", []):
                    results[int(item.get("id", 0))] = min(1.0, max(0.0, float(item.get("score", 0))))
        except Exception as exc:
            logger.warning("[RedmineAgent] AI semantic similarity failed: %s", exc)

        return results

    # AI model interaction
