"""笔记 AI：结构化整理、打标签、摘要、智能问答。

复用 redmine analysis_ai.py 的配置加载与 HTTP 调用模式（ai_models 配置 +
ANTHROPIC_* 环境变量覆盖），自包含实现，不依赖 redmine 的 analyzer 类。
AI 全程可选：调用失败或未配置时返回空结构，由 service 层降级。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from foundation.config import config_manager

logger = logging.getLogger(__name__)

AI_MODEL_TIMEOUT = 120
AI_MODEL_MAX_TOKENS = 2400


def load_ai_config() -> dict[str, Any]:
    """加载 AI 配置：ai_models 配置 + ANTHROPIC_* 环境变量覆盖（env 优先）。"""
    config = config_manager.load_config().get("ai_models", {}) or {}
    env_base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    env_model = os.getenv("ANTHROPIC_MODEL", "").strip()
    env_token = os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip()
    if not (env_base_url or env_model or env_token):
        return config
    provider = dict((config.get("providers") or {}).get(config.get("primary_provider") or "", {}))
    provider.update({"name": "GLM-Local", "enabled": True, "api_format": "anthropic"})
    if env_base_url:
        provider["base_url"] = env_base_url
    if env_model:
        provider["model"] = env_model
    if env_token:
        provider["api_key"] = env_token
    return {
        **config,
        "enabled": True,
        "primary_provider": "env_anthropic",
        "providers": {"env_anthropic": provider},
    }


def _get_provider(config: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    if not config.get("enabled"):
        return None
    providers = config.get("providers") or {}
    primary = config.get("primary_provider")
    if primary and providers.get(primary, {}).get("enabled"):
        return primary, providers[primary]
    for name, prov in providers.items():
        if prov.get("enabled"):
            return name, prov
    return None


def _api_format(provider: dict[str, Any]) -> str:
    fmt = provider.get("api_format")
    if fmt:
        return fmt
    base_url = (provider.get("base_url") or "").lower()
    if "anthropic" in base_url or base_url.endswith("/messages"):
        return "anthropic"
    return "openai"


def call_model(prompt: str, *, max_tokens: int = AI_MODEL_MAX_TOKENS) -> str:
    """调用模型，返回原始文本。失败返回 ''。"""
    config = load_ai_config()
    got = _get_provider(config)
    if not got:
        return ""
    _provider_name, provider = got
    base_url = provider.get("base_url") or ""
    model = provider.get("model") or ""
    api_key = provider.get("api_key") or ""
    if not base_url or not model:
        return ""
    fmt = _api_format(provider)
    if fmt == "anthropic":
        url = base_url if base_url.endswith("/messages") else f"{base_url}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        data = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    else:
        url = (
            base_url
            if base_url.endswith(("/chat/completions", "/completions"))
            else f"{base_url}/v1/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = {
            "model": model,
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    try:
        resp = requests.post(url, headers=headers, json=data, timeout=AI_MODEL_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        logger.error("[Notes] AI 请求失败: %s", exc)
        return ""
    if resp.status_code != 200:
        logger.error("[Notes] AI HTTP %s: %s", resp.status_code, resp.text[:300])
        return ""
    return _extract_text(resp.json(), fmt)


def _extract_text(payload: dict[str, Any], fmt: str) -> str:
    try:
        if fmt == "anthropic":
            blocks = payload.get("content") or []
            return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        # openai
        choices = payload.get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content", "") or ""
    except Exception as exc:
        logger.debug("[Notes] 解析 AI 响应失败: %s", exc)
    return ""


def _parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        result = json.loads(match.group(0) if match else raw)
        return result if isinstance(result, dict) else None
    except (json.JSONDecodeError, AttributeError):
        return None


def _question_terms(question: str) -> list[str]:
    text = (question or "").strip().lower()
    if not text:
        return []
    terms: list[str] = []
    terms.extend(t for t in re.split(r"\s+", text) if t)
    terms.extend(re.findall(r"[a-z0-9][a-z0-9_.+-]*", text))
    terms.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    for cjk in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        terms.extend(cjk[i : i + 2] for i in range(len(cjk) - 1))
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def _best_excerpt(content: str, question: str, size: int = 1500) -> str:
    text = content or ""
    if len(text) <= size:
        return text
    lower = text.lower()
    hit = -1
    for term in _question_terms(question):
        hit = lower.find(term.lower())
        if hit >= 0:
            break
    if hit < 0:
        return text[:size]
    start = max(0, hit - size // 3)
    end = min(len(text), start + size)
    start = max(0, end - size)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


# ---------- 两个业务 prompt ----------
def structure_note(raw_text: str) -> dict[str, Any]:
    """结构化一条笔记。返回 {title, tags, summary, keywords, content}。

    失败/未配置时返回空字段，调用方降级（标题取首行）。
    """
    truncated = raw_text[:6000]
    prompt = (
        "你是知识笔记整理助手。把下面这段可能杂乱的笔记整理成结构化 Markdown，"
        "并输出严格 JSON（不要 markdown 代码块，不要多余文字），字段：\n"
        '- title: 简短标题（<=30字，中文）\n'
        '- tags: 2-5 个主题标签数组（如 ["adb","显示","帧率"]，小写无空格）\n'
        '- summary: 一句话摘要（<=60字）\n'
        '- keywords: 3-8 个关键词数组\n'
        '- content: 整理后的 Markdown 正文（命令用代码块，保留原始命令/路径/diff，'
        "补充必要分组小标题，不要臆造内容）\n\n"
        f"笔记原文：\n{truncated}\n\n只输出 JSON。"
    )
    raw = call_model(prompt)
    parsed = _parse_json(raw)
    if not parsed:
        return {}
    return {
        "title": str(parsed.get("title") or "").strip()[:200],
        "tags": ",".join(str(t).strip() for t in (parsed.get("tags") or []) if str(t).strip()),
        "summary": str(parsed.get("summary") or "").strip(),
        "keywords": ",".join(str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()),
        "content": str(parsed.get("content") or "").strip(),
    }


def answer_question(question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """基于召回的笔记片段回答问题。返回 {answer, source_note_ids}。"""
    if not contexts:
        return {"answer": "知识库中暂无相关笔记，无法回答。", "source_note_ids": []}
    blocks = []
    for i, ctx in enumerate(contexts, 1):
        excerpt = _best_excerpt(ctx.get("content") or "", question)
        blocks.append(
            f"[笔记{i}] 标题: {ctx.get('title', '')}\n标签: {ctx.get('tags', '')}\n内容:\n{excerpt}"
        )
    joined = "\n\n---\n\n".join(blocks)
    ids = [c.get("note_id") for c in contexts if c.get("note_id")]
    prompt = (
        "你是知识笔记问答助手。根据下面的笔记片段回答用户问题。"
        "优先引用笔记中的具体命令/步骤；若笔记不足以回答，如实说明。"
        "输出严格 JSON（不要代码块）：\n"
        '- answer: 回答正文（Markdown，可含命令代码块）\n'
        f"- 笔记片段：\n{joined}\n\n"
        f"用户问题：{question}\n\n只输出 JSON，字段 answer。"
    )
    raw = call_model(prompt, max_tokens=1600)
    parsed = _parse_json(raw)
    answer = (parsed or {}).get("answer") if parsed else None
    if not answer:
        # 降级：直接把最相关笔记摘要拼出来。
        answer = "已从全库找到相关笔记。当前 AI 模型未返回结构化回答，先列出命中片段：\n\n" + "\n\n".join(
            f"**{c.get('title', '')}**\n{_best_excerpt(c.get('content', ''), question, 700)}" for c in contexts[:3]
        )
    return {"answer": str(answer), "source_note_ids": ids}
