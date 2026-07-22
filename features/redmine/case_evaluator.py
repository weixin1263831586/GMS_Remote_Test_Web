"""Compare internal case output against a reference (GMS assistant / manual).

This is the **evaluation-only** channel (Redmine.txt §1, §5.8). Reference
outputs are stored in ``redmine_reference_outputs`` and never feed the
production auto-reply (see :mod:`reply_drafter`).

Dimensions compared (§5.8): title, platform, android version, test module,
failing case, symptoms, root cause, solution, verification steps, notes,
rules. Produces a 0–100 score, missing fields and mismatch fields.
"""

from __future__ import annotations

import re
from typing import Any

from .knowledge_repository import RedmineKnowledgeDB


# 各评估维度及内部、参考值提取器。
def _platform(case: dict) -> str:
    return str(case.get("chip_platform") or (case.get("scope") or {}).get("chip_platform") or "")


def _android(case: dict) -> str:
    return str(case.get("android_version") or (case.get("scope") or {}).get("android_version") or "")


def _module(case: dict) -> str:
    return str(case.get("module") or (case.get("scope") or {}).get("module") or "")


def _solution(case: dict) -> str:
    sol = case.get("solution")
    if isinstance(sol, dict):
        return str(sol.get("overview") or "")
    return str(sol or "")


_DIMENSIONS = [
    ("title", lambda c: str(c.get("title") or c.get("subject") or ""), lambda r: str(r.get("title") or "")),
    ("platform", _platform, lambda r: str(r.get("platform") or r.get("chip_platform") or "")),
    ("android_version", _android, lambda r: str(r.get("android_version") or "")),
    ("test_module", _module, lambda r: str(r.get("module") or r.get("test_module") or "")),
    ("failing_case", lambda c: str((c.get("symptoms_json") or [{}])[0] if c.get("symptoms_json") else ""), lambda r: str(r.get("failing_case") or "")),
    ("symptoms", lambda c: " ".join(c.get("symptoms_json") or []), lambda r: str(r.get("symptoms") or "")),
    ("root_cause", lambda c: str(c.get("root_cause") or ""), lambda r: str(r.get("root_cause") or "")),
    ("solution", _solution, lambda r: str(r.get("solution") or "")),
    ("verification", lambda c: str(c.get("verification") or ""), lambda r: str(r.get("verification") or "")),
    ("notes", lambda c: " ".join(c.get("notes_json") or []), lambda r: str(r.get("notes") or "")),
    ("rules", lambda c: " ".join((rule.get("content", "") if isinstance(rule, dict) else str(rule)) for rule in (c.get("rules_json") or [])), lambda r: str(r.get("rules") or "")),
]


class CaseEvaluator:
    """Persist reference outputs and score internal cases against them."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB):
        self.db = knowledge_db

    def import_reference_output(self, issue_id: int, payload: dict[str, Any]) -> int:
        source = str(payload.get("source") or "manual").strip() or "manual"
        if source not in ("gms_assistant", "manual", "imported"):
            source = "imported"
        return self.db.insert_reference_output(
            issue_id,
            source,
            {
                "title": payload.get("title") or "",
                "markdown": payload.get("markdown") or "",
                "raw_output": payload.get("raw_output") or "",
                "structured_json": payload.get("structured_json") or payload.get("structured") or {},
            },
        )

    def evaluate_case(self, issue_id: int, *, internal_case: dict[str, Any] | None = None, reference: dict[str, Any] | None = None) -> dict[str, Any]:
        internal_case = internal_case or self.db.get_case_fact(issue_id) or {}
        if reference is None:
            refs = self.db.get_reference_outputs(issue_id)
            reference = (refs[0] if refs else {}).get("structured_json") or {}
        structured_ref = reference if isinstance(reference, dict) else {}

        missing_fields: list[str] = []
        mismatch_fields: list[dict[str, str]] = []
        matched = 0

        for key, internal_fn, ref_fn in _DIMENSIONS:
            internal_val = (internal_fn(internal_case) or "").strip()
            ref_val = (ref_fn(structured_ref) or "").strip()
            if not ref_val:
                # Reference doesn't cover this dimension — skip scoring it.
                continue
            if not internal_val:
                missing_fields.append(key)
                continue
            if self._loosely_equal(internal_val, ref_val):
                matched += 1
            else:
                mismatch_fields.append({"field": key, "internal": internal_val[:200], "reference": ref_val[:200]})

        covered = sum(1 for _, _, ref_fn in _DIMENSIONS if (ref_fn(structured_ref) or "").strip())
        score = round(100.0 * matched / covered, 1) if covered else 0.0

        suggestions = self._suggestions(missing_fields, mismatch_fields)
        payload = {
            "internal_case": internal_case,
            "reference_case": structured_ref,
            "score": score,
            "missing_fields": missing_fields,
            "mismatch_fields": mismatch_fields,
            "suggestions": suggestions,
        }
        eval_id = self.db.insert_case_evaluation(issue_id, payload)
        return {"evaluation_id": eval_id, **payload}

    @classmethod
    def _loosely_equal(cls, a: str, b: str) -> bool:
        a_norm = a.lower().replace(" ", "")
        b_norm = b.lower().replace(" ", "")
        if not a_norm or not b_norm:
            return False
        if a_norm == b_norm:
            return True
        # 双向子串匹配覆盖部分重叠文本。
        if a_norm in b_norm or b_norm in a_norm:
            return True
        # Semantic overlap for long-form fields (root_cause / solution / ...):
        # if the two texts share most meaningful keywords, treat as a match
        # even when the wording differs (e.g. internal "未切 production key" vs
        # reference "仍使用 test key").
        return cls._semantic_overlap(a, b)

    @staticmethod
    def _semantic_overlap(a: str, b: str, *, threshold: float = 0.5) -> bool:
        def tokens(text: str) -> set[str]:
            # Alphanumeric tokens (len>=2) + CJK runs (len>=2), lowercased.
            return {t.lower() for t in re.findall(r"[A-Za-z0-9]{2,}|[一-龥]{2,}", text or "")}

        ta, tb = tokens(a), tokens(b)
        if not ta or not tb:
            return False
        # 短标签已由精确和子串规则处理。
        if len(ta) < 3 and len(tb) < 3:
            return False
        overlap = len(ta & tb)
        smaller = min(len(ta), len(tb))
        return (overlap / smaller) >= threshold

    @staticmethod
    def _suggestions(missing_fields: list[str], mismatch_fields: list[dict[str, str]]) -> list[str]:
        suggestions: list[str] = []
        for field in missing_fields:
            label = {"root_cause": "根因", "solution": "解决方案", "verification": "验证步骤", "rules": "关键经验规则", "failing_case": "失败用例"}.get(field, field)
            suggestions.append(f"缺少「{label}」字段，建议补充以对齐参考输出")
        for mismatch in mismatch_fields:
            label = {"root_cause": "根因", "solution": "解决方案"}.get(mismatch["field"], mismatch["field"])
            suggestions.append(f"「{label}」与参考输出不一致，建议核对：内部={mismatch['internal'][:60]} / 参考={mismatch['reference'][:60]}")
        return suggestions
