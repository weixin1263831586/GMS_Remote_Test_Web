"""User website-shortcuts (tools data) APIs.

从 assets.py 拆出（test_file_size_rules 600 行预算）：仅承载
``/api/websites/{save,load,sync}`` 与其持久化/校验辅助；文件列表、
OpenGrok、favicon 仍归 assets.py。
"""

import contextlib
import json
import logging
import os
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.users import get_client_display_id_from_request, get_client_id_from_request
from foundation.config import TOOLS_DATA_FILE
from foundation.errors import handle_api_errors
from foundation.private_config import write_private_json
from foundation.responses import error_response


logger = logging.getLogger(__name__)

router = APIRouter()


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
        write_private_json(Path(TOOLS_DATA_FILE), tools_data)
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
        try:
            data = await request.json()
        except ValueError:
            return error_response('Invalid JSON body', status_code=400)
        if not isinstance(data, dict):
            return error_response('Invalid request body', status_code=400)
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

    except Exception:
        logger.exception("[ToolsData] Error in save_user_tools")
        return error_response('Internal server error', status_code=500)


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

    except Exception:
        logger.exception("[ToolsData] Error in load_user_tools")
        return error_response('Internal server error', status_code=500)


@router.post("/api/websites/sync")
@handle_api_errors
async def sync_user_tools(request: Request):
    """Sync the user's tools data, keeping whichever copy (local or server) is newer."""
    try:
        try:
            data = await request.json()
        except ValueError:
            return error_response('Invalid JSON body', status_code=400)
        if not isinstance(data, dict):
            return error_response('Invalid request body', status_code=400)
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
        if (
            isinstance(server_timestamp, str)
            and isinstance(local_timestamp, str)
            and server_timestamp
            and local_timestamp
        ):
            try:
                server_time = datetime.fromisoformat(server_timestamp.replace('Z', '+00:00'))
                local_time = datetime.fromisoformat(local_timestamp.replace('Z', '+00:00'))
                # 服务端写入的是本地 naive 时间，客户端（如
                # new Date().toISOString()）带 Z 后缀为 aware 时间；
                # naive 一侧先归一到本地时区再比较，避免直接比较抛 TypeError。
                if server_time.tzinfo is None and local_time.tzinfo is not None:
                    server_time = server_time.astimezone()
                if local_time.tzinfo is None and server_time.tzinfo is not None:
                    local_time = local_time.astimezone()
                use_local = local_time >= server_time
            except (ValueError, TypeError) as e:
                logger.warning(f"[ToolsData] Error comparing timestamps: {e}, using local data")
                use_local = True
        elif local_tools:
            use_local = True

        if use_local:
            if not _save_user_tools_entry(all_tools_data, client_id, local_tools, request):
                return error_response('Failed to save tools data', status_code=500)
            merged_tools = local_tools
            source = 'local'
            last_updated = all_tools_data.get(client_id, {}).get('last_updated')
        else:
            merged_tools = server_tools
            source = 'server'
            last_updated = server_user_data.get('last_updated')

        logger.info(f"[ToolsData] Synced tools data for {client_id}, source: {source}")

        return JSONResponse(content={
            'success': True,
            'tools': merged_tools,
            'source': source,
            'last_updated': last_updated
        })

    except Exception:
        logger.exception("[ToolsData] Error in sync_user_tools")
        return error_response('Internal server error', status_code=500)
