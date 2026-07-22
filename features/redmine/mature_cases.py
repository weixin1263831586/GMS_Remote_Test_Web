"""Aggregation of duplicate issues into a single "mature case".

A mature case (Redmine.txt §4) is the canonical, approved answer for a
recurring problem, e.g. "RK3576 Android16 BTS — VBMeta test key". It is built
from one or more source case facts: shared scope fields are taken by majority
vote, the root cause / solution prefer closed-then-confirmed issues, and the
evidence keeps pointers back to every source issue.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .case_extractor import (
    _SIG_KNOWLEDGE,
    decode_json_list,
    decode_json_obj,
    first_meaningful,
    is_meaningful,
)
from .knowledge_repository import RedmineKnowledgeDB


class MatureCaseBuilder:
    """Build and persist mature cases from case facts."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB):
        self.db = knowledge_db

    def build_from_issues(self, issue_ids: list[int], *, title: str = "") -> dict[str, Any]:
        issue_ids = [int(i) for i in (issue_ids or []) if i]
        if not issue_ids:
            raise ValueError("build_from_issues requires at least one issue_id")

        facts_by_id = self.db.get_case_facts_for_issue_ids(issue_ids)
        facts = [facts_by_id[i] for i in issue_ids if facts_by_id.get(i)]
        if not facts:
            raise ValueError("no case facts found for the given issue_ids")

        # Majority vote on shared scope dimensions.
        chip_platform = self._majority([f.get("chip_platform") for f in facts])
        android_version = self._majority([f.get("android_version") for f in facts])
        certification_type = self._majority([f.get("certification_type") for f in facts])
        module = self._majority([f.get("module") for f in facts])
        product_form = self._majority([f.get("product_form") for f in facts])
        region = self._majority([f.get("region") for f in facts])
        # Canonical error signature: prefer the most common non-empty one.
        signatures = [f.get("error_signature") for f in facts if f.get("error_signature")]
        canonical_signature = self._majority(signatures) if signatures else ""

        # 排序：已关闭、已确认、其他。
        ordered = sorted(
            facts,
            key=lambda f: (
                0 if self._is_closed(f) else (1 if self._is_confirmed(f) else 2),
                -float(f.get("confidence") or 0),
            ),
        )
        anchor = ordered[0]

        root_cause = first_meaningful([f.get("root_cause") for f in ordered])
        solution_text = first_meaningful([f.get("solution") for f in ordered]) or anchor.get("solution") or ""
        verification = first_meaningful([f.get("verification") for f in ordered])
        problem_summary = first_meaningful([f.get("problem_summary") for f in ordered]) or anchor.get("problem_summary") or ""

        # Fallback to the signature knowledge base when the issues had no
        # 新导入事实缺少明确根因或验证时补充结构化信息。
        sig_knowledge = _SIG_KNOWLEDGE.get(canonical_signature, {})
        if not is_meaningful(root_cause):
            root_cause = sig_knowledge.get("root_cause", "")
        if not is_meaningful(verification):
            verification = sig_knowledge.get("verification", "")

        # Merge symptoms / keywords across all facts (dedup, preserve order).
        symptoms = self._merge_lists([f.get("symptoms_json") for f in facts])
        keywords = self._merge_lists([f.get("keywords_json") for f in facts])

        resolved_title = title or self._majority([f.get("subject") for f in facts]) or f"{module or canonical_signature or 'Redmine'} 历史问题汇总"

        cleaned_overview = self._clean_solution_overview(solution_text)
        solution_payload = {
            "overview": cleaned_overview or solution_text,
            "steps": self._solution_steps(solution_text),
        }
        scope = {
            "chip_platform": chip_platform,
            "android_version": android_version,
            "test_version": certification_type,
            "module": module,
            "product_form": product_form,
            "region": region,
        }
        evidence = self._aggregate_evidence(facts)
        reply_template = self._build_reply_template(resolved_title, module, canonical_signature, root_cause, solution_payload, verification)
        confidence = min(100.0, max([float(f.get("confidence") or 0) for f in facts] + [0]) + 5 * (len(facts) - 1))

        case_payload = {
            "title": resolved_title,
            "status": "draft",
            "canonical_error_signature": canonical_signature,
            "chip_platform": chip_platform,
            "android_version": android_version,
            "certification_type": certification_type,
            "module": module,
            "product_form": product_form,
            "region": region,
            "problem_summary": problem_summary,
            "scope": scope,
            "symptoms": symptoms,
            "root_cause": root_cause,
            "solution": solution_payload,
            "notes": [],
            "rules": self._build_rules(module, canonical_signature, root_cause),
            "reply_template": reply_template,
            "source_issue_ids": issue_ids,
            "evidence": evidence,
            "keywords": keywords,
            "confidence": confidence,
        }
        case_id = self.db.upsert_mature_case(case_payload)
        for fact in facts:
            self.db.link_case_issue(case_id, int(fact["issue_id"]), score=float(fact.get("confidence") or 0), reason=self._link_reason(fact))
        stored = self.db.get_mature_case(case_id) or {}
        return {"case_id": case_id, **stored}

    # Helpers

    @staticmethod
    def _majority(values: list[Any]) -> str:
        counter = Counter(str(v or "").strip() for v in values if str(v or "").strip())
        if not counter:
            return ""
        return counter.most_common(1)[0][0]


    @staticmethod
    def _is_closed(fact: dict[str, Any]) -> bool:
        return "closed" in str(fact.get("status_name") or "").lower()

    @staticmethod
    def _is_confirmed(fact: dict[str, Any]) -> bool:
        return "confirm" in str(fact.get("status_name") or "").lower()

    @staticmethod
    def _merge_lists(lists: list[Any]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for lst in lists or []:
            lst = decode_json_list(lst)
            for item in lst or []:
                value = str(item).strip()
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    merged.append(value)
        return merged

    # 构造解决步骤前过滤日志和 AI 元数据行。
    _META_PREFIXES = (
        "✓ 已解决", "✓已解决", "已解决:", "已解决：",
        "方案说明:", "方案说明：", "方案说明 ",
        "您好", "你好",
        "未找到明确的解决方案", "未经客户确认",
    )

    @classmethod
    def _is_meta_line(cls, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return True
        return any(stripped.startswith(prefix) or stripped.startswith(prefix.replace("：", ":")) for prefix in cls._META_PREFIXES)

    @classmethod
    def _clean_solution_overview(cls, text: str) -> str:
        """Drop leading meta lines (✓ 已解决 / 方案说明: / 您好 ...) from the overview."""
        lines = str(text or "").splitlines()
        kept: list[str] = []
        for line in lines:
            if cls._is_meta_line(line):
                continue
            kept.append(line)
        return "\n".join(kept).strip() or str(text or "").strip()

    @classmethod
    def _solution_steps(cls, solution_text: str) -> list[str]:
        steps: list[str] = []
        for raw in str(solution_text or "").splitlines():
            line = raw.strip()
            if cls._is_meta_line(line):
                continue
            # Strip leading "N." / "-" / "•" numbering.
            cleaned = line.lstrip("0123456789.-、)•● ").strip()
            if cleaned and not cls._is_meta_line(cleaned):
                steps.append(cleaned)
        return steps[:12]

    @staticmethod
    def _aggregate_evidence(facts: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge each source case_fact's structured evidence (attachments +
        journals timeline) into the mature case (Redmine.txt §4 evidence)."""
        def _evidence_of(fact: dict[str, Any]) -> dict[str, Any]:
            ev = fact.get("evidence_json") or fact.get("evidence") or {}
            return decode_json_obj(ev)

        source_facts: list[dict[str, Any]] = []
        all_attachments: list[dict[str, Any]] = []
        all_journals: list[dict[str, Any]] = []
        for f in facts:
            ev = _evidence_of(f)
            source_facts.append({
                "issue_id": f.get("issue_id"),
                "subject": f.get("subject"),
                "status_name": f.get("status_name"),
                "root_cause": (f.get("root_cause") or "")[:400],
                "solution": (f.get("solution") or "")[:400],
            })
            for att in (ev.get("attachments") or [])[:10]:
                att = dict(att)
                att["source_issue_id"] = f.get("issue_id")
                all_attachments.append(att)
            for jn in (ev.get("journals") or [])[:15]:
                all_journals.append(jn)
        return {
            "source_facts": source_facts,
            "attachments": all_attachments[:30],
            "attachments_count": len(all_attachments),
            "journals": all_journals[:50],
            "journals_count": len(all_journals),
            "redmine_description": (_evidence_of(facts[0]) if facts else {}).get("redmine_description", "")[:1500],
        }

    @staticmethod
    def _build_rules(module: str, signature: str, root_cause: str) -> list[dict[str, str]]:
        if not signature:
            return []
        if signature == "PowerAidl hasFixedPerformance unsupported":
            return [
                {
                    "title": "Android 大版本升级时必须完整迁移 vendor HAL 能力声明",
                    "content": (
                        "VTS 新增或收紧 HAL AIDL 能力校验时，需同步检查 vendor HAL 对应模式的 "
                        "isModeSupported / isBoostSupported 等能力声明，避免沿用旧 SDK 默认 false。"
                    ),
                }
            ]
        if signature == "VBMeta test key":
            return [
                {
                    "title": f"{signature} 类问题须先确认量产 key 配置",
                    "content": f"出现 {signature} 时优先核查是否使用 production key / 量产配置，而非沿用公开 test key。",
                }
            ]
        return [
            {
                "title": f"{signature} 类问题优先按同签名历史案例复核",
                "content": f"出现 {signature} 时优先匹配同错误签名、同模块、同 Android 版本的历史 Redmine 案例。",
            }
        ]

    @staticmethod
    def _link_reason(fact: dict[str, Any]) -> str:
        bits = []
        if fact.get("error_signature"):
            bits.append(f"同错误签名({fact['error_signature']})")
        if fact.get("module"):
            bits.append(f"同模块({fact['module']})")
        if fact.get("status_name"):
            bits.append(f"状态:{fact['status_name']}")
        return "；".join(bits) or "历史相似工单"

    @staticmethod
    def _build_reply_template(title: str, module: str, signature: str, root_cause: str, solution: dict[str, Any], verification: str) -> str:
        lines = [f"【成熟案例】{title}", ""]
        if module:
            lines.append(f"- 模块：{module}")
        if signature:
            lines.append(f"- 问题：{signature}")
        if root_cause:
            lines.append(f"- 根因：{root_cause}")
        overview = (solution or {}).get("overview") or ""
        if overview:
            lines.extend(["", "解决步骤：", overview])
        if verification:
            lines.extend(["", "验证方式：", verification])
        return "\n".join(lines)
