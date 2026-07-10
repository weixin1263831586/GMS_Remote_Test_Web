from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from features.assistant import get_universal_analyzer

from .parsers import parse_file
from .storage import KnowledgeStore


def _title_from_text(text: str, fallback: str = "无标题") -> str:
    for line in (text or "").splitlines():
        value = line.strip().lstrip("#").strip()
        if value:
            return value[:120]
    return fallback


def _summary(text: str, size: int = 220) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value[:size] + ("..." if len(value) > size else "")


def _auto_tags(text: str, limit: int = 6) -> list[str]:
    tags: list[str] = []
    patterns = [
        r"\bRK\d{4,}\b",
        r"\b(?:CTS|GTS|VTS|STS|BTS|Mainline|GMS)\b",
        r"\bAndroid\s?\d{1,2}\b",
        r"[\u4e00-\u9fff]{2,6}",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text or "", flags=re.IGNORECASE):
            tag = re.sub(r"\s+", "", str(match)).strip()
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= limit:
                return tags
    return tags


class KnowledgeService:
    def __init__(self, store: KnowledgeStore | None = None) -> None:
        self.store = store or KnowledgeStore()

    def create_doc_from_text(
        self,
        user_id: str,
        *,
        space_id: str,
        title: str = "",
        content: str,
        parent_id: str = "",
        tags: Any = None,
        links: list[dict[str, Any]] | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"error": "文档内容不能为空"}
        tag_list = tags if tags is not None else _auto_tags(content)
        return self.store.create_doc(
            user_id,
            space_id=space_id,
            parent_id=parent_id,
            title=title or _title_from_text(content),
            content_md=content,
            raw_content=content,
            summary=_summary(content),
            tags=tag_list,
            source=source,
            links=links or [],
        )

    def create_doc_from_file(
        self,
        user_id: str,
        *,
        space_id: str,
        file_path: str,
        filename: str,
        parent_id: str = "",
        tags: Any = None,
    ) -> dict[str, Any]:
        parsed = parse_file(file_path, filename)
        text = (parsed.get("text") or "").strip()
        if not text:
            return {"error": f"无法从文件 {filename} 提取文本"}
        doc = self.store.create_doc(
            user_id,
            space_id=space_id,
            parent_id=parent_id,
            title=_title_from_text(text, Path(filename).stem or filename),
            content_md=text,
            raw_content=text,
            summary=_summary(text),
            tags=tags if tags is not None else _auto_tags(text),
            source=parsed.get("source") or "upload",
            source_file=filename,
        )
        self.store.add_attachment(
            user_id,
            doc["doc_id"],
            source_path=file_path,
            original_name=filename,
            extracted_text=text,
        )
        return self.store.get_doc(user_id, doc["doc_id"]) or doc

    def ask(self, user_id: str, question: str, *, space_id: str = "", limit: int = 8) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"answer": "请输入问题。", "contexts": []}
        contexts = self.store.retrieve_contexts(user_id, question, space_id=space_id, limit=max(1, min(limit, 12)))
        if not contexts:
            return {"answer": "没有检索到相关知识文档。", "contexts": []}
        prompt = self._build_rag_prompt(question, contexts)
        system = (
            "你是个人知识库 AI Agent。只能基于给定知识库片段回答；"
            "如果片段不足，明确说明缺口。回答要给出可执行步骤、关键词和引用来源编号。"
        )
        try:
            result = get_universal_analyzer().generate(
                prompt,
                system_prompt=system,
                max_tokens=1800,
                preferred_provider="glm_local",
            )
        except Exception as exc:
            result = {"success": False, "error": str(exc)}

        if result.get("success") and result.get("content"):
            answer = result["content"]
            mode = "ai"
            provider = result.get("provider")
            error = ""
        else:
            lines = [f"AI 不可用，先返回检索到的相关片段。原因：{result.get('error') or '未配置 AI'}", ""]
            for idx, ctx in enumerate(contexts[:5], 1):
                lines.append(f"[{idx}] {ctx.get('title')}: {_summary(ctx.get('snippet') or ctx.get('summary') or '', 220)}")
            answer = "\n".join(lines)
            mode = "retrieval"
            provider = ""
            error = result.get("error") or ""

        return {
            "answer": answer,
            "mode": mode,
            "provider": provider,
            "error": error,
            "contexts": contexts,
        }

    @staticmethod
    def _build_rag_prompt(question: str, contexts: list[dict[str, Any]]) -> str:
        blocks = []
        for idx, ctx in enumerate(contexts, 1):
            tags = ", ".join(ctx.get("tags") or [])
            snippet = str(ctx.get("snippet") or "").strip()
            blocks.append(
                f"[{idx}] 标题: {ctx.get('title') or '无标题'}\n"
                f"标签: {tags or '-'}\n"
                f"片段:\n{snippet}"
            )
        return (
            f"用户问题：{question}\n\n"
            "知识库片段如下：\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n\n请基于这些片段回答。若用户问的是测试主题（如 GTS/CTS/GMS），"
              "优先总结：是什么、入口/前置条件、执行或排查步骤、常见关键词、需要继续查看哪些文档。"
        )
