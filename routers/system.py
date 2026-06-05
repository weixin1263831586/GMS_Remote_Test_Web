"""System router - WebSocket, health check, docs, help, skills download, root page."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from core.api_docs_list import API_DOCS_LIST
from core.config import config_manager
from core.settings import DEFAULT_SERVER_URL, PROJECT_ROOT
from core.error_handling import handle_api_errors
from core.file_utils import FileUtils
from core.state import global_state
from core.terminal import (
    refresh_devices_websocket,
    handle_tradefed_list_results,
    handle_terminal_connect,
    handle_terminal_input,
    handle_terminal_resize,
    close_terminal_session_resources,
)
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)
router = APIRouter()

# Template factory (initialized from app.py)
_templates = None


def init_templates(templates):
    """Initialize Jinja2 templates reference from the main app."""
    global _templates
    _templates = templates


# ==================== Root Page ====================

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板"""
    config = config_manager.load_config()

    response = _templates.TemplateResponse(
        "index_fastapi.html",
        {
            "request": request,
            "config": config
        }
    )
    # HTML页面不缓存（确保用户获取最新版本）
    response.headers["Cache-Control"] = "no-cache"
    return response


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

        # 使用相对路径避免硬编码
        skills_base_dir = os.path.join(PROJECT_ROOT, 'skills')
        skills_dir = os.path.join(skills_base_dir, skill_name)

        if not os.path.exists(skills_dir):
            logger.error(f"[SKILLS_DOWNLOAD] 技能目录不存在：{skills_dir}")
            return JSONResponse(
                content={'success': False, 'error': f'技能目录不存在：{skill_name}'},
                status_code=404
            )

        # 使用共享工具创建ZIP
        zip_filename = f"{skill_name}-skills.zip"
        result = FileUtils.create_zip_from_directory(skills_dir, zip_filename)

        if result is None:
            return JSONResponse(
                content={'success': False, 'error': 'ZIP 文件创建失败：目录为空'},
                status_code=500
            )

        zip_data, file_count = result

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

        with open(install_sh_path, 'rb') as f:
            content = f.read()

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


# ==================== Architecture Page ====================

@router.get("/templates/architecture.html")
async def get_architecture():
    """获取系统架构图"""
    architecture_file = os.path.join(PROJECT_ROOT, 'templates', 'architecture.html')
    if os.path.exists(architecture_file):
        with open(architecture_file, 'r', encoding='utf-8') as f:
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
        raise HTTPException(status_code=500, detail=str(e))


# ==================== API Help ====================

@router.get("/api/system/help")
async def get_api_help(api_path: Optional[str] = None):
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
        # 如果指定了api_path，返回单个API的详细帮助
        if api_path:
            # 查找匹配的API
            api_doc = None
            for api in API_DOCS_LIST:
                # 移除开头的斜杠进行匹配
                if api['path'].lstrip('/') == api_path:
                    api_doc = api
                    break

            if not api_doc:
                raise HTTPException(status_code=404, detail=f"API not found: /{api_path}")

            # 生成帮助文本
            help_text = generate_per_api_help_text(api_doc['method'], api_doc['path'])

            if not help_text:
                raise HTTPException(status_code=404, detail=f"Help not available for: /{api_path}")

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
        raise HTTPException(status_code=500, detail=str(e))


# ==================== WebSocket ====================

@router.websocket("/api/system/websocket/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()
    with global_state.websocket_connections_lock:
        global_state.websocket_connections[client_id] = websocket
    logger.info(f"WebSocket client connected: {client_id}")

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
            if client_id in global_state.websocket_connections:
                del global_state.websocket_connections[client_id]

        # 清理终端SSH会话（如果存在）
        with global_state.terminal_lock:
            if client_id in global_state.terminal_ssh_sessions:
                session_info = global_state.terminal_ssh_sessions[client_id]
                close_terminal_session_resources(session_info)
                del global_state.terminal_ssh_sessions[client_id]


# ==================== Helper Functions ====================

SKILL_COMMAND_PREFIX = "gms-rt-"


def generate_skill_name(api_path: str) -> str:
    """
    根据API路径生成skill命令名称

    规则:
    - 移除/api/前缀
    - 将/替换为-
    - 移除路径参数(如{report_timestamp})
    - 添加gms-rt-前缀

    特殊情况:
    - / → gms-rt-docs (根路径特殊处理)
    """
    if api_path == "/":
        return f"{SKILL_COMMAND_PREFIX}docs"

    # 移除/api/前缀
    path_without_api = api_path.replace("/api/", "")

    # 移除路径参数
    path_without_params = re.sub(r'\{[^}]+\}', '', path_without_api).strip('/')

    # 将/替换为-
    skill_name = path_without_params.replace("/", "-")

    return f"{SKILL_COMMAND_PREFIX}{skill_name}"


def generate_per_api_help_text(method: str, path: str) -> Optional[str]:
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
            if ord(char) > 127:  # 非ASCII字符（中文等）
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
            'response': '{"success": true, "running": true, "url": "http://xxx:6080/vnc.html"}',
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
            'response': '{"success": true, "url": "http://xxx:6080/vnc.html"}',
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


def generate_curl_example(api):
    """生成API的curl示例命令"""
    method = api['method']
    path = api['path']
    params = api.get('params', [])
    base_url = DEFAULT_SERVER_URL

    if method == 'GET':
        if params:
            # 有参数的GET请求
            param = params[0]
            return f'curl -s "{base_url}{path}?{param["name"]}=VALUE"'
        else:
            return f'curl -s "{base_url}{path}"'

    elif method == 'POST':
        if params:
            # 检查是否有file类型参数
            has_file = any(p.get('type') == 'file' for p in params)
            if has_file:
                # FormData格式
                file_params = [p for p in params if p.get('type') == 'file']
                other_params = [p for p in params if p.get('type') != 'file']

                parts = []
                for p in file_params:
                    parts.append(f'-F "{p["name"]}=@VALUE"')
                for p in other_params[:2]:  # 最多显示2个参数
                    parts.append(f'-F "{p["name"]}=VALUE"')

                cmd = f'curl -sX POST "{base_url}{path}"'
                if parts:
                    cmd += ' \\\n  ' + ' \\\n  '.join(parts)
                return cmd
            else:
                # JSON格式
                json_body = "{"
                for i, p in enumerate(params[:2]):  # 最多显示2个参数
                    comma = "," if i < min(len(params), 2) - 1 else ""
                    json_body += f'\\n    "{p["name"]}": "VALUE"{comma}'
                json_body += "\\n  }"

                return f'curl -sX POST "{base_url}{path}" \\\n  -H "Content-Type: application/json" \\\n  -d \'{json_body}\''
        else:
            return f'curl -sX POST "{base_url}{path}"'

    elif method == 'DELETE':
        if params:
            param = params[0]
            return f'curl -X DELETE "{base_url}{path}" \\\n  -G \\\n  -d "{param["name"]}=VALUE"'
        else:
            return f'curl -X DELETE "{base_url}{path}"'

    else:
        return f'curl -X {method} "{base_url}{path}"'


def generate_api_example(api):
    """生成API使用示例"""
    method = api['method']
    path = api['path']
    params = api.get('params', [])

    base_url = DEFAULT_SERVER_URL

    if method == 'GET':
        if params:
            # 有参数的GET请求
            param_str = "&".join([f"{p['name']}=VALUE" for p in params[:2]])
            return f'curl -s "{base_url}{path}?{param_str}"'
        else:
            return f'curl -s "{base_url}{path}"'

    elif method == 'POST':
        if params:
            # 检查是否有file类型参数
            has_file = any(p.get('type') == 'file' for p in params)
            if has_file:
                # FormData格式
                param_str = " \\\n  ".join([
                    f'-F "{p["name"]}=@{p.get("desc", "path/to/file")}"' if p.get('type') == 'file' else f'-F "{p["name"]}=VALUE"'
                    for p in params[:3]
                ])
                return f'curl -sX POST "{base_url}{path}" \\\n  {param_str}'
            else:
                # JSON格式
                body = "{"
                for i, p in enumerate(params[:3]):
                    comma = "," if i < len(params) - 1 else ""
                    body += f'\n    "{p["name"]}": "VALUE"{comma}'
                body += "\n  }"
                return f'curl -sX POST "{base_url}{path}" \\\n  -H "Content-Type: application/json" \\\n  -d \'{body}\''
        else:
            return f'curl -sX POST "{base_url}{path}"'

    elif method == 'DELETE':
        if params:
            param = params[0]['name']
            return f'curl -X DELETE "{base_url}{path}" \\\n  -G \\\n  -d "{param}=VALUE"'
        else:
            return f'curl -X DELETE "{base_url}{path}"'

    else:
        return f'curl -X {method} "{base_url}{path}"'
