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
    """\u590d\u7528 storage._search_terms \u7684\u4e2d\u82f1\u6587\u5206\u8bcd\uff0c\u4fdd\u8bc1\u95ee\u7b54\u4e0e\u68c0\u7d22\u7528\u540c\u4e00\u5957\u89c4\u5219\u3002"""
    from features.notes.storage import _search_terms

    return _search_terms(question)


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

# 单段精炼输入上限与分段阈值（字符）。
_STRUCTURE_CHUNK = 6000
_STRUCTURE_LARGE_THRESHOLD = 8000  # 超过此长度走分段精炼。
# 大文档分段精炼的段数上限与并行度。段数越多 AI 调用越多越慢，故封顶。
_MAX_SEGMENTS = 8
_SEGMENT_PARALLELISM = 4


def _chunk_text(text: str, size: int) -> list[str]:
    """按大致段落边界把长文本切成 <= size 的块（尽量在换行处断开）。"""
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # 向后找最近的换行，避免把一行/一个命令从中间切断。
            nl = text.rfind("\n", start, end)
            if nl > start + size // 2:
                end = nl + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def structure_note(raw_text: str) -> dict[str, Any]:
    """结构化一条笔记。返回 {title, tags, summary, keywords, content}。

    短文本一次精炼；超长文本分段精炼后合并，避免只读到开头。
    失败/未配置时返回空字段，调用方降级（标题取首行）。
    """
    text = raw_text or ""
    if len(text) > _STRUCTURE_LARGE_THRESHOLD:
        return _structure_note_large(text)
    return _structure_note_single(text[:_STRUCTURE_CHUNK])


def _structure_note_single(text: str, *, is_segment: bool = False) -> dict[str, Any]:
    """精炼单段文本。is_segment=True 时不输出全局 title/summary（分段调用用）。"""
    prompt = (
        "你是知识笔记整理助手。把下面这段可能杂乱的笔记整理成结构化 Markdown，"
        "并输出严格 JSON（不要 markdown 代码块，不要多余文字），字段：\n"
        '- title: 简短标题（<=30字，中文）\n'
        '- tags: 2-5 个主题标签数组（如 ["adb","显示","帧率"]，小写无空格）\n'
        '- summary: 一句话摘要（<=60字）\n'
        '- keywords: 3-8 个关键词数组\n'
        '- content: 整理后的 Markdown 正文（命令用代码块，保留原始命令/路径/diff，'
        "补充必要分组小标题，不要臆造内容）\n\n"
        f"笔记原文：\n{text}\n\n只输出 JSON。"
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


def _structure_note_large(text: str) -> dict[str, Any]:
    """大文档分段精炼：每段独立精炼正文，再合并 title/tags/summary/keywords。

    性能：先按 _MAX_SEGMENTS 上限重新切块（避免超长文档切出几十段、串行调用几十秒到数分钟），
    再用线程池并行精炼各段。每段失败时用该段原文兜底，不丢内容。

    - 第一段产生全局 title/summary；
    - tags/keywords 取所有段的并集去重；
    - content 为各段精炼正文按顺序拼接。
    """
    # 按段数上限反推每段大小：总长 / 上限段数，但不小于单段阈值（保证每段不超模型输入上限）。
    n = len(text)
    if n <= _STRUCTURE_CHUNK:
        chunks = [text]
    else:
        target_chunk = max(_STRUCTURE_CHUNK, (n + _MAX_SEGMENTS - 1) // _MAX_SEGMENTS)
        chunks = _chunk_text(text, target_chunk)
    if not chunks:
        return {}

    # 并行精炼各段（保留顺序）。
    from concurrent.futures import ThreadPoolExecutor

    def _process(chunk: str) -> dict[str, Any]:
        seg = _structure_note_single(chunk)
        if seg:
            return seg
        # 失败兜底：原文进 content，不丢失。
        return {"content": chunk.strip(), "tags": "", "keywords": "", "title": "", "summary": ""}

    segments: list[dict[str, Any]] = [None] * len(chunks)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=_SEGMENT_PARALLELISM) as pool:
        for idx, result in enumerate(pool.map(_process, chunks)):
            segments[idx] = result
    segments = [s for s in segments if s]
    if not segments:
        return {}

    merged_content = "\n\n".join(s.get("content") or "" for s in segments if (s.get("content") or "").strip())
    # 全局标题/摘要取第一段；tags/keywords 全段并集去重。
    first = segments[0]
    tags_set: list[str] = []
    kw_set: list[str] = []

    def _add(items: list[str], sink: list[str]) -> None:
        for it in items:
            it = it.strip()
            if it and it not in sink:
                sink.append(it)

    for s in segments:
        _add([t for t in (s.get("tags") or "").split(",") if t.strip()], tags_set)
        _add([k for k in (s.get("keywords") or "").split(",") if k.strip()], kw_set)
    return {
        "title": first.get("title") or "",
        "tags": ",".join(tags_set[:8]),
        "summary": first.get("summary") or "",
        "keywords": ",".join(kw_set[:12]),
        "content": merged_content,
    }


def answer_question(question: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """基于召回的笔记片段回答问题。返回 {answer, source_note_ids}。"""
    if not contexts:
        return {"answer": "知识库中暂无相关笔记，无法回答。", "source_note_ids": []}
    blocks = []
    for i, ctx in enumerate(contexts, 1):
        # 大文档原文较长，每条取较大的最佳命中片段（默认 1500 偏小）。
        excerpt = _best_excerpt(ctx.get("content") or "", question, size=2800)
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
