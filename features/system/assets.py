"""Assets router - file listing, favicon, and user tools APIs."""

import asyncio
import contextlib
import html
import json
import logging
import mimetypes
import os
import re
import shlex
import stat
import urllib.parse
from datetime import datetime

import aiohttp
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from features.auth import CurrentUser, require_elevated_admin_when_auth_required
from features.system.icon_fetcher import IconFetcher
from features.system.ssh import ssh_manager
from features.users import get_client_display_id_from_request, get_client_id_from_request
from foundation.config import DEFAULT_FAVICON_TIMEOUT, MAX_BATCH_SIZE, TOOLS_DATA_FILE, config_manager
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

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


def ssh_connection_failed_response():
    return JSONResponse(
        content={'success': False, 'error': 'SSH connection failed'},
        status_code=500,
    )


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
            return error_response('Failed to list directory', status_code=500)

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
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


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
async def search_opengrok(req: dict):
    """Search configured OpenGrok source index and return parsed source links."""
    query = str(req.get('query') or '').strip()
    if not query:
        return error_response('query 参数不能为空', status_code=400)

    full = bool(req.get('full', False))
    limit = int(req.get('limit') or 30)
    limit = max(1, min(limit, 100))

    config = config_manager.load_config()
    opengrok_config = config.get('opengrok') or {}
    base_url = str(opengrok_config.get('base_url') or '').strip()
    project = str(opengrok_config.get('default_project') or '').strip()
    if not base_url or not project:
        return error_response('OpenGrok未配置，请在configs/config.json中配置opengrok段', status_code=404)

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
async def batch_fetch_favicons(request: Request):
    """批量获取网站 Favicon。"""
    data = await request.json()
    urls = data.get('urls', [])
    timeout = data.get('timeout', DEFAULT_FAVICON_TIMEOUT)

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


def load_tools_data():
    try:
        if os.path.exists(TOOLS_DATA_FILE):
            with open(TOOLS_DATA_FILE, encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"[ToolsData] Error loading tools data: {e}")
        return {}


def save_tools_data(tools_data):
    try:
        os.makedirs(os.path.dirname(TOOLS_DATA_FILE), exist_ok=True)
        with open(TOOLS_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(tools_data, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"[ToolsData] Error saving tools data: {e}")
        return False


def _validate_tools_data(tools_data: dict) -> None:
    """Validate persisted website shortcuts without changing the legacy schema."""
    if len(tools_data) > 50:
        raise ValueError('Too many website categories (maximum 50)')
    if len(json.dumps(tools_data, ensure_ascii=False).encode('utf-8')) > 256 * 1024:
        raise ValueError('Website tools data is too large')

    total_tools = 0
    for category, tools in tools_data.items():
        if not isinstance(category, str) or not category.strip() or len(category) > 80:
            raise ValueError('Invalid website category name')
        if not isinstance(tools, list) or len(tools) > 100:
            raise ValueError('Invalid website tools list')
        total_tools += len(tools)

        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError('Invalid website tool entry')
            title = tool.get('title')
            url = tool.get('url')
            icon = tool.get('icon', '')
            if not isinstance(title, str) or not title.strip() or len(title) > 200:
                raise ValueError('Invalid website tool title')
            if not isinstance(url, str) or not url.strip() or len(url) > 2048:
                raise ValueError('Invalid website tool URL')
            if not isinstance(icon, str) or len(icon) > 2048:
                raise ValueError('Invalid website tool icon')

            value = url.strip()
            if value.startswith('//') or '\\' in value:
                raise ValueError('Invalid website tool URL')
            parsed = urllib.parse.urlparse(value)
            if parsed.scheme and parsed.scheme.lower() not in {'http', 'https'}:
                raise ValueError('Unsupported website tool URL protocol')
            if value.startswith('/'):
                continue
            candidate = value if parsed.scheme else f'https://{value}'
            try:
                if not urllib.parse.urlparse(candidate).hostname:
                    raise ValueError('Invalid website tool URL')
            except ValueError as exc:
                raise ValueError('Invalid website tool URL') from exc

    if total_tools > 250:
        raise ValueError('Too many website tools (maximum 250)')


def _save_user_tools_entry(all_tools_data, client_id, tools, request):
    """Update and save a single user's tools entry."""
    all_tools_data[client_id] = {
        'tools': tools,
        'last_updated': datetime.now().isoformat(),
        'client_ip': request.client.host if request.client else 'unknown',
    }
    return save_tools_data(all_tools_data)


def _tools_data_keys_for_request(request: Request) -> list[str]:
    keys = []
    with contextlib.suppress(Exception):
        keys.append(get_client_display_id_from_request(request))
    with contextlib.suppress(Exception):
        keys.append(get_client_id_from_request(request))
    return [key for index, key in enumerate(keys) if key and key not in keys[:index]]


def _tools_data_primary_key_for_request(request: Request) -> str:
    with contextlib.suppress(Exception):
        display_id = get_client_display_id_from_request(request)
        if display_id:
            return display_id
    return get_client_id_from_request(request)


@router.post("/api/websites/save")
@handle_api_errors
async def save_user_tools(request: Request):
    """Persist the calling user's tools/shortcuts data."""
    try:
        data = await request.json()
        client_id = _tools_data_primary_key_for_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        tools_data = data.get('tools')
        if not isinstance(tools_data, dict):
            return error_response('Invalid tools data format', status_code=400)
        try:
            _validate_tools_data(tools_data)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        all_tools_data = load_tools_data()

        if _save_user_tools_entry(all_tools_data, client_id, tools_data, request):
            logger.info(f"[ToolsData] Saved tools data for {client_id}")
            return JSONResponse(content={'success': True})
        else:
            return error_response('Failed to save tools data', status_code=500)

    except Exception as e:
        logger.error(f"[ToolsData] Error in save_user_tools: {e}")
        return error_response(str(e), status_code=500)


@router.get("/api/websites/load")
@handle_api_errors
async def load_user_tools(request: Request):
    """Return the calling user's tools/shortcuts data."""
    try:
        client_id = _tools_data_primary_key_for_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        all_tools_data = load_tools_data()

        user_data = {}
        for key in _tools_data_keys_for_request(request):
            user_data = all_tools_data.get(key, {})
            if user_data:
                break
        tools = user_data.get('tools', {})
        last_updated = user_data.get('last_updated')

        logger.info(f"[ToolsData] Loaded tools data for {client_id}, last_updated: {last_updated}")

        return JSONResponse(content={
            'success': True,
            'tools': tools,
            'last_updated': last_updated
        })

    except Exception as e:
        logger.error(f"[ToolsData] Error in load_user_tools: {e}")
        return error_response(str(e), status_code=500)


@router.post("/api/websites/sync")
@handle_api_errors
async def sync_user_tools(request: Request):
    """Sync the user's tools data, keeping whichever copy (local or server) is newer."""
    try:
        data = await request.json()
        client_id = _tools_data_primary_key_for_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        local_tools = data.get('tools')
        local_timestamp = data.get('timestamp')

        if not isinstance(local_tools, dict):
            return error_response('Invalid local tools data', status_code=400)
        try:
            _validate_tools_data(local_tools)
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

        all_tools_data = load_tools_data()
        server_user_data = {}
        for key in _tools_data_keys_for_request(request):
            server_user_data = all_tools_data.get(key, {})
            if server_user_data:
                break
        server_tools = server_user_data.get('tools', {})
        server_timestamp = server_user_data.get('last_updated')

        use_local = False
        if server_timestamp and local_timestamp:
            try:
                server_time = datetime.fromisoformat(server_timestamp.replace('Z', '+00:00'))
                local_time = datetime.fromisoformat(local_timestamp.replace('Z', '+00:00'))
                use_local = local_time >= server_time
            except (ValueError, TypeError) as e:
                logger.warning(f"[ToolsData] Error comparing timestamps: {e}, using local data")
                use_local = True
        elif local_tools:
            use_local = True

        if use_local:
            merged_tools = local_tools
            source = 'local'
            _save_user_tools_entry(all_tools_data, client_id, local_tools, request)
        else:
            merged_tools = server_tools
            source = 'server'

        logger.info(f"[ToolsData] Synced tools data for {client_id}, source: {source}")

        return JSONResponse(content={
            'success': True,
            'tools': merged_tools,
            'source': source,
            'last_updated': all_tools_data.get(client_id, {}).get('last_updated')
        })

    except Exception as e:
        logger.error(f"[ToolsData] Error in sync_user_tools: {e}")
        return error_response(str(e), status_code=500)
