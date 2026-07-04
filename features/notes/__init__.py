"""个人知识笔记 feature。

支持粘贴文本笔记、上传 PDF/TXT/MD/代码/DOCX/图片文件自动解析，AI 自动结构化
整理 + 打标签 + 摘要，FTS5 全文检索 + 智能问答。镜像 Redmine 知识库模式。
"""

from __future__ import annotations

from . import knowledge_api
from .api import page_router, router

__all__ = ["router", "page_router", "knowledge_api"]
