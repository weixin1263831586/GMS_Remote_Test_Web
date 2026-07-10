"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from features.redmine.config import config_manager
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
    r"\bFAIL(?:URE)?:",
    r"\bASSUMPTION_FAILURE:",
    r"\bFATAL\b",
    r"\bdenied\b",
    r"\bError:\s",
    # Assertion diffs (gtest / JUnit style)
    r"\bActual:\s",
    r"\bExpected:\s",
    r"\[\s*FAILED\s*\]",
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


def _load_agent_config() -> dict[str, Any]:
    """Load redmine_agent section from config.json, with env overrides."""
    cfg = config_manager.load_config().get("redmine_agent", {})
    return {
        "max_issues_per_run": int(os.getenv("REDMINE_AGENT_MAX_ISSUES", cfg.get("max_issues_per_run", 50))),
        "sync_max_issues": int(os.getenv("REDMINE_AGENT_SYNC_MAX_ISSUES", cfg.get("sync_max_issues", 5000))),
        "detail_sync_limit": int(os.getenv("REDMINE_AGENT_DETAIL_SYNC_LIMIT", cfg.get("detail_sync_limit", 5000))),
    }


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



class ResolutionAnalysisMixin:
    @staticmethod
    def _extract_patch_from_journals(journals: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Extract existing <pre><code class="diff"> patches from journal notes."""
        patches = []
        for journal in journals:
            notes = str(journal.get("notes") or "")
            user = journal.get("user") or ""
            # Extract <pre><code class="diff">...</code></pre> blocks
            diff_blocks = re.findall(
                r'<pre><code\s+class="diff">\s*(.*?)\s*</code></pre>',
                notes,
                re.S,
            )
            for block in diff_blocks:
                patches.append({"user": user, "patch": block.strip()})
            # Also extract bare <pre>...</pre> blocks that look like diffs
            if not diff_blocks:
                pre_blocks = re.findall(r"<pre>\s*(.*?)\s*</pre>", notes, re.S)
                for block in pre_blocks:
                    if re.search(r"^diff\s|--\s+a/|\+\+\+\s+b/", block, re.M):
                        patches.append({"user": user, "patch": block.strip()})
        return patches

    @staticmethod
    def _detect_confirmed_in_journals(journals: list[dict[str, Any]]) -> str | None:
        """Detect if the issue was confirmed resolved in journal comments."""
        confirm_patterns = ["测试ok", "测试通过", "验证ok", "验证通过", "已解决", "问题已解决", "可以关闭"]
        for journal in reversed(journals):
            notes = str(journal.get("notes") or "").lower()
            user = journal.get("user") or ""
            for pattern in confirm_patterns:
                if pattern in notes:
                    return f"{user}: {str(journal.get('notes', ''))[:100]}"
        return None

    @staticmethod
    def _analyze_resolution_from_journals(journals: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze journal history to determine the correct resolution for a closed issue.

        Returns a structured resolution summary:
        {
            "has_resolution": bool,
            "status": "verified" | "resolved" | "unclear",
            "provider": str,   # who provided the solution
            "provider_time": str,
            "confirmer": str,  # who confirmed the solution
            "confirmer_time": str,
            "confirm_note": str,
            "patches": [...],  # extracted diff/patch blocks
            "solution_text": str,  # plain text solution from provider
            "summary": str,    # one-line resolution summary
        }
        """
        patches = ResolutionAnalysisMixin._extract_patch_from_journals(journals)
        confirmed = ResolutionAnalysisMixin._detect_confirmed_in_journals(journals)

        # Walk journals forward to find: provider (has code/diff) -> confirmer (says "ok")
        provider = ""
        provider_time = ""
        provider_notes = ""
        confirmer = ""
        confirmer_time = ""
        confirm_note = ""

        # Patterns indicating someone is providing a solution
        solution_indicators = ["<pre>", "<code", "diff --git", "git diff", "patch", "修改方法", "解决方案", "修改如下", "改法"]
        confirm_indicators = ["测试ok", "测试通过", "验证ok", "验证通过", "已解决", "可以关闭", "没问题了"]

        for journal in journals:
            notes = str(journal.get("notes") or "")
            user = journal.get("user") or ""
            created = journal.get("created_on") or ""
            notes_lower = notes.lower()

            # Detect if this journal is providing a solution
            is_solution = False
            if any(ind in notes_lower for ind in solution_indicators):
                is_solution = True
            if any(ind in notes for ind in ["<pre><code", "diff --git"]):
                is_solution = True

            if is_solution and not provider:
                provider = user
                provider_time = created
                pre_idx = notes.find("<pre>")
                provider_notes = notes[:pre_idx].strip() if pre_idx > 0 else notes[:500].strip()

            # Detect if this journal confirms the solution
            is_confirm = any(ind in notes_lower for ind in confirm_indicators)
            if is_confirm and not confirmer and provider and user != provider:
                confirmer = user
                confirmer_time = created
                confirm_note = notes.strip()

        # Determine resolution status
        has_resolution = bool(patches) or bool(provider)
        if has_resolution and confirmed:
            status = "verified"
        elif has_resolution:
            status = "resolved"
        else:
            status = "unclear"

        # Build summary
        summary = ""
        if status == "verified":
            summary = f"✅ 已验证: {provider} 提供方案，{confirmer} 确认通过 ({confirm_note[:50]})"
        elif status == "resolved":
            summary = f"✓ 已解决: {provider} 提供方案（未经客户确认）"
        else:
            summary = "⚠ 未找到明确的解决方案"

        return {
            "has_resolution": has_resolution,
            "status": status,
            "provider": provider,
            "provider_time": provider_time,
            "confirmer": confirmer,
            "confirmer_time": confirmer_time,
            "confirm_note": confirm_note,
            "patches": patches,
            "solution_text": provider_notes,
            "summary": summary,
        }

    @staticmethod
    def _detect_version_type(fixed_version: str) -> str:
        """Detect version type (GMS/SDK) from fixed_version string."""
        text = str(fixed_version or "").upper()
        if "GMS" in text:
            return "GMS"
        if "SDK" in text or "SSI" in text:
            return "SDK"
        return "-"

    # ------------------------------------------------------------------
    # Structured field extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _format_journal_patches(patches: list[dict[str, str]]) -> str:
        """Format journal-extracted patches into joined HTML code blocks."""
        if not patches:
            return ""
        return "\n\n".join(f'<pre><code class="diff">{jp["patch"]}</code></pre>' for jp in patches)

    def _extract_structured_fields(
        self,
        ai_result: dict[str, Any],
        issue_payload: dict[str, Any],
        failures: list[dict[str, Any]],
        references: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Map AI output + rule-based extraction to the seven display fields."""
        subject = issue_payload.get("subject") or ""
        journals = issue_payload.get("journals_json") or []
        # Analyze resolution from journals (for closed/resolved issues)
        resolution = self._analyze_resolution_from_journals(journals)

        # 1. title
        title = ai_result.get("title") or subject

        # 2. problem_description
        problem_description = ai_result.get("problem_description") or ""
        if not problem_description:
            problem_description = _truncate(issue_payload.get("description") or subject, 500)

        # 3. error_info
        error_info = ai_result.get("error_info") or ""
        if not error_info and failures:
            error_info = self.extract_error_from_failures(failures)
        if not error_info:
            error_info = self._extract_error_from_issue_evidence(issue_payload)
        if error_info and not str(error_info).lstrip().startswith("```"):
            error_info = self._ensure_code_block(error_info, "")

        # 4. error_analysis
        error_analysis = ai_result.get("error_analysis") or ai_result.get("root_cause_guess") or ""
        if not error_analysis:
            error_analysis = self._rule_error_analysis(issue_payload, failures, references)

        # 5. solution
        # Priority: AI result > journal resolution > rule-based
        solution = ai_result.get("solution") or ""
        if not solution:
            if resolution["has_resolution"]:
                parts = [resolution["summary"]]
                if resolution.get("solution_text"):
                    parts.append(f"方案说明: {resolution['solution_text']}")
                solution = "\n".join(parts)
            else:
                solution = self._rule_solution(issue_payload, failures, references)

        # 6. patch_direction
        # Priority: verified journal patches > AI result > journal patches > rule-based
        journal_patch_html = self._format_journal_patches(resolution.get("patches") or [])
        patch_direction = ai_result.get("patch_direction") or ai_result.get("risk") or ""

        if resolution["status"] == "verified" and journal_patch_html:
            # Verified resolution from journals always takes priority
            patch_direction = journal_patch_html
        elif not patch_direction:
            patch_direction = journal_patch_html or self._rule_patch_direction(issue_payload, failures, references)
        else:
            patch_direction = self._markdown_to_html_code_blocks(
                self._wrap_patch_direction(patch_direction)
            )

        # 7. reference_redmine (formatted)
        ai_refs = ai_result.get("reference_redmine") or []
        if not ai_refs:
            reference_redmine = "; ".join(f"#{r.get('issue_id')}" for r in references[:3]) if references else ""
        else:
            reference_redmine = "; ".join(f"#{r.get('issue_id')}({r.get('reason', '')[:30]})" for r in ai_refs)

        summary = ai_result.get("summary") or self._rule_summary(issue_payload, failures, references)
        reply_draft = ai_result.get("reply_draft") or self._reply_draft(issue_payload, failures, references, solution, patch_direction)

        return {
            "title": title,
            "problem_description": problem_description,
            "error_info": error_info,
            "error_analysis": error_analysis,
            "solution": solution,
            "patch_direction": patch_direction,
            "reference_redmine": reference_redmine,
            "summary": summary,
            "reply_draft": reply_draft,
            "resolution_json": resolution if resolution["has_resolution"] else None,
            "references_json": references,
            "ai_json": ai_result,
        }

    @classmethod
    def enrich_issue_display_fields(cls, issue_payload: dict[str, Any]) -> dict[str, Any]:
        """Fill list/detail display fields from existing Redmine evidence.

        This is a read-time fallback for old synced rows. It never invents a
        root cause or solution: when evidence is incomplete it says what was
        found and what still needs confirmation.
        """
        issue = dict(issue_payload or {})
        failures = issue.get("failures_json") or []
        references = issue.get("references_json") or []
        journals = issue.get("journals_json") or []
        resolution = cls._analyze_resolution_from_journals(journals)
        # Extract error evidence once and thread it through the rule methods so
        # they don't each re-scan attachments/description on the list endpoint.
        extracted_evidence = cls._extract_error_from_issue_evidence(issue)

        if not cls._meaningful_field(issue.get("problem_description")):
            issue["problem_description"] = _truncate(issue.get("description") or issue.get("subject") or "", 500)
        if not cls._meaningful_field(issue.get("error_info")):
            error_info = ""
            if failures:
                error_info = cls.extract_error_from_failures(failures)
            error_info = error_info or extracted_evidence
            issue["error_info"] = cls._ensure_code_block(error_info, "") if error_info else ""
        if not cls._meaningful_field(issue.get("error_analysis")):
            issue["error_analysis"] = cls._rule_error_analysis(issue, failures, references, extracted_evidence)
        if not cls._meaningful_field(issue.get("solution")):
            if resolution["has_resolution"]:
                parts = [resolution["summary"]]
                if resolution.get("solution_text"):
                    parts.append(f"方案说明: {resolution['solution_text']}")
                issue["solution"] = "\n".join(parts)
            else:
                issue["solution"] = cls._rule_solution(issue, failures, references, extracted_evidence)
        if not cls._meaningful_field(issue.get("patch_direction")):
            patch = cls._format_journal_patches(resolution.get("patches") or [])
            issue["patch_direction"] = patch or cls._rule_patch_direction(issue, failures, references, extracted_evidence)
        return issue

    def _rule_summary(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
        if failures:
            first = failures[0]
            return f"{first.get('module') or '未知模块'} / {first.get('name') or '未知用例'} 失败：{_truncate(first.get('reason') or '', 180)}"
        return _truncate(issue_payload.get("description") or issue_payload.get("subject") or "未提取到描述", 240)

    @staticmethod
    def _meaningful_field(value: Any) -> bool:
        text = str(value or "").strip()
        if not text or text == "-":
            return False
        placeholders = ("暂无分析结果", "待进一步分析确认解决方案", "需要进一步分析具体日志和源码", "未提取到描述")
        return not any(ph in text for ph in placeholders)

    @staticmethod
    def _plain_text(value: Any) -> str:
        text = str(value or "")
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @classmethod
    def _iter_attachment_evidence(cls, issue_payload: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for att in issue_payload.get("attachments_json") or []:
            if not isinstance(att, dict):
                continue
            analysis = att.get("analysis_json") or {}
            details = analysis.get("details") or {}
            failures = analysis.get("failures") or []
            detected = details.get("detected_errors") or []
            excerpt = analysis.get("text_excerpt") or details.get("ocr_text") or ""
            items.append({
                "filename": att.get("filename") or "",
                "type": details.get("type") or analysis.get("type") or "",
                "detected_errors": detected if isinstance(detected, list) else [],
                "failures": failures if isinstance(failures, list) else [],
                "excerpt": str(excerpt or ""),
            })
        return items

    @classmethod
    def _extract_error_from_issue_evidence(cls, issue_payload: dict[str, Any]) -> str:
        chunks: list[str] = []
        description = str(issue_payload.get("description") or "")
        for line in description.splitlines():
            if _ERROR_LINE_RE.search(line):
                chunks.append(line.strip())
            if len(chunks) >= MAX_FAILURE_LINES:
                break
        for item in cls._iter_attachment_evidence(issue_payload):
            for failure in item["failures"][:5]:
                if isinstance(failure, dict):
                    name = cls._plain_text(failure.get("name") or "")
                    reason = cls._plain_text(failure.get("reason") or "")
                    if name or reason:
                        chunks.append(f"{item['filename']}: {name} {reason}".strip())
            for err in item["detected_errors"][:5]:
                chunks.append(f"{item['filename']}: {cls._plain_text(err)}")
            excerpt = str(item.get("excerpt") or "")
            for line in excerpt.splitlines():
                if _ERROR_LINE_RE.search(line):
                    chunks.append(f"{item['filename']}: {line.strip()}")
                if len(chunks) >= MAX_FAILURE_LINES:
                    break
        seen: set[str] = set()
        unique: list[str] = []
        for chunk in chunks:
            chunk = chunk.strip()
            key = chunk.lower()
            if chunk and key not in seen:
                seen.add(key)
                unique.append(chunk)
        return "\n".join(unique[:MAX_FAILURE_LINES])

    @classmethod
    def _evidence_notes(cls, issue_payload: dict[str, Any], failures: list[dict[str, Any]], extracted_evidence: str | None = None) -> list[str]:
        notes: list[str] = []
        if failures:
            first = failures[0]
            if first.get("module"):
                notes.append(f"失败模块: {first.get('module')}")
            if first.get("name"):
                notes.append(f"失败用例: {first.get('name')}")
            reason = cls._plain_text(first.get("reason") or "")
            if reason:
                notes.append(f"关键报错: {_truncate(reason, 200)}")
        else:
            extracted = extracted_evidence if extracted_evidence is not None else cls._extract_error_from_issue_evidence(issue_payload)
            if extracted:
                first_line = extracted.splitlines()[0].strip()
                if first_line:
                    notes.append(f"描述/附件报错: {_truncate(first_line, 200)}")
        attachments = cls._iter_attachment_evidence(issue_payload)
        parsed = [a for a in attachments if a.get("failures") or a.get("detected_errors") or a.get("excerpt")]
        if parsed:
            names = ", ".join(str(a.get("filename") or "-") for a in parsed[:5])
            notes.append(f"附件证据: {names}")
        journals = [
            j for j in issue_payload.get("journals_json") or []
            if isinstance(j, dict) and str(j.get("notes") or "").strip()
        ]
        if journals:
            notes.append(f"历史回复: {len(journals)} 条有内容回复可参考")
        return notes

    @classmethod
    def _rule_solution(cls, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]], extracted_evidence: str | None = None) -> str:
        lines = []
        for note in cls._evidence_notes(issue_payload, failures, extracted_evidence):
            lines.append(f"- {note}")
        lines.append("- 当前证据中未找到明确已验证解决方案；需要结合上述日志/附件和历史回复继续确认。")
        if references:
            ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in references[:3])
            lines.append(f"- 可参考历史单: {ref_ids}")
        return "\n".join(lines)

    @classmethod
    def _rule_error_analysis(cls, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]], extracted_evidence: str | None = None) -> str:
        """Rule-based error analysis when AI is unavailable."""
        parts = []
        if failures:
            first = failures[0]
            module = first.get("module") or ""
            name = first.get("name") or ""
            reason = str(first.get("reason") or "")
            parts.append(f"失败模块: {module}")
            parts.append(f"失败用例: {name}")
            # Extract key error type from reason
            error_type_match = re.search(r"(\w+(?:Exception|Error))", reason)
            if error_type_match:
                parts.append(f"异常类型: {error_type_match.group(1)}")
            # Extract key error message (first line of reason)
            first_reason_line = reason.split("\n")[0].strip()[:200] if reason else ""
            if first_reason_line:
                parts.append(f"关键报错: {first_reason_line}")
        else:
            # failures_json 为空时，回退到从 description/附件文本提取错误日志，
            # 否则仅靠 _evidence_notes 的首行会让报错分析过于单薄。
            extracted = extracted_evidence if extracted_evidence is not None else cls._extract_error_from_issue_evidence(issue_payload)
            if extracted:
                lines = [ln.strip() for ln in extracted.splitlines() if ln.strip()]
                if lines:
                    parts.append(f"关键报错: {_truncate(lines[0], 200)}")
                    # 附带其余错误行（最多再补 4 行），保留断言/堆栈上下文。
                    extra = [_truncate(ln, 200) for ln in lines[1:5]]
                    if extra:
                        parts.append("相关上下文:\n" + "\n".join(extra))
                # 尝试从描述里识别失败模块/用例（如 -m Module -t testcase）。
                desc = str(issue_payload.get("description") or "")
                mod = re.search(r"(?:^|\s)-m\s+(\S+)", desc)
                tc = re.search(r"(?:^|\s)-t\s+(\S+)", desc)
                if mod:
                    parts.append(f"失败模块: {mod.group(1)}")
                if tc:
                    parts.append(f"失败用例: {tc.group(1)}")
                # 断言类失败（Actual/Expected）专门点出，便于快速定位。
                assertion = re.search(r"Actual:\s*(.+?)\s*Expected:\s*(.+)", extracted)
                if assertion:
                    parts.append(f"断言失败: 实际={assertion.group(1).strip()} / 期望={assertion.group(2).strip()}")
        # Check references for similar resolved issues
        if references:
            high_refs = [r for r in references if r.get("similarity_level") == "high"]
            if high_refs:
                ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in high_refs[:3])
                parts.append(f"高度相似的历史单: {ref_ids}（可参考其解决方案）")
        for note in cls._evidence_notes(issue_payload, [], extracted_evidence):
            if note not in parts:
                parts.append(note)
        if not parts:
            parts.append("当前工单未提取到明确失败日志、附件错误或历史回复证据，暂无法给出事实依据充分的报错分析。")
        return "\n".join(parts)

    @classmethod
    def _rule_patch_direction(cls, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]], extracted_evidence: str | None = None) -> str:
        """Rule-based patch direction when AI is unavailable."""
        desc = str(issue_payload.get("description") or "")
        parts = []
        # Extract file paths mentioned in description
        file_paths = re.findall(r"[\./]?[\w/]+(?:config_user_types|config\.xml|\.xml|\.java|\.kt|\.prop|\.mk|\.cfg)[\w/.-]*", desc)
        if file_paths:
            unique_paths = list(dict.fromkeys(file_paths))[:5]
            parts.append("涉及文件:")
            for fp in unique_paths:
                parts.append(f"  - {fp}")
        # Suggest checking references
        if references:
            ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in references[:3])
            parts.append(f"建议参考历史单 {ref_ids} 中的补丁方案")
        if not parts:
            evidence = cls._evidence_notes(issue_payload, failures, extracted_evidence)
            if evidence:
                parts.append("未从现有证据中提取到明确补丁；需基于上述报错/附件/历史回复继续确认修改点。")
            else:
                parts.append("当前缺少可定位补丁的日志、附件或历史回复证据。")
        return "\n".join(parts)

    def _reply_draft(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]], solution: str = "", patch_direction: str = "") -> str:
        lines = [
            "Hi，问题已收到，初步分析如下：",
            "",
            f"- Redmine: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}",
        ]
        if failures:
            first = failures[0]
            reason_text = _truncate(first.get("reason") or "", 300)
            reason_lang = self._detect_code_lang(reason_text)
            lines.extend([
                f"- 失败模块: {first.get('module') or '-'}",
                f"- 失败用例: {first.get('name') or '-'}",
                f"- 关键报错: {self._html_code_block(reason_text, reason_lang)}",
            ])
        if references:
            lines.append("- 可参考历史单: " + ", ".join(f"#{ref['issue_id']}" for ref in references[:3]))

        # Add solution
        if solution:
            # Convert any markdown code blocks in solution to HTML
            solution_html = self._markdown_to_html_code_blocks(solution)
            lines.extend(["", "解决方法:", solution_html])

        # Add patch direction
        if patch_direction and patch_direction != "需要进一步分析具体日志和源码":
            patch_html = self._markdown_to_html_code_blocks(patch_direction)
            lines.extend(["", "补丁方向:", patch_html])

        lines.extend(["", "我会继续结合日志和历史修改记录确认根因，并同步后续处理结论。"])
        return "\n".join(lines)
