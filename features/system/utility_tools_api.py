"""Allowlisted utility tool listing, browsing, and download routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from foundation.errors import handle_api_errors
from foundation.responses import error_response


router = APIRouter()
UTILITY_TOOLS_DIR = Path(__file__).resolve().parents[2] / 'tools'
UTILITY_TOOL_MANIFEST = {
    'gerrit_patch_export_and_apply_tool.sh',
    'scrcpy-linux-x86_64-v3.3.4.tar.gz',
    'upgrade_tool',
    'misc.img',
}


def _resolve_allowed_utility_tool(file_path: str) -> Path:
    normalized = str(Path(file_path or ''))
    if normalized not in UTILITY_TOOL_MANIFEST:
        raise HTTPException(status_code=403, detail='Tool is not available for download')

    full_path = (UTILITY_TOOLS_DIR / normalized).resolve()
    try:
        full_path.relative_to(UTILITY_TOOLS_DIR.resolve())
    except ValueError as error:
        raise HTTPException(status_code=403, detail='Access denied') from error
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail='File not found')
    return full_path


@router.get('/api/tools/list')
@handle_api_errors
async def list_utility_tools():
    """列出可下载的常用工具文件"""
    if not UTILITY_TOOLS_DIR.exists():
        return JSONResponse(content={'success': True, 'files': []})

    files = []
    for relative_path in sorted(UTILITY_TOOL_MANIFEST):
        try:
            entry = _resolve_allowed_utility_tool(relative_path)
        except HTTPException:
            continue
        stat = entry.stat()
        files.append(
            {
                'name': relative_path,
                'size': stat.st_size,
                'modified': stat.st_mtime,
            }
        )
    return JSONResponse(content={'success': True, 'files': files})


@router.post('/api/tools/browse')
@handle_api_errors
async def browse_utility_tools(req: dict):
    """浏览可下载工具清单，返回与 /api/files/list 相同格式以便复用文件浏览器弹框"""
    subpath = str(req.get('path') or '').strip('/')
    if '..' in Path(subpath).parts:
        return error_response('非法路径', status_code=400)

    files = []
    directories = set()
    for relative_path in sorted(UTILITY_TOOL_MANIFEST):
        relative = Path(relative_path)
        if subpath:
            try:
                remaining = relative.relative_to(subpath)
            except ValueError:
                continue
        else:
            remaining = relative

        if len(remaining.parts) > 1:
            directories.add(remaining.parts[0])
            continue
        try:
            entry = _resolve_allowed_utility_tool(relative_path)
        except HTTPException:
            continue
        files.append(
            {'name': remaining.name, 'type': 'file', 'size': entry.stat().st_size}
        )

    files.extend(
        {'name': name, 'type': 'directory', 'size': 0}
        for name in sorted(directories)
    )
    files.sort(key=lambda item: (item['type'] != 'directory', item['name'].lower()))
    return JSONResponse(content={'success': True, 'path': subpath, 'files': files})


@router.get('/api/tools/download/{file_path:path}')
@handle_api_errors
async def download_utility_tool(file_path: str):
    """下载 tools/ 目录下的指定文件"""
    full_path = _resolve_allowed_utility_tool(file_path)
    return FileResponse(
        path=str(full_path),
        filename=full_path.name,
        media_type='application/octet-stream',
    )
