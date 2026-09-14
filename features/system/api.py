"""System router - WebSocket, health check, docs, help, skills download, root page."""

import asyncio
import hashlib
import html
import logging
import os
import re
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from features.auth import AUTH_COOKIE_NAME, auth_service
from features.system import agent_package_registry, gms_assistant_proxy, jq_binary
from features.system.api_docs_list import API_DOCS_LIST
from features.system.api_help import generate_per_api_help_text
from features.system.skill_archive_signing import (
    sign_skill_archive,
)
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
from features.system.websocket_security import (
    authorize_websocket_identity,
)
from features.system.websocket_security import (
    get_websocket_client_ip as _get_websocket_client_ip,
)
from features.users import runtime as users_runtime
from foundation.cluster_port import get_local_worker_id as _get_local_worker_id
from foundation.config import DEFAULT_SERVER_URL, PROJECT_ROOT, config_manager
from foundation.files import FileUtils
from foundation.responses import error_response


logger = logging.getLogger(__name__)
router = APIRouter()

# GMS Assistant boot shell + same-origin proxy live in their own module
# (api.py size budget); their routes are included here.
router.include_router(gms_assistant_proxy.router)

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

def init_templates(templates):
    """Initialize Jinja2 templates reference from the main app."""
    global _templates
    _templates = templates


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

    # local_worker_id: 单一真值, 随页面注入前端 (shell.html bootstrap)。
    response = _templates.TemplateResponse(
        request=request, name="shell.html",
        context={"config": config, "initial_title": initial_title,
                 "local_worker_id": _get_local_worker_id()},
    )
    # 短暂复用导航外壳；must-revalidate 保证过期后确认新版本。
    response.headers["Cache-Control"] = "private, max-age=10, must-revalidate"
    return response


# ==================== Skills Download ====================
# agent/gms-remote-test is the single source root. The legacy
# /api/system/skills* endpoints remain as compatibility wrappers over the
# agent package source (skill content now lives in agent/.../skill/). The
# modern install path is GET /api/agent/install (bootstrap → gms-agent).

def _skill_directory(skill_name: str) -> str | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", skill_name or ""):
        return None
    skills_base_dir = os.path.realpath(
        os.path.join(PROJECT_ROOT, "agent", "gms-remote-test", "skill")
    )
    skills_dir = os.path.realpath(os.path.join(skills_base_dir, skill_name))
    if not skills_dir.startswith(skills_base_dir + os.sep):
        return None
    return skills_dir


@router.get("/api/system/skills")
async def download_skills_zip(
    request: Request,
    skill_name: str = Query(
        "gms-remote-test",
        description="技能名称（兼容参数：当前唯一技能源是 agent/gms-remote-test/skill/）",
    ),
):
    """下载技能 zip（目录重构后的兼容端点包装）

    旧结构 zips skills/<name>/；新结构的技能源位于
    agent/gms-remote-test/skill/（内容即旧 skills/gms-remote-test/ 主体）。
    这里将 skill/ 内容以 gms-remote-test/ 根打包，保持下载语义不变。
    唯一合法的 skill_name 是 gms-remote-test。

    Returns:
        ZIP 文件下载
    """
    try:
        logger.info(f"[SKILLS_DOWNLOAD] 请求下载技能包: {skill_name}")

        if skill_name != "gms-remote-test":
            return JSONResponse(
                content={'success': False, 'error': f'未知技能：{skill_name}'},
                status_code=404
            )
        # 新源：agent/gms-remote-test/skill/（arcname 根为 gms-remote-test/）
        skills_dir = os.path.realpath(
            os.path.join(PROJECT_ROOT, "agent", "gms-remote-test", "skill")
        )
        if not os.path.isdir(skills_dir):
            logger.error(f"[SKILLS_DOWNLOAD] 技能源目录不存在：{skills_dir}")
            return JSONResponse(
                content={'success': False, 'error': f'技能目录不存在：{skill_name}'},
                status_code=404
            )

        zip_filename = f"{skill_name}-skills.zip"
        # skill 源位于 agent/gms-remote-test/skill/，打包时以
        # gms-remote-test/ 为 arcname 根，保持旧下载语义（解压出
        # gms-remote-test/ 目录）不变。
        result = FileUtils.create_zip_from_multiple_directories(
            {skills_dir: skill_name}, zip_filename
        )

        if result is None:
            return JSONResponse(
                content={'success': False, 'error': 'ZIP 文件创建失败：目录为空'},
                status_code=500
            )

        zip_data, _file_count = result

        # 与本次响应字节严格一致的完整性哈希：安装器用它校验下载，
        # 防止传输损坏/代理篡改（配合默认开启的 TLS 证书校验）。
        zip_sha256 = hashlib.sha256(zip_data).hexdigest()
        zip_signature = sign_skill_archive(zip_data)
        headers = {
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
            "X-GMS-SHA256": zip_sha256,
            # Legacy compatibility wrapper: the modern install path is
            # GET /api/agent/install (bootstrap → gms-agent). Advertise the
            # successor so old clients/scripts migrate before removal.
            "Deprecation": "true",
            "Sunset": "Wed, 31 Dec 2025 23:59:59 GMT",
            'Link': '</api/agent/install.sh>; rel="successor-version"',
        }
        if zip_signature:
            headers.update({
                "X-GMS-Signature": zip_signature,
                "X-GMS-Signature-Algorithm": "ed25519",
            })

        return Response(
            content=zip_data,
            media_type="application/zip",
            headers=headers,
        )

    except Exception as e:
        logger.error(f"[SKILLS_DOWNLOAD] Error: {e}", exc_info=True)
        return error_response("技能包下载失败", status_code=500)


# Enterprise build servers often cannot reach
# github.com, so the installer prefers a jq binary served by the Controller
# itself over the GitHub fallback. Integrity is double-checked: the endpoint
# only serves the pinned file pinned path, and the installer verifies the
# SHA-256 it receives in the X-GMS-SHA256 header before installing.
# Implementation lives in features/system/jq_binary.py (api.py size budget).


@router.get("/api/system/tools/jq")
async def download_jq_binary(request: Request):
    """Serve the pinned jq binary for the skill installer."""
    return await jq_binary.serve(request)


# ==================== Agent Package Registry (Phase 3) ====================
# The Controller is the single production distribution source for the GMS
# Agent Runtime; implementation lives in agent_package_registry.py (size
# budget). Endpoints:
#   GET /api/agent/install                                 bootstrap installer
#   GET /api/agent/install.sh                              one-line curl|bash installer
#   GET /api/agent/ca.crt                                  Controller CA (install.sh TOFU source)
#   GET /api/agent/packages/gms-remote-test/manifest      latest version + SHA-256
#   GET /api/agent/packages/gms-remote-test/{version}     distribution zip


@router.get("/api/agent/install")
async def agent_install_bootstrap(request: Request):
    return await agent_package_registry.agent_bootstrap_installer(request)


@router.get("/api/agent/install.sh")
async def agent_install_sh_endpoint(request: Request):
    """一行安装器: curl -fsSL .../api/agent/install.sh | bash -s -- [配对码]"""
    return await agent_package_registry.agent_install_sh(request)


@router.get("/api/agent/ca.crt")
async def agent_ca_cert_endpoint(request: Request):
    """Controller CA 证书分发(install.sh 在系统校验失败时的 TOFU 信任源)"""
    return await agent_package_registry.agent_ca_cert(request)


@router.get("/api/agent/packages/gms-remote-test/manifest")
async def agent_package_manifest_endpoint(request: Request):
    return await agent_package_registry.agent_package_manifest(request)


@router.get("/api/agent/packages/gms-remote-test/{version}")
async def agent_package_download_endpoint(version: str, request: Request):
    return await agent_package_registry.agent_package_download(version, request)


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
        # A user may open several tabs; store ALL connections per
        # client so the second tab no longer silently steals pushes from
        # the first.  {client_id: set[websocket]}.
        connections = global_state.websocket_connections.get(client_id)
        if isinstance(connections, set):
            connections.add(websocket)
        else:
            global_state.websocket_connections[client_id] = {websocket}
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
        # 清理WebSocket连接（按 set 成员移除，不影响同账号其他标签页）
        with global_state.websocket_connections_lock:
            connections = global_state.websocket_connections.get(client_id)
            if isinstance(connections, set):
                connections.discard(websocket)
                if not connections:
                    global_state.websocket_connections.pop(client_id, None)
            elif connections is websocket:
                global_state.websocket_connections.pop(client_id, None)

        close_websocket_terminal(websocket)
