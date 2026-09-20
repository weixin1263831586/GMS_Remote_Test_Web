"""Allowlisted utility tool listing, browsing, and download routes."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from features.auth import (
    CurrentUser,
    require_authenticated_user_when_auth_required,
)
from foundation.errors import handle_api_errors


router = APIRouter()
UTILITY_TOOLS_DIR = Path(__file__).resolve().parents[2] / 'tools'
# Stable tool IDs — the browser never learns real paths, so files can move
# under tools/ without UI changes. `path` is relative to tools/ and the
# download endpoint serves the file under `download_name`.
UTILITY_TOOL_MANIFEST = {
    'gerrit-patch': {
        'path': 'scripts/utilities/gerrit_patch_export_and_apply.sh',
        'download_name': 'gerrit_patch_export_and_apply_tool.sh',
    },
    'scrcpy': {
        'path': 'scrcpy-linux-x86_64-v3.3.4.tar.gz',
        'download_name': 'scrcpy-linux-x86_64-v3.3.4.tar.gz',
    },
    'upgrade-tool': {
        'path': 'upgrade_tool',
        'download_name': 'upgrade_tool',
    },
    'misc-img': {
        'path': 'misc.img',
        'download_name': 'misc.img',
    },
}


def _resolve_allowed_utility_tool(tool_id: str) -> Path:
    entry = UTILITY_TOOL_MANIFEST.get(tool_id or '')
    if entry is None:
        raise HTTPException(status_code=403, detail='Tool is not available for download')

    full_path = (UTILITY_TOOLS_DIR / entry['path']).resolve()
    try:
        full_path.relative_to(UTILITY_TOOLS_DIR.resolve())
    except ValueError as error:
        raise HTTPException(status_code=403, detail='Access denied') from error
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail='File not found')
    return full_path


def _download_name(tool_id: str) -> str:
    return UTILITY_TOOL_MANIFEST[tool_id]['download_name']


def _resolve_tool_id(tool_id_or_name: str) -> str:
    """Map a request path to a manifest tool_id.

    Accepts the stable ID first; a legacy manifest file name (e.g. a tool
    card saved in a browser's localStorage before the stable-ID migration)
    is tolerated and mapped to its tool so old client state keeps working.
    """
    normalized = str(tool_id_or_name or '').strip('/')
    if normalized in UTILITY_TOOL_MANIFEST:
        return normalized
    for tool_id, entry in UTILITY_TOOL_MANIFEST.items():
        if normalized in (entry['download_name'], entry['path']):
            return tool_id
    return normalized


@router.get('/api/tools/list')
@handle_api_errors
async def list_utility_tools():
    """列出可下载的常用工具文件"""
    if not UTILITY_TOOLS_DIR.exists():
        return JSONResponse(content={'success': True, 'files': []})

    files = []
    for tool_id in sorted(UTILITY_TOOL_MANIFEST):
        try:
            entry = _resolve_allowed_utility_tool(tool_id)
        except HTTPException:
            continue
        stat = entry.stat()
        files.append(
            {
                'tool_id': tool_id,
                'name': _download_name(tool_id),
                'size': stat.st_size,
                'modified': stat.st_mtime,
            }
        )
    return JSONResponse(content={'success': True, 'files': files})


@router.post('/api/tools/browse')
@handle_api_errors
async def browse_utility_tools(
    req: dict,
    request: Request,
    _user: CurrentUser | None = Depends(
        require_authenticated_user_when_auth_required
    ),
):
    """浏览可下载工具清单，返回与 /api/files/list 相同格式以便复用文件浏览器弹框

    稳定 tool_id 设计：浏览器只看到 tool_id 与 download_name，永远拿不到
    tools/ 下的真实路径，因此这里返回扁平清单、不展开目录层级。
    """
    files = []
    for tool_id in sorted(UTILITY_TOOL_MANIFEST):
        try:
            entry = _resolve_allowed_utility_tool(tool_id)
        except HTTPException:
            continue
        files.append(
            {
                'tool_id': tool_id,
                'name': _download_name(tool_id),
                'type': 'file',
                'size': entry.stat().st_size,
            }
        )
    files.sort(key=lambda item: item['name'].lower())
    return JSONResponse(content={'success': True, 'path': '', 'files': files})


@router.get('/api/tools/download/{tool_id:path}')
@handle_api_errors
async def download_utility_tool(tool_id: str):
    """按稳定 tool_id 下载 tools/ 清单中的文件（兼容历史文件名）"""
    resolved_id = _resolve_tool_id(tool_id)
    full_path = _resolve_allowed_utility_tool(resolved_id)
    return FileResponse(
        path=str(full_path),
        filename=_download_name(resolved_id),
        media_type='application/octet-stream',
    )
