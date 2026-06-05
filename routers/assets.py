"""Assets router - file listing, favicon, and user tools APIs."""

import html
import logging
import re
import urllib.parse
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.api_response import error_response
from core.config import config_manager
from core.devices import ssh_connection_failed_response
from core.error_handling import handle_api_errors
from core.ssh import ssh_manager
from core.state import global_state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/files/progress")
async def get_upload_progress(upload_id: Optional[str] = None):
    """获取上传进度"""
    try:
        # 返回上传进度（这里需要实现实际的进度跟踪）
        return JSONResponse(content={
            "success": True,
            "data": {
                "upload_id": upload_id,
                "progress": 100,
                "status": "completed"
            }
        })
    except Exception as e:
        logger.error(f"Error getting upload progress: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )


@router.post("/api/files/list")
async def list_files(req: dict):
    """文件列表 - 通过SSH连接到远程主机"""
    try:
        path = req.get('path', '')
        config = config_manager.load_config()

        if not path:
            # Default to user home directory
            path = f"/home/{config_manager.get_ubuntu_user(config)}"

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            list_cmd = f"ls -la '{path}' 2>/dev/null || echo 'ERROR'"
            output, error, code = ssh_manager.execute_command(ssh, list_cmd)

            if 'ERROR' in output or code != 0:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': 'Failed to list directory'}, status_code=500)

            files = []
            for line in output.split('\n'):
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

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'path': path,
                'files': files
            })
        except Exception:
            ssh_manager.return_connection(ssh)
            raise
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
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(search_url) as response:
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
import logging
import mimetypes
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from core.api_response import error_response, success_response
from core.settings import DEFAULT_FAVICON_TIMEOUT, MAX_BATCH_SIZE
from core.error_handling import handle_api_errors
from modules.icon_fetcher import IconFetcher

logger = logging.getLogger(__name__)



@router.get("/api/favicon/fetch")
@handle_api_errors
async def fetch_website_favicon(
    request: Request,
    url: str = Query(..., description="网站URL"),
    timeout: int = Query(DEFAULT_FAVICON_TIMEOUT, description="超时时间（秒）")
):
    """获取网站的真实Favicon图标

    支持从多种来源获取图标：
    1. 从HTML页面中提取图标链接（最准确）
    2. 尝试网站根目录的常见图标文件
    3. 使用第三方图标服务（Google、DuckDuckGo）

    Args:
        url: 要获取图标的网站URL
        timeout: 请求超时时间（秒）

    Returns:
        {
            "success": true/false,
            "icon_url": "图标URL",
            "icon_type": "svg/ico/png等",
            "source": "html/root/api",
            "size": 图标尺寸
        }
    """
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
            # 兼容旧前端直接读取 result.icon_url，同时保留统一 data 响应。
            return JSONResponse(content={'success': True, **payload, 'data': payload})
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
    """批量获取多个网站的Favicon图标

    Body:
        {
            "urls": ["https://google.com", "https://github.com"],
            "timeout": 10
        }

    Returns:
        {
            "success": true,
            "results": [
                {
                    "url": "https://google.com",
                    "success": true,
                    "icon_url": "..."
                },
                ...
            ]
        }
    """
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
import json
import logging
import os
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.api_response import error_response
from core.clients import get_client_id_from_request
from core.settings import TOOLS_DATA_FILE
from core.error_handling import handle_api_errors

logger = logging.getLogger(__name__)



def load_tools_data():
    """加载所有用户的工具数据"""
    try:
        if os.path.exists(TOOLS_DATA_FILE):
            with open(TOOLS_DATA_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    except Exception as e:
        logger.error(f"[ToolsData] Error loading tools data: {e}")
        return {}


def save_tools_data(tools_data):
    """保存所有用户的工具数据"""
    try:
        with open(TOOLS_DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(tools_data, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"[ToolsData] Error saving tools data: {e}")
        return False


@router.post("/api/tools/save")
@handle_api_errors
async def save_user_tools(request: Request):
    """保存用户的工具数据"""
    try:
        data = await request.json()
        client_id = get_client_id_from_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        tools_data = data.get('tools')
        if not isinstance(tools_data, dict):
            return error_response('Invalid tools data format', status_code=400)

        # 加载现有数据
        all_tools_data = load_tools_data()

        # 更新当前用户的数据
        all_tools_data[client_id] = {
            'tools': tools_data,
            'last_updated': datetime.now().isoformat(),
            'client_ip': request.client.host if request.client else 'unknown'
        }

        # 保存到文件
        if save_tools_data(all_tools_data):
            logger.info(f"[ToolsData] Saved tools data for {client_id}")
            return JSONResponse(content={'success': True})
        else:
            return error_response('Failed to save tools data', status_code=500)

    except Exception as e:
        logger.error(f"[ToolsData] Error in save_user_tools: {e}")
        return error_response(str(e), status_code=500)


@router.get("/api/tools/load")
@handle_api_errors
async def load_user_tools(request: Request):
    """加载用户的工具数据"""
    try:
        client_id = get_client_id_from_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        # 加载所有用户数据
        all_tools_data = load_tools_data()

        # 获取当前用户的数据
        user_data = all_tools_data.get(client_id, {})
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


@router.post("/api/tools/sync")
@handle_api_errors
async def sync_user_tools(request: Request):
    """同步用户的工具数据（智能合并本地和服务器数据）"""
    try:
        data = await request.json()
        client_id = get_client_id_from_request(request)

        if not client_id:
            return error_response('Unable to identify user', status_code=400)

        local_tools = data.get('tools')
        local_timestamp = data.get('timestamp')

        if not isinstance(local_tools, dict):
            return error_response('Invalid local tools data', status_code=400)

        # 加载服务器数据
        all_tools_data = load_tools_data()
        server_user_data = all_tools_data.get(client_id, {})
        server_tools = server_user_data.get('tools', {})
        server_timestamp = server_user_data.get('last_updated')

        # 智能合并策略：选择最新的数据
        if server_timestamp and local_timestamp:
            try:
                server_time = datetime.fromisoformat(server_timestamp.replace('Z', '+00:00'))
                local_time = datetime.fromisoformat(local_timestamp.replace('Z', '+00:00'))

                if server_time > local_time:
                    # 服务器数据更新，使用服务器数据
                    merged_tools = server_tools
                    source = 'server'
                else:
                    # 本地数据更新，使用本地数据
                    merged_tools = local_tools
                    source = 'local'

                    # 更新服务器数据
                    all_tools_data[client_id] = {
                        'tools': local_tools,
                        'last_updated': datetime.now().isoformat(),
                        'client_ip': request.client.host if request.client else 'unknown'
                    }
                    save_tools_data(all_tools_data)
            except (ValueError, TypeError) as e:
                logger.warning(f"[ToolsData] Error comparing timestamps: {e}, using local data")
                merged_tools = local_tools
                source = 'local'

                # 更新服务器数据
                all_tools_data[client_id] = {
                    'tools': local_tools,
                    'last_updated': datetime.now().isoformat(),
                    'client_ip': request.client.host if request.client else 'unknown'
                }
                save_tools_data(all_tools_data)
        elif local_tools:
            # 只有本地数据，使用本地数据并更新服务器
            merged_tools = local_tools
            source = 'local'

            all_tools_data[client_id] = {
                'tools': local_tools,
                'last_updated': datetime.now().isoformat(),
                'client_ip': request.client.host if request.client else 'unknown'
            }
            save_tools_data(all_tools_data)
        else:
            # 使用服务器数据
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
