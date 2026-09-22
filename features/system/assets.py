"""Assets router - file listing, favicon, and user tools APIs."""

import asyncio
import html
import logging
import mimetypes
import os
import re
import shlex
import stat
import urllib.parse

import aiohttp
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from features.auth import CurrentUser, require_elevated_admin_when_auth_required
from features.system.icon_fetcher import IconFetcher
from features.system.ssh import ssh_manager
from foundation.config import DEFAULT_FAVICON_TIMEOUT, MAX_BATCH_SIZE, config_manager
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from .assets_ssh import ssh_connection_failed_response
from .tools_data_api import router as tools_data_router
from .utility_tools_api import (
    browse_utility_tools as browse_utility_tools,
)
from .utility_tools_api import (
    download_utility_tool as download_utility_tool,
)
from .utility_tools_api import (
    list_utility_tools as list_utility_tools,
)
from .utility_tools_api import (
    router as utility_tools_router,
)


logger = logging.getLogger(__name__)

router = APIRouter()
router.include_router(utility_tools_router)
router.include_router(tools_data_router)


def _remote_list_command(path: str) -> str:
    """Build a non-interactive listing command without exposing shell syntax."""
    return f'ls -la -- {shlex.quote(str(path))} 2>/dev/null'


def _expand_user_path(path: str, username: str) -> str:
    """Expand a configured user's home without relying on the server process user."""
    if path == "~":
        return f"/home/{username}"
    if path.startswith("~/"):
        return f"/home/{username}/{path[2:]}"
    return path


def _list_local_files(path: str) -> list[dict]:
    files = []
    with os.scandir(path) as entries:
        for entry in entries:
            entry_stat = entry.stat(follow_symlinks=False)
            is_dir = entry.is_dir(follow_symlinks=True)
            files.append({
                'name': entry.name,
                'type': 'directory' if is_dir else 'file',
                'size': 0 if is_dir else entry_stat.st_size,
                'permissions': stat.filemode(entry_stat.st_mode),
            })
    files.sort(key=lambda item: (item['type'] != 'directory', item['name'].lower()))
    return files


@router.get("/api/files/progress")
async def get_upload_progress(upload_id: str | None = None):
    """Return the current upload progress for an upload_id (always completed)."""
    return JSONResponse(content={
        "success": True,
        "data": {
            "upload_id": upload_id,
            "progress": 100,
            "status": "completed"
        }
    })


@router.post("/api/files/list")
async def list_files(
    req: dict,
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """文件列表 - 通过SSH连接到远程主机"""
    try:
        config = config_manager.load_config()
        username = config_manager.get_ubuntu_user(config)
        path = str(req.get('path') or '').strip()

        if not path:
            path = str(config.get('suites_path') or f"/home/{username}")
        path = _expand_user_path(path, username)

        if config_manager.is_config_host_local(config):
            path = os.path.abspath(path)
            try:
                files = await asyncio.to_thread(_list_local_files, path)
            except FileNotFoundError:
                return error_response('Directory not found', status_code=404)
            except NotADirectoryError:
                return error_response('Path is not a directory', status_code=400)
            except PermissionError:
                return error_response('Permission denied', status_code=403)
            return JSONResponse(content={
                'success': True,
                'path': path,
                'files': files,
            })

        def _list_remote_files():
            with ssh_manager.optional_connection(config) as ssh:
                if not ssh:
                    return None
                return ssh_manager.execute_command(ssh, _remote_list_command(path))

        command_result = await asyncio.to_thread(_list_remote_files)
        if command_result is None:
            return ssh_connection_failed_response()

        if not command_result.ok:
            # Preserve remote context while exposing the dependency failure as 502.
            detail = (command_result.stderr or command_result.stdout or '').strip()
            return error_response('Failed to list directory on remote host',
                                  status_code=502, detail=detail[:300] or None)

        files = []
        for line in command_result.stdout.split('\n'):
            if line.startswith('total') or not line.strip():
                continue

            parts = line.split()
            if len(parts) >= 9:
                permissions = parts[0]
                name = ' '.join(parts[8:])
                is_dir = permissions.startswith('d')
                size = parts[4] if not is_dir else '0'

                if name in ['.', '..']:
                    continue

                files.append({
                    'name': name,
                    'type': 'directory' if is_dir else 'file',
                    'size': int(size),
                    'permissions': permissions
                })

        files.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))

        return JSONResponse(content={
            'success': True,
            'path': path,
            'files': files
        })
    except Exception:
        logger.exception("Error listing files")
        return error_response('Internal server error', status_code=500)


def _build_opengrok_search_url(base_url: str, project: str, query: str, full: bool) -> str:
    params = {'project': project}
    if full:
        params['full'] = query
    else:
        params['defs'] = query
        params['refs'] = query
    return f"{base_url.rstrip('/')}/search?{urllib.parse.urlencode(params)}"


def _parse_opengrok_results(base_url: str, project: str, html_text: str, limit: int = 30):
    results = []
    seen = set()
    base = base_url.rstrip('/')
    pattern = re.compile(r'href=["\']([^"\']*/xref/[^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)

    for match in pattern.finditer(html_text or ''):
        href = html.unescape(match.group(1))
        label = re.sub(r'<[^>]+>', '', html.unescape(match.group(2))).strip()
        if not href:
            continue

        absolute_url = urllib.parse.urljoin(base + '/', href)
        parsed = urllib.parse.urlparse(absolute_url)
        path = parsed.path
        marker = f'/xref/{project}/'
        file_path = path.split(marker, 1)[1] if marker in path else path.split('/xref/', 1)[-1]
        file_path = urllib.parse.unquote(file_path)

        line = None
        if parsed.fragment:
            line_match = re.search(r'\d+', parsed.fragment)
            if line_match:
                line = int(line_match.group(0))

        dedupe_key = (file_path, line)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        results.append({
            'file': file_path,
            'path': file_path,
            'line': line,
            'label': label or file_path,
            'url': absolute_url,
        })
        if len(results) >= limit:
            break

    return results


@router.post("/api/opengrok/search")
@handle_api_errors
async def search_opengrok(
    req: dict,
    _user: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """Search configured OpenGrok source index and return parsed source links."""
    query = str(req.get('query') or '').strip()
    if not query:
        return error_response('query 参数不能为空', status_code=400)

    full = bool(req.get('full', False))
    try:
        limit = int(req.get('limit') or 30)
    except (TypeError, ValueError):
        return error_response('limit 必须是整数', status_code=400)
    limit = max(1, min(limit, 100))

    config = config_manager.load_config()
    opengrok_config = config.get('opengrok') or {}
    base_url = str(opengrok_config.get('base_url') or '').strip()
    project = str(opengrok_config.get('default_project') or '').strip()
    if not base_url or not project:
        return error_response('OpenGrok未配置，请在configs/local/config.json中配置opengrok段', status_code=404)

    search_url = _build_opengrok_search_url(base_url, project, query, full)
    results = []
    fetch_error = ''

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(search_url) as response:
            body = await response.text(errors='replace')
            if response.status >= 400:
                fetch_error = f'OpenGrok HTTP {response.status}'
            else:
                results = _parse_opengrok_results(base_url, project, body, limit)
    except Exception as e:
        fetch_error = str(e)

    return JSONResponse(content={
        'success': True,
        'query': query,
        'full': full,
        'project': project,
        'search_url': search_url,
        'results': results,
        'count': len(results),
        'warning': fetch_error,
    })


@router.get("/api/favicon/fetch")
@handle_api_errors
async def fetch_website_favicon(
    request: Request,
    url: str = Query(..., description="网站URL"),
    timeout: int = Query(DEFAULT_FAVICON_TIMEOUT, description="超时时间（秒）")
):
    """从网页、站点根目录或图标服务获取 Favicon。"""
    if not url or not url.strip():
        return error_response('URL参数不能为空', status_code=400)

    fetcher = IconFetcher(timeout=timeout)
    try:
        icon_result = await fetcher.fetch_icon_async(url, write_cache=False)

        if icon_result.success:
            logger.info(f"[Favicon] Successfully fetched icon for {url}: {icon_result.icon_url}")
            payload = {
                'icon_url': icon_result.icon_url,
                'icon_type': icon_result.icon_type,
                'source': icon_result.source,
                'size': icon_result.size,
                'original_icon_url': icon_result.original_icon_url
            }
            return success_response(payload)
        else:
            logger.warning(f"[Favicon] Failed to fetch icon for {url}: {icon_result.error}")
            return error_response(
                icon_result.error or '无法获取网站图标',
                status_code=404,
                detail={'fallback_icon': '🌐'}
            )
    finally:
        await fetcher.close()


@router.get("/api/favicon/proxy")
@handle_api_errors
async def proxy_favicon(
    request: Request,
    url: str = Query(..., description="远程图标URL"),
    timeout: int = Query(DEFAULT_FAVICON_TIMEOUT, description="超时时间（秒）")
):
    """把远程图标下载到本地后返回本地文件，失败时返回本地默认图标。"""
    icon_path = IconFetcher.default_icon_path()

    if url and IconFetcher.is_local_static_url(url):
        local_path = IconFetcher.static_url_to_path(url)
        if local_path and os.path.exists(local_path):
            icon_path = local_path
    elif url and IconFetcher.is_remote_url(url):
        fetcher = IconFetcher(timeout=timeout)
        try:
            icon_result = await fetcher.localize_icon_url(url)
            local_path = IconFetcher.static_url_to_path(icon_result.icon_url)
            if icon_result.success and local_path and os.path.exists(local_path):
                icon_path = local_path
            else:
                logger.debug(f"[FaviconProxy] Using fallback for {url}: {icon_result.error}")
        finally:
            await fetcher.close()

    media_type = mimetypes.guess_type(icon_path)[0] or 'image/svg+xml'
    return FileResponse(icon_path, media_type=media_type)


@router.post("/api/favicon/batch")
@handle_api_errors
async def batch_fetch_favicons(
    request: Request,
    _admin: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """批量获取网站 Favicon。"""
    try:
        data = await request.json()
    except ValueError:
        return error_response('Invalid JSON body', status_code=400)
    if not isinstance(data, dict):
        return error_response('Invalid request body', status_code=400)
    urls = data.get('urls', [])
    timeout = data.get('timeout', DEFAULT_FAVICON_TIMEOUT)
    if not isinstance(timeout, (int, float)):
        return error_response('timeout 必须是数字', status_code=400)

    if not isinstance(urls, list):
        return error_response('urls必须是数组格式', status_code=400)

    if len(urls) > MAX_BATCH_SIZE:
        return error_response(f'批量请求不能超过{MAX_BATCH_SIZE}个URL', status_code=400)

    fetcher = IconFetcher(timeout=timeout)
    try:
        results = await fetcher.batch_fetch_icons_async(urls, write_cache=False)
        successful = sum(1 for r in results if r['success'])

        return success_response({
            'results': results,
            'total': len(urls),
            'successful': successful,
            'failed': len(results) - successful
        })
    finally:
        await fetcher.close()
