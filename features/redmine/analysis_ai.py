"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import requests

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



class AiAnalysisMixin:
    async def _summarize_with_model(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> dict[str, Any]:
        config = self._load_ai_config()
        if self.ai_analyzer_factory is None:
            return {"success": False, "error": "AI model not configured"}
        analyzer = self.ai_analyzer_factory(config)
        provider_name = analyzer.get_primary_provider()
        if not provider_name:
            return {"success": False, "error": "AI model not configured"}
        provider = config.get("providers", {}).get(provider_name, {})
        prompt = self._build_ai_prompt(issue_payload, failures, references)
        try:
            return await asyncio.to_thread(self._call_model, analyzer, provider_name, provider, prompt)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def _load_ai_config(self) -> dict[str, Any]:
        if self._ai_config_cache is not None:
            return self._ai_config_cache

        config = config_manager.load_config().get("ai_models", {}) or {}
        env_base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
        env_model = os.getenv("ANTHROPIC_MODEL", "").strip()
        env_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
        if not (env_base_url or env_model or env_token):
            self._ai_config_cache = config
            return config

        provider = dict((config.get("providers") or {}).get(config.get("primary_provider") or "", {}))
        provider.update({
            "name": "GLM-5.1 Local",
            "enabled": True,
            "api_format": "anthropic",
        })
        if env_base_url:
            provider["base_url"] = env_base_url
        if env_model:
            provider["model"] = env_model
        if env_token:
            provider["api_key"] = env_token

        result = {
            **config,
            "enabled": True,
            "primary_provider": "env_anthropic",
            "providers": {"env_anthropic": provider},
        }
        self._ai_config_cache = result
        return result

    def _call_model(self, analyzer: Any, provider_name: str, provider: dict[str, Any], prompt: str) -> dict[str, Any]:
        """Call the AI model and parse the structured seven-field JSON response."""
        raw = self._call_model_raw(analyzer, provider_name, provider, prompt)
        try:
            match = re.search(r"\{.*\}", raw, re.S)
            result = json.loads(match.group(0) if match else raw)
            if isinstance(result, str):
                result = json.loads(result)
            # Handle nested summary in old format
            if isinstance(result.get("summary"), str) and result["summary"].lstrip().startswith("{"):
                nested = json.loads(result["summary"])
                nested["success"] = True
                nested["provider"] = provider_name
                return nested
            result["success"] = True
            result["provider"] = provider_name
            return result
        except Exception:
            return {"success": False, "provider": provider_name, "summary": raw[:1200], "reply_draft": ""}

    def _call_model_raw(self, analyzer: Any, provider_name: str, provider: dict[str, Any], prompt: str) -> str:
        """Call the AI model and return raw text response.

        Reuses UniversalAIAnalyzer's HTTP request logic to avoid duplicating
        endpoint construction, header setup, and response parsing.
        """
        api_key = provider.get("api_key") or ""
        base_url = provider.get("base_url") or ""
        model = provider.get("model") or ""
        if not base_url or not model:
            return ""
        api_format = analyzer._get_api_format(provider_name, provider)

        if api_format == analyzer.API_FORMAT_ANTHROPIC:
            url = f"{base_url}/v1/messages" if not base_url.endswith("/messages") else base_url
            headers = {"x-api-key": api_key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            data = {"model": model, "max_tokens": AI_MODEL_MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}
        else:
            url = f"{base_url}/v1/chat/completions" if not base_url.endswith(("/chat/completions", "/completions")) else base_url
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            data = {"model": model, "temperature": 0.2, "max_tokens": AI_MODEL_MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}

        try:
            resp = requests.post(url, headers=headers, json=data, timeout=AI_MODEL_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            logger.error("[RedmineAgent] model request failed: %s", exc)
            return ""
        if resp.status_code != 200:
            logger.error("[RedmineAgent] model HTTP %s: %s", resp.status_code, resp.text[:300])
            return ""

        # Delegate response parsing to the analyzer's robust parser
        return analyzer._parse_response_raw(resp.json(), api_format)

    # AI Prompt — structured seven-field output

    def _build_ai_prompt(self, issue_payload: dict[str, Any], failures: list[dict[str, Any]], references: list[dict[str, Any]]) -> str:
        journals = issue_payload.get("journals_json") or []
        journals_text = "\n".join(
            f"[{j.get('created_on', '')}] {j.get('user', '')}: {j.get('notes', '')}"
            for j in journals[-5:]
        )[:2000]

        ref_text = "\n".join(
            f"- #{r.get('issue_id')} ({r.get('similarity_level', '')} 相似度{r.get('score', 0)}) {r.get('subject', '')} | {r.get('reason', '')}"
            for r in (references or [])[:5]
        )

        return f"""你是 Rockchip FAE 团队的 Android GMS/CTS/VTS/GTS 问题分析专家。
请分析以下 Redmine 问题并返回结构化 JSON。

## 问题信息
Redmine: #{issue_payload.get('issue_id')} {issue_payload.get('subject')}
描述:
{_truncate(issue_payload.get('description') or '', 3000)}

## 附件报告失败项
{json.dumps(failures[:10], ensure_ascii=False)[:8000]}

## 历史参考单
{ref_text or '暂无'}

## 历史沟通记录
{journals_text or '暂无'}

---

请严格按以下JSON格式返回（不要markdown标记，不要解释文字，直接返回JSON）：

{{
  "title": "简洁中文标题（含平台、Android版本、模块名、失败用例）",
  "problem_description": "问题现象的中文描述：客户报告了什么、什么场景下触发、什么设备/平台上",
  "error_info": "提取的核心报错信息，保留原始英文。长报错堆栈用```代码块包裹，包含异常类型、关键错误消息、堆栈中的失败位置",
  "error_analysis": "根因分析：为什么会触发此错误，底层机制是什么，与什么配置或代码相关",
  "solution": "具体的解决步骤（编号列表），包含文件路径和验证命令。shell命令用```shell代码块包裹（命令行$前缀），配置修改用```xml或```diff代码块",
  "patch_direction": "补丁方向。如涉及文件修改，必须用unified diff格式包裹在```diff代码块中，示例:\\n```diff\\n--- a/path/to/file\\n+++ b/path/to/file\\n@@ -1,4 +1,4 @@\\n-旧内容\\n+新内容\\n```\\n如涉及shell命令，用```shell代码块（$前缀）。XML配置用```xml代码块",
  "reference_redmine": [
    {{"issue_id": 12345, "reason": "同模块同失败原因，已解决"}}
  ]
}}

分析要点：
1. 从失败项的 module 和 name 中识别测试模块和用例
2. 从 reason/stack_trace 中提取实际的异常类型和触发点
3. 结合问题描述中客户提到的文件路径（如 config_user_types.xml）分析
4. 从参考单中找出同模块或同失败模式的已解决问题
5. patch_direction 要给出具体的文件路径和修改内容，不要笼统说"修改配置"
6. reference_redmine 从上面的历史参考单中选择确实相关的，并说明为什么相关
7. 代码块格式：diff内容用```diff（必须有--- a/file, +++ b/file, @@...@@, -/+行），shell命令用```shell（$前缀），XML配置用```xml，纯报错堆栈用```
"""

    # Code block formatting helpers

    @staticmethod
    def _ensure_code_block(text: str, lang: str = "") -> str:
        """Wrap text in a markdown code block if not already wrapped."""
        text = str(text or "").strip()
        if not text or text.startswith("```"):
            return text
        return f"```{lang}\n{text}\n```"

    @staticmethod
    def _wrap_patch_direction(text: str) -> str:
        """Intelligently wrap patch_direction in the appropriate code block."""
        text = str(text or "").strip()
        if not text or text.startswith("```"):
            return text
        # Detect unified diff patterns
        if re.search(r"^---\s+[ab]/", text, re.M) or re.search(r"^\+\+\+\s+[ab]/", text, re.M):
            return f"```diff\n{text}\n```"
        # Detect shell command patterns ($ prefix)
        if re.search(r"^\$\s+", text, re.M):
            return f"```shell\n{text}\n```"
        # Detect XML content
        if re.search(r"<\?xml|<[\w:-]+\s+[^>]*>", text):
            return f"```xml\n{text}\n```"
        # Default: wrap as generic code
        return f"```\n{text}\n```"

    @staticmethod
    def _html_code_block(text: str, lang: str = "") -> str:
        """Wrap text in <pre><code class="lang"> HTML block for Redmine display."""
        text = str(text or "").strip()
        if not text:
            return ""
        cls = f' class="{lang}"' if lang else ""
        return f"<pre><code{cls}>\n{text}\n</code></pre>"

    @staticmethod
    def _detect_code_lang(text: str) -> str:
        """Detect code language from content patterns."""
        text = str(text or "")
        if re.search(r"^---\s+[ab]/", text, re.M) or re.search(r"^\+\+\+\s+[ab]/", text, re.M):
            return "diff"
        if re.search(r"^\$\s+", text, re.M):
            return "shell"
        if re.search(r"<\?xml|<[\w:-]+\s+[^>]*>", text):
            return "xml"
        return ""
