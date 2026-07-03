"""笔记 API 路由。prefix=/api/notes"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse

from features.users import get_client_id_from_request
from foundation.config import settings
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response
from foundation.uploads import save_upload_to_path

from .service import NotesService
from .storage import UPLOAD_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notes")
page_router = APIRouter()

_service = NotesService()

# 上传大小上限 100MB（笔记文件通常远小于此）。
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
LIST_CONTENT_PREVIEW_CHARS = 600


def _user(request: Request) -> str:
    # get_client_id_from_request 对登录用户返回 user.id，匿名用户回退 username@ip。
    return get_client_id_from_request(request)


def _list_preview(note: dict[str, Any]) -> dict[str, Any]:
    """列表/搜索页只返回预览，避免大 PDF 正文压垮前端。详情接口仍返回全文。"""
    item = dict(note)
    content = str(item.get("content") or "")
    raw_content = str(item.get("raw_content") or "")
    item["content"] = content[:LIST_CONTENT_PREVIEW_CHARS]
    item["raw_content"] = raw_content[:LIST_CONTENT_PREVIEW_CHARS] if raw_content else ""
    item["content_truncated"] = len(content) > LIST_CONTENT_PREVIEW_CHARS
    return item


# ==================== 页面 ====================
@page_router.get("/notes", response_class=HTMLResponse)
async def notes_page():
    # 笔记页是 shell.html 内联 page-content，这里仅作为占位路由（不实际渲染主体）。
    return HTMLResponse("<!-- notes page is rendered inline in shell.html -->")


# ==================== 笔记本 / 标签 ====================
@router.get("/meta/notebooks")
@handle_api_errors
async def list_notebooks(request: Request):
    user_id = _user(request)
    return success_response(data={"notebooks": _service.storage.list_notebooks(user_id)})


@router.get("/meta/tags")
@handle_api_errors
async def list_tags(request: Request):
    user_id = _user(request)
    return success_response(data={"tags": _service.storage.list_tags(user_id)})


# ==================== 笔记 CRUD ====================
@router.post("")
@handle_api_errors
async def create_note(request: Request):
    """创建文本笔记（粘贴）。body: {content, notebook?}"""
    user_id = _user(request)
    data = await request.json()
    content = str(data.get("content") or "")
    notebook = str(data.get("notebook") or "")
    if not content.strip():
        return error_response("笔记内容不能为空", 400)
    note = await asyncio.to_thread(_service.create_from_text, user_id, content, notebook)
    if isinstance(note, dict) and note.get("error"):
        return error_response(note["error"], 400)
    return success_response(data=note, message="笔记已创建")


@router.get("")
@handle_api_errors
async def list_notes(
    request: Request,
    notebook: str = Query(""),
    tag: str = Query(""),
    q: str = Query(""),
    limit: int = Query(200, ge=1, le=500),
):
    """列出笔记。q 非空时走全文检索，否则按 notebook/tag 过滤。"""
    user_id = _user(request)
    if q.strip():
        notes = _service.storage.search(user_id, q.strip(), limit=limit)
    else:
        notes = _service.storage.list_notes(user_id, notebook=notebook, tag=tag, limit=limit)
    return success_response(data={"notes": [_list_preview(note) for note in notes], "count": len(notes)})


@router.get("/{note_id}")
@handle_api_errors
async def get_note(request: Request, note_id: str):
    user_id = _user(request)
    note = _service.storage.get_note(user_id, note_id)
    if not note:
        return error_response("笔记不存在", 404)
    return success_response(data=note)


@router.put("/{note_id}")
@handle_api_errors
async def update_note(request: Request, note_id: str):
    user_id = _user(request)
    data = await request.json()
    allowed = {k: v for k, v in data.items() if k in ("notebook", "title", "content", "tags", "summary", "keywords")}
    note = _service.storage.update_note(user_id, note_id, allowed)
    if not note:
        return error_response("笔记不存在", 404)
    return success_response(data=note, message="已更新")


@router.delete("/{note_id}")
@handle_api_errors
async def delete_note(request: Request, note_id: str):
    user_id = _user(request)
    ok = _service.storage.delete_note(user_id, note_id)
    if not ok:
        return error_response("笔记不存在", 404)
    return success_response(message="已删除")


# ==================== 文件上传 ====================
@router.post("/upload")
@handle_api_errors
async def upload_note_file(
    request: Request,
    file: UploadFile | None = File(None),
    notebook: str = Form(""),
):
    """上传文件 → 解析 → AI 结构化 → 存为笔记。支持 PDF/TXT/MD/代码/DOCX/图片。"""
    if not file or not file.filename:
        return error_response("未提供文件", 400)

    user_id = _user(request)
    safe_name = os.path.basename(file.filename)
    note_tmp_id = uuid.uuid4().hex[:12]
    dest_dir = Path(UPLOAD_DIR) / user_id / note_tmp_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    try:
        await save_upload_to_path(file, str(dest_path), MAX_UPLOAD_SIZE)
    except ValueError as exc:
        return error_response(str(exc), 400)

    note = await asyncio.to_thread(_service.create_from_file, user_id, str(dest_path), safe_name, notebook)
    if isinstance(note, dict) and note.get("error"):
        return error_response(note["error"], 400)
    return success_response(data=note, message=f"已解析 {safe_name} 并创建笔记")


# ==================== 智能问答 ====================
@router.post("/ask")
@handle_api_errors
async def ask_notes(request: Request):
    """智能问答。body: {question, limit?}"""
    user_id = _user(request)
    data = await request.json()
    question = str(data.get("question") or "")
    limit = int(data.get("limit") or 8)
    result = await asyncio.to_thread(_service.ask, user_id, question, limit)
    return success_response(data=result)
