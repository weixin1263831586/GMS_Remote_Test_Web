"""System router - WebSocket, health check, docs, help, skills download, root page."""

import asyncio
import json
import logging
import os
import re
import tarfile
import tempfile
from datetime import datetime
from urllib.parse import urlparse

import aiohttp
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask

from features.auth import AUTH_COOKIE_NAME, auth_service
from features.system.api_docs_list import API_DOCS_LIST
from features.system.state import global_state
from features.system.terminal_service import (
    close_terminal_session_resources,
    handle_terminal_connect,
    handle_terminal_input,
    handle_terminal_resize,
    handle_tradefed_list_results,
    refresh_devices_websocket,
)
from features.system.vnc import NOVNC_WEB_PORT
from features.users import get_client_ip
from features.users import runtime as users_runtime
from foundation.config import DEFAULT_SERVER_URL, PROJECT_ROOT, config_manager
from foundation.errors import handle_api_errors
from foundation.files import FileUtils
from foundation.responses import error_response


logger = logging.getLogger(__name__)
router = APIRouter()
GMS_ASSISTANT_UPSTREAM = os.getenv("GMS_ASSISTANT_URL", "http://172.16.14.248:5173").rstrip("/")

# Template factory (initialized from app.py)
_templates = None

SHELL_PAGE_TITLES = {
    "test": "测试界面 - GMS远程测试",
    "desktop": "主机桌面 - GMS远程测试",
    "terminal": "主机终端 - GMS远程测试",
    "users": "用户管理 - GMS远程测试",
    "devices": "设备管理 - GMS远程测试",
    "reports": "报告管理 - GMS远程测试",
    "report-analysis": "报告分析 - GMS远程测试",
    "apk-analysis": "APK分析 - GMS远程测试",
    "test-suites": "测试套件 - GMS远程测试",
    "api-docs": "系统接口 - GMS远程测试",
    "architecture": "系统架构 - GMS远程测试",
    "websites": "常用网址 - GMS远程测试",
    "tools": "常用工具 - GMS远程测试",
    "security-audit": "安全审计 - GMS 远程测试",
    "gms-assistant": "GMS助手 - GMS 远程测试",
    "automation": "GMS ATS - GMS 远程测试",
    "redmine-agent": "Redmine - GMS 远程测试",
    "gerrit-dashboard": "Gerrit看板 - GMS 远程测试",
    "agent": "对话Agent - GMS 远程测试",
}


def init_templates(templates):
    """Initialize Jinja2 templates reference from the main app."""
    global _templates
    _templates = templates


def _get_websocket_client_ip(websocket: WebSocket) -> str:
    """Resolve browser client IP for WebSocket requests."""
    return get_client_ip(websocket)


def _get_websocket_client_identity(websocket: WebSocket, path_client_id: str) -> tuple[str, str, str]:
    """Return (state client id, display id, username) for WebSocket state."""
    user = auth_service.get_user_for_token(websocket.cookies.get(AUTH_COOKIE_NAME))
    client_ip = _get_websocket_client_ip(websocket)
    if user:
        display_id = f"{user.username}@{client_ip}" if client_ip and client_ip != "unknown" else user.username
        if path_client_id.startswith("terminal_"):
            return path_client_id, display_id, user.username
        # HTTP APIs key runtime state by authenticated username.  WebSocket
        # connections must use the same key or real-time test logs are sent to
        # a different entry and silently disappear from the browser.
        return user.username, display_id, user.username

    username = "unknown"
    try:
        config = config_manager.load_config()
        username = str((config.get("client_hosts") or {}).get(client_ip) or "").strip() or "unknown"
    except Exception:
        username = "unknown"

    display_id = f"{username}@{client_ip}" if username != "unknown" and client_ip != "unknown" else client_ip
    if path_client_id.startswith("terminal_"):
        return path_client_id, display_id or path_client_id, username
    resolved_id = display_id or path_client_id or "unknown"
    if path_client_id and path_client_id != resolved_id:
        logger.debug("WebSocket anonymous client_id adjusted: path=%s resolved=%s", path_client_id, resolved_id)
    return resolved_id, display_id or resolved_id, username


# ==================== Root Page ====================

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板"""
    config = config_manager.load_config()
    saved_page = request.cookies.get("gms_current_page") or "test"
    initial_title = SHELL_PAGE_TITLES.get(saved_page, SHELL_PAGE_TITLES["test"])

    response = _templates.TemplateResponse(
        request=request,
        name="shell.html",
        context={"config": config, "initial_title": initial_title},
    )
    # 短暂复用导航外壳；must-revalidate 保证过期后确认新版本。
    response.headers["Cache-Control"] = "private, max-age=10, must-revalidate"
    return response


def _rewrite_gms_assistant_content(text: str, request: Request, proxy_base: str = "") -> str:
    """Rewrite upstream absolute URLs to this HTTPS origin."""
    upstream_https = re.sub(r"^http://", "https://", GMS_ASSISTANT_UPSTREAM)
    base = proxy_base.rstrip("/")
    replacements = {
        GMS_ASSISTANT_UPSTREAM: base,
        upstream_https: base,
        GMS_ASSISTANT_UPSTREAM.replace("/", "\\/"): base.replace("/", "\\/"),
        upstream_https.replace("/", "\\/"): base.replace("/", "\\/"),
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    root_prefixes = ("/assets/", "/api/")
    for prefix in root_prefixes:
        text = text.replace(f'"{prefix}', f'"{base}{prefix}')
        text = text.replace(f"'{prefix}", f"'{base}{prefix}")
        text = text.replace(f"`{prefix}", f"`{base}{prefix}")
    return text


async def _proxy_gms_assistant_path(path: str, request: Request, proxy_base: str = ""):
    """Same-origin HTTPS proxy for the external HTTP GMS assistant."""
    upstream_url = f"{GMS_ASSISTANT_UPSTREAM}/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    excluded_headers = {
        "connection",
        "content-encoding",
        "content-length",
        "content-security-policy",
        "date",
        "etag",
        "expires",
        "host",
        "keep-alive",
        "last-modified",
        "proxy-authenticate",
        "proxy-authorization",
        "server",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in excluded_headers
    }
    request_headers["Host"] = urlparse(GMS_ASSISTANT_UPSTREAM).netloc

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.request(
            request.method,
            upstream_url,
            headers=request_headers,
            data=await request.body(),
            allow_redirects=False,
        ) as upstream_response:
            body = await upstream_response.read()
            content_type = upstream_response.headers.get("content-type", "")
            response_headers = {
                key: value
                for key, value in upstream_response.headers.items()
                if key.lower() not in excluded_headers
            }

            if upstream_response.status in {301, 302, 303, 307, 308}:
                location = response_headers.get("Location") or response_headers.get("location")
                if location:
                    response_headers["Location"] = location.replace(GMS_ASSISTANT_UPSTREAM, "/gms-assistant")

            if any(marker in content_type for marker in ("text/", "javascript", "json")):
                try:
                    text = body.decode(upstream_response.charset or "utf-8", errors="replace")
                    body = _rewrite_gms_assistant_content(text, request, proxy_base=proxy_base).encode("utf-8")
                    response_headers.pop("Content-Length", None)
                    response_headers.pop("content-length", None)
                except Exception:
                    logger.debug("[GMS_ASSISTANT_PROXY] 跳过内容重写: %s", upstream_url, exc_info=True)

            return Response(
                content=body,
                status_code=upstream_response.status,
                media_type=content_type.split(";")[0] if content_type else None,
                headers=response_headers,
            )
    except Exception as e:
        logger.error("[GMS_ASSISTANT_PROXY] 代理失败 %s: %s", upstream_url, e, exc_info=True)
        return JSONResponse(
            content={"success": False, "error": f"GMS助手代理失败: {e}"},
            status_code=502,
        )


@router.api_route(
    "/gms-assistant/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant(path: str, request: Request):
    return await _proxy_gms_assistant_path(path, request, proxy_base="/gms-assistant")


@router.api_route(
    "/public/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant_public(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"public/{path}", request)


@router.api_route(
    "/assets/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_assets(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"assets/{path}", request)


@router.api_route(
    "/@vite/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_vite(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"@vite/{path}", request)


@router.api_route(
    "/@react-refresh",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_react_refresh(request: Request):
    return await _proxy_gms_assistant_path("@react-refresh", request)


@router.api_route(
    "/src/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_src(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"src/{path}", request)


@router.api_route(
    "/node_modules/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_node_modules(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"node_modules/{path}", request)


@router.api_route(
    "/@id/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_vite_id(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"@id/{path}", request)


@router.api_route(
    "/@fs/{path:path}",
    methods=["GET"],
    include_in_schema=False,
)
async def proxy_gms_assistant_vite_fs(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"@fs/{path}", request)


@router.api_route(
    "/api/public/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_gms_assistant_public_api(path: str, request: Request):
    return await _proxy_gms_assistant_path(f"api/public/{path}", request)


# ==================== Health Check ====================

@router.get("/api/system/health")
@handle_api_errors
async def health_check():
    """健康检查"""
    return JSONResponse(content={
        "status": "ok",
        "service": "GMS Auto Test - FastAPI Server (Port 5001)",
        "framework": "FastAPI",
        "version": "4.0.0",
        "timestamp": datetime.now().isoformat(),
        "websocket_connections": len(global_state.websocket_connections),
        "modules": {
            "config_manager": "✓",
            "device_manager": "✓",
            "test_runner": "✓",
            "test_report_manager": "✓",
            "vnc_manager": "✓",
            "adb_forward_manager": "✓",
            "usbip_manager": "✓",
            "client_manager": "✓",
            "device_lock_manager": "✓",
            "test_logs_manager": "✓"
        }
    })


# ==================== Skills Download ====================

@router.get("/api/system/skills")
async def download_skills_zip(request: Request, skill_name: str = Query("gms-remote-test", description="技能名称")):
    """下载指定技能目录的 zip 文件

    Args:
        skill_name: 技能名称，默认为 gms-remote-test

    Returns:
        ZIP 文件下载
    """
    try:
        logger.info(f"[SKILLS_DOWNLOAD] 请求下载技能包: {skill_name}")

        skills_base_dir = os.path.join(PROJECT_ROOT, 'skills')
        skills_dir = os.path.join(skills_base_dir, skill_name)

        if not os.path.exists(skills_dir):
            logger.error(f"[SKILLS_DOWNLOAD] 技能目录不存在：{skills_dir}")
            return JSONResponse(
                content={'success': False, 'error': f'技能目录不存在：{skill_name}'},
                status_code=404
            )

        zip_filename = f"{skill_name}-skills.zip"
        result = FileUtils.create_zip_from_directory(skills_dir, zip_filename)

        if result is None:
            return JSONResponse(
                content={'success': False, 'error': 'ZIP 文件创建失败：目录为空'},
                status_code=500
            )

        zip_data, _file_count = result

        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{zip_filename}\""
            }
        )

    except Exception as e:
        logger.error(f"[SKILLS_DOWNLOAD] Error: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


# ==================== Install.sh Download ====================

@router.get("/api/system/install-sh")
async def download_install_sh(request: Request):
    """下载 install.sh 部署脚本

    Returns:
        install.sh 脚本文件
    """
    try:
        logger.info("[INSTALL_SH_DOWNLOAD] 请求下载 install.sh")

        install_sh_path = os.path.join(PROJECT_ROOT, 'install.sh')

        if not os.path.exists(install_sh_path):
            logger.error(f"[INSTALL_SH_DOWNLOAD] 文件不存在：{install_sh_path}")
            return JSONResponse(
                content={'success': False, 'error': '部署脚本文件不存在'},
                status_code=404
            )

        with open(install_sh_path, encoding='utf-8') as f:
            content = f.read()

        base_url = str(request.base_url).rstrip('/')
        lines = content.splitlines(keepends=True)
        injected = f'export GMS_INSTALL_BASE_URL="{base_url}"\n'
        if lines and lines[0].startswith('#!'):
            content = ''.join([lines[0], injected, *lines[1:]])
        else:
            content = injected + content

        return Response(
            content=content,
            media_type="text/x-shellscript",
            headers={
                "Content-Disposition": "attachment; filename=\"install.sh\""
            }
        )

    except Exception as e:
        logger.exception(f"[INSTALL_SH_DOWNLOAD] 下载失败：{e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


@router.get("/api/system/install-package")
async def download_install_package(request: Request):
    """下载当前 Web App 安装包，用于 curl | bash 远程部署。"""
    tmp = None
    try:
        logger.info("[INSTALL_PACKAGE_DOWNLOAD] 请求下载安装包")
        with tempfile.NamedTemporaryFile(prefix='gms-web-app-', suffix='.tar.gz', delete=False) as tmp:
            tmp_path = tmp.name

        root_name = 'gms-web-app'
        exclude_dirs = {
            '.git', '.agents', '.codex', '__pycache__', '.pytest_cache',
            '.certs', '.venv', 'dist', 'logs', 'data/apk_uploads',
        }
        exclude_files = {
            'local.diff',
            'fastapi.pid',
            'configs/config_runtime.json',
            'configs/client_ssh_credentials.local.json',
            'configs/redmine_auth.json',
        }

        def should_exclude(rel_path: str) -> bool:
            rel_path = rel_path.strip('/')
            if not rel_path:
                return False
            parts = rel_path.split('/')
            if any(part in exclude_dirs for part in parts):
                return True
            if rel_path in exclude_files:
                return True
            name = parts[-1]
            if name.endswith(('.pyc', '.pyo', '.log')) or '.log.backup.' in name:
                return True
            if rel_path.startswith('data/') and name.endswith('.json'):
                return True
            return False

        with tarfile.open(tmp_path, 'w:gz') as tar:
            for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
                rel_dir = os.path.relpath(dirpath, PROJECT_ROOT)
                rel_dir = '' if rel_dir == '.' else rel_dir
                dirnames[:] = [
                    d for d in dirnames
                    if not should_exclude(os.path.join(rel_dir, d))
                ]
                for filename in filenames:
                    rel_path = os.path.join(rel_dir, filename) if rel_dir else filename
                    if should_exclude(rel_path):
                        continue
                    full_path = os.path.join(dirpath, filename)
                    tar.add(full_path, arcname=os.path.join(root_name, rel_path), recursive=False)

        return FileResponse(
            tmp_path,
            media_type="application/gzip",
            filename="gms-web-app.tar.gz",
            background=BackgroundTask(lambda path: os.path.exists(path) and os.unlink(path), tmp_path),
        )

    except Exception as e:
        logger.exception(f"[INSTALL_PACKAGE_DOWNLOAD] 下载失败：{e}")
        if tmp is not None and os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


# ==================== Architecture Page ====================

@router.get("/templates/architecture.html")
async def get_architecture():
    """获取系统架构图"""
    architecture_file = os.path.join(PROJECT_ROOT, 'web', 'templates', 'architecture.html')
    if os.path.exists(architecture_file):
        with open(architecture_file, encoding='utf-8') as f:
            content = f.read()
        return HTMLResponse(content=content)
    return JSONResponse(status_code=404, content={"error": "Architecture diagram not found"})


# ==================== API Docs ====================

@router.get("/api/system/docs")
async def get_api_docs():
    """获取所有API文档"""
    try:
        # 直接返回预定义的API列表，避免每次请求重新构建
        return JSONResponse(
            content={
                "success": True,
                "apis": API_DOCS_LIST,
                "total": len(API_DOCS_LIST)
            },
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Content-Type-Options": "nosniff"
            }
        )
    except Exception as e:
        logger.error(f"Error getting API docs: {e}")
        return error_response(str(e), status_code=500)


# ==================== API Help ====================

@router.get("/api/system/help")
async def get_api_help(api_path: str | None = None):
    """获取API帮助信息（统一接口）

    Args:
        api_path: 可选的API路径（如 'api/test/start'）
                  - 不提供：返回所有API列表
                  - 提供：返回指定API的详细帮助

    Examples:
        # 获取所有API列表
        curl -s "http://localhost:5001/api/system/help"

        # 获取单个API详细帮助
        curl -s "http://localhost:5001/api/system/help?api_path=api/test/start"
    """
    try:
        if api_path:
            # 查找匹配的API
            api_doc = None
            for api in API_DOCS_LIST:
                # 移除开头的斜杠进行匹配
                if api['path'].lstrip('/') == api_path:
                    api_doc = api
                    break

            if not api_doc:
                return error_response(f"API not found: /{api_path}", status_code=404)

            # 生成帮助文本
            help_text = generate_per_api_help_text(api_doc['method'], api_doc['path'])

            if not help_text:
                return error_response(f"Help not available for: /{api_path}", status_code=404)

            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

        # 否则返回所有API列表
        # 按方法类型和路径排序
        sorted_apis = sorted(API_DOCS_LIST, key=lambda x: (x['method'], x['path']))

        # 生成纯文本API列表
        api_list = []
        for api in sorted_apis:
            # 格式：METHOD    PATH
            api_list.append(f"{api['method']:<10} {api['path']}")

        # 直接返回纯文本（每个API一行）
        text_content = "GMS Auto Test API List\n"
        text_content += "=" * 60 + "\n\n"
        text_content += f"Total: {len(api_list)} APIs\n"
        text_content += f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        text_content += "=" * 60 + "\n\n"
        text_content += "\n".join(api_list) + "\n"  # 确保最后也有换行

        # 添加使用示例
        text_content += "\n" + "=" * 60 + "\n"
        text_content += "Usage Examples:\n"
        text_content += f'  curl -s "{DEFAULT_SERVER_URL}/api/system/help"                          \n'
        text_content += f'  curl -s "{DEFAULT_SERVER_URL}/api/system/help?api_path=api/devices/list"\n'
        text_content += f'  curl -s "{DEFAULT_SERVER_URL}/api/devices/list?help=1"                 \n'
        text_content += f'  curl -s "{DEFAULT_SERVER_URL}/api/test/status?help=1"                   \n'

        return PlainTextResponse(
            content=text_content,
            headers={
                "Cache-Control": "public, max-age=300",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting API help: {e}")
        return error_response(str(e), status_code=500)


# ==================== WebSocket ====================

@router.websocket("/api/system/websocket/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    client_id, display_client_id, username = _get_websocket_client_identity(websocket, client_id)
    client_ip = _get_websocket_client_ip(websocket)

    await websocket.accept()
    if users_runtime.get_or_create_user_state:
        users_runtime.get_or_create_user_state(client_id)
    else:
        with global_state.user_states_lock:
            global_state.user_states.setdefault(client_id, {
                "running": False,
                "devices": [],
                "created_at": datetime.now().isoformat(),
                "last_seen": datetime.now().isoformat(),
            })
    with global_state.user_states_lock:
        state = global_state.user_states.get(client_id)
        if state is not None:
            state["client_username"] = username
            state["client_ip"] = client_ip
            state["display_client_id"] = display_client_id
    with global_state.websocket_connections_lock:
        global_state.websocket_connections[client_id] = websocket
    logger.info(f"WebSocket client connected: {client_id} ({display_client_id})")

    try:
        while True:
            # 接收消息（添加30秒超时，用于心跳检测）
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30.0)
                message_type = data.get('type')
            except asyncio.TimeoutError:
                # 超时后发送心跳包，保持连接活跃
                try:
                    await websocket.send_json({
                        'type': 'heartbeat',
                        'timestamp': datetime.now().isoformat()
                    })
                    continue  # 继续下一次心跳检测
                except Exception as e:
                    logger.warning(f"[WebSocket] Failed to send heartbeat for {client_id}: {e}")
                    break

            # 处理接收到的消息
            if message_type == 'ping':
                await websocket.send_json({
                    'type': 'pong',
                    'timestamp': datetime.now().isoformat()
                })

            elif message_type == 'refresh_devices':
                await refresh_devices_websocket(client_id, websocket)

            elif message_type == 'terminal_connect':
                await handle_terminal_connect(client_id, websocket, data)

            elif message_type == 'terminal_input':
                await handle_terminal_input(client_id, websocket, data)

            elif message_type == 'terminal_resize':
                await handle_terminal_resize(client_id, websocket, data)

            elif message_type == 'tradefed_list_results':
                await handle_tradefed_list_results(client_id, websocket, data)

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
    finally:
        # 清理WebSocket连接
        with global_state.websocket_connections_lock:
            if global_state.websocket_connections.get(client_id) is websocket:
                del global_state.websocket_connections[client_id]

        # 清理终端SSH会话（如果存在）
        with global_state.terminal_lock:
            if client_id in global_state.terminal_ssh_sessions:
                session_info = global_state.terminal_ssh_sessions[client_id]
                close_terminal_session_resources(session_info)
                del global_state.terminal_ssh_sessions[client_id]


# ==================== Helper Functions ====================



def generate_per_api_help_text(method: str, path: str) -> str | None:
    """为指定API生成详细帮助文本

    Args:
        method: HTTP方法 (GET/POST/DELETE等)
        path: API路径

    Returns:
        格式化的帮助文本，如果API不存在则返回None
    """

    def get_display_width(text):
        """计算字符串的显示宽度（中文算2个字符）"""
        width = 0
        for char in text:
            if ord(char) > 127:
                width += 2
            else:
                width += 1
        return width

    def pad_string(text, target_width, align='left'):
        """填充字符串到目标显示宽度，考虑中文"""
        current_width = get_display_width(text)
        padding = target_width - current_width

        if align == 'center':
            left_pad = padding // 2
            right_pad = padding - left_pad
            return ' ' * left_pad + text + ' ' * right_pad
        elif align == 'right':
            return ' ' * padding + text
        else:  # left
            return text + ' ' * padding

    base_url = DEFAULT_SERVER_URL

    # 详细的API参数映射（与前端保持一致）
    API_DETAILS_MAP = {
        '/api/test/start': {
            'title': '启动测试',
            'description': '启动GMS测试(CTS/VTS/GTS等)',
            'params': [
                {'name': 'devices', 'type': 'array', 'required': True, 'desc': '设备序列号数组'},
                {'name': 'test_type', 'type': 'string', 'required': True, 'desc': '测试类型: CTS|VTS|STS|GTS|CTS_VERIFIER'},
                {'name': 'test_module', 'type': 'string', 'required': True, 'desc': '测试模块名称'},
                {'name': 'test_case', 'type': 'string', 'required': False, 'desc': '具体测试用例(可选)'},
                {'name': 'retry_dir', 'type': 'string', 'required': False, 'desc': '重试目录(可选)'},
                {'name': 'test_suite', 'type': 'string', 'required': False, 'desc': '测试套件路径(可选)'}
            ],
            'response': '{"success": true, "message": "测试已启动"}',
            'usage': '核心接口'
        },
        '/api/test/stop': {
            'title': '停止测试',
            'description': '停止当前正在运行的测试',
            'params': [],
            'response': '{"success": true, "message": "测试已停止"}',
            'usage': ''
        },
        '/api/test/suites': {
            'title': '列出测试套件',
            'description': '列出指定路径下所有可用的测试套件',
            'params': [
                {'name': 'base_path', 'type': 'string', 'required': False, 'desc': '搜索路径，默认使用配置的 suites_path'}
            ],
            'response': '{"success": true, "suites": [{"test_type": "cts", "version": "android-cts-16_r4", "tools_path": "...", "full_path": "...", "binary": "cts-tradefed"}], "count": 9, "base_path": "~/GMS-Suite"}',
            'usage': 'gms-rt-test-suites'
        },
        '/api/devices/list': {
            'title': '获取设备列表',
            'description': '获取所有已连接的设备列表',
            'params': [],
            'response': '{"success": true, "devices": [...]}',
            'usage': ''
        },
        '/api/burn/firmware': {
            'title': '烧写固件',
            'description': '上传固件文件并烧写设备',
            'params': [
                {'name': 'firmware_file', 'type': 'file', 'required': True, 'desc': '固件文件（.img格式）'},
                {'name': 'devices', 'type': 'string', 'required': True, 'desc': '设备序列号（多个用逗号分隔）'},
                {'name': 'wipe_data', 'type': 'boolean', 'required': False, 'desc': '是否清除数据（默认true）'}
            ],
            'response': '{"success": true, "message": "固件烧写完成"}',
            'usage': ''
        },
        '/api/usbip/connect': {
            'title': '启动 USB/IP 连接',
            'description': '通过 USB/IP 连接到远程设备',
            'params': [
                {'name': 'device_host', 'type': 'string', 'required': True, 'desc': 'Windows 主机地址 (user@ip)'},
                {'name': 'device_password', 'type': 'string', 'required': True, 'desc': 'SSH 密码'}
            ],
            'response': '{"success": true, "devices": [...]}',
            'usage': ''
        },
        '/api/desktop/vnc/status': {
            'title': '查询Ubuntu主机桌面VNC状态',
            'description': '查询Ubuntu桌面VNC服务状态（运行中/已停止）和远程访问地址',
            'params': [],
            'response': f'{{"success": true, "running": true, "url": "http://xxx:{NOVNC_WEB_PORT}/vnc.html"}}',
            'usage': '检查Ubuntu桌面VNC服务是否正在运行，获取远程访问URL'
        },
        '/api/desktop/vnc/start': {
            'title': '启动Ubuntu主机桌面VNC',
            'description': '启动Ubuntu桌面VNC服务，返回VNC访问URL用于远程桌面连接',
            'params': [
                {'name': 'host', 'type': 'string', 'required': False, 'desc': 'Ubuntu主机桌面地址，格式：user@ip（可选，使用配置默认值）'},
                {'name': 'password', 'type': 'string', 'required': False, 'desc': 'SSH登录密码（可选）'},
                {'name': 'vnc_password', 'type': 'string', 'required': False, 'desc': 'VNC访问密码（可选）'}
            ],
            'response': f'{{"success": true, "url": "http://xxx:{NOVNC_WEB_PORT}/vnc.html"}}',
            'usage': '启动Ubuntu桌面的VNC服务，通过浏览器远程访问图形化桌面'
        },
        '/api/desktop/vnc/stop': {
            'title': '停止Ubuntu主机桌面VNC',
            'description': '停止Ubuntu桌面VNC服务，断开所有远程桌面连接',
            'params': [],
            'response': '{"success": true, "message": "Ubuntu主机桌面VNC已停止"}',
            'usage': '停止Ubuntu桌面VNC服务，释放系统资源'
        },
        '/api/desktop/validate': {
            'title': '验证Ubuntu主机',
            'description': '验证Ubuntu主机SSH连接并检查VNC服务可用性（host格式：user@ip）',
            'params': [
                {'name': 'host', 'type': 'string', 'required': True, 'desc': '主机地址（格式：user@ip，如user@192.168.1.100）'},
                {'name': 'password', 'type': 'string', 'required': False, 'desc': 'SSH登录密码（可选）'}
            ],
            'response': '{"success": true, "message": "SSH连接成功，VNC服务可用"}',
            'usage': '连接Ubuntu主机桌面前验证SSH连接和VNC服务状态'
        },
        '/api/ssh/ping': {
            'title': '测试网络连通性',
            'description': '测试测试主机和客户端之间的网络连通性（ping 测试）',
            'params': [
                {'name': 'test_host_ip', 'type': 'string', 'required': True, 'desc': '测试主机 IP 地址'},
                {'name': 'client_ip', 'type': 'string', 'required': True, 'desc': '客户端 IP 地址'}
            ],
            'response': '{"success": true, "reachable": true, "latency": "0.301ms", "same_network": false}',
            'usage': 'gms-rt-ssh-ping'
        }
    }
    # 查找 API 详情
    api_details = API_DETAILS_MAP.get(path)
    if not api_details:
        return None

    params = api_details.get('params', [])

    # 构建帮助文本
    help_text = ""

    # 固定的边框线（70个字符宽，包含左右边框）
    border_line = "+" + "=" * 68 + "+"
    mid_line = "+" + "=" * 68 + "+"
    bottom_line = "+" + "=" * 68 + "+"

    help_text += f"{border_line}\n"

    # 第一行：方法 + 路径
    method_part = f"  {method}  "
    # 目标：让字符串长度与边框线一致（70个字符）
    # 内容区：70 - 2(左右|) = 68个字符
    content_length = 68
    method_length = len(method_part)
    path_length = len(path)
    needed_padding = content_length - method_length - path_length
    path_part = path + ' ' * needed_padding

    help_text += f"|{method_part}{path_part}|\n"

    help_text += f"{mid_line}\n"

    # 第二行：描述
    description = api_details['description']
    desc_prefix = "  Desc: "
    prefix_length = len(desc_prefix)
    desc_length = len(description)

    # 对于包含中文的行，需要调整填充以确保视觉对齐
    # 计算中文字符数量
    chinese_chars = len([c for c in description + desc_prefix if ord(c) > 127])
    # 每个中文字符的显示宽度比字符长度多1，所以需要减少相应数量的空格
    # 但不能减少太多，否则字符串长度会不够
    # 这里我们减少一半的差值作为平衡
    visual_adjustment = chinese_chars // 2
    needed_padding = content_length - prefix_length - desc_length + visual_adjustment

    desc_part = description + ' ' * needed_padding

    help_text += f"|{desc_prefix}{desc_part}|\n"

    help_text += f"{bottom_line}\n\n"

    # 完整curl命令
    if method == 'GET':
        # 特殊处理文件下载端点
        if '/skills' in path:
            help_text += f'curl -s -OJ "{base_url}{path}"\n\n'
        else:
            help_text += f'curl -s "{base_url}{path}"\n\n'
    elif method == 'POST':
        has_file = any(p.get('type') == 'file' for p in params)
        if has_file:
            # FormData格式
            curl_cmd = f'curl -sX POST "{base_url}{path}"'
            for p in params:
                if p.get('type') == 'file':
                    curl_cmd += f' \\\n  -F "{p["name"]}=@VALUE"'
                elif p.get('type') == 'boolean':
                    curl_cmd += f' \\\n  -F "{p["name"]}=true"'
                else:
                    curl_cmd += f' \\\n  -F "{p["name"]}=VALUE"'
            help_text += curl_cmd + "\n\n"
        else:
            # JSON格式
            curl_cmd = f'curl -sX POST "{base_url}{path}"'
            if params:
                curl_cmd += ' \\\n  -H "Content-Type: application/json" \\\n  -d \''
                body_lines = ['{']
                for i, p in enumerate(params):
                    comma = "," if i < len(params) - 1 else ""
                    value = '["Serial"]' if p.get('type') == 'array' else '"VALUE"'
                    body_lines.append(f'    "{p["name"]}": {value}{comma}')
                body_lines.append('  }')
                curl_cmd += '\n'.join(body_lines) + '\''
            help_text += curl_cmd + "\n\n"
    elif method == 'DELETE':
        help_text += f'curl -X DELETE "{base_url}{path}"\n\n'

    # 标题
    usage = api_details.get('usage', '')
    if usage:
        help_text += f"### {api_details['title']} {usage}\n\n"
    else:
        help_text += f"### {api_details['title']}\n\n"

    # HTTP信息
    help_text += f"{method} {path}\n"
    if method == 'POST':
        has_file = any(p.get('type') == 'file' for p in params)
        if not has_file:
            help_text += "Content-Type: application/json\n"
    help_text += "\n"

    # 参数说明（表格格式）
    if params:
        help_text += "API Parameters\n\n"

        # 计算列宽（使用显示宽度，但确保最小宽度）
        name_width = max(get_display_width('API Param'), max((get_display_width(p['name']) for p in params), default=get_display_width('API Param')))
        desc_width = max(get_display_width('Description'), max(((get_display_width(p['desc'].split('(')[0]) + 6) for p in params), default=get_display_width('Description')))

        # 表格字符定义
        border_char = '-'
        corner_tl = '+'
        corner_tr = '+'
        corner_bl = '+'
        corner_br = '+'
        tee_top = '+'
        tee_bottom = '+'
        tee_cross = '+'
        bar = '|'

        # 列宽定义（固定）
        col1_width = name_width + 2      # API 参数列（含左右空格）
        col2_width = 6                    # 类型列（固定 6 字符，确保对齐）
        col3_width = desc_width + 10      # 说明列（含标记）
        col4_width = 14                   # 默认值列（固定 14 字符）

        # 构建表格行（使用显示宽度计算表头）
        top_border     = f"{corner_tl}{border_char * col1_width}{tee_top}{border_char * col2_width}{tee_top}{border_char * col3_width}{tee_top}{border_char * col4_width}{corner_tr}\n"
        header_row     = f"{bar}{pad_string('API Param', col1_width, 'center')}{bar}{pad_string('Type', col2_width, 'center')}{bar}{pad_string('Description', col3_width, 'center')}{bar}{pad_string('Default', col4_width, 'center')}{bar}\n"
        header_border  = f"{bar}{border_char * col1_width}{tee_top}{border_char * col2_width}{tee_top}{border_char * col3_width}{tee_top}{border_char * col4_width}{bar}\n"

        # 创建一个函数来生成正确长度的分隔线
        def create_separator():
            # 生成一个示例数据行来获取实际长度
            sample_row = f"{bar}{pad_string('sample', col1_width, 'center')}{bar}{pad_string('str', col2_width, 'center')}{bar}{pad_string('sample text', col3_width, 'left')}{bar}{pad_string('', col4_width, 'center')}{bar}"
            # 获取每一节的实际长度
            sections = []
            current_section = ""
            in_section = False
            for char in sample_row:
                if char == bar:
                    if in_section:
                        sections.append(current_section)
                        current_section = ""
                    in_section = True
                elif in_section:
                    current_section += char
            if current_section:
                sections.append(current_section)

            # 使用实际的字符串长度来构建分隔线
            if len(sections) >= 4:
                return f"{bar}{border_char * len(sections[0])}{tee_cross}{border_char * len(sections[1])}{tee_cross}{border_char * len(sections[2])}{tee_cross}{border_char * len(sections[3])}{bar}\n"
            else:
                # 备用方案
                return f"{bar}{border_char * col1_width}{tee_cross}{border_char * col2_width}{tee_cross}{border_char * col3_width}{tee_cross}{border_char * col4_width}{bar}\n"

        row_separator  = create_separator()
        bottom_border  = f"{corner_bl}{border_char * col1_width}{tee_bottom}{border_char * col2_width}{tee_bottom}{border_char * col3_width}{tee_bottom}{border_char * col4_width}{corner_br}\n"

        # 添加表头部分
        help_text += f"  {top_border}"
        help_text += f"  {header_row}"
        help_text += f"  {header_border}"

        # 参数行
        for i, param in enumerate(params):
            name = param['name']
            ptype = param.get('type', 'string')
            # 统一类型缩写，确保对齐
            type_map = {
                'array': 'arr',
                'string': 'str',
                'number': 'num',
                'integer': 'int',
                'boolean': 'bool',
                'object': 'obj'
            }
            ptype = type_map.get(ptype.lower(), ptype[:3])
            desc = param['desc'].split('(')[0].strip()  # 去掉 (可选) 等后缀
            default_val = param.get('default', '')
            required = param.get('required', False)

            # 在说明中添加必需/可选标记
            if required:
                desc_with_mark = f"{desc} *"
            else:
                desc_with_mark = f"{desc} (optional)"

            # 使用新的填充函数格式化每个单元格
            name_formatted = pad_string(name, col1_width, 'center')
            ptype_formatted = pad_string(ptype, col2_width, 'center')
            desc_formatted = pad_string(desc_with_mark, col3_width, 'left')
            default_formatted = pad_string(default_val, col4_width, 'center')

            row = f"{bar}{name_formatted}{bar}{ptype_formatted}{bar}{desc_formatted}{bar}{default_formatted}{bar}\n"
            help_text += f"  {row}"

            # 在每一行后面添加分隔线（除了最后一行）
            if i < len(params) - 1:
                help_text += f"  {row_separator}"

        # 表尾
        help_text += f"  {bottom_border}"
        help_text += "\n"

    # 响应示例
    help_text += "Response Example:\n"
    response_str = api_details.get('response', '{"success": true}')
    try:
        response_obj = json.loads(response_str)
        help_text += json.dumps(response_obj, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        help_text += response_str

    # 添加结尾换行符（两个换行，视觉上更明显）
    help_text += "\n\n"

    return help_text
