"""笔记业务编排：创建笔记（AI 结构化）、上传文件、智能问答。"""

from __future__ import annotations

import logging
from typing import Any

from . import ai
from .parsers import parse_file
from .storage import NotesStorage

logger = logging.getLogger(__name__)

# 问答召回的笔记条数。全库问答需要更多候选，再交给模型压缩。
_ASK_RECALL_TOP = 8


class NotesService:
    def __init__(self, storage: NotesStorage | None = None) -> None:
        self.storage = storage or NotesStorage()

    # ---------- 创建文本笔记 ----------
    def create_from_text(
        self,
        user_id: str,
        content: str,
        notebook: str = "",
        links: Any = None,
        related_module: str = "",
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"error": "笔记内容不能为空"}

        # AI 层内部已做分段精炼（超长文档切块合并），这里传全文不截断。
        structured = ai.structure_note(content)
        if structured and structured.get("content"):
            # AI 成功：用结构化结果，原始文本留档。
            payload = {
                "title": structured.get("title") or _first_line(content),
                "content": structured["content"],
                "raw_content": content,
                "tags": structured.get("tags", ""),
                "summary": structured.get("summary", ""),
                "keywords": structured.get("keywords", ""),
                "source": "manual",
            }
        else:
            # AI 未配置/失败：降级，标题取首行，正文用原文。
            payload = {
                "title": _first_line(content),
                "content": content,
                "raw_content": content,
                "source": "manual",
            }
        if notebook:
            payload["notebook"] = notebook
        # links / related_module 由调用方指定（如「存为Wiki」按钮），AI 结构化不覆盖。
        if links is not None:
            payload["links"] = links
        if related_module:
            payload["related_module"] = related_module
        return self.storage.create_note(user_id, payload)

    # ---------- 上传文件 ----------
    def create_from_file(
        self,
        user_id: str,
        file_path: str,
        filename: str,
        notebook: str = "",
        links: Any = None,
        related_module: str = "",
    ) -> dict[str, Any]:
        parsed = parse_file(file_path, filename)
        text = (parsed.get("text") or "").strip()
        if not text:
            return {"error": f"无法从文件 {filename} 提取文本（可能为空或格式不支持）"}

        # AI 层内部已做分段精炼（超长文档切块合并），这里传全文不截断。
        structured = ai.structure_note(text)
        if structured and structured.get("content"):
            payload = {
                "title": structured.get("title") or _filename_stem(filename),
                "content": structured["content"],
                "raw_content": text,
                "tags": structured.get("tags", ""),
                "summary": structured.get("summary", ""),
                "keywords": structured.get("keywords", ""),
                "source": parsed.get("source") or "txt",
                "source_file": filename,
            }
        else:
            payload = {
                "title": _filename_stem(filename),
                "content": text,
                "raw_content": text,
                "source": parsed.get("source") or "txt",
                "source_file": filename,
            }
        if notebook:
            payload["notebook"] = notebook
        if links is not None:
            payload["links"] = links
        if related_module:
            payload["related_module"] = related_module
        return self.storage.create_note(user_id, payload)

    # ---------- 问答 ----------
    def ask(self, user_id: str, question: str, limit: int = _ASK_RECALL_TOP) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"answer": "请输入问题。", "source_note_ids": [], "contexts": []}
        limit = max(1, min(int(limit or _ASK_RECALL_TOP), 20))
        contexts = self.storage.search(user_id, question, limit=limit)
        # 问答上下文优先用原文全文（raw_content）：大文档的命中常在原文中段，
        # 只看 AI 精炼后的 content 会漏掉细节。answer_question 内部会按问题取最佳片段。
        for ctx in contexts:
            raw = (ctx.get("raw_content") or "").strip()
            if raw:
                ctx["content"] = raw
        result = ai.answer_question(question, contexts)
        result["contexts"] = [
            {"note_id": c.get("note_id"), "title": c.get("title"), "tags": c.get("tags")}
            for c in contexts
        ]
        return result


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "无标题"


def _filename_stem(filename: str) -> str:
    import os

    return os.path.splitext(os.path.basename(filename))[0] or filename
