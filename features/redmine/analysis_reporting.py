"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from features.redmine.analysis_common import _truncate


logger = logging.getLogger(__name__)


def _doc_line(value: Any) -> str:
    """Coerce a document line to a single str.

    AI output and decoded JSON fields (journal notes, solution, reply_draft, …)
    can occasionally be a list instead of a str; joining such an item would raise
    ``TypeError: sequence item N: expected str instance, list found``. This
    flattens lists (newline-joined) and stringifies everything else.
    """
    if isinstance(value, list):
        return "\n".join(_doc_line(v) for v in value)
    return str(value or "")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ReportingAnalysisMixin:
    @staticmethod
    def _markdown_to_html_code_blocks(text: str) -> str:
        """Convert markdown ```lang``` code blocks to <pre><code class="lang"> HTML blocks."""
        def _replace(match):
            lang = match.group(1) or ""
            code = match.group(2) or ""
            cls = f' class="{lang}"' if lang else ""
            return f"<pre><code{cls}>\n{code}</code></pre>"
        return re.sub(r"```(\w*)\n([\s\S]*?)```", _replace, str(text or ""))

    def _build_issue_document(self, item: dict[str, Any]) -> str:
        failures = item.get("failures_json") or []
        references = item.get("references_json") or []
        attachments = item.get("attachments_json") or []
        journals = item.get("journals_json") or []

        lines = [
            f"# Redmine #{item.get('issue_id')} - {item.get('subject')}",
            "",
            "## 基本信息",
            f"- 状态: {item.get('status_name') or '-'}",
            f"- 优先级: {item.get('priority_name') or '-'}",
            f"- 项目: {item.get('project_name') or '-'}",
            f"- 分类: {item.get('category') or '-'}",
            f"- 指派: {item.get('assigned_to_name') or '-'}",
            f"- 创建: {item.get('created_on') or '-'}",
            f"- 更新: {item.get('updated_on') or '-'}",
            "",
            "## 平台与版本",
            "| SoC | Android | 类型 | Component | 指派 | 创建 |",
            "|-----|---------|------|-----------|------|------|",
            f"| {item.get('soc_platform') or '-'} | {item.get('android_version') or '-'} | {self._detect_version_type(item.get('fixed_version') or '')} | {item.get('component') or '-'} | {item.get('assigned_to_name') or '-'} | {(item.get('created_on') or '-')[:10]} |",
            "",
            "## 问题描述",
            item.get("problem_description") or _truncate(item.get("description") or "", 4000) or "-",
            "",
            "## 报错信息",
            self._ensure_code_block(item.get("error_info") or "-", ""),
            "",
            "## 报错分析",
            item.get("error_analysis") or "-",
            "",
        ]

        # Add resolution assessment section for resolved/closed issues
        resolution_json = item.get("resolution_json")
        if resolution_json and resolution_json.get("has_resolution"):
            status_icon = {"verified": "✅", "resolved": "✓", "unclear": "⚠"}.get(resolution_json.get("status", ""), "")
            lines.extend([
                f"## {status_icon} 解决方案判定",
                f"- 判定结果: **{resolution_json.get('summary', '-')}**",
                f"- 方案提供者: {resolution_json.get('provider') or '-'} ({(resolution_json.get('provider_time') or '-')[:10]})",
            ])
            if resolution_json.get("confirmer"):
                lines.append(f"- 确认者: {resolution_json.get('confirmer')} ({(resolution_json.get('confirmer_time') or '-')[:10]}) — {resolution_json.get('confirm_note', '')[:80]}")
            lines.append("")

        lines.extend([
            "## 解决方法",
            item.get("solution") or "-",
            "",
            "## 解决补丁",
            item.get("patch_direction") or "-",
            "",
            "## 参考Redmine",
        ])

        if references:
            for ref in references:
                level = ref.get("similarity_level") or ""
                score = ref.get("score") or 0
                level_label = {"high": "🔴 高", "medium": "🟡 中"}.get(level, "⚪")
                lines.append(f"- {level_label} #{ref.get('issue_id')} (相似度: {score}) {ref.get('reason', '')} | {ref.get('subject', '')}")
        else:
            lines.append("- 暂无参考单")

        lines.extend(["", "## 附件分析"])
        if attachments:
            for att in attachments:
                analysis = att.get("analysis_json") or {}
                details = analysis.get("details") or {}
                attachment_id = att.get("attachment_id") or att.get("id") or "-"
                lines.append(
                    f"- {att.get('filename') or '-'} (id={attachment_id}, size={att.get('filesize') or 0}, status={att.get('status') or '-'})"
                )
                if details.get("type") == "image":
                    size_text = f"{details.get('width') or '-'}x{details.get('height') or '-'}"
                    lines.append(f"  - 截图信息: {size_text}, format={details.get('format') or '-'}, mode={details.get('mode') or '-'}")
                elif details.get("type"):
                    lines.append(f"  - 解析类型: {details.get('type')}")
                text_excerpt = analysis.get("text_excerpt") or ""
                if text_excerpt:
                    lines.append(f"  - 文本摘录: {_truncate(text_excerpt, 500)}")
                if att.get("error"):
                    lines.append(f"  - 错误: {att.get('error')}")
        else:
            lines.append("- 无附件")

        lines.extend(["", "## 报告失败项"])
        if failures:
            for idx, failure in enumerate(failures[:20], 1):
                lines.extend([
                    f"### {idx}. {failure.get('name') or 'Unknown'}",
                    f"- 模块: {failure.get('module') or '-'}",
                    f"- 原因:\n{self._ensure_code_block(_truncate(failure.get('reason') or '', 1200), '')}",
                    "",
                ])
        else:
            lines.append("- 未解析到报告失败项")

        lines.extend(["", "## 历史记录"])
        for journal in journals[-15:]:
            lines.extend([
                f"### {journal.get('created_on')} {journal.get('user')}",
                journal.get("notes") or "-",
                "",
            ])

        lines.extend(["", "## 建议回复草稿", item.get("reply_draft") or "", ""])

        # Coerce every item to a single str so the document always builds.
        return "\n".join(_doc_line(x) for x in lines).strip() + "\n"

    def _build_run_report(self, run_id: str, issues: list[dict[str, Any]]) -> str:
        lines = [
            f"# RedmineAgent 日报 {run_id}",
            "",
            f"- 生成时间: {_now_iso()}",
            f"- 处理问题数: {len(issues)}",
            "",
            "## 概览",
        ]

        high_priority = [i for i in issues if any(kw in (i.get("priority_name") or "") for kw in ("紧急", "Urgent", "高"))]
        medium_priority = [i for i in issues if any(kw in (i.get("priority_name") or "") for kw in ("正常", "Normal"))]

        lines.extend([
            f"- 高优先级: {len(high_priority)}",
            f"- 普通: {len(medium_priority)}",
            f"- 其他: {len(issues) - len(high_priority) - len(medium_priority)}",
            "",
        ])

        lines.append("## 新增问题")
        if not issues:
            lines.append("- 过去 24 小时没有扫描到 assigned_to 我的新增 Redmine 单")
        for item in issues:
            refs = item.get("references_json") or []
            ref_display = ", ".join(f'#{r.get("issue_id")}({r.get("similarity_level", "")})' for r in refs[:3]) if refs else "暂无"
            lines.extend([
                f"### #{item.get('issue_id')} {item.get('subject')}",
                f"- 优先级: {item.get('priority_name') or '-'}",
                f"- 分类: {item.get('category') or '-'}",
                f"- 报错信息:\n{self._ensure_code_block(_truncate(item.get('error_info') or '-', 200), '')}",
                f"- 报错分析: {_truncate(item.get('error_analysis') or '-', 200)}",
                f"- 解决方向: {_truncate(item.get('solution') or '-', 200)}",
                f"- 参考单: {ref_display}",
                "",
            ])

        if high_priority:
            lines.extend(["", "## 待关注（高优先级）"])
            for item in high_priority:
                lines.append(f"- #{item.get('issue_id')} {item.get('subject')} — {item.get('error_analysis') or '-'}")

        return "\n".join(lines).strip() + "\n"

    # Shared helpers (used by router for field enrichment)

    @staticmethod
    def extract_error_from_failures(failures: list) -> str:
        """Format failure items into a readable error summary."""
        if not failures:
            return ""
        parts = []
        for f in failures[:5]:
            name = f.get("name") or ""
            reason = f.get("reason") or ""
            module = f.get("module") or ""
            parts.append(f"[{module}] {name}: {reason[:200]}")
        return "\n".join(parts)

    @staticmethod
    def extract_description(issue: dict) -> str:
        """Extract a short description from an issue record."""
        desc = issue.get("description") or ""
        if desc:
            return desc[:500]
        return issue.get("subject") or ""
