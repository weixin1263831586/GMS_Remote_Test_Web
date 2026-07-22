"""根据成熟案例或相似工单生成 FAE 回复草稿。"""

from __future__ import annotations

import re
from typing import Any

from .case_extractor import (
    _SIG_KNOWLEDGE,
    RedmineCaseExtractor,
    decode_json_list,
    decode_json_obj,
    meaningful_text,
)
from .case_search import RedmineCaseSearch
from .knowledge_repository import RedmineKnowledgeDB


# 匹配文档中的黄超群回复块，正则在模块加载时编译。
_OWNER_REPLY_RE = re.compile(r"^###\s+[^\n]*黄\s*超群[^\n]*\n(?P<body>.*?)(?=^###\s+|^##\s+|\Z)", re.M | re.S)


class ReplyDrafter:
    """Compose a customer-facing reply draft for a Redmine issue."""

    def __init__(self, knowledge_db: RedmineKnowledgeDB, *, show_internal_refs: bool = False):
        self.db = knowledge_db
        self.show_internal_refs = show_internal_refs
        self.search = RedmineCaseSearch(knowledge_db)

    def draft_reply(
        self,
        issue: dict[str, Any],
        *,
        exclude_issue_id: int = 0,
        mature_case: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        issue = issue or {}
        issue_id = int(issue.get("issue_id") or exclude_issue_id or 0)
        exclude = exclude_issue_id or issue_id

        # 优先使用人工整理的案例事实，否则从工单快照提取。
        issue_id_for_fact = int(issue.get("issue_id") or 0)
        stored_fact = self.db.get_case_fact(issue_id_for_fact) if issue_id_for_fact else None
        if stored_fact and self._meaningful_root_case(stored_fact):
            fact = stored_fact
        else:
            fact = RedmineCaseExtractor.extract(issue)
        similar = self.search.search_similar(fact, limit=5, exclude_issue_id=exclude)

        # 优先使用传入案例，否则按签名和模块查找。
        resolved_case = mature_case
        if resolved_case is None:
            resolved_case = self.find_best_mature_case(fact, similar)

        if resolved_case:
            body = self._render_from_mature_case(resolved_case, issue_id, similar)
            source = "mature_case"
        else:
            body = self._render_from_similar(issue, fact, similar)
            source = "similar_issues"

        return {
            "issue_id": issue_id,
            "source": source,
            "module": fact.get("module") or "",
            "error_signature": fact.get("error_signature") or "",
            "mature_case_id": (resolved_case or {}).get("case_id"),
            "similar_issues": [
                {
                    "issue_id": s["issue_id"],
                    "subject": s["subject"],
                    "score": s["score"],
                    "module": s.get("module") or "",
                    "error_signature": s.get("error_signature") or "",
                }
                for s in similar[:5]
            ],
            "reply_draft": body,
        }

    @staticmethod
    def _meaningful_root_case(fact: dict[str, Any]) -> bool:
        """True when a curated fact carries a distilled root cause worth using."""
        return len(meaningful_text(fact.get("root_cause"))) >= 20

    # Mature case resolution

    def find_best_mature_case(self, fact: dict[str, Any], similar: list[dict[str, Any]]) -> dict[str, Any] | None:
        signature = fact.get("error_signature") or ""
        module = fact.get("module") or ""
        if not signature and not module:
            return None
        # 一次读取成熟案例并按 ID 建立索引。
        cases_by_id: dict[int, dict[str, Any]] = {
            int(c.get("case_id") or 0): c for c in self.db.list_mature_cases(limit=200) if c.get("case_id")
        }
        # Collect candidate case ids from links of high-similarity facts.
        candidate_case_ids: list[int] = []
        for hit in similar[:5]:
            for link in self.db.list_links_for_issue(int(hit["issue_id"])):
                cid = int(link.get("case_id") or 0)
                if cid and cid not in candidate_case_ids:
                    candidate_case_ids.append(cid)
        # Also include approved mature cases whose signature/module match.
        for cid, case in cases_by_id.items():
            if cid in candidate_case_ids:
                continue
            case_sig = case.get("canonical_error_signature") or ""
            case_module = case.get("module") or ""
            if (signature and case_sig == signature) or (module and case_module == module and not signature):
                candidate_case_ids.append(cid)
        # Score candidates by scope overlap with the probe fact.
        best: tuple[float, dict[str, Any]] | None = None
        for cid in candidate_case_ids[:50]:
            case = cases_by_id.get(cid)
            if not case:
                continue
            score = self._case_overlap(fact, case)
            if best is None or score > best[0]:
                best = (score, case)
        if best and best[0] >= 40:
            return best[1]
        return None

    @staticmethod
    def _case_overlap(fact: dict[str, Any], case: dict[str, Any]) -> float:
        score = 0.0
        if fact.get("error_signature") and fact["error_signature"] == case.get("canonical_error_signature"):
            score += 50
        if fact.get("module") and fact["module"] == case.get("module"):
            score += 20
        if fact.get("chip_platform") and fact["chip_platform"] == case.get("chip_platform"):
            score += 15
        if fact.get("android_version") and fact["android_version"] == case.get("android_version"):
            score += 10
        if case.get("status") == "approved":
            score += 5
        return score

    # Rendering

    def _render_from_mature_case(self, case: dict[str, Any], issue_id: int, similar: list[dict[str, Any]]) -> str:
        module = case.get("module") or "-"
        signature = case.get("canonical_error_signature") or ""
        root_cause = case.get("root_cause") or ""
        solution = decode_json_obj(case.get("solution_json"))
        overview = solution.get("overview") or case.get("reply_template") or ""
        # Verification: prefer the signature knowledge base (most accurate
        # validation guidance), then fall back to the case's rules.
        sig_kb = _SIG_KNOWLEDGE.get(signature, {})
        verification = sig_kb.get("verification", "") or self._case_verification(case)
        source_ids = decode_json_list(case.get("source_issue_ids_json"))

        lines = [
            f"您好，针对 #{issue_id} 该问题已有成熟处理方案，可参考如下：",
            "",
            f"- 模块：{module}",
        ]
        if signature:
            lines.append(f"- 问题：{signature}")
        if root_cause:
            lines.append(f"- 根因：{root_cause}")
        if overview:
            lines.extend(["", "解决步骤：", overview])
        if verification:
            lines.extend(["", "验证方式：", verification])
        if self.show_internal_refs and source_ids:
            lines.extend(["", "关联历史工单：" + " ".join(f"#{i}" for i in source_ids[:8])])
        elif source_ids:
            lines.extend(["", "（已匹配内部历史成熟案例，历史单号默认不对客户展示）"])
        lines.extend(["", "如有进一步日志请提供，可继续协助确认根因，谢谢。"])
        return "\n".join(lines)

    @staticmethod
    def _case_verification(case: dict[str, Any]) -> str:
        rules = decode_json_list(case.get("rules_json"))
        for rule in rules or []:
            if isinstance(rule, dict) and rule.get("content"):
                return str(rule["content"])
        return ""

    def _render_from_similar(self, issue: dict[str, Any], fact: dict[str, Any], similar: list[dict[str, Any]]) -> str:
        issue_id = int(issue.get("issue_id") or 0)
        subject = issue.get("subject") or ""
        module = fact.get("module") or "-"
        signature = fact.get("error_signature") or ""
        root_cause = self._meaningful_text(fact.get("root_cause"))
        solution = self._meaningful_text(fact.get("solution"))
        # 每个候选文本只解析一次。
        candidate_solutions = [self._candidate_solution_text(s) for s in similar]
        best_solution = next((t for t in candidate_solutions if t), "")
        candidate_roots = [self._meaningful_text(s.get("root_cause")) for s in similar]
        best_root = next((t for t in candidate_roots if t), "")

        lines = [
            f"您好，关于 #{issue_id} {subject}，初步分析如下：",
            "",
            f"- 模块：{module}",
        ]
        if signature:
            lines.append(f"- 问题：{signature}")
        if root_cause or best_root:
            lines.append(f"- 根因：{root_cause or best_root}")
        if solution or best_solution:
            lines.extend(["", "建议处理：", solution or best_solution])

        high = [s for s in similar if s.get("similarity_level") == "high"][:3]
        medium = [s for s in similar if s.get("similarity_level") == "medium"][:2]
        refs = high or medium
        if refs:
            if self.show_internal_refs:
                lines.extend(["", "可参考历史工单：" + " ".join(f"#{s['issue_id']}" for s in refs)])
            else:
                lines.extend(["", f"已匹配 {len(refs)} 条相似历史工单（默认不对客户展示历史单号），结论与上方一致。"])
        lines.extend(["", "如有进一步日志请提供，可继续协助确认根因，谢谢。"])
        return "\n".join(lines)

    @staticmethod
    def _meaningful_text(value: Any) -> str:
        return meaningful_text(value)

    @classmethod
    def _candidate_solution_text(cls, item: dict[str, Any]) -> str:
        excerpt = str(item.get("doc_excerpt") or "")
        owner_reply = cls._extract_owner_reply(excerpt)
        if owner_reply:
            return owner_reply
        for key in ("solution", "reply_template"):
            text = cls._meaningful_text(item.get(key))
            if text:
                return text
        return ""

    @classmethod
    def _extract_owner_reply(cls, excerpt: str) -> str:
        text = str(excerpt or "").strip()
        if not text:
            return ""
        # 优先提取文档中的黄超群回复块。
        pattern = _OWNER_REPLY_RE
        matches = [m.group("body").strip() for m in pattern.finditer(text)]
        candidates = [cls._redmine_pre_to_markdown(m) for m in matches if cls._meaningful_text(m)]
        if candidates:
            # Prefer a reply that carries an actual patch/diff.
            candidates.sort(key=lambda s: (0 if "diff --git" in s or "```diff" in s else 1, -len(s)))
            return candidates[0]
        return ""

    @staticmethod
    def _redmine_pre_to_markdown(text: str) -> str:
        def repl(match: re.Match) -> str:
            lang = (match.group(1) or "").strip() or ""
            body = match.group(2) or ""
            return f"```{lang}\n{body.strip()}\n```"
        return re.sub(r"<pre><code(?:\s+class=\"([^\"]+)\")?\s*>\s*(.*?)\s*</code></pre>", repl, text, flags=re.S)
