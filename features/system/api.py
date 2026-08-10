"""System router - WebSocket, health check, docs, help, skills download, root page."""

import asyncio
import hmac
import html
import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote, urlparse

import aiohttp
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from features.auth import AUTH_COOKIE_NAME, CurrentUser, auth_service, require_role
from features.system.api_docs_list import API_DOCS_LIST
from features.system.health import readiness
from features.system.metrics import metrics_token, render_metrics
from features.system.state import global_state
from features.system.terminal_auxiliary import (
    handle_tradefed_list_results,
    refresh_devices_websocket,
)
from features.system.terminal_service import (
    close_websocket_terminal,
    handle_terminal_connect,
    handle_terminal_input,
    handle_terminal_resize,
)
from features.system.vnc import NOVNC_WEB_PORT
from features.system.websocket_security import (
    authorize_websocket_identity,
)
from features.system.websocket_security import (
    get_websocket_client_ip as _get_websocket_client_ip,
)
from features.users import runtime as users_runtime
from foundation.config import DEFAULT_SERVER_URL, PROJECT_ROOT, config_manager
from foundation.files import FileUtils
from foundation.responses import error_response


logger = logging.getLogger(__name__)
router = APIRouter()

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
    "cluster": "主机集群 - GMS 远程测试",
    "redmine-agent": "Redmine - GMS 远程测试",
    "gerrit-dashboard": "Gerrit看板 - GMS 远程测试",
    "agent": "对话Agent - GMS 远程测试",
    "notes": "个人知识库 - GMS 远程测试",
}

_EXTERNAL_GOOGLE_FONT_LINK_RE = re.compile(
    r"<link\b(?=[^>]*\bhref\s*=\s*['\"]https://fonts\.(?:googleapis|gstatic)\.com(?:/[^'\"]*)?['\"])[^>]*>\s*",
    re.IGNORECASE,
)


def init_templates(templates):
    """Initialize Jinja2 templates reference from the main app."""
    global _templates
    _templates = templates


def _gms_assistant_upstream() -> str:
    """Resolve the optional upstream from environment or product config."""
    env_url = str(os.getenv("GMS_ASSISTANT_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    config = config_manager.load_config()
    external = config.get("external_services") or {}
    return str(external.get("gms_assistant_url") or "").strip().rstrip("/")


# ==================== Root Page ====================

@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the browser's conventional favicon URL."""
    return FileResponse(
        os.path.join(PROJECT_ROOT, "web", "static", "favicon.svg"),
        media_type="image/svg+xml",
    )


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板"""
    config = dict(config_manager.load_config())
    # 模板使用解析后的运行时主机和用户名。
    config["ubuntu_user"] = config_manager.get_ubuntu_user(config)
    config["ubuntu_host"] = config_manager.get_ubuntu_host(config)
    request_host = str(request.url.hostname or "").strip()
    if (
        str(config["ubuntu_host"]).strip().lower() in {"127.0.0.1", "localhost", "::1"}
        and request_host
        and request_host.lower() not in {"127.0.0.1", "localhost", "::1"}
    ):
        config["ubuntu_host"] = request_host
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


def _rewrite_gms_assistant_content(
    text: str,
    request: Request,
    proxy_base: str = "",
    upstream: str = "",
) -> str:
    """Rewrite upstream absolute URLs to this HTTPS origin."""
    # The shell deliberately keeps style-src restricted to same-origin CSS.
    # Remove the assistant's optional Google Fonts link so the iframe uses its
    # local/system fallback fonts without producing CSP violations.
    text = _EXTERNAL_GOOGLE_FONT_LINK_RE.sub("", text)
    upstream = upstream or _gms_assistant_upstream()
    if not upstream:
        return text
    upstream_https = re.sub(r"^http://", "https://", upstream)
    base = proxy_base.rstrip("/")
    replacements = {
        upstream: base,
        upstream_https: base,
        upstream.replace("/", "\\/"): base.replace("/", "\\/"),
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
    upstream = _gms_assistant_upstream()
    if not upstream:
        if path.startswith("public/agents/") and path.endswith("/chat"):
            return HTMLResponse(
                """<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<title>GMS助手未配置</title><style>
body{font-family:system-ui,sans-serif;margin:0;padding:32px;color:#243042;background:#f7f8fa}
main{max-width:640px;margin:8vh auto;padding:28px;background:#fff;border:1px solid #e3e7ed;border-radius:12px}
code{background:#f0f2f5;padding:3px 6px;border-radius:4px}
</style><main><h2>GMS助手暂未配置</h2>
<p>请在服务器配置中设置 <code>external_services.gms_assistant_url</code>，然后刷新此页面。</p>
</main>""",
                status_code=200,
            )
        return JSONResponse(
            content={"success": False, "error": "GMS助手未配置，请设置 external_services.gms_assistant_url"},
            status_code=503,
        )
    upstream_url = f"{upstream}/{path}"
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
    request_headers["Host"] = urlparse(upstream).netloc

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
                    response_headers["Location"] = location.replace(upstream, "/gms-assistant")

            if any(marker in content_type for marker in ("text/", "javascript", "json")):
                try:
                    text = body.decode(upstream_response.charset or "utf-8", errors="replace")
                    body = _rewrite_gms_assistant_content(
                        text, request, proxy_base=proxy_base, upstream=upstream
                    ).encode("utf-8")
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
async def health_check():
    """Compatibility liveness probe; dependency readiness has its own route."""
    return JSONResponse(content={
        "status": "alive",
        "service": "gms-remote-test-controller",
        "version": "4.0.0",
        "timestamp": datetime.now().isoformat(),
    })


@router.get("/api/system/health/live")
async def liveness_check():
    return {"status": "alive", "timestamp": datetime.now().isoformat()}


@router.get("/api/system/health/ready")
async def readiness_check(request: Request):
    result = await asyncio.to_thread(readiness, request.app)
    return JSONResponse(
        status_code=200 if result["ready"] else 503,
        content={
            "status": "ready" if result["ready"] else "not_ready",
            "timestamp": datetime.now().isoformat(),
        },
    )


@router.get("/api/system/health/details")
async def health_details(
    request: Request,
    _admin: CurrentUser = Depends(require_role("admin")),
):
    result = await asyncio.to_thread(readiness, request.app, force=True)
    return JSONResponse(
        status_code=200 if result["ready"] else 503,
        content=result,
    )


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    authorization: str = Header(default="", alias="Authorization"),
):
    expected = metrics_token()
    if not expected:
        raise HTTPException(status_code=503, detail="GMS_METRICS_TOKEN is required")
    scheme, separator, supplied = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied.strip(), expected)
    ):
        raise HTTPException(status_code=401, detail="Invalid metrics credential")
    return Response(
        content=await asyncio.to_thread(render_metrics),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


# ==================== Skills Download ====================

def _skill_directory(skill_name: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skill_name or ""):
        return None
    skills_base_dir = os.path.realpath(os.path.join(PROJECT_ROOT, "skills"))
    skills_dir = os.path.realpath(os.path.join(skills_base_dir, skill_name))
    if not skills_dir.startswith(skills_base_dir + os.sep):
        return None
    return skills_dir


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

        skills_dir = _skill_directory(skill_name)

        if not skills_dir or not os.path.isdir(skills_dir):
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
        return error_response("技能包下载失败", status_code=500)


@router.get("/api/system/skills/install.sh")
async def download_skill_installer(request: Request):
    """Return a Controller-bound one-command installer for gms-remote-test."""
    installer_path = os.path.join(
        PROJECT_ROOT,
        "skills",
        "gms-remote-test",
        "scripts",
        "install.sh",
    )
    try:
        def read_installer() -> str:
            with open(installer_path, encoding="utf-8") as installer_file:
                return installer_file.read()

        template = await asyncio.to_thread(read_installer)
    except OSError:
        logger.exception("[SKILLS_INSTALLER] installer script is unavailable")
        return error_response("技能安装脚本不可用", status_code=500)

    server_url = str(request.base_url).rstrip("/")
    download_url = (
        f"{server_url}/api/system/skills?"
        f"skill_name={quote('gms-remote-test')}"
    )

    def shell_literal(value: str) -> str:
        return value.replace("'", "'\"'\"'")

    content = template.replace(
        "__GMS_REMOTE_TEST_SERVER__",
        shell_literal(server_url),
    ).replace(
        "__GMS_SKILL_DOWNLOAD_URL__",
        shell_literal(download_url),
    )
    return Response(
        content=content,
        media_type="text/x-shellscript",
        headers={
            "Content-Disposition": 'inline; filename="install-gms-remote-test.sh"',
            "Cache-Control": "no-store",
        },
    )


# ==================== Architecture Page ====================

@router.get("/templates/architecture.html")
async def get_architecture():
    """获取系统架构图"""
    architecture_file = os.path.join(PROJECT_ROOT, 'web', 'templates', 'architecture.html')
    if os.path.exists(architecture_file):
        with open(architecture_file, encoding='utf-8') as f:
            content = f.read()
        config = config_manager.load_config()
        ui_defaults = config.get("ui_defaults") or {}
        build_server = str(ui_defaults.get("architecture_build_server") or "未配置")
        content = content.replace("{{BUILD_SERVER_LABEL}}", html.escape(build_server))
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
    """返回全部 API 列表或指定路径的详细帮助。"""
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
    identity = await authorize_websocket_identity(websocket, client_id)
    if identity is None:
        return
    client_id, display_client_id, username = identity
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
                token = websocket.cookies.get(AUTH_COOKIE_NAME)
                live_user = auth_service.get_user_for_token(token, refresh=False)
                if live_user is not None and auth_service.get_elevated_until(token):
                    await handle_terminal_connect(client_id, websocket, data)
                else:
                    await websocket.send_json({
                        'type': 'terminal_error',
                        'error': '需要已提权的管理员会话',
                        'elevation_required': True,
                    })

            elif message_type == 'terminal_input':
                token = websocket.cookies.get(AUTH_COOKIE_NAME)
                live_user = auth_service.get_user_for_token(token, refresh=False)
                if live_user is not None and auth_service.get_elevated_until(token):
                    await handle_terminal_input(client_id, websocket, data)
                else:
                    close_websocket_terminal(websocket)
                    await websocket.send_json({
                        'type': 'terminal_error',
                        'error': '管理员提权已失效，终端已关闭',
                        'elevation_required': True,
                    })

            elif message_type == 'terminal_resize':
                token = websocket.cookies.get(AUTH_COOKIE_NAME)
                live_user = auth_service.get_user_for_token(token, refresh=False)
                if live_user is not None and auth_service.get_elevated_until(token):
                    await handle_terminal_resize(client_id, websocket, data)
                else:
                    close_websocket_terminal(websocket)
                    await websocket.send_json({
                        'type': 'terminal_error',
                        'error': '管理员提权已失效，终端已关闭',
                        'elevation_required': True,
                    })

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

        close_websocket_terminal(websocket)


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

    # 按中文字符宽度修正填充，保持表格基本对齐。
    chinese_chars = len([c for c in description + desc_prefix if ord(c) > 127])
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
