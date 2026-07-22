from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from features.users import get_client_id_from_request
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response
from foundation.uploads import save_upload_to_path

from .service import KnowledgeService
from .storage import ATTACHMENT_DIR, DEFAULT_SPACES


router = APIRouter(prefix="/api/knowledge")
page_router = APIRouter()
_service = KnowledgeService()

MAX_UPLOAD_SIZE = 100 * 1024 * 1024
LIST_PREVIEW_CHARS = 500


@contextlib.asynccontextmanager
async def _stage_upload(file: UploadFile, user_id: str):
    """把上传文件落到一个临时路径，用完即删；供附件/文档入库两个端点复用。"""
    tmp_dir = Path(ATTACHMENT_DIR) / "_tmp" / user_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = os.path.basename(file.filename)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex[:12]}_{safe_name}"
    try:
        await save_upload_to_path(file, str(tmp_path), MAX_UPLOAD_SIZE)
        yield tmp_path, safe_name
    finally:
        with contextlib.suppress(Exception):
            tmp_path.unlink()


def _user(request: Request) -> str:
    return get_client_id_from_request(request)


def _space_id(user_id: str, requested: str = "") -> str:
    requested = str(requested or "").strip()
    if requested in {item[0] for item in DEFAULT_SPACES}:
        return _service.store.default_space_id(user_id, requested)
    owned = {
        item["space_id"] for item in _service.store.list_spaces(user_id)
    }
    if requested and requested in owned:
        return requested
    return _service.store.default_space_id(user_id, "gms")


def _preview(doc: dict[str, Any]) -> dict[str, Any]:
    item = dict(doc)
    content = str(item.get("content_md") or item.get("raw_content") or "")
    item["content_md"] = content[:LIST_PREVIEW_CHARS]
    item["raw_content"] = str(item.get("raw_content") or "")[:LIST_PREVIEW_CHARS]
    item["content_truncated"] = len(content) > LIST_PREVIEW_CHARS
    return item


def _validated_links(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_links = payload.get("links")
    if raw_links is None:
        return []
    if not isinstance(raw_links, list):
        raise ValueError("links must be an array")
    if len(raw_links) > 50:
        raise ValueError("links cannot contain more than 50 items")
    allowed_types = {
        "test_report",
        "redmine_issue",
        "gerrit_change",
        "test_case",
    }
    links: list[dict[str, str]] = []
    for raw in raw_links[:50]:
        if not isinstance(raw, dict):
            raise ValueError("each knowledge link must be an object")
        target_type = str(raw.get("target_type") or "").strip()
        target_id = str(raw.get("target_id") or "").strip()
        if target_type not in allowed_types or not target_id:
            raise ValueError("knowledge link target is invalid")
        links.append({
            "target_type": target_type,
            "target_id": target_id[:256],
            "title": str(raw.get("title") or target_id).strip()[:256],
        })
    return links


@page_router.get("/notes", response_class=HTMLResponse)
async def knowledge_page():
    return HTMLResponse("<!-- knowledge page is rendered inline in shell.html -->")


@router.get("/spaces")
@handle_api_errors
async def list_spaces(request: Request):
    return success_response(data={"spaces": _service.store.list_spaces(_user(request))})


@router.post("/spaces")
@handle_api_errors
async def create_space(request: Request):
    data = await request.json()
    name = str(data.get("name") or "").strip()
    if not name:
        return error_response("知识库名称不能为空", 400)
    return success_response(data=_service.store.create_space(_user(request), name, str(data.get("icon") or "")))


@router.get("/tree")
@handle_api_errors
async def get_tree(request: Request, space_id: str = Query("gms")):
    user_id = _user(request)
    return success_response(data={"nodes": _service.store.list_tree(user_id, _space_id(user_id, space_id))})


@router.post("/folders")
@handle_api_errors
async def create_folder(request: Request):
    data = await request.json()
    title = str(data.get("title") or "").strip()
    if not title:
        return error_response("目录名称不能为空", 400)
    user_id = _user(request)
    node = _service.store.create_folder(
        user_id,
        _space_id(user_id, str(data.get("space_id") or "")),
        title,
        str(data.get("parent_id") or ""),
    )
    return success_response(data=node)


@router.post("/docs")
@handle_api_errors
async def create_doc(request: Request):
    data = await request.json()
    user_id = _user(request)
    content = str(data.get("content_md") or data.get("content") or "")
    try:
        links = _validated_links(data)
    except ValueError as exc:
        return error_response(str(exc), 400)
    note = await asyncio.to_thread(
        _service.create_doc_from_text,
        user_id,
        space_id=_space_id(user_id, str(data.get("space_id") or "")),
        parent_id=str(data.get("parent_id") or ""),
        title=str(data.get("title") or ""),
        content=content,
        tags=data.get("tags"),
        links=links,
        source=str(data.get("source") or "manual"),
    )
    if note.get("error"):
        return error_response(note["error"], 400)
    return success_response(data=note, message="文档已创建")


@router.get("/docs")
@handle_api_errors
async def list_docs(
    request: Request,
    space_id: str = Query(""),
    parent_id: str = Query(""),
    tag: str = Query(""),
    favorite: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    docs = _service.store.list_docs(
        _user(request),
        space_id=space_id,
        parent_id=parent_id if parent_id else None,
        tag=tag,
        favorite=favorite,
        limit=limit,
    )
    return success_response(data={"docs": [_preview(d) for d in docs], "count": len(docs)})


@router.get("/docs/{doc_id}")
@handle_api_errors
async def get_doc(request: Request, doc_id: str):
    doc = _service.store.get_doc(_user(request), doc_id)
    if not doc:
        return error_response("文档不存在", 404)
    return success_response(data=doc)


@router.put("/docs/{doc_id}")
@handle_api_errors
async def update_doc(request: Request, doc_id: str):
    data = await request.json()
    allowed = {
        k: v
        for k, v in data.items()
        if k in {"title", "content_md", "raw_content", "summary", "tags", "links", "favorite", "source", "source_file"}
    }
    doc = _service.store.update_doc(_user(request), doc_id, allowed)
    if not doc:
        return error_response("文档不存在", 404)
    return success_response(data=doc, message="已保存")


@router.get("/docs/{doc_id}/versions")
@handle_api_errors
async def list_doc_versions(request: Request, doc_id: str, limit: int = Query(100, ge=1, le=500)):
    user_id = _user(request)
    if not _service.store.get_doc(user_id, doc_id):
        return error_response("文档不存在", 404)
    versions = _service.store.list_versions(user_id, doc_id, limit)
    return success_response(data={"versions": versions, "count": len(versions)})


@router.post("/docs/{doc_id}/versions/{version_id}/restore")
@handle_api_errors
async def restore_doc_version(request: Request, doc_id: str, version_id: str):
    doc = _service.store.restore_version(_user(request), doc_id, version_id)
    if not doc:
        return error_response("文档或历史版本不存在", 404)
    return success_response(data=doc, message="历史版本已恢复")


@router.delete("/nodes/{node_id}")
@handle_api_errors
async def delete_node(request: Request, node_id: str):
    if not _service.store.delete_node(_user(request), node_id):
        return error_response("节点不存在", 404)
    return success_response(message="已删除")


@router.post("/nodes/{node_id}/move")
@handle_api_errors
async def move_node(request: Request, node_id: str):
    data = await request.json()
    ok = _service.store.move_node(
        _user(request),
        node_id,
        str(data.get("parent_id") or ""),
        int(data["sort_order"]) if data.get("sort_order") is not None else None,
    )
    if not ok:
        return error_response("节点不存在", 404)
    return success_response(message="已移动")


@router.get("/search")
@handle_api_errors
async def search_docs(
    request: Request,
    q: str = Query(""),
    space_id: str = Query(""),
    tag: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    docs = _service.store.search(_user(request), q, space_id=space_id, tag=tag, limit=limit)
    return success_response(data={"items": [_preview(d) for d in docs], "count": len(docs)})


@router.get("/tags")
@handle_api_errors
async def list_tags(request: Request):
    return success_response(data={"tags": _service.store.list_tags(_user(request))})


@router.post("/docs/{doc_id}/attachments")
@handle_api_errors
async def upload_attachment(request: Request, doc_id: str, file: UploadFile | None = File(None)):
    if not file or not file.filename:
        return error_response("未提供文件", 400)
    user_id = _user(request)
    async with _stage_upload(file, user_id) as (tmp_path, safe_name):
        attachment = _service.store.add_attachment(
            user_id,
            doc_id,
            source_path=str(tmp_path),
            original_name=safe_name,
            mime=file.content_type or "",
        )
    return success_response(data=attachment)


@router.get("/docs/{doc_id}/attachments/{attachment_id}/download")
@handle_api_errors
async def download_attachment(request: Request, doc_id: str, attachment_id: str):
    user_id = _user(request)
    doc = _service.store.get_doc(user_id, doc_id)
    attachment = next(
        (
            item
            for item in (doc or {}).get("attachments") or []
            if item.get("attachment_id") == attachment_id
        ),
        None,
    )
    if not attachment:
        return error_response("附件不存在", 404)
    path = Path(str(attachment.get("path") or "")).resolve()
    root = (Path(ATTACHMENT_DIR) / user_id / doc_id).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        return error_response("附件文件不存在", 404)
    return FileResponse(
        path,
        filename=str(attachment.get("original_name") or path.name),
        media_type=str(attachment.get("mime") or "application/octet-stream"),
    )


@router.post("/upload")
@handle_api_errors
async def upload_doc_file(
    request: Request,
    file: UploadFile | None = File(None),
    space_id: str = Form("gms"),
    parent_id: str = Form(""),
    tags: str = Form(""),
):
    if not file or not file.filename:
        return error_response("未提供文件", 400)
    user_id = _user(request)
    async with _stage_upload(file, user_id) as (tmp_path, safe_name):
        doc = await asyncio.to_thread(
            _service.create_doc_from_file,
            user_id,
            space_id=_space_id(user_id, space_id),
            parent_id=parent_id,
            file_path=str(tmp_path),
            filename=safe_name,
            tags=tags,
        )
    if doc.get("error"):
        return error_response(doc["error"], 400)
    return success_response(data=doc, message="文件已入库")


@router.post("/ask")
@handle_api_errors
async def ask_knowledge(request: Request):
    data = await request.json()
    result = await asyncio.to_thread(
        _service.ask,
        _user(request),
        str(data.get("question") or ""),
        space_id=str(data.get("space_id") or ""),
        limit=int(data.get("limit") or 8),
    )
    return success_response(data=result)
