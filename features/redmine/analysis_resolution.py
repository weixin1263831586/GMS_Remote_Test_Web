"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from typing import Any

from features.redmine.config import config_manager


logger = logging.getLogger(__name__)


PROCESS_ATTACHMENT_RE = re.compile(r"\.(zip|7z|rar|tar|tgz|gz|xml|txt|log|png|jpg|jpeg|webp|bmp|docx)$", re.IGNORECASE)
IMAGE_ATTACHMENT_RE = re.compile(r"\.(png|jpg|jpeg|webp|bmp)$", re.IGNORECASE)

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
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


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
                # Extract plain text before the first <pre> as solution description
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

    def _rule_summary(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
        if failures:
            first = failures[0]
            return f"{first.get('module') or '未知模块'} / {first.get('name') or '未知用例'} 失败：{_truncate(first.get('reason') or '', 180)}"
        return _truncate(issue_payload.get("description") or issue_payload.get("subject") or "未提取到描述", 240)

    def _rule_solution(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
        lines = []
        if failures:
            first = failures[0]
            lines.append(f"1. 失败模块: {first.get('module') or '-'}")
            lines.append(f"2. 失败用例: {first.get('name') or '-'}")
        lines.append("3. 待进一步分析确认解决方案。")
        if references:
            ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in references[:3])
            lines.append(f"4. 可参考历史单: {ref_ids}")
        return "\n".join(lines)

    def _rule_error_analysis(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
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
        # Check references for similar resolved issues
        if references:
            high_refs = [r for r in references if r.get("similarity_level") == "high"]
            if high_refs:
                ref_ids = ", ".join(f"#{r.get('issue_id')}" for r in high_refs[:3])
                parts.append(f"高度相似的历史单: {ref_ids}（可参考其解决方案）")
        if not parts:
            parts.append("暂无分析结果")
        return "\n".join(parts)

    def _rule_patch_direction(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
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
            parts.append("需要进一步分析具体日志和源码")
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
