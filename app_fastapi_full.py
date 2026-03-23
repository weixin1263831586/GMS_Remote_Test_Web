#!/usr/bin/env python3
"""
GMS Auto Test - 完整版FastAPI应用（端口5001）

完全替代Flask版本，实现所有60个端点
"""

import os
import sys
import logging
import subprocess
import configparser
import socket
import uuid
import paramiko
import threading
import time
import re
import json
import shlex
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Body, Query

# ==================== Lifespan 事件处理 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info("Application startup")
    logger.info("=" * 60)

    # 初始化USB监控
    try:
        # 创建一个队列用于跨线程通信
        import queue
        if not hasattr(app.state, 'usb_event_queue'):
            app.state.usb_event_queue = queue.Queue()

        def get_devices():
            """获取当前设备列表"""
            try:
                return device_manager.get_connected_devices()
            except Exception as e:
                logger.error(f"Error getting devices for USB monitor: {e}")
                return []

        def on_usb_devices_changed(devices):
            """USB设备变化回调"""
            logger.info(f"USB devices changed: {devices}")

            # 将事件放入队列，让后台任务处理
            try:
                app.state.usb_event_queue.put({
                    'type': 'devices_changed',
                    'devices': devices,
                    'timestamp': datetime.now().isoformat()
                })
                logger.info(f"USB device change event queued, current devices: {devices}")
            except Exception as e:
                logger.error(f"Error queuing device change event: {e}")

        # 初始化并启动USB监控
        init_usb_monitor(
            device_getter=get_devices,
            on_devices_changed=on_usb_devices_changed,
            check_interval=2.0,
            use_udev=True
        )
        start_usb_monitor()
        logger.info("USB monitor started successfully")
    except Exception as e:
        logger.error(f"Failed to start USB monitor: {e}")

    yield

    # 关闭时执行
    logger.info("Application shutdown")

    # 停止USB监控
    try:
        stop_usb_monitor()
        logger.info("USB monitor stopped")
    except Exception as e:
        logger.error(f"Error stopping USB monitor: {e}")

    logger.info("=" * 60)

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入核心模块
from core.config import config_manager
from core.ssh import ssh_manager
from core.device import device_manager
from core.test_runner import test_runner
from core.test_report import test_report_manager
from core.vnc import vnc_manager, calculate_window_positions
from core.adb_forward import adb_forward_manager
from core.usbip import usbip_manager
from report_analyzer import ReportAnalyzer
from test_report_db import test_report_db

# 导入管理模块
from modules.client_manager import client_manager
from modules.device_lock_manager import device_lock_manager
from modules.test_logs_manager import test_logs_manager

# 导入USB监控模块
from core.usb_monitor import init_usb_monitor, start_usb_monitor, stop_usb_monitor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== FastAPI应用 ====================

app = FastAPI(
    title="GMS Auto Test - FastAPI Server (Port 5001)",
    description="完整的测试管理服务（替代Flask版本）",
    version="4.0.0",
    lifespan=lifespan
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
static_dir = os.path.join(os.path.dirname(__file__), 'static')
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 配置Jinja2模板
templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
templates = Jinja2Templates(directory=templates_dir)

# 自定义url_for函数，兼容Flask语法
def url_for_static(filename: str) -> str:
    """生成静态文件URL（兼容Flask语法）"""
    return f"/static/{filename}"

# 将自定义函数添加到模板全局变量中
templates.env.globals['url_for'] = lambda endpoint, filename='': (
    url_for_static(filename) if endpoint == 'static' else f'/{endpoint}'
)

# ==================== 全局状态管理 ====================

class GlobalState:
    """全局状态管理"""
    def __init__(self):
        self.running_tests = {}  # {client_id: test_info}
        self.test_logs = {}      # {client_id: log_entries}
        self.ssh_connections = {}  # {client_id: ssh_connection}
        self.scrcpy_sessions = {}  # {device_id: session_info}
        self.device_cache = {'devices': [], 'timestamp': 0}  # 3秒TTL
        self.websocket_connections = {}  # {client_id: websocket}
        self.usbip_states = {}  # {client_id: {'connected': bool, 'timestamp': float}}
        self.usbip_devices_source = {}  # {device_id: {'source': device_host, 'timestamp': float}}
        self.terminal_ssh_sessions = {}  # {session_id: {'ssh': ssh, 'channel': channel, 'websocket': websocket}}
        self.terminal_lock = threading.Lock()  # 终端会话锁
        self.user_states = {}  # {client_id: {running, devices, logs, created_at, last_seen}}
        self.user_states_lock = threading.Lock()  # 用户状态锁
        self.usbip_states_lock = threading.Lock()  # USB/IP状态锁（与Flask一致）
        self.usbip_devices_source_lock = threading.Lock()  # USB/IP设备来源锁（与Flask一致）

global_state = GlobalState()

DEVICE_CACHE_TTL = 3

# ==================== 统一响应格式工具类 ====================

class ApiResponse:
    """统一的API响应格式（与Flask完全兼容）"""

    @staticmethod
    def success(data=None, message="操作成功"):
        """成功响应"""
        response = {'success': True}
        if data is not None:
            response['data'] = data
        if message:
            response['message'] = message
        return JSONResponse(content=response)

    @staticmethod
    def error(error_message, status_code=500, **extra_fields):
        """错误响应（与Flask格式一致）"""
        response = {'success': False, 'error': error_message}
        response.update(extra_fields)
        return JSONResponse(content=response, status_code=status_code)

    @staticmethod
    def device_results(results, operation_name):
        """设备批量操作结果"""
        success_count = sum(1 for r in results if r.get('success', False))
        fail_count = len(results) - success_count
        return ApiResponse.success({
            'results': results,
            'summary': {'total': len(results), 'success': success_count, 'failed': fail_count}
        }, f"{operation_name}完成: 成功 {success_count} 台, 失败 {fail_count} 台")

# ==================== 用户状态管理辅助函数 ====================

def save_test_report_to_db(
    client_id: str,
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    user_logs: List[str]
) -> Optional[str]:
    """
    从测试日志中提取 RESULT DIRECTORY 并记录测试报告到数据库（与Flask版本一致）

    Args:
        client_id: 客户端ID
        config: 配置字典
        test_params: 测试参数
        user_logs: 用户日志列表

    Returns:
        报告时间戳，如果失败则返回 None
    """
    try:
        # 从日志中提取 RESULT DIRECTORY
        result_dir = None
        for log in reversed(user_logs):
            log_str = str(log)
            if 'RESULT DIRECTORY' in log_str:
                # 提取 RESULT DIRECTORY 后面的路径
                match = re.search(r'RESULT DIRECTORY\s*:\s*(/[^\s]+)', log_str)
                if match:
                    result_dir = match.group(1).strip()
                    logger.info(f"[ReportDB] 找到 RESULT DIRECTORY: {result_dir}")
                    break

        if not result_dir or not os.path.exists(result_dir):
            logger.warning(f"[ReportDB] 未找到 RESULT DIRECTORY 或目录不存在: {result_dir}")
            return None

        # 提取时间戳
        timestamp = os.path.basename(result_dir)

        # 检查是否已记录
        existing = test_report_db.get_report_by_timestamp(timestamp)
        if existing:
            logger.info(f"[ReportDB] 报告已存在: {timestamp}")
            return timestamp

        # 解析 test_result.xml
        xml_path = os.path.join(result_dir, 'test_result.xml')
        report_info = {
            'timestamp': timestamp,
            'test_type': test_params.get('test_type', 'UNKNOWN').upper(),
            'client_id': client_id,
            'devices': test_params.get('devices', []),
            'result_dir': result_dir,
            'suite_path': test_params.get('test_suite', ''),
            'status': 'completed'
        }

        # 提取用户名
        if '@' in client_id:
            report_info['user'] = client_id.split('@')[0]

        # 解析XML获取测试结果统计
        if os.path.exists(xml_path):
            try:
                result = ReportAnalyzer().analyze_file(xml_path)
                if result:
                    report_info.update({
                        'pass': result['summary']['pass'],
                        'fail': result['summary']['fail'],
                        'total': result['summary']['total'],
                        'pass_rate': result['summary']['pass_rate'],
                        'device': result['details']['device'],
                        'start_time': result['details']['start_time']
                    })
            except Exception as e:
                logger.warning(f"[ReportDB] 解析 XML 失败: {e}")

        # 添加到数据库（使用add_report与Flask版本一致）
        if test_report_db.add_report(report_info):
            logger.info(f"[ReportDB] 报告已记录: {timestamp}")
            return timestamp

        return None

    except Exception as e:
        logger.error(f"[ERROR] 保存报告到数据库失败: {e}")
        return None

def get_client_id_from_request(request: Request) -> str:
    """从请求中获取client_id（优先从配置文件读取用户名）"""
    client_ip = (
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.headers.get('X-Real-IP') or
        request.client.host if request.client else 'unknown'
    )

    # 优先从配置文件读取client_hosts映射
    config = config_manager.load_config()
    client_hosts = config.get('client_hosts', {})

    # 如果client_hosts中有该IP的映射，使用映射的用户名
    if client_ip in client_hosts:
        username = client_hosts[client_ip]
    else:
        # 其次从请求头获取用户名
        username = request.headers.get('X-Client-Username')
        if not username or username == 'unknown':
            # 最后从配置文件读取默认用户名
            username = config.get('client_username', 'unknown')

    return client_manager.get_client_id(client_ip, username)

async def broadcast_device_lock_update(device_ids: list = None):
    """广播设备锁定更新（快速版本，不需要SSH查询）"""
    try:
        # 获取所有锁定的设备信息
        all_locks = device_lock_manager.get_all_locks()

        # 构建设备更新消息
        device_updates = []
        if device_ids:
            # 只更新指定的设备
            logger.debug(f"[Broadcast Device Lock] 更新指定设备: {device_ids}")
            for device_id in device_ids:
                if device_id in all_locks:
                    lock_info = all_locks[device_id]
                    # 直接使用client_id (username@ip格式)
                    locked_by = lock_info['client_id']
                    device_updates.append({
                        'device_id': device_id,
                        'locked': True,
                        'locked_by': locked_by,
                        'locked_at': lock_info['timestamp']
                    })
                    logger.debug(f"[Broadcast Device Lock] 设备 {device_id} 已锁定 by {locked_by}")
                else:
                    device_updates.append({
                        'device_id': device_id,
                        'locked': False
                    })
                    logger.debug(f"[Broadcast Device Lock] 设备 {device_id} 已解锁")
        else:
            # 更新所有锁定的设备
            logger.debug(f"[Broadcast Device Lock] 更新所有锁定设备")
            for device_id, lock_info in all_locks.items():
                # 直接使用client_id (username@ip格式)
                locked_by = lock_info['client_id']
                device_updates.append({
                    'device_id': device_id,
                    'locked': True,
                    'locked_by': locked_by,
                    'locked_at': lock_info['timestamp']
                })

        # 广播到所有连接的客户端
        logger.debug(f"[Broadcast Device Lock] 广播到 {len(global_state.websocket_connections)} 个客户端")
        for client_id, ws in global_state.websocket_connections.items():
            try:
                await ws.send_json({
                    'type': 'device_lock_update',
                    'devices': device_updates
                })
                logger.debug(f"[Broadcast Device Lock] 成功发送到客户端 {client_id}")
            except Exception as e:
                logger.warning(f"[Broadcast Device Lock] 发送到客户端 {client_id} 失败: {e}")

        logger.debug(f"[Broadcast Device Lock] 已发送设备锁定更新到 {len(global_state.websocket_connections)} 个客户端")
    except Exception as e:
        logger.error(f"[Broadcast Device Lock] 广播设备锁定更新失败: {e}")

def get_or_create_user_state(client_id: str) -> dict:
    """获取或创建用户状态（不修正client_id，使用原始key）"""
    with global_state.user_states_lock:
        if client_id not in global_state.user_states:
            global_state.user_states[client_id] = {
                'running': False,
                'devices': [],
                'logs': [],
                'ssh_connected': False,
                'log_file': None,
                'test_type': 'cts',
                'created_at': datetime.now().isoformat(),
                'client_id': client_id,
                'last_seen': datetime.now().isoformat()
            }
        else:
            # 更新last_seen时间
            global_state.user_states[client_id]['last_seen'] = datetime.now().isoformat()
        return global_state.user_states[client_id]

def update_user_state_field(client_id: str, updates: dict):
    """更新用户状态的特定字段"""
    with global_state.user_states_lock:
        if client_id in global_state.user_states:
            global_state.user_states[client_id].update(updates)
            logger.info(f"[State] Updated {client_id}: {list(updates.keys())} = {updates}")
        else:
            logger.warning(f"[State] Client {client_id} not found in user_states")

# ==================== Pydantic数据模型 ====================

class ClientInfoRequest(BaseModel):
    """客户端信息请求"""
    username: Optional[str] = None
    password: Optional[str] = None
    ip: Optional[str] = None

class DeviceLockRequest(BaseModel):
    """设备锁定请求（支持单设备和批量操作）"""
    device_id: Optional[str] = None  # 单设备ID（旧格式）
    devices: Optional[List[str]] = None  # 设备ID列表（新格式，支持批量）
    action: str = 'lock'  # lock, unlock

class TestStartRequest(BaseModel):
    """测试启动请求"""
    test_type: str = "cts"
    test_module: str = ""
    test_case: str = ""
    retry_dir: str = ""
    test_suite: str = ""
    local_server: str = ""
    devices: List[str] = []
    client_id: str = "test_client"

class DeviceActionRequest(BaseModel):
    """设备操作请求"""
    devices: List[str] = Field(..., description="设备ID列表")

class WifiConnectRequest(DeviceActionRequest):
    """WiFi连接请求"""
    ssid: str = "AndroidWifi"
    password: str = "1234567890"

class VNCStartRequest(BaseModel):
    """VNC启动请求"""
    host: Optional[str] = None
    password: Optional[str] = None
    vnc_password: Optional[str] = None

class ADBForwardStartRequest(BaseModel):
    """ADB转发启动请求"""
    device_host: str
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")

class USBIPStartRequest(BaseModel):
    """USB/IP启动请求"""
    device_host: Optional[str] = None
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")

class AutocompleteSuiteRequest(BaseModel):
    """自动完成测试套件请求"""
    test_type: str
    base_path: str

class VPNConnectRequest(BaseModel):
    """VPN连接请求（所有字段可选，兼容前端无参数调用）"""
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

class FirmwareBurnRequest(BaseModel):
    """固件烧录请求 - 与Flask版本一致"""
    devices: List[str]
    system_img: str
    vendor_img: Optional[str] = ""
    misc_img: Optional[str] = ""

class GSIBurnRequest(BaseModel):
    """GSI烧录请求 - 与Flask版本一致"""
    devices: List[str]
    system_img: str
    vendor_img: Optional[str] = ""
    script_path: Optional[str] = ""

class SNBurnRequest(BaseModel):
    """SN烧录请求 - 与Flask版本一致"""
    devices: List[str]
    sn_code: str

class ScreenStartRequest(BaseModel):
    """屏幕录制启动请求"""
    device_id: str
    duration: int = 60

# ==================== 基础端点 ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板（移除Socket.IO依赖）"""
    config = config_manager.load_config()

    # 使用FastAPI专用模板（移除了Socket.IO）
    response = templates.TemplateResponse(
        "index_fastapi.html",
        {
            "request": request,
            "config": config
        }
    )
    # 添加缓存控制头，防止浏览器缓存旧代码
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/health")
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

# ==================== 客户端管理 ====================



@app.get("/api/config/validate")
async def validate_config():
    """验证配置文件"""
    try:
        config = config_manager.load_config()

        errors = []
        warnings = []

        # 检查必要字段
        required_fields = ['ubuntu_host', 'ubuntu_user', 'ubuntu_pswd', 'suites_path', 'script_path']
        for field in required_fields:
            if field not in config or not config[field]:
                errors.append(f"缺少必要字段: {field}")

        # 检查ubuntu_host
        ubuntu_host = config.get('ubuntu_host', '')
        if ubuntu_host in ['test', 'localhost', '127.0.0.1']:
            warnings.append(f"ubuntu_host '{ubuntu_host}' 可能无法从远程访问")

        # 检查路径是否存在
        for path_field in ['suites_path', 'script_path']:
            path = config.get(path_field, '')
            if path and not os.path.exists(path):
                warnings.append(f"路径不存在: {path_field} = {path}")

        # 检查SSH凭据
        if not config.get('ubuntu_pswd'):
            warnings.append("未设置ubuntu_pswd，可能影响SSH连接")

        return JSONResponse(content={
            "success": len(errors) == 0,
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "config": {
                "ubuntu_host": config.get('ubuntu_host'),
                "ubuntu_user": config.get('ubuntu_user'),
                "suites_path": config.get('suites_path'),
                "script_path": config.get('script_path')
            }
        })
    except Exception as e:
        logger.error(f"Error validating config: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )


@app.get("/api/config/values")
async def get_config_values():
    """获取配置值供前端使用（不包含敏感信息）"""
    config = config_manager.load_config()

    # 只返回前端需要的配置项
    safe_config = {
        'script_path': config.get('script_path', ''),
        'suites_path': config.get('suites_path', ''),
        'ubuntu_user': config.get('ubuntu_user', ''),
        'ubuntu_host': config.get('ubuntu_host', 'localhost'),
        'local_server': config.get('local_server', '')
        # 不返回密码
    }

    return JSONResponse(content={"success": True, "data": safe_config})

@app.get("/api/client-info")
async def get_client_info(request: Request):
    """获取客户端信息（返回client_id用于WebSocket连接）"""
    # 使用统一的client_id获取逻辑（优先从client_hosts读取）
    client_id = get_client_id_from_request(request)

    # 确保用户状态存在
    get_or_create_user_state(client_id)

    # 解析client_id获取IP和用户名
    parts = client_id.split('@')
    username = parts[0] if len(parts) > 0 else 'unknown'
    client_ip = parts[1] if len(parts) > 1 else 'unknown'

    logger.info(f"[ClientInfo] GET - IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "ip": client_ip,
        "client_id": client_id,
        "username": username
    })

@app.post("/api/client-info")
async def set_client_info(req: ClientInfoRequest, request: Request):
    """设置客户端信息"""
    client_ip = req.ip or (
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.headers.get('X-Real-IP') or
        request.client.host if request.client else 'unknown'
    )

    username = req.username or 'unknown'

    # 保存客户端信息
    config = config_manager.load_config()
    config['client_ip'] = client_ip
    config['client_username'] = username
    config_manager.save_dynamic_config({
        'client_ip': client_ip,
        'client_username': username
    })

    client_id = client_manager.get_client_id(client_ip, username)

    # 更新用户状态
    get_or_create_user_state(client_id)
    update_user_state_field(client_id, {
        'client_ip': client_ip,
        'client_username': username,
        'last_seen': datetime.now().isoformat()
    })

    # 清理同一IP的旧记录（不同用户名的记录）
    with global_state.user_states_lock:
        to_remove = []
        for existing_id, existing_state in global_state.user_states.items():
            if existing_id == client_id:
                continue
            # 检查是否是同一IP但不同用户名的旧记录
            existing_ip = existing_state.get('client_ip', '')
            if existing_ip == client_ip:
                to_remove.append(existing_id)

        for old_id in to_remove:
            del global_state.user_states[old_id]
            logger.info(f"[ClientInfo] Removed old user record for same IP: {old_id} -> {client_id}")

    logger.info(f"[ClientInfo] IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "success": True,
        "client_id": client_id
    })

@app.post("/api/client-info/detect")
async def detect_client(req: ClientInfoRequest, request: Request):
    """自动检测客户端用户名"""
    client_ip = req.ip or (
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.headers.get('X-Real-IP') or
        request.client.host if request.client else 'unknown'
    )

    success, username, error = client_manager.detect_username(
        client_ip,
        req.username,
        req.password
    )

    if success:
        return JSONResponse(content={
            "success": True,
            "username": username
        })
    else:
        return JSONResponse(content={
            "success": False,
            "error": error
        }, status_code=401)

@app.get("/api/users")
async def list_users():
    """获取所有在线用户列表"""
    users = []
    now = datetime.now()

    # 本地地址列表，不显示在用户列表中
    local_addresses = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}

    with global_state.user_states_lock:
        # 收集所有用户
        temp_users = {}
        for client_id, state in global_state.user_states.items():
            # 检查会话是否活跃（最近24小时内有活动）
            if 'last_seen' in state:
                try:
                    last_seen = datetime.fromisoformat(state['last_seen'])
                    if (now - last_seen) > timedelta(hours=24):
                        continue
                except:
                    continue

            # 解析client_id (username@ip)
            parts = client_id.split('@')
            username_from_id = parts[0] if len(parts) > 0 else 'unknown'
            ip = parts[1] if len(parts) > 1 else 'unknown'

            # 优先使用state中存储的username（更准确）
            username = state.get('client_username', username_from_id)
            if username == 'unknown':
                username = username_from_id

            # 过滤本地地址
            if ip in local_addresses:
                continue

            # 如果同一个IP有多个用户记录，优先保留非unknown的用户
            if ip in temp_users:
                existing_user = temp_users[ip]
                if existing_user['username'] == 'unknown' and username != 'unknown':
                    # 用真实用户替换unknown用户
                    temp_users[ip] = {
                        'client_id': client_id,
                        'username': username,
                        'ip': ip,
                        'running': state.get('running', False),
                        'devices': state.get('devices', []),
                        'last_seen': state.get('last_seen', ''),
                        'created_at': state.get('created_at', '')
                    }
                # 否则保留第一个
            else:
                temp_users[ip] = {
                    'client_id': client_id,
                    'username': username,
                    'ip': ip,
                    'running': state.get('running', False),
                    'devices': state.get('devices', []),
                    'last_seen': state.get('last_seen', ''),
                    'created_at': state.get('created_at', '')
                }

        users = list(temp_users.values())

    return JSONResponse(content={
        'total': len(users),
        'users': users
    })

# ==================== 配置管理 ====================

@app.get("/api/config")
async def get_config(request: Request):
    """获取配置 - 与Flask版本一致，直接返回配置对象"""
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    config = config_manager.load_config()
    # 直接返回配置对象，与Flask版本一致
    return JSONResponse(content=config)

@app.post("/api/config")
async def update_config(req: dict):
    """更新配置 - 与Flask版本一致，保留SSH密码"""
    new_config = req.copy()
    existing_config = config_manager.load_config()

    # 保留SSH密码（与Flask版本一致）
    for key in ['ubuntu_pswd', 'device_pswd']:
        if key not in new_config or new_config.get(key, '') == '':
            if key in existing_config:
                new_config[key] = existing_config[key]

    if config_manager.save_config(new_config):
        return JSONResponse(content={'success': True})
    else:
        raise HTTPException(status_code=500, detail="保存配置失败")

# ==================== 设备管理 ====================

@app.get("/api/devices")
async def list_devices(request: Request):
    """获取设备列表 - 与Flask一致，直接返回数组"""
    try:
        # 跟踪用户访问
        client_id = get_client_id_from_request(request)
        get_or_create_user_state(client_id)

        config = config_manager.load_config()

        # 先刷新设备列表（需要最新状态来清理记录）
        devices = device_manager.get_connected_devices()

        # 清理已不存在的设备来源记录（与Flask版本一致）
        # 如果设备已不在当前设备列表中，说明设备已断开/移除，应该清除其来源记录
        current_device_set = set(devices)
        devices_to_remove = [
            dev_id for dev_id in global_state.usbip_devices_source.keys()
            if dev_id not in current_device_set
        ]
        if devices_to_remove:
            logger.info(f"[Devices API] Cleaning up removed devices: {devices_to_remove}")
            with global_state.usbip_devices_source_lock:
                for dev_id in devices_to_remove:
                    del global_state.usbip_devices_source[dev_id]

        # 检查缓存（清理后检查）
        now = datetime.now().timestamp()
        if now - global_state.device_cache['timestamp'] < DEVICE_CACHE_TTL:
            cached_devices = global_state.device_cache['devices']
            return JSONResponse(content=cached_devices)

        devices_with_status = []

        for device_id in devices:
            device_info = {
                'device_id': device_id,
                'status': 'online',
                'locked': False
            }

            # 检查锁定状态
            client_ip = (
                request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
                request.headers.get('X-Real-IP') or
                request.client.host if request.client else 'unknown'
            )
            client_id = client_manager.get_client_id(client_ip)
            lock_status = device_lock_manager.get_lock_status(device_id)

            if lock_status:
                device_info['locked'] = True
                device_info['locked_by'] = lock_status['locked_by']
                device_info['locked_by_self'] = lock_status.get('client_id') == client_id
                device_info['locked_at'] = lock_status['locked_at']
            else:
                device_info['locked_by'] = ''
                device_info['locked_by_self'] = False

            # 检查USB/IP来源
            if device_id in global_state.usbip_devices_source:
                source = global_state.usbip_devices_source[device_id]
                device_info['source'] = source['source']
                device_info['is_usbip'] = True

            devices_with_status.append(device_info)

        # 更新缓存
        global_state.device_cache = {
            'devices': devices_with_status,
            'timestamp': now
        }

        # 直接返回数组（与Flask一致）
        return JSONResponse(content=devices_with_status)
    except Exception as e:
        logger.error(f"Error listing devices: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/devices/lock")
async def lock_devices(req: DeviceLockRequest, request: Request):
    """锁定/解锁设备（使用run_Device_Lock.sh脚本 - 与Flask版本完全一致）"""
    try:
        # 兼容两种请求格式：单设备（device_id）和批量（devices）
        devices = req.devices if req.devices else []
        if req.device_id:
            devices = [req.device_id]

        if not devices:
            return ApiResponse.error("未选择设备", status_code=400)

        action = req.action
        config = config_manager.load_config()

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ApiResponse.error('SSH连接失败', status_code=500)

        try:
            results = []

            # 本地脚本路径 - 使用tools目录
            local_script = os.path.join(os.path.dirname(__file__), 'tools', 'run_Device_Lock.sh')
            # 远程脚本路径
            remote_script = f"/home/{config['ubuntu_user']}/GMS-Suite/run_Device_Lock.sh"

            # 检查本地脚本是否存在
            if not os.path.exists(local_script):
                return ApiResponse.error(f'脚本文件不存在: {local_script}', status_code=404)

            # 上传脚本到远程服务器
            try:
                sftp = ssh.open_sftp()
                sftp.put(local_script, remote_script)
                sftp.close()
                # 设置执行权限
                ssh_manager.execute_command(ssh, f"chmod +x '{remote_script}'")
            except Exception as e:
                return ApiResponse.error(f'上传脚本失败: {str(e)}', status_code=500)

            # 对每个设备执行锁定/解锁操作
            for device_id in devices:
                try:
                    # 执行脚本
                    cmd = f"bash '{remote_script}' '{device_id}' '{action}'"
                    output, error, code = ssh_manager.execute_command(ssh, cmd)

                    # 等待设备重新上线
                    if code == 0:
                        start_time = time.time()
                        while time.time() - start_time < 60:  # 等待最多60秒
                            check_cmd = f"adb -s {device_id} get-state"
                            check_output, _, check_code = ssh_manager.execute_command(ssh, check_cmd)
                            if 'device' in check_output.lower():
                                break
                            time.sleep(2)

                    results.append({
                        'device': device_id,
                        'success': code == 0,
                        'output': output[-200:] if output else error
                    })
                except Exception as e:
                    results.append({
                        'device': device_id,
                        'success': False,
                        'error': str(e)
                    })

            ssh_manager.return_connection(ssh)
            return ApiResponse.success({'results': results}, '设备锁定操作完成')

        except Exception as e:
            ssh_manager.return_connection(ssh)
            return ApiResponse.error(str(e), status_code=500)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error managing device lock: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/lock-status")
async def check_lock_status(req: DeviceActionRequest):
    """Check verified boot lock status of selected devices（与Flask版本完全一致）"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ApiResponse.error('SSH连接失败', status_code=500)

        try:
            results = []
            for device_id in req.devices:
                # Check verified boot state (GREEN = locked, ORANGE = unlocked)
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate"
                )
                state = output.strip()

                # 根据状态判断是否锁定
                if state == 'green':
                    is_locked = True
                    status_text = '已锁定 (GREEN)'
                elif state == 'orange':
                    is_locked = False
                    status_text = '未锁定 (ORANGE)'
                elif state == 'yellow':
                    is_locked = False
                    status_text = '未锁定 (YELLOW)'
                else:
                    is_locked = False
                    status_text = f'未知状态 ({state})'

                results.append({
                    'device': device_id,
                    'locked': is_locked,
                    'state': state,
                    'status': status_text
                })

            ssh_manager.return_connection(ssh)
            return ApiResponse.success({'results': results}, '锁定状态检查完成')

        except Exception as e:
            ssh_manager.return_connection(ssh)
            return ApiResponse.error(str(e), status_code=500)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking lock status: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest):
    """获取设备详细信息 - 与Flask一致，返回15个关键字段的中文标签"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 定义信息命令（与Flask完全一致）
            info_commands = [
                ("设备序列号", "adb -s {device} shell getprop ro.serialno"),
                ("设备型号", "adb -s {device} shell getprop ro.product.model"),
                ("Android版本", "adb -s {device} shell getprop ro.build.version.release"),
                ("编译类型", "adb -s {device} shell getprop ro.build.type"),
                ("编译标签", "adb -s {device} shell getprop ro.build.tags"),
                ("编译时间", "adb -s {device} shell getprop ro.build.date"),
                ("SDK版本", "adb -s {device} shell getprop ro.build.version.sdk"),
                ("DATA分区", "adb -s {device} shell cat vendor/etc/fstab.rk30board | grep userdata"),
                ("API级别", "adb -s {device} shell getprop | grep api_level"),
                ("Mali库版本", "adb -s {device} shell getprop sys.gmali.version"),
                ("安全补丁", "adb -s {device} shell getprop ro.build.version.security_patch"),
                ("指纹", "adb -s {device} shell getprop ro.build.fingerprint"),
                ("内存信息", "adb -s {device} shell cat /proc/meminfo | grep -E 'MemTotal|MemFree'"),
                ("时区设置", "adb -s {device} shell getprop persist.sys.timezone"),
                ("语言设置", "adb -s {device} shell getprop persist.sys.locale")
            ]

            results = []
            for device_id in req.devices:
                device_info = {'device': device_id, 'properties': {}}

                for label, cmd_template in info_commands:
                    cmd = cmd_template.format(device=device_id)
                    stdout, stderr, code = ssh_manager.execute_command(ssh, cmd, timeout=10)

                    # 清理输出
                    value = stdout.strip()
                    if '\n' in value:
                        # 如果是多行，取第一行
                        value = value.split('\n')[0].strip()
                    elif not value:
                        value = "未知"

                    device_info['properties'][label] = value

                results.append(device_info)

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={'success': True, 'results': results})

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/devices/management")
@app.post("/api/devices/management")
async def devices_management():
    """设备管理页面（支持GET和POST）- 与Flask一致"""
    try:
        config = config_manager.load_config()

        # 从持久化文件加载USB/IP设备来源
        import json
        try:
            with open(config_manager.dynamic_config_path, 'r') as f:
                dynamic_config = json.load(f)
                persisted_usbip_sources = dynamic_config.get('usbip_devices_source', {})
        except:
            persisted_usbip_sources = {}

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'devices': []})

        try:
            # 获取基本设备列表
            output, _, _ = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = []
            for line in output.split('\n')[1:]:
                if line.strip() and '\tdevice' in line:
                    device_id = line.split('\t')[0]
                    device_ids.append(device_id)

            if not device_ids:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'devices': []})

            # 获取设备锁定状态
            client_ip = '127.0.0.1'  # 本地调用
            client_id = client_manager.get_client_id(client_ip)
            locks = device_lock_manager.get_all_locks()

            # 批量获取设备属性（包括电池电量）
            device_props_cmd = " && ".join([
                f"adb -s {device_id} shell 'echo \"===DEVICE:{device_id}===\" && getprop ro.serialno && getprop ro.product.model && getprop ro.build.version.release && dumpsys battery | grep level | cut -d: -f2 | tr -d \" \"'"
                for device_id in device_ids
            ])

            props_output, _, _ = ssh_manager.execute_command(ssh, device_props_cmd, timeout=15)

            # 解析批量输出
            device_data = {}
            current_device = None

            for line in props_output.split('\n'):
                line = line.strip()
                if line.startswith('===DEVICE:'):
                    current_device = line.split('===DEVICE:')[1].split('===')[0]
                    device_data[current_device] = {'serial_no': '', 'model': '', 'android_version': '', 'battery_level': ''}
                elif current_device and line:
                    if not device_data[current_device]['serial_no']:
                        device_data[current_device]['serial_no'] = line
                    elif not device_data[current_device]['model']:
                        device_data[current_device]['model'] = line
                    elif not device_data[current_device]['android_version']:
                        device_data[current_device]['android_version'] = line
                    elif not device_data[current_device]['battery_level']:
                        device_data[current_device]['battery_level'] = line

            ssh_manager.return_connection(ssh)

            # 构建响应（与Flask版本保持一致）
            devices_info = []
            ubuntu_host = config.get("ubuntu_host", "")
            ubuntu_user = config.get("ubuntu_user", "")

            # 合并所有USB/IP设备来源字典（包括持久化的）
            all_usbip_sources = {}
            all_usbip_sources.update(global_state.usbip_devices_source)
            all_usbip_sources.update(usbip_manager.device_sources)
            all_usbip_sources.update(persisted_usbip_sources)

            # 清理已不存在的设备来源记录（与Flask版本一致）
            # 如果设备已不在当前设备列表中，说明设备已断开/移除，应该清除其来源记录
            current_device_set = set(device_ids)
            devices_to_remove = [
                dev_id for dev_id in all_usbip_sources.keys()
                if dev_id not in current_device_set
            ]
            if devices_to_remove:
                logger.info(f"[Device Management] Cleaning up removed devices: {devices_to_remove}")
                # 从全局状态中清除
                with global_state.usbip_devices_source_lock:
                    for dev_id in devices_to_remove:
                        if dev_id in global_state.usbip_devices_source:
                            del global_state.usbip_devices_source[dev_id]
                # 从usbip_manager中清除
                for dev_id in devices_to_remove:
                    if dev_id in usbip_manager.device_sources:
                        del usbip_manager.device_sources[dev_id]
                # 也要从合并后的字典中清除
                for dev_id in devices_to_remove:
                    if dev_id in all_usbip_sources:
                        del all_usbip_sources[dev_id]

            for device_id in device_ids:
                props = device_data.get(device_id, {})
                lock_info = locks.get(device_id, {})

                # 判断设备来源类型（与Flask版本一致）
                if device_id in all_usbip_sources:
                    # 设备在 USB/IP 记录中 -> 通过 USB/IP 添加的设备
                    source_type = 'usbip'
                    source_host = all_usbip_sources.get(device_id, {}).get('source', 'Unknown')
                else:
                    # 设备不在 USB/IP 记录中 -> 本地直连设备
                    source_type = 'local'
                    source_host = f'{ubuntu_user}@{ubuntu_host}'

                device_info = {
                    'device_id': device_id,
                    'serial_no': props.get('serial_no', device_id),
                    'model': props.get('model', ''),
                    'android_version': props.get('android_version', ''),
                    'battery_level': props.get('battery_level', ''),
                    'source_type': source_type,
                    'source_host': source_host,
                    'status': 'online',
                    'locked_by': lock_info.get('client_id', '') if device_id in locks else '',
                    'locked_by_self': lock_info.get('client_id') == client_id if device_id in locks else False
                }
                devices_info.append(device_info)

            return JSONResponse(content={
                'devices': devices_info
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error getting devices management: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/devices/locks")
async def list_device_locks():
    """列出所有设备锁定"""
    return JSONResponse(content={
        "success": True,
        "data": device_lock_manager.get_all_locks()
    })

@app.post("/api/devices/reboot")
async def reboot_devices(req: DeviceActionRequest):
    """重启设备"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            raise HTTPException(status_code=500, detail="SSH连接失败")

        results = []
        for device_id in req.devices:
            result = device_manager.reboot_device(device_id, ssh)
            result['device'] = device_id
            results.append(result)

        ssh_manager.return_connection(ssh)

        success_count = sum(1 for r in results if r.get('success', False))
        return JSONResponse(content={
            "success": True,
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": len(results) - success_count
            }
        })
    except Exception as e:
        logger.error(f"Error rebooting devices: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/devices/remount")
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount设备"""
    try:
        # 获取client_id
        client_id = get_client_id_from_request(request)

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            raise HTTPException(status_code=500, detail="SSH连接失败")

        results = []
        for device_id in req.devices:
            # 执行 adb root
            output, error, code = ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} root",
                timeout=15
            )

            # 发送输出到前端
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': f"[{device_id}] adb root: {output.strip()}",
                        'log_type': 'info'
                    })
                except:
                    pass

            import time
            time.sleep(2)

            # 执行 remount
            output, error, code = ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} remount",
                timeout=15
            )

            # 发送输出到前端
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': f"[{device_id}] adb remount: {output.strip()}",
                        'log_type': 'info'
                    })
                except:
                    pass

            result = device_manager.remount_device(device_id, ssh)
            result['device'] = device_id
            results.append(result)

        ssh_manager.return_connection(ssh)

        success_count = sum(1 for r in results if r.get('success', False))
        return JSONResponse(content={
            "success": True,
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": len(results) - success_count
            }
        })
    except Exception as e:
        logger.error(f"Error remounting devices: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/devices/connect-wifi")
async def connect_wifi(req: WifiConnectRequest):
    """连接WiFi"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            raise HTTPException(status_code=500, detail="SSH连接失败")

        results = []
        for device_id in req.devices:
            enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
            connect_cmd = f'adb -s {device_id} shell cmd wifi connect-network "{req.ssid}" wpa2 "{req.password}"'
            full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"

            output, error, code = ssh_manager.execute_command(ssh, full_cmd)
            results.append({'device': device_id, 'success': code == 0})

        ssh_manager.return_connection(ssh)

        success_count = sum(1 for r in results if r.get('success', False))
        return JSONResponse(content={
            "success": True,
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": len(results) - success_count
            }
        })
    except Exception as e:
        logger.error(f"Error connecting WiFi: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

class DeviceShellRequest(BaseModel):
    """设备Shell请求"""
    serial_no: str = Field(..., description="设备序列号")

@app.post("/api/device/shell")
async def open_device_shell(req: DeviceShellRequest, request: Request):
    """打开设备ADB Shell - 为终端页面准备设备连接"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "message": "SSH连接失败"},
                status_code=500
            )

        # 验证设备是否在线
        check_cmd = f"adb -s {req.serial_no} shell echo 'ready'"
        output, error, code = ssh_manager.execute_command(ssh, check_cmd)

        ssh_manager.return_connection(ssh)

        if code == 0 and 'ready' in output:
            # 将设备信息保存到会话中,供WebSocket终端使用
            client_id = get_client_id_from_request(request)

            # 保存到全局状态
            if not hasattr(global_state, 'device_shells'):
                global_state.device_shells = {}

            global_state.device_shells[client_id] = {
                'serial_no': req.serial_no,
                'connected_at': datetime.now().isoformat()
            }

            return JSONResponse(content={
                "success": True,
                "message": f"设备 {req.serial_no} 已准备就绪",
                "serial_no": req.serial_no
            })
        else:
            return JSONResponse(
                content={"success": False, "message": f"设备 {req.serial_no} 不在线或无响应"},
                status_code=400
            )
    except Exception as e:
        logger.error(f"Error opening device shell: {e}")
        return JSONResponse(
            content={"success": False, "message": f"打开Shell失败: {str(e)}"},
            status_code=500
        )

# ==================== 测试管理 ====================

@app.post("/api/test/start")
async def start_test(req: TestStartRequest, request: Request):
    """启动测试 - 与Flask版本逻辑一致（后台执行，立即返回）"""
    # 从请求中获取client_id
    client_id = get_client_id_from_request(request)

    # 检查用户是否已有测试在运行
    user_state = get_or_create_user_state(client_id)
    if user_state.get('running', False):
        return JSONResponse(
            content={'success': False, 'error': '您已有测试正在运行'},
            status_code=400
        )

    # 检查设备锁定状态
    devices = req.devices
    if not devices:
        return JSONResponse(
            content={'success': False, 'error': 'No devices selected'},
            status_code=400
        )

    # 获取用户名
    config = config_manager.load_config()
    username = config.get('client_username', 'unknown')

    # 尝试锁定设备
    locked_devices = []
    failed_devices = []

    for device_id in devices:
        success, message = device_lock_manager.lock_device(device_id, client_id, username)
        if success:
            locked_devices.append(device_id)
        else:
            failed_devices.append({'device_id': device_id, 'error': message})

    # 如果有设备锁定失败，释放已锁定的设备并返回错误
    if failed_devices:
        for device_id in locked_devices:
            device_lock_manager.unlock_device(device_id, client_id)

        error_msg = "以下设备已被其他用户占用：\n"
        for fail in failed_devices:
            error_msg += f"- {fail['device_id']} ({fail['error']})\n"

        return JSONResponse(
            content={
                'success': False,
                'error': error_msg.strip(),
                'failed_devices': failed_devices
            },
            status_code=409
        )

    # 准备测试参数
    test_params = req.model_dump()
    test_params['client_id'] = client_id

    # 确保用户状态存在（重要：在更新之前先创建）
    user_state = get_or_create_user_state(client_id)
    logger.info(f"[TestStart] Client state created/loaded: {client_id}")

    # 立即更新用户状态为运行中（与Flask版本一致）
    logger.info(f"[TestStart] Setting running=True for {client_id}")
    update_user_state_field(client_id, {
        'running': True,
        'devices': devices,
        'test_type': req.test_type,
        'logs': []  # 初始化日志列表
    })

    # 在后台任务中执行测试（不阻塞HTTP响应）
    asyncio.create_task(
        run_test_background(
            config,
            test_params,
            client_id,
            locked_devices
        )
    )

    # 立即返回响应（与Flask版本一致）
    return JSONResponse(content={"success": True, "message": "测试已启动"})


async def run_test_background(
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    client_id: str,
    locked_devices: List[str]
):
    """
    后台运行测试（与Flask版本的run_test_suite逻辑一致）
    """
    ssh = None

    # 定义日志回调
    async def log_callback(message: str, log_type: str = 'info'):
        # 构建时间戳
        timestamp_str = datetime.now().strftime('%H:%M:%S')

        # 使用与Flask版本一致的字符串格式
        log_str = f"[{timestamp_str}] {message}"

        # 保存到全局状态（限制数量，防止内存溢出）
        if client_id not in global_state.test_logs:
            global_state.test_logs[client_id] = []
        global_state.test_logs[client_id].append({
            'message': message,
            'type': log_type,
            'timestamp': datetime.now().isoformat()
        })
        # 限制最多保留1000条日志
        if len(global_state.test_logs[client_id]) > 1000:
            global_state.test_logs[client_id] = global_state.test_logs[client_id][-1000:]

        # 保存到用户状态（限制数量，防止内存溢出）
        user_state = get_or_create_user_state(client_id)
        if 'logs' not in user_state:
            user_state['logs'] = []
        user_state['logs'].append(log_str)
        # 限制最多保留1000条日志
        if len(user_state['logs']) > 1000:
            user_state['logs'] = user_state['logs'][-1000:]

        # 通过WebSocket推送
        if client_id in global_state.websocket_connections:
            try:
                await global_state.websocket_connections[client_id].send_json({
                    'type': 'log_update',
                    'log': message,
                    'log_type': log_type
                })
            except:
                pass

    try:
        # 检查测试是否仍在运行（可能被停止）
        user_state = get_or_create_user_state(client_id)
        if not user_state.get('running', False):
            await log_callback("测试已取消", 'warning')
            return

        # 生成进程组ID
        import time
        process_group_id = f"gms_test_{client_id.replace('@', '_')}_{int(time.time() * 1000)}"
        update_user_state_field(client_id, {'process_group_id': process_group_id})

        await log_callback(f"🔖 进程组ID: {process_group_id}", 'info')

        # 建立SSH连接
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await log_callback("❌ SSH连接失败", 'error')
            update_user_state_field(client_id, {'running': False})
            return

        await log_callback("✅ SSH 连接成功", 'success')

        # 上传测试脚本
        local_script = os.path.join(
            os.path.dirname(__file__),
            'tools',
            'run_GMS_Test_Auto.sh'
        )

        if os.path.exists(local_script):
            suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
            remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

            script_size = os.path.getsize(local_script)
            size_kb = script_size / 1024

            await log_callback(f"📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)", 'info')

            try:
                sftp = ssh.open_sftp()
                sftp.put(local_script, remote_script)
                sftp.close()

                # 设置可执行权限
                stdin, stdout, stderr = ssh.exec_command(f"chmod +x '{remote_script}'")
                stdout.read()

                await log_callback(f"✅ 上传完成 ({size_kb:.2f}KB)", 'success')
            except Exception as e:
                await log_callback(f"⚠️ 脚本上传失败: {str(e)}", 'warning')
        else:
            await log_callback("⚠️ 本地脚本不存在，使用远程脚本", 'warning')

        # 构建测试命令
        test_type = test_params.get('test_type', 'cts')
        test_module = test_params.get('test_module', '')
        test_case = test_params.get('test_case', '')
        retry_dir = test_params.get('retry_dir', '')
        test_suite = test_params.get('test_suite', '')
        local_server = test_params.get('local_server', '')
        devices = test_params.get('devices', [])

        suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
        remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

        # 构建命令参数
        cmd_parts = [remote_script]

        # 添加测试类型
        if retry_dir:
            timestamp = os.path.basename(retry_dir.strip().rstrip('/'))
            cmd_parts.extend([test_type, "retry", timestamp])
            await log_callback(f"Retry mode: {timestamp}", 'info')
        else:
            cmd_parts.append(test_type)
            if test_module:
                cmd_parts.append(test_module)
                await log_callback(f"Test module: {test_module}", 'info')
            if test_case:
                cmd_parts.append(test_case)
                await log_callback(f"Test case: {test_case}", 'info')

        # 添加设备参数
        if devices:
            device_args_list = []
            if len(devices) > 1:
                device_args_list.extend(["--shard-count", str(len(devices))])
                await log_callback(f"Sharding across {len(devices)} devices", 'info')
            for device in devices:
                device_args_list.extend(["-s", device])

            device_args_str = " ".join(device_args_list)
            cmd_parts.extend(["--device-args", device_args_str])
            await log_callback(f"Devices: {', '.join(devices)}", 'info')

        # 添加测试套件
        if test_suite:
            cmd_parts.extend(["--test-suite", test_suite])
            await log_callback(f"📂 测试套件: {test_suite}", 'info')

        # 添加本地服务器
        if local_server:
            cmd_parts.extend(["--local-server", local_server])
            await log_callback(f"🌐 本地主机: {local_server}", 'info')

        # 构建最终命令
        import shlex
        command = ' '.join(shlex.quote(part) for part in cmd_parts)
        command_full = f"cd {os.path.dirname(remote_script)} && {command}"

        await log_callback(f"🚀 执行命令: {command}", 'info')

        # 执行测试命令（使用PTY获取实时输出）
        stdin, stdout, stderr = ssh.exec_command(command_full, get_pty=True)

        # 实时读取输出
        while not stdout.channel.exit_status_ready():
            # 检查是否被停止
            user_state = get_or_create_user_state(client_id)
            if not user_state.get('running', False):
                await log_callback("⏹️ 测试已被用户停止", 'warning')
                # 终止进程
                try:
                    ssh.exec_command("pkill -f 'run_GMS_Test_Auto.sh'")
                except:
                    pass
                break

            if stdout.channel.recv_ready():
                try:
                    data = stdout.channel.recv(65536).decode('utf-8', errors='replace')
                    if data:
                        lines = data.split('\n')
                        for line in lines:
                            if line.strip():
                                await log_callback(line.strip(), 'info')
                except Exception as e:
                    logger.error(f"Error reading stdout: {e}")

            if stderr.channel.recv_stderr_ready():
                try:
                    error = stderr.channel.recv_stderr(65536).decode('utf-8', errors='replace')
                    if error:
                        lines = error.split('\n')
                        for line in lines:
                            if line.strip():
                                await log_callback(line.strip(), 'error')
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")

            await asyncio.sleep(0.05)

        # 获取退出码
        exit_code = stdout.channel.recv_exit_status()

        if exit_code == 0:
            await log_callback(f"✅ Test completed successfully (exit code: {exit_code})", 'success')
        else:
            await log_callback(f"❌ Test failed with exit code: {exit_code}", 'error')

    except Exception as e:
        logger.error(f"Error in run_test_background: {e}")
        await log_callback(f"❌ 测试执行出错: {str(e)}", 'error')

    finally:
        # 保存测试日志
        try:
            user_state = get_or_create_user_state(client_id)
            user_logs = user_state.get('logs', [])

            # 记录测试报告到数据库（从 RESULT DIRECTORY 获取）
            report_timestamp = save_test_report_to_db(client_id, config, test_params, user_logs)
            if report_timestamp:
                await log_callback(f"📊 测试报告已记录: {report_timestamp}", 'success')

        except Exception as e:
            logger.error(f"保存测试报告失败: {e}")
            import traceback
            traceback.print_exc()

        # 清理资源
        if ssh:
            ssh_manager.return_connection(ssh)

        # 释放设备锁
        for device_id in locked_devices:
            device_lock_manager.unlock_device(device_id, client_id)

        # 更新状态为停止
        update_user_state_field(client_id, {'running': False, 'devices': []})

        # 发送test_complete事件
        if client_id in global_state.websocket_connections:
            try:
                await global_state.websocket_connections[client_id].send_json({
                    'type': 'test_complete'
                })
            except:
                pass

@app.post("/api/test/stop")
async def stop_test(request: Request):
    """停止测试 - 与Flask版本逻辑一致"""
    client_id = get_client_id_from_request(request)
    user_state = get_or_create_user_state(client_id)
    process_group_id = user_state.get('process_group_id')

    # 立即设置running=False（与Flask版本一致）
    update_user_state_field(client_id, {'running': False})

    # 添加停止日志
    timestamp_str = datetime.now().strftime('%H:%M:%S')
    log_str = f"[{timestamp_str}] ⏹️ 用户请求停止测试..."
    if 'logs' not in user_state:
        user_state['logs'] = []
    user_state['logs'].append(log_str)

    # 立即释放设备锁（与Flask版本一致）
    devices_to_release = user_state.get('devices', [])
    for device_id in devices_to_release:
        device_lock_manager.unlock_device(device_id, client_id)
    update_user_state_field(client_id, {'devices': []})

    # 通过SSH杀死测试进程
    config = config_manager.load_config()
    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return JSONResponse(
            content={'success': False, 'error': 'SSH连接失败'},
            status_code=500
        )

    try:
        killed_count = 0

        # 方法1: 使用进程组ID杀死进程（多用户隔离）
        if process_group_id:
            # 通过环境变量 GMS_TEST_PGID 来查找和杀死相关进程
            find_cmd = f"ps eww -e | grep 'GMS_TEST_PGID={process_group_id}' | grep -v grep | awk '{{print $1}}'"
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🧹 正在终止测试进程组: {process_group_id}...")

            # 获取进程ID并杀死
            output, error, code = ssh_manager.execute_command(ssh, find_cmd, timeout=10)
            if output.strip():
                pids = output.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        # 杀死子进程
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                # 等待进程终止
                import time
                time.sleep(1)

                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 已终止 {killed_count} 个测试进程")
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={"success": True, "message": "测试已停止"})

            # 回退：尝试通过命令行参数查找
            fallback_cmd = f"ps aux | grep -- '--pgid {process_group_id}' | grep -v grep | awk '{{print $2}}'"
            output2, error2, code2 = ssh_manager.execute_command(ssh, fallback_cmd, timeout=10)
            if output2.strip():
                pids = output2.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                time.sleep(1)
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 已终止 {killed_count} 个测试进程（命令行匹配）")
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={"success": True, "message": "测试已停止"})

        # 方法2: 回退到传统方法（杀死tradefed进程）
        test_type = user_state.get('test_type', 'cts')
        binary_map = {
            'cts': 'cts-tradefed',
            'gsi': 'cts-tradefed',
            'gts': 'gts-tradefed',
            'sts': 'sts-tradefed',
            'vts': 'vts-tradefed',
            'xts': 'xts-tradefed'
        }
        tradefed_bin = binary_map.get(test_type, 'tradefed')
        kill_cmd = f"pkill -f '[./]?{tradefed_bin}.*run commandAndExit'"

        output, error, code = ssh_manager.execute_command(ssh, kill_cmd, timeout=10)
        ssh_manager.return_connection(ssh)

        if code == 0:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {test_type.upper()} tradefed 进程已终止")
            return JSONResponse(content={"success": True, "message": "测试已停止"})
        else:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 未找到运行中的测试进程")
            return JSONResponse(content={"success": True, "message": "测试已停止"})

    except Exception as e:
        ssh_manager.return_connection(ssh)
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 停止测试时出错: {str(e)}")
        logger.error(f"Error stopping test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/test/clean")
async def clean_test_logs():
    """清理测试日志"""
    try:
        result = test_logs_manager.clean_old_logs(days=7)
        return JSONResponse(content={
            "success": True,
            "message": f"清理了 {result['cleaned_files']} 个文件，释放 {result['freed_space_mb']} MB",
            "data": result
        })
    except Exception as e:
        logger.error(f"Error cleaning logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# 存储最后保存的日志文件路径（用于GET下载）
last_saved_log_file = {}

@app.get("/api/test/logs/download")
async def download_current_log(request: Request):
    """下载当前测试日志"""
    global last_saved_log_file

    try:
        client_id = get_client_id_from_request(request)
        log_file = last_saved_log_file.get(client_id)

        if not log_file or not os.path.exists(log_file):
            from pathlib import Path
            logs_dir = Path(os.path.join(os.path.dirname(__file__), 'logs'))
            if logs_dir.exists():
                existing_files = [(f, f.stat().st_mtime)
                                 for f in logs_dir.glob('*.log')
                                 if f.exists()]
                if existing_files:
                    log_file = str(max(existing_files, key=lambda x: x[1])[0])

        if not log_file or not os.path.exists(log_file):
            user_state = get_or_create_user_state(client_id)
            log_file = user_state.get('log_file')

        if not log_file or not os.path.exists(log_file):
            raise HTTPException(status_code=404, detail="No log file available")

        from fastapi.responses import FileResponse as FastAPIFileResponse
        filename = os.path.basename(log_file)

        return FastAPIFileResponse(
            log_file,
            media_type='text/plain',
            filename=filename
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading current log: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/test/logs/download")
async def download_test_logs(req: dict):
    """下载测试日志"""
    try:
        file_paths = req.get('files', [])
        if not file_paths:
            raise HTTPException(status_code=400, detail="未选择文件")

        result = test_logs_manager.download_logs(file_paths)

        if result['success']:
            return FileResponse(
                result['zip_path'],
                media_type='application/zip',
                filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            )
        else:
            raise HTTPException(status_code=500, detail=result['error'])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/test/logs/save-current")
async def save_current_log(req: dict):
    """保存当前日志"""
    global last_saved_log_file

    log_content = req.get('content', '')
    client_id = req.get('client_id', 'test_client')
    test_type = req.get('test_type', '').strip()

    if not log_content:
        raise HTTPException(status_code=400, detail='No log content provided')

    try:
        logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
        os.makedirs(logs_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        config = config_manager.load_config()

        display_test_type = "MANUAL" if not test_type or test_type.lower() == 'unknown' else test_type.upper()

        if client_id == 'test_client':
            user_id = config.get('ubuntu_user', 'hcq')
        else:
            user_id = client_id.split('@')[0] if '@' in client_id else client_id

        log_filename = f"{user_id}_{display_test_type}_{timestamp}.log"
        log_path = os.path.join(logs_dir, log_filename)

        from pathlib import Path
        log_file = Path(log_path)

        log_file.write_text(
            f"GMS 测试日志 - {display_test_type}\n"
            f"保存时间: {timestamp}\n"
            f"用户标识: {user_id}\n"
            f"完整Client ID: {client_id}\n"
            f"{'=' * 80}\n\n"
            f"{log_content}",
            encoding='utf-8'
        )

        last_saved_log_file[client_id] = str(log_file)

        return JSONResponse(content={
            'success': True,
            'log_file': str(log_file),
            'filename': log_filename,
            'message': f'日志已保存: {log_filename}'
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving log: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"{str(e)}. 请检查配置和参数是否正确。"
        )

@app.get("/api/test/logs/list")
async def list_test_logs():
    """列出测试日志"""
    try:
        result = test_logs_manager.list_log_files()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error listing logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/test/autocomplete-suite")
async def autocomplete_suite(req: AutocompleteSuiteRequest):
    """Auto-complete test suite path with tools subdirectory"""
    try:
        test_type = req.test_type.lower()
        base_path = req.base_path

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            if not base_path:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': 'Base path is required'}, status_code=400)

            # Map test types to their suite directories and binaries (same as GUI)
            suite_map = {
                'cts': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
                'gsi': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
                'gts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'},
                'sts': {'subdir': 'android-sts', 'binary': 'sts-tradefed'},
                'vts': {'subdir': 'android-vts', 'binary': 'vts-tradefed'},
                'apts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'}
            }

            config_info = suite_map.get(test_type)
            if not config_info:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': f'不支持的测试类型: {test_type}'}, status_code=400)

            subdir = config_info['subdir']
            binary = config_info['binary']

            # Try multiple path patterns to find the test suite
            candidates = []

            # Pattern 1: {base_path}/{subdir}/tools (standard structure)
            candidates.append(f"{base_path}/{subdir}/tools")

            # Pattern 2: Search for {subdir} in subdirectories of base_path
            # This handles structures like: base_path/android-gts-13.1-R1/android-gts/tools
            find_cmd = f"find '{base_path}' -maxdepth 3 -type d -name '{subdir}' 2>/dev/null | head -5"
            find_output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=10)

            if find_output.strip():
                for line in find_output.strip().split('\n'):
                    # Add tools subdirectory to each found subdir
                    candidates.append(f"{line}/tools")

            # Pattern 3: Check if base_path itself is already the tools directory
            # Check for binary directly in base_path
            check_direct = f"[ -x '{base_path}/{binary}' ] && echo '{base_path}' || echo ''"
            direct_output, _, _ = ssh_manager.execute_command(ssh, check_direct)
            if direct_output.strip():
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={
                    'success': True,
                    'path': base_path,
                    'binary': binary,
                    'autocompleted': True
                })

            # Try each candidate path
            for candidate in candidates:
                check_cmd = f"[ -x '{candidate}/{binary}' ] && echo '{candidate}' || echo ''"
                output, error, code = ssh_manager.execute_command(ssh, check_cmd)

                if output.strip():
                    final_path = output.strip()
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': True,
                        'path': final_path,
                        'binary': binary,
                        'autocompleted': True
                    })

            # If binary not found, return original path with warning (GUI behavior)
            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'path': base_path,
                'autocompleted': False,
                'warning': f'未找到 {binary}，请确认路径正确'
            })

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error autocompleting suite: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/status")
async def get_status(request: Request):
    """获取测试状态 - 优化版本，减少数据传输"""
    try:
        # 处理USB事件队列（如果有）
        if hasattr(app.state, 'usb_event_queue'):
            import queue
            try:
                while True:
                    event = app.state.usb_event_queue.get_nowait()
                    # 向所有连接的WebSocket客户端发送设备变化通知
                    for client_id, ws in list(global_state.websocket_connections.items()):
                        try:
                            await ws.send_json(event)
                            logger.info(f"Sent USB event to client {client_id}: {event.get('type')}")
                        except Exception as e:
                            logger.error(f"Error sending USB event to client {client_id}: {e}")
            except queue.Empty:
                pass  # 队列为空，正常情况

        # 跟踪用户访问
        client_id = get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)

        logger.info(f"[Status] Client {client_id} running={user_state.get('running', False)}")

        # 获取请求参数
        since = request.query_params.get('since')
        include_logs = request.query_params.get('logs', 'true').lower() == 'true'

        response = {
            'running': user_state.get('running', False),
            'devices': user_state.get('devices', []),
        }

        # 添加 USB 监控器状态信息
        from core.usb_monitor import get_usb_monitor
        usb_monitor = get_usb_monitor()
        if usb_monitor:
            response['usb_monitor'] = {
                'mode': usb_monitor.mode,
                'running': usb_monitor.is_running,
                'pyudev_available': usb_monitor.pyudev_available
            }

        # 只在需要时返回日志
        if include_logs:
            logs = user_state.get('logs', [])
            if since is not None and since.isdigit():
                since_int = int(since)
                if 0 <= since_int < len(logs):
                    # 只返回新日志（增量）
                    response['logs'] = logs[since_int:]
                    response['log_count'] = len(logs)
                else:
                    # 返回最近50条日志
                    response['logs'] = logs[-50:]
                    response['log_count'] = len(logs)
            else:
                # 返回最近50条日志
                response['logs'] = logs[-50:]
                response['log_count'] = len(logs)

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# ==================== 报告管理 ====================

@app.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False):
    """
    从数据库获取测试报告列表

    Args:
        user_only: 是否只显示当前用户的报告，默认 False 显示所有用户的报告
    """
    try:
        # 从数据库获取报告
        all_reports = test_report_db.get_reports(limit=100)

        # 如果要求只显示当前用户的报告，进行过滤
        if user_only:
            # 获取当前用户ID
            client_id = get_client_id_from_request(request)

            # 对于本地访问（127.0.0.1或::1），也显示配置文件中client_ip对应的报告
            config = config_manager.load_config()
            configured_ip = config.get('client_ip', '')
            username = config.get('client_username', 'unknown')

            # 构建可能的client_id列表
            possible_client_ids = [client_id]
            if configured_ip:
                # 如果当前是本地访问，添加配置文件IP对应的client_id
                if '@127.0.0.1' in client_id or '@::1' in client_id or '@localhost' in client_id:
                    possible_client_ids.append(f"{username}@{configured_ip}")

            # 过滤当前用户的报告（支持多个可能的client_id）
            all_reports = [
                r for r in all_reports
                if r.get('client_id') in possible_client_ids
            ]

        # 返回报告列表
        return JSONResponse(content={'reports': all_reports})

    except Exception as e:
        logger.error(f"获取报告列表失败: {e}")
        return JSONResponse(content={'reports': []})

@app.get("/api/reports/{report_timestamp}/files")
async def list_report_files(report_timestamp: str):
    """从数据库获取报告目录并列出文件（与Flask版本一致）"""
    try:
        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 获取 result_dir 路径
        report_dir = report.get('result_dir')
        if not report_dir or not os.path.exists(report_dir):
            return JSONResponse(
                content={'success': False, 'error': '报告目录不存在'},
                status_code=404
            )

        # 列出文件
        files = []
        for root, dirs, filenames in os.walk(report_dir):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                # 相对于报告目录的路径，不包括时间戳目录名
                rel_path = os.path.relpath(file_path, report_dir)

                # 获取文件大小
                try:
                    file_size = os.path.getsize(file_path)
                except:
                    file_size = 0

                files.append({
                    'name': filename,
                    'path': file_path,
                    'relative_path': rel_path,
                    'size': file_size
                })

                # 限制返回数量
                if len(files) >= 100:
                    break

            if len(files) >= 100:
                break

        return JSONResponse(content={'success': True, 'files': files})

    except Exception as e:
        logger.error(f"Error listing report files: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/api/reports/{report_timestamp}/analyze")
async def analyze_report(report_timestamp: str):
    """从数据库分析测试报告（与Flask版本一致）"""
    try:
        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 获取 result_dir 路径
        result_dir = report.get('result_dir')
        if not result_dir or not os.path.exists(result_dir):
            return JSONResponse(
                content={'success': False, 'error': '报告目录不存在'},
                status_code=404
            )

        # 查找 test_result.xml
        result_xml = os.path.join(result_dir, 'test_result.xml')
        if not os.path.exists(result_xml):
            return JSONResponse(
                content={'success': False, 'error': 'test_result.xml 不存在'},
                status_code=404
            )

        # 使用 ReportAnalyzer 解析 XML
        analyzer = ReportAnalyzer()
        result = analyzer.analyze_file(result_xml)

        if not result:
            return JSONResponse(
                content={'success': False, 'error': '解析 XML 失败'},
                status_code=500
            )

        # 转换为前端需要的格式（与Flask版本一致）
        analysis = {
            'summary': result['summary'],
            'device_info': {
                'device': result['details']['device'],
                'android_version': result['details']['android_version']
            },
            'test_info': {
                'start_time': result['details']['start_time'],
                'test_type': result['details']['test_type']
            },
            'failures': result['failures']
        }

        return JSONResponse(content={'success': True, 'data': analysis})

    except Exception as e:
        logger.error(f"Error analyzing report: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/api/reports/view")
async def view_report_file(request: Request):
    """查看报告文件内容（与Flask版本一致，通过SSH读取远程文件）"""
    try:
        file_path = request.query_params.get('path')
        if not file_path:
            return JSONResponse(
                content={'success': False, 'error': 'File path is required'},
                status_code=400
            )

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'},
                status_code=500
            )

        try:
            # 读取文件内容
            cat_cmd = f"cat '{file_path}' 2>/dev/null"
            output, error, code = ssh_manager.execute_command(ssh, cat_cmd, timeout=30)

            ssh_manager.return_connection(ssh)

            # 确定内容类型
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext in ['.xml', '.html']:
                content_type = 'text/html'
            elif file_ext == '.json':
                content_type = 'application/json'
            elif file_ext in ['.log', '.txt']:
                content_type = 'text/plain'
            else:
                content_type = 'text/plain'

            return JSONResponse(content={
                'success': True,
                'content': output,
                'content_type': content_type
            })

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error viewing report file: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.delete("/api/reports/delete")
async def delete_report(request: Request, timestamp: str = Query(..., description="报告时间戳")):
    """删除测试报告（仅限报告所有者）"""
    try:
        # 获取当前客户端信息
        client_id = get_client_id_from_request(request)

        # 从数据库获取报告
        report = test_report_db.get_report_by_timestamp(timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 权限校验：只允许报告的所有者删除
        report_client_id = report.get('client_id')
        if report_client_id != client_id:
            logger.warning(f"[DELETE] 权限拒绝: 客户端 {client_id} 尝试删除客户端 {report_client_id} 的报告")
            return JSONResponse(
                content={'success': False, 'error': '您没有权限删除此报告'},
                status_code=403
            )

        # 删除报告目录
        result_dir = report.get('result_dir')
        if result_dir and os.path.exists(result_dir):
            import shutil
            try:
                shutil.rmtree(result_dir)
                logger.info(f"已删除报告目录: {result_dir}")
            except Exception as e:
                logger.error(f"删除报告目录失败: {e}")
                return JSONResponse(
                    content={'success': False, 'error': f'删除报告目录失败: {str(e)}'},
                    status_code=500
                )

        # 从数据库删除记录
        success = test_report_db.delete_report(timestamp)

        if success:
            return JSONResponse(content={'success': True, 'message': '报告已删除'})
        else:
            return JSONResponse(
                content={'success': False, 'error': '删除数据库记录失败'},
                status_code=500
            )

    except Exception as e:
        logger.error(f"Error deleting report: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/reports/{report_timestamp}/download")
async def download_report(request: Request, report_timestamp: str):
    """下载测试报告（打包为ZIP文件）"""
    import io
    import zipfile
    from fastapi.responses import Response

    try:
        logger.info(f"[DOWNLOAD] 请求下载报告: timestamp='{report_timestamp}'")

        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)
        logger.info(f"[DOWNLOAD] 查询报告结果: {report is not None}")

        if not report:
            logger.error(f"[DOWNLOAD] 报告不存在: {report_timestamp}")
            # 尝试列出所有报告以供调试
            all_reports = test_report_db.get_reports(limit=10)
            logger.info(f"[DOWNLOAD] 数据库中的报告列表: {[r['timestamp'] for r in all_reports]}")
            return JSONResponse(
                content={'success': False, 'error': f'报告不存在: {report_timestamp}'},
                status_code=404
            )

        # 获取 result_dir 路径
        report_dir = report.get('result_dir')
        logger.info(f"[DOWNLOAD] 报告目录: {report_dir}, 存在: {os.path.exists(report_dir) if report_dir else False}")

        if not report_dir or not os.path.exists(report_dir):
            logger.error(f"[DOWNLOAD] 报告目录不存在: {report_dir}")
            return JSONResponse(
                content={'success': False, 'error': f'报告目录不存在: {report_dir}'},
                status_code=404
            )

        # 创建ZIP文件到内存
        zip_buffer = io.BytesIO()
        zip_filename = f"report_{report_timestamp}.zip"
        file_count = 0

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, dirs, filenames in os.walk(report_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    # 计算相对路径，保持目录结构
                    arcname = os.path.relpath(file_path, os.path.dirname(report_dir))

                    try:
                        zip_file.write(file_path, arcname)
                        file_count += 1
                    except Exception as e:
                        logger.warning(f"无法添加文件到ZIP: {file_path}, 错误: {e}")

        logger.info(f"创建ZIP文件: {zip_filename}, 包含 {file_count} 个文件")

        # 获取ZIP数据
        zip_data = zip_buffer.getvalue()

        if len(zip_data) == 0:
            return JSONResponse(
                content={'success': False, 'error': 'ZIP文件创建失败'},
                status_code=500
            )

        # 返回ZIP文件
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{zip_filename}\""
            }
        )

    except Exception as e:
        logger.error(f"Error downloading report: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/report/analyze")
async def analyze_test_report(
    file: Optional[UploadFile] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    files_array: Optional[List[UploadFile]] = File(default=None, alias='files[]')
):
    """
    分析上传的测试报告文件或文件夹（使用新的简化分析器模块）

    Request: multipart/form-data
        - 'file': 单个文件上传（XML、ZIP、TAR.GZ）
        - 'files': 多文件上传（文件夹模式）
        - 'files[]': 多文件上传（HTML标准格式，兼容Flask版本）

    Response:
        {
            "success": true,
            "data": {
                "test_type": "GTS",
                "device": "device_serial",
                "android_version": "15",
                "start_time": "2025-12-02 09:35:01",
                "total": 100,
                "pass_count": 95,
                "fail_count": 5,
                "pass_rate": "95.00%",
                "failures": [
                    {
                        "name": "com.example.Test#testMethod",
                        "reason": "Failure reason...",
                        "module": "ModuleName"
                    }
                ]
            }
        }
    """
    import tempfile

    # 支持多种上传方式 - 优先使用 files[] 参数（Flask兼容）
    all_files = []
    if file:
        all_files = [file]
    elif files_array:
        all_files = files_array
    elif files:
        all_files = files

    if not all_files or len(all_files) == 0:
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'error': '没有上传文件'
            }
        )

    if len(all_files) == 1 and all_files[0].filename == '':
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'error': '文件名为空'
            }
        )

    try:
        # 保存上传文件到临时位置
        with tempfile.TemporaryDirectory() as temp_dir:
            # 如果是单文件（XML、ZIP、TAR.GZ）
            if len(all_files) == 1:
                uploaded_file = all_files[0]
                temp_file_path = os.path.join(temp_dir, uploaded_file.filename)

                # 保存文件内容
                with open(temp_file_path, 'wb') as f:
                    content = await uploaded_file.read()
                    f.write(content)

                # 使用 ReportAnalyzer 分析报告
                analyzer = ReportAnalyzer(temp_dir=temp_dir)
                result = analyzer.analyze_file(temp_file_path)

                if result:
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })
                else:
                    return JSONResponse(
                        status_code=400,
                        content={
                            'success': False,
                            'error': '无法解析报告文件',
                            'message': '请确保文件是有效的XML或压缩包格式'
                        }
                    )

            # 如果是多文件（文件夹上传）
            else:
                # 保存所有文件到临时目录
                for uploaded_file in all_files:
                    if uploaded_file.filename:
                        # 保持相对路径结构
                        file_path = os.path.join(temp_dir, uploaded_file.filename)
                        # 确保目录存在
                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                        # 保存文件内容
                        with open(file_path, 'wb') as f:
                            content = await uploaded_file.read()
                            f.write(content)

                # 查找 test_result.xml 或 host_log（支持两种模式）
                analyzer = ReportAnalyzer(temp_dir=temp_dir)
                xml_path = analyzer.file_handler.find_xml_file()

                # 如果没有 test_result.xml，尝试使用日志分析器
                if not xml_path:
                    logger.info(f"未找到 test_result.xml，尝试使用HostLog日志分析器")
                    result = analyzer.analyze_log_dir(temp_dir)

                    if not result:
                        return JSONResponse(
                            status_code=400,
                            content={
                                'success': False,
                                'error': '未找到 test_result.xml 或 host_log 文件',
                                'message': f'已接收 {len(all_files)} 个文件，但文件夹中既不包含 test_result.xml 也不包含 host_log'
                            }
                        )

                    # 标记为日志分析结果
                    result['report_type'] = 'log'
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })

                # 分析报告（使用 analyze_file 方法来获得正确的字典格式）
                result = analyzer.analyze_file(xml_path)

                if result:
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })
                else:
                    return JSONResponse(
                        status_code=400,
                        content={
                            'success': False,
                            'error': '无法解析报告文件',
                            'message': 'test_result.xml 文件格式无效或损坏'
                        }
                    )

    except Exception as e:
        logger.error(f"报告分析失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'error': '报告分析失败',
                'message': str(e)
            }
        )

# ==================== 测试分析辅助函数 ====================

def parse_cts_failure_info(test_name, error_message):
    """
    解析CTS失败信息，提取关键信息

    Args:
        test_name: 测试用例名称，如 com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted
        error_message: 错误消息

    Returns:
        dict: 包含解析后的信息
    """
    result = {
        'class_name': None,
        'method_name': None,
        'package': None,
        'error_type': None,
        'error_keywords': []
    }

    # 解析测试名称
    if test_name and '#' in test_name:
        class_part, method_part = test_name.split('#', 1)
        result['class_name'] = class_part.strip()
        result['method_name'] = method_part.strip()

        # 提取包名
        if '.' in result['class_name']:
            parts = result['class_name'].split('.')
            result['package'] = '.'.join(parts[:-1])  # 去掉最后的类名

    # 解析错误类型
    if error_message:
        error_patterns = [
            r'(java\.lang\.(\w+Exception))',
            r'(java\.lang\.(\w+Error))',
            r'(android\.view\.(\w+Exception))',
            r'(android\.util\.(\w+Exception))',
        ]

        for pattern in error_patterns:
            match = re.search(pattern, error_message)
            if match:
                result['error_type'] = match.group(1)
                break

        # 提取错误关键词
        keyword_patterns = [
            r'Process crashed',
            r'Instrumentation run failed',
            r'Permission denied',
            r'SecurityException',
            r'NullPointerException',
            r'IllegalArgumentException',
            r'package not found',
            r'Unable to resolve',
            r'Connection refused',
        ]

        for pattern in keyword_patterns:
            if re.search(pattern, error_message, re.IGNORECASE):
                result['error_keywords'].append(pattern)

    return result


def construct_source_search_url(search_term, search_type='code'):
    """
    构造Android源码搜索URL

    Args:
        search_term: 搜索词（通常是类名）
        search_type: 搜索类型 (code, symbol, file)

    Returns:
        str: 完整的搜索URL
    """
    base_url = "https://cs.android.com/android/platform/superproject"
    encoded_term = urllib.parse.quote(search_term)

    if search_type == 'symbol':
        return f"{base_url}/+/refs/heads/main:qd/?q={encoded_term}"
    else:
        # 使用文件名搜索（添加.java扩展名），这样更容易找到源文件
        # 例如: AngleAllowlistTraceTest -> AngleAllowlistTraceTest.java
        return f"{base_url}/+/android-latest-release:qd/?q={encoded_term}.java&ss=android%2Fplatform%2Fsuperproject"


def analyze_test_failure_class(class_name, error_type=None):
    """
    分析测试失败的类，提供可能的源码位置和修复建议

    Args:
        class_name: 类名（如 com.google.android.gts.multiuser.RestrictedProfileHostTest）
        error_type: 错误类型（如 java.lang.AssertionError）

    Returns:
        dict: 分析结果
    """
    analysis = {
        'test_type': 'unknown',
        'possible_causes': [],
        'source_links': [],
        'suggestions': []
    }

    # 判断测试类型
    if class_name:
        if 'GmsCore' in class_name or 'gmscore' in class_name.lower():
            analysis['test_type'] = 'GMS Core测试'
            analysis['possible_causes'].append('GMS Core相关功能缺失或配置错误')
            analysis['source_links'].append({
                'title': 'GMS Core源码',
                'url': construct_source_search_url('GmsCore')
            })
        elif 'Multiuser' in class_name or 'multiuser' in class_name.lower():
            analysis['test_type'] = '多用户测试'
            analysis['possible_causes'].append('多用户功能实现不完整')
            analysis['source_links'].append({
                'title': '多用户管理源码',
                'url': 'https://cs.android.com/android/platform/superproject/+/refs/heads/main:frameworks/base/services/core/java/com/android/server/pm/UserManagerService.java'
            })
        elif 'Permission' in class_name or 'permission' in class_name.lower():
            analysis['test_type'] = '权限测试'
            analysis['possible_causes'].append('权限配置缺失或不正确')
            analysis['source_links'].append({
                'title': '权限管理源码',
                'url': construct_source_search_url('PermissionManager')
            })

    # 根据错误类型添加建议
    if error_type:
        if 'AssertionError' in error_type:
            analysis['suggestions'].append('检查测试条件是否符合预期')
            analysis['suggestions'].append('验证相关功能的实现是否正确')
        elif 'SecurityException' in error_type:
            analysis['suggestions'].append('检查权限声明')
            analysis['suggestions'].append('验证签名和证书配置')
        elif 'NullPointerException' in error_type:
            analysis['suggestions'].append('检查空指针引用')
            analysis['suggestions'].append('验证初始化流程')

    # 如果没有特定建议，添加通用建议
    if not analysis['suggestions']:
        analysis['suggestions'].extend([
            '检查相关功能的完整实现',
            '验证系统配置是否符合要求',
            '查看CTS测试文档了解详细要求'
        ])

    return analysis


def extract_suggestions_from_text(text):
    """从文本中提取建议"""
    suggestions = []
    lines = text.split('\n')

    for i, line in enumerate(lines):
        line = line.strip()
        # 查找包含建议关键词的行
        if any(keyword in line for keyword in ['建议', '应该', '需要', '可以', '解决', '修复', '检查']):
            suggestions.append(line)
            # 包含后续几行（如果它们是详细的说明）
            for j in range(i + 1, min(i + 3, len(lines))):
                next_line = lines[j].strip()
                if next_line and (next_line.startswith(' ') or next_line.startswith('\t')):
                    suggestions[-1] += ' ' + next_line
                elif next_line:
                    break

    return suggestions[:5]  # 最多返回5条建议


def parse_ai_response(ai_response):
    """
    解析AI的响应，结构化返回

    Args:
        ai_response: AI返回的文本

    Returns:
        dict: 结构化的分析结果
    """
    result = {
        'raw_response': ai_response,
        'analysis': '',
        'suggestions': [],
        'root_cause': '',
        'related_docs': []
    }

    # 尝试从AI响应中提取结构化信息
    lines = ai_response.split('\n')
    current_section = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 识别章节
        if '根本原因' in line or '问题分析' in line:
            current_section = 'root_cause'
            result['root_cause'] = line.split(':', 1)[1].strip() if ':' in line else ''
        elif '解决' in line or '修复' in line or '方案' in line:
            current_section = 'suggestions'
        elif '分析' in line and '根本原因' not in line:
            current_section = 'analysis'
        elif line.startswith(('-', '*', '•')) or (line[0].isdigit() and '.' in line):
            # 列表项
            item = line.lstrip('-*•0123456789. ').strip()
            if current_section == 'suggestions' and item:
                result['suggestions'].append(item)
            elif current_section == 'root_cause' and item:
                result['root_cause'] += ' ' + item
            elif current_section == 'analysis' and item:
                result['analysis'] += ' ' + item
        else:
            # 普通文本
            if current_section == 'analysis':
                result['analysis'] += line + '\n'
            elif current_section == 'root_cause':
                result['root_cause'] += line + '\n'
            else:
                result['analysis'] += line + '\n'

    # 如果没有提取到结构化信息，将整个响应作为分析
    if not result['analysis'] and not result['root_cause']:
        result['analysis'] = ai_response

    # 如果没有建议，从分析中提取
    if not result['suggestions']:
        result['suggestions'] = extract_suggestions_from_text(ai_response)

    return result


def call_ai_api(api_url, api_key, model, prompt):
    """调用AI API进行分析"""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }

    # 根据不同的API提供商构建请求体
    if 'openai' in api_url.lower():
        data = {
            'model': model or 'gpt-3.5-turbo',
            'messages': [
                {'role': 'system', 'content': '你是一个专业的Android测试分析专家，精通CTS/GTS测试和Android系统开发。'},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.7
        }
    elif 'anthropic' in api_url.lower():
        data = {
            'model': model or 'claude-3-sonnet-20240229',
            'max_tokens': 2000,
            'messages': [
                {'role': 'user', 'content': prompt}
            ]
        }
    else:
        # 通用格式
        data = {
            'model': model,
            'prompt': prompt,
            'max_tokens': 2000
        }

    req = urllib.request.Request(
        api_url,
        data=json.dumps(data).encode('utf-8'),
        headers=headers,
        method='POST'
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))

    # 解析返回结果
    if 'choices' in result:  # OpenAI格式
        ai_response = result['choices'][0]['message']['content']
    elif 'completion' in result:  # 其他格式
        ai_response = result['completion']
    else:
        ai_response = str(result)

    return parse_ai_response(ai_response)


def call_ollama(prompt):
    """
    调用本地ollama模型进行分析
    """
    logger = logging.getLogger(__name__)

    try:
        # 检查ollama是否安装
        check_cmd = ['which', 'ollama']
        result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=5)

        if result.returncode != 0:
            logger.info("Ollama未安装")
            raise Exception('Ollama未安装')

        # 使用ollama API（默认运行在localhost:11434）
        data = {
            'model': 'llama2',  # 默认模型
            'prompt': prompt,
            'stream': False
        }

        req = urllib.request.Request(
            'http://localhost:11434/api/generate',
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            ai_response = result.get('response', '')

        return parse_ai_response(ai_response)

    except subprocess.TimeoutExpired:
        logger.warning("Ollama检查超时")
        raise Exception('Ollama检查超时')
    except FileNotFoundError:
        logger.info("Ollama命令未找到")
        raise Exception('Ollama未安装')
    except urllib.error.URLError as e:
        logger.warning(f"Ollama服务连接失败: {str(e)}")
        raise Exception(f'Ollama服务不可用: {str(e)}')
    except Exception as e:
        logger.warning(f"Ollama调用失败: {str(e)}")
        raise Exception(f'Ollama调用失败: {str(e)}')


def rule_based_analysis(test_name, error_message, stack_trace, module):
    """
    基于规则的分析（当AI不可用时）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块

    Returns:
        dict: 分析结果
    """
    # 解析失败信息
    failure_info = parse_cts_failure_info(test_name, error_message)

    analysis_parts = []
    suggestions = []
    root_cause = ""
    related_docs = []

    # 根据错误类型分析
    if 'Process crashed' in error_message or 'Instrumentation run failed' in error_message:
        root_cause = "测试进程崩溃，可能是由于目标应用或服务异常退出导致"
        analysis_parts.append("测试执行过程中进程异常终止")
        suggestions.extend([
            "检查设备日志（logcat）查找崩溃原因",
            "验证被测试的应用是否正常安装和运行",
            "检查设备内存是否充足",
            "查看系统日志中是否有ANR或FC信息"
        ])
        related_docs.append({
            'title': 'Android调试指南',
            'url': 'https://source.android.com/docs/core/debug'
        })

    elif 'Permission' in error_message or 'SecurityException' in error_message:
        root_cause = "权限相关错误，缺少必要的权限声明或配置"
        analysis_parts.append("测试用例需要特定权限但未获得授权")
        suggestions.extend([
            "检查AndroidManifest.xml中的权限声明",
            "验证runtime permission是否正确请求",
            "检查签名是否匹配",
            "确认premission-level是否正确"
        ])
        related_docs.append({
            'title': 'Android权限文档',
            'url': 'https://developer.android.com/guide/topics/permissions/overview'
        })

    elif 'AssertionError' in error_message:
        root_cause = "断言失败，测试条件不满足"
        analysis_parts.append("测试断言检查失败")

        if 'multiuser' in test_name.lower():
            analysis_parts.append("多用户功能测试失败")
            suggestions.extend([
                "检查UserManager服务是否正常",
                "验证多用户配置是否正确",
                "确认restricted profile功能已实现",
                "检查用户切换相关API"
            ])
            related_docs.append({
                'title': 'Android多用户文档',
                'url': 'https://source.android.com/docs/core/architecture/configuration/multi-user'
            })

        if 'GmsCore' in test_name or 'gmscore' in test_name.lower():
            analysis_parts.append("GMS Core相关测试失败")
            suggestions.extend([
                "检查GMS Core包是否正确安装",
                "验证GMS服务权限配置",
                "检查Google Play Services版本",
                "确认GMS证书配置正确"
            ])
            related_docs.append({
                'title': 'GMS Core文档',
                'url': 'https://developer.android.com/google/play/services'
            })

    elif 'package not found' in error_message.lower():
        root_cause = "目标包未找到或未安装"
        suggestions.extend([
            "确认目标应用已正确安装",
            "检查包名是否正确",
            "验证应用是否与当前Android版本兼容"
        ])

    # 通用建议
    if not suggestions:
        suggestions = [
            "查看完整的测试日志了解详细错误信息",
            "检查设备状态是否正常",
            "验证测试环境配置",
            "查阅CTS/GTS测试文档了解测试要求"
        ]

    # 组合分析结果
    analysis = "\n".join(analysis_parts) if analysis_parts else "测试执行失败，请查看详细错误信息"

    # 如果没有根本原因，从错误消息中推断
    if not root_cause:
        if failure_info.get('error_type'):
            root_cause = f"错误类型: {failure_info['error_type']}"
        else:
            root_cause = "测试执行过程中出现异常"

    return {
        'analysis': analysis,
        'suggestions': suggestions[:8],  # 最多8条建议
        'root_cause': root_cause,
        'related_docs': related_docs,
        'ai_enabled': False  # 标记这不是AI分析
    }


def analyze_with_ai(test_name, error_message, stack_trace='', module=''):
    """
    调用大模型API分析测试失败（支持多个AI提供商，自动获取源码）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块名称

    Returns:
        dict: AI分析结果（包含源码分析）
    """
    logger = logging.getLogger(__name__)

    # 优先使用通用AI分析器
    try:
        from core.universal_ai import get_universal_analyzer

        # 获取通用AI分析器
        ai_analyzer = get_universal_analyzer()

        # 解析测试信息
        failure_info = parse_cts_failure_info(test_name, error_message)

        # 调用AI分析（自动获取源码）
        result = ai_analyzer.analyze_test_failure(
            class_name=failure_info.get('class_name', ''),
            method_name=failure_info.get('method_name'),
            error_message=error_message,
            stack_trace=stack_trace,
            auto_fetch_source=True  # 启用自动源码获取
        )

        if result['success']:
            # 转换为旧格式以保持兼容性
            provider_name = result.get('provider', 'unknown')
            provider_display = {
                'zhipu': 'GLM-4 (智谱AI)',
                'ollama': 'Ollama本地模型',
                'openai': 'GPT-4 (OpenAI)',
                'anthropic': 'Claude (Anthropic)'
            }.get(provider_name, provider_name)

            response = {
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'root_cause': result.get('solution', {}).get('problem_description', ''),
                'related_docs': [],
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name
            }

            # 添加源码信息
            if result.get('source_info'):
                source_info = result['source_info']
                response['source_code_fetched'] = True
                response['source_url'] = source_info.get('source_url', '')
                response['source_file_path'] = source_info.get('file_path', '')
                logger.info(f"分析包含源码: {source_info.get('file_path', 'unknown')}")

            return response
        else:
            logger.warning(f"AI分析失败: {result.get('error')}")
            raise Exception(result.get('error', 'AI分析失败'))

    except ImportError:
        logger.info("通用AI分析器未安装")
    except Exception as e:
        logger.warning(f"通用AI分析失败: {str(e)}")

    # 回退到旧的AI分析方法
    try:
        # 构建分析提示词
        prompt = f"""请分析以下CTS测试失败信息，给出详细的原因分析和解决方案：

测试用例: {test_name}
测试模块: {module if module else '未知'}
错误信息: {error_message}

{f'''堆栈跟踪:
{stack_trace}
''' if stack_trace else ''}

请提供：
1. 问题根本原因分析
2. 具体的解决方案和修复步骤
3. 需要检查的系统配置或代码位置
4. 相关的Android源码模块或类

请用中文回答，格式清晰，包含具体的操作步骤。"""

        # 尝试调用本地安装的AI模型（如通过ollama）
        # 首先检查配置中是否有AI API设置
        config = config_manager.load_config()
        ai_api_key = config.get('ai_api_key', '')
        ai_api_url = config.get('ai_api_url', '')
        ai_model = config.get('ai_model', '')

        logger.info(f"AI配置检查: api_url={ai_api_url}, api_key_set={bool(ai_api_key)}, model={ai_model}")

        # 如果配置了API，使用API调用
        if ai_api_url and ai_api_key:
            logger.info("使用AI API进行分析")
            return call_ai_api(ai_api_url, ai_api_key, ai_model, prompt)
        else:
            # 尝试使用本地ollama
            logger.info("尝试使用本地ollama进行分析")
            return call_ollama(prompt)
    except Exception as e:
        # 如果AI调用失败，返回基于规则的分析
        logger.warning(f"AI调用失败，使用基于规则的分析: {str(e)}")
        try:
            return rule_based_analysis(test_name, error_message, stack_trace, module)
        except Exception as rule_error:
            logger.error(f"规则分析也失败: {str(rule_error)}")
            # 最后的兜底响应
            return {
                'analysis': f'测试分析遇到错误: {str(e)}',
                'suggestions': ['检查服务器日志了解详细错误信息'],
                'root_cause': '分析服务异常',
                'related_docs': [],
                'ai_enabled': False
            }


def get_source_code_suggestions(test_name, error_message, stack_trace=None):
    """
    根据测试失败信息获取源码查询链接和分析建议

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪（可选）

    Returns:
        dict: 包含搜索链接和分析建议
    """
    # 解析失败信息
    failure_info = parse_cts_failure_info(test_name, error_message)

    # 分析测试失败
    analysis = analyze_test_failure_class(
        failure_info.get('class_name', ''),
        failure_info.get('error_type')
    )

    result = {
        'test_info': {
            'name': test_name,
            'class': failure_info.get('class_name'),
            'method': failure_info.get('method_name'),
            'package': failure_info.get('package')
        },
        'error_info': {
            'type': failure_info.get('error_type'),
            'message': error_message[:500] if error_message else '',
            'keywords': failure_info.get('error_keywords', [])
        },
        'analysis': analysis,
        'search_links': [],
        'source_analysis': None  # 新增：源码分析结果
    }

    # 尝试进行源码分析（异步，不阻塞主流程）
    try:
        from core.source_analyzer import source_analyzer

        class_name = failure_info.get('class_name', '')
        if class_name:
            # 提取简单类名
            simple_class_name = class_name.split('.')[-1]

            # 执行源码分析
            source_analysis_result = source_analyzer.analyze_failure_with_source(
                class_name=simple_class_name,
                method_name=failure_info.get('method_name'),
                error_message=error_message,
                stack_trace=stack_trace
            )

            result['source_analysis'] = source_analysis_result

            # 如果找到了源码，添加分析结果和建议
            if source_analysis_result.get('source_found'):
                # 合并源码分析的结果到主分析中
                if source_analysis_result.get('analysis'):
                    analysis['possible_causes'].extend(source_analysis_result['analysis'])
                if source_analysis_result.get('suggestions'):
                    analysis['suggestions'].extend(source_analysis_result['suggestions'])

                # 添加源码链接
                if source_analysis_result.get('source_url'):
                    result['search_links'].insert(0, {
                        'title': f'查看源码: {simple_class_name}.java',
                        'url': source_analysis_result['source_url']
                    })

    except Exception as e:
        logger.warning(f"源码分析失败: {e}")
        result['source_analysis'] = {
            'source_found': False,
            'error': str(e)
        }

    # 生成搜索链接
    if failure_info.get('class_name'):
        # 只搜索类名（不含包名）
        class_name = failure_info["class_name"]
        simple_class_name = class_name.split('.')[-1]  # 提取简单类名

        result['search_links'].append({
            'title': f'搜索测试类: {simple_class_name}',
            'url': construct_source_search_url(simple_class_name)
        })

    # 搜索错误类型
    if failure_info.get('error_type'):
        result['search_links'].append({
            'title': f'搜索错误类型: {failure_info["error_type"]}',
            'url': construct_source_search_url(failure_info["error_type"])
        })

    # 如果有堆栈跟踪，提取相关类
    if stack_trace:
        # 提取at行中的类名
        at_pattern = r'at\s+([a-zA-Z0-9.$_]+)\.'
        classes_found = set(re.findall(at_pattern, stack_trace))

        for cls in list(classes_found)[:3]:  # 最多3个
            if not cls.startswith(failure_info.get('package', '')):
                result['search_links'].append({
                    'title': f'搜索相关类: {cls}',
                    'url': construct_source_search_url(cls)
                })

    # 添加通用搜索链接
    if failure_info.get('error_keywords'):
        keyword = failure_info['error_keywords'][0]
        result['search_links'].append({
            'title': f'搜索问题: {keyword}',
            'url': construct_source_search_url(keyword)
        })

    return result


@app.post("/api/test/analyze-source")
async def analyze_test_source(req: dict):
    """
    分析测试失败并提供Android源码查询链接

    Request body:
        {
            "test_name": "com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted",
            "error_message": "java.lang.AssertionError: ...",
            "stack_trace": "..."  // 可选
        }

    Response:
        {
            "success": true,
            "data": {
                "test_info": {...},
                "error_info": {...},
                "analysis": {...},
                "search_links": [...]
            }
        }
    """
    try:
        test_name = req.get('test_name', '')
        error_message = req.get('error_message', '')
        stack_trace = req.get('stack_trace', '')

        if not test_name:
            raise HTTPException(status_code=400, detail="缺少test_name参数")

        # 获取源码建议
        result = get_source_code_suggestions(test_name, error_message, stack_trace)

        return JSONResponse(content={'success': True, 'data': result})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"源码分析失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"源码分析失败: {str(e)}"
        )


@app.post("/api/test/ai-analyze")
async def ai_analyze_failure(req: dict):
    """
    使用AI分析测试失败（自动获取源码并分析）

    功能说明：
    - 自动从 https://cs.android.com/android/platform/superproject 获取测试用例源码
    - 结合源码、错误信息和测试逻辑进行综合分析
    - 提供诊断结果和修复建议

    Request body:
        {
            "test_name": "com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted",
            "error_message": "java.lang.AssertionError: ...",
            "stack_trace": "...",  // 可选
            "module": "GtsGmscoreHostTestCases"  // 可选
        }

    Response:
        {
            "success": true,
            "data": {
                "analysis": "...",  // AI分析结果（包含源码分析）
                "suggestions": [...],  // 解决建议
                "root_cause": "...",  // 根本原因
                "source_code_fetched": true,  // 是否成功获取源码
                "source_url": "...",  // 源码链接（如果获取成功）
                "source_file_path": "...",  // 源码文件路径
                "ai_model": "GLM-4 (智谱AI)",  // 使用的AI模型
                "related_docs": [...]
            }
        }
    """
    try:
        test_name = req.get('test_name', '')
        error_message = req.get('error_message', '')
        stack_trace = req.get('stack_trace', '')
        module = req.get('module', '')

        if not test_name:
            raise HTTPException(status_code=400, detail="缺少test_name参数")

        # 调用AI分析
        result = analyze_with_ai(test_name, error_message, stack_trace, module)

        return JSONResponse(content={'success': True, 'data': result})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI分析失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"AI分析失败: {str(e)}"
        )

# ==================== VNC管理 ====================

@app.post("/api/vnc/start")
async def start_vnc(req: Optional[VNCStartRequest] = Body(default=None)):
    """启动VNC"""
    if req is None:
        # 如果没有提供请求体，使用配置文件的默认值
        config = config_manager.load_config()
        host = f"{config.get('ubuntu_user', 'hcq')}@{config.get('ubuntu_host', 'localhost')}"
        password = config.get('ubuntu_pswd', '')
        vnc_password = ''
    else:
        host = req.host
        password = req.password
        vnc_password = req.vnc_password or ''

    result = vnc_manager.start_vnc(host, password, vnc_password)
    return JSONResponse(content=result)

@app.post("/api/vnc/stop")
async def stop_vnc():
    """停止VNC"""
    result = vnc_manager.stop_vnc()
    return JSONResponse(content=result)

@app.get("/api/vnc/status")
async def get_vnc_status():
    """获取VNC状态"""
    try:
        result = vnc_manager.get_vnc_status()
        return JSONResponse(content={
            "success": True,
            "data": result
        })
    except Exception as e:
        logger.error(f"Error getting VNC status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/desktop/vnc-start")
async def start_desktop_vnc(req: Optional[VNCStartRequest] = Body(default=None)):
    """启动桌面VNC - 支持多主机VNC连接（与Flask版本完全一致）"""
    import time
    try:
        # 如果没有提供参数，使用配置文件的默认值
        config = config_manager.load_config()

        if req is None:
            # 使用配置中的默认值
            host_connection = f"{config.get('ubuntu_user', 'hcq')}@{config.get('ubuntu_host', 'localhost')}"
            password = config.get('ubuntu_pswd', '')
            vnc_password = config.get('vnc_password', '')
        else:
            host_connection = req.host or f"{config.get('ubuntu_user', 'hcq')}@{config.get('ubuntu_host', 'localhost')}"
            password = req.password or config.get('ubuntu_pswd', '')
            vnc_password = req.vnc_password or config.get('vnc_password', '')

        if not host_connection or '@' not in host_connection:
            raise HTTPException(
                status_code=400,
                detail='无效的主机格式，请使用: 用户名@IP地址'
            )

        # 解析主机信息
        try:
            user, ip = host_connection.split('@', 1)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail='主机格式错误'
            )

        # 检查是否是本地主机
        local_hosts = ['localhost', '127.0.0.1', '::1']
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            local_hosts.append(local_ip)
        except:
            local_ip = None

        is_local = ip in local_hosts

        if is_local:
            # 本地主机的 VNC 启动 - 免密码模式
            logger.info(f"[Desktop] Starting local VNC for {host_connection}...")
            # 本地主机不需要VNC密码
            result = vnc_manager.start_vnc(host_connection, password, None)
            if result.get('success'):
                # 统一URL格式，移除用户名前缀和密码参数，只使用IP地址
                if 'url' in result:
                    # 将 http://hcq@172.16.14.233:6080 替换为 http://172.16.14.233:6080
                    import re
                    # 移除用户名@部分
                    result['url'] = re.sub(r'^(https?://)[^@]+@', r'\1', result['url'])
                    # 移除URL中的密码参数 (&password=xxx 或 ?password=xxx)
                    result['url'] = re.sub(r'[?&]password=[^&]*', '', result['url'])
                    # 修复可能出现的 ?& 问题
                    result['url'] = result['url'].replace('?&', '?')
                return JSONResponse(content=result)
            else:
                raise HTTPException(status_code=500, detail=result.get('error', 'VNC服务启动失败'))

        # 远程主机的 VNC 启动
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        try:
            # 如果提供了密码，使用密码连接
            if password:
                ssh.connect(ip, username=user, password=password, timeout=10)
            else:
                # 尝试使用密钥
                ssh.connect(ip, username=user, timeout=10)

            logger.info(f"[Desktop] Connected to {host_connection}, starting VNC...")

            # 检查noVNC
            check_novnc_cmd = "[ -d /opt/noVNC ] && echo 'exists' || echo 'missing'"
            stdin, stdout, stderr = ssh.exec_command(check_novnc_cmd)
            novnc_output = stdout.read().decode()

            if "missing" in novnc_output:
                ssh.close()
                raise HTTPException(
                    status_code=404,
                    detail='noVNC未安装'
                )

            # 等待显示就绪
            display_ready = False
            for _ in range(30):
                display_cmd = "export DISPLAY=:0 && xprop -root &>/dev/null && echo 'ready'"
                stdin, stdout, stderr = ssh.exec_command(display_cmd)
                disp_output = stdout.read().decode()
                if "ready" in disp_output:
                    display_ready = True
                    break
                time.sleep(1)

            if not display_ready:
                ssh.close()
                raise HTTPException(
                    status_code=503,
                    detail='DISPLAY未就绪'
                )

            # 检查并启动x11vnc - 支持免密或密码模式
            check_x11_cmd = "pgrep -f 'x11vnc.*:0' && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdin, stdout, stderr = ssh.exec_command(check_x11_cmd)
            check_output = stdout.read().decode()
            x11vnc_running = 'RUNNING' in check_output

            if not x11vnc_running:
                if vnc_password:
                    # 使用密码模式：需要创建密码文件
                    x11vnc_cmd = (
                        "export DISPLAY=:0 && "
                        f"echo '{vnc_password}' | x11vnc -display :0 -forever -shared -rfbport 5900 "
                        "-storepasswd ~/.vnc/passwd && "
                        "nohup x11vnc -display :0 -forever -shared -rfbport 5900 "
                        "-rfbauth ~/.vnc/passwd -o /tmp/x11vnc.log > /dev/null 2>&1 &"
                    )
                else:
                    # 免密模式：不使用 -rfbauth 参数
                    x11vnc_cmd = (
                        "export DISPLAY=:0 && "
                        "nohup x11vnc -display :0 -forever -shared -rfbport 5900 "
                        "-nopw -o /tmp/x11vnc.log > /dev/null 2>&1 &"
                    )
                ssh.exec_command(x11vnc_cmd)
                time.sleep(2)

            # 检查并启动websockify
            check_web_cmd = "pgrep -f 'websockify.*6080' && echo 'RUNNING' || echo 'NOT_RUNNING'"
            stdin, stdout, stderr = ssh.exec_command(check_web_cmd)
            web_output = stdout.read().decode()
            websockify_running = 'RUNNING' in web_output

            if not websockify_running:
                websockify_cmd = (
                    "cd /opt/noVNC && "
                    "nohup python3 utils/novnc_proxy --vnc localhost:5900 --listen 6080 "
                    "> /tmp/websockify.log 2>&1 &"
                )
                ssh.exec_command(websockify_cmd)
                time.sleep(2)

            ssh.close()

            # 等待VNC服务就绪
            time.sleep(2)

            # 构建VNC URL，如果提供了密码则添加到URL中
            vnc_url = f"http://{ip}:6080/vnc.html?autoconnect=true"
            if vnc_password:
                from urllib.parse import quote
                vnc_url += f"&password={quote(vnc_password)}"

            return JSONResponse(content={
                'success': True,
                'message': f'✅ VNC服务已启动: {host_connection}',
                'url': vnc_url,
                'local': False
            })

        except paramiko.AuthenticationException:
            ssh.close()
            raise HTTPException(
                status_code=401,
                detail={'error': 'SSH认证失败', 'needs_password': True}
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting desktop VNC: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/desktop/validate-host")
async def validate_host(req: dict = Body(...)):
    """验证主机连接并检查VNC服务（与Flask版本完全一致）"""
    try:
        host_connection = req.get('host', '')
        password = req.get('password', '')

        if not host_connection or '@' not in host_connection:
            return JSONResponse(
                content={'success': False, 'error': '无效的主机格式'},
                status_code=400
            )

        try:
            user, ip = host_connection.split('@', 1)
        except ValueError:
            return JSONResponse(
                content={'success': False, 'error': '主机格式错误'},
                status_code=400
            )

        # 检查是否是本地主机
        local_hosts = ['localhost', '127.0.0.1', '::1']
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            local_hosts.append(local_ip)
        except:
            pass

        is_local = ip in local_hosts

        if is_local:
            # 本地主机直接验证成功
            return JSONResponse(content={
                'success': True,
                'message': '本地主机验证成功',
                'needs_password': False,
                'local': True
            })

        # 远程主机验证
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        if password:
            try:
                ssh.connect(ip, username=user, password=password, timeout=10)
            except paramiko.AuthenticationException:
                ssh.close()
                return JSONResponse(
                    content={'success': False, 'error': 'SSH认证失败', 'needs_password': True},
                    status_code=401
                )
        else:
            # 尝试无密码连接（密钥认证）
            try:
                ssh.connect(ip, username=user, timeout=10)
            except paramiko.AuthenticationException:
                ssh.close()
                return JSONResponse(
                    content={'success': False, 'error': '需要SSH密码', 'needs_password': True},
                    status_code=401
                )

        # 检查VNC密码文件
        check_passwd_cmd = "[ -f ~/.vnc/passwd ] && echo 'exists' || echo 'missing'"
        stdin, stdout, stderr = ssh.exec_command(check_passwd_cmd)
        passwd_output = stdout.read().decode()

        ssh.close()

        if "missing" in passwd_output:
            return JSONResponse(
                content={'success': False, 'error': 'VNC密码文件不存在', 'needs_password': True},
                status_code=404
            )

        return JSONResponse(content={
            'success': True,
            'message': '主机验证成功',
            'needs_password': False,
            'password': password if password else ''
        })

    except paramiko.AuthenticationException:
        return JSONResponse(
            content={'success': False, 'error': 'SSH认证失败', 'needs_password': True},
            status_code=401
        )
    except Exception as e:
        logger.error(f"Error validating host: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/devices/screen")
async def show_device_screens(req: DeviceActionRequest):
    """显示设备屏幕"""
    try:
        if not req.devices:
            raise HTTPException(status_code=400, detail="未选择设备")

        result = vnc_manager.show_device_screens(req.devices)
        if result.get('success'):
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=500, detail=result.get('error', '设备屏幕显示失败'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error showing device screens: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# ==================== ADB转发 ====================

@app.post("/api/adb-forward/start")
async def start_adb_forward(req: ADBForwardStartRequest):
    """启动ADB转发"""
    try:
        result = adb_forward_manager.start_forward(req.device_host, req.device_password)
        if result.get('success'):
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=500, detail=result.get('error', 'ADB转发启动失败'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting ADB forward: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/adb-forward/stop")
async def stop_adb_forward():
    """停止ADB转发"""
    try:
        client_id = 'test_client'
        result = adb_forward_manager.stop_forward(client_id)
        if result.get('success'):
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=500, detail=result.get('error', 'ADB转发停止失败'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping ADB forward: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# ==================== USB/IP ====================

@app.post("/api/usbip/start")
async def start_usbip(req: Optional[USBIPStartRequest] = Body(default=None), request: Request = None):
    """启动 USB/IP 转发（使用usbip_manager.start_usbip高级封装方法 - 与Flask版本一致）"""
    try:
        config = config_manager.load_config()
        client_id = get_client_id_from_request(request)

        # 从请求中获取参数
        request_data = req.model_dump() if req else {}

        # 获取device_host（优先级：请求参数 > 配置文件 > client_id）
        device_host = request_data.get('device_host') or config.get('usbip_device_host') or config.get('device_host')
        if not device_host:
            device_host = client_id

        logger.info(f"[USB/IP] Using device_host: {device_host}")

        # 保存原始 Windows 设备主机地址，用于记录设备来源
        windows_device_host = device_host

        # 获取密码
        device_password = request_data.get('device_password') or find_device_host_password(config, device_host) or config.get('device_pswd', '')

        if not device_password:
            return ApiResponse.error(
                f'未找到 {device_host} 的SSH凭据，请先在登录页面输入SSH密码',
                status_code=401
            )

        # 直接调用高级封装方法（简化实现，与Flask版本一致）
        result = usbip_manager.start_usbip(device_host, device_password)

        # 更新连接状态（使用线程锁 - 与Flask版本一致）
        if result.get('success'):
            with global_state.usbip_states_lock:
                global_state.usbip_states[client_id] = {'connected': True, 'timestamp': time.time()}
            logger.info(f"[USB/IP Start] Set connected=True for client_id={client_id}")

            # 记录设备来源（使用线程锁 - 与Flask版本一致）
            device_list = result.get('device_list', [])
            if device_list:
                with global_state.usbip_devices_source_lock:
                    for device_id in device_list:
                        global_state.usbip_devices_source[device_id] = {
                            'source': windows_device_host,
                            'timestamp': time.time()
                        }
                logger.info(f"[USB/IP Start] Recorded device source: {windows_device_host} for devices: {device_list}")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting USB/IP: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/usbip/stop")
async def stop_usbip(request: Request):
    """停止 USB/IP 转发（与5000端口完全一致）"""
    config = config_manager.load_config()
    client_id = get_client_id_from_request(request)
    device_host = client_id
    config['device_host'] = device_host

    # 自动从 client_ssh_credentials 中查找密码
    device_password = find_device_host_password(config, device_host)
    if not device_password:
        device_password = config.get('device_pswd', '')

    if device_password:
        config['device_pswd'] = device_password

    win_ssh = create_device_ssh_connection(config)
    if not win_ssh:
        # 无法连接到 Windows，只清除连接状态
        # 注意：不清除设备来源记录，因为设备仍然在测试主机上
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared (device source preserved)")
        return JSONResponse(content={'success': True, 'message': '本地设备已断开'})

    try:
        ssh_manager.execute_command(win_ssh, 'usbipd unbind --all', timeout=10)
        win_ssh.close()
        time.sleep(2)

        # 只更新 USB/IP 连接状态，不清除设备来源记录
        # 设备仍然在测试主机上，来源信息应该保留
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared (device source preserved)")

        return JSONResponse(content={
            'success': True,
            'message': '本地设备已断开'
        })
    except Exception as e:
        win_ssh.close()
        # 即使失败也清除连接状态，但保留设备来源记录
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared on error (device source preserved)")
        return JSONResponse(content={'success': True, 'message': '本地设备已断开'})

@app.get("/api/usbip/status")
async def get_usbip_status(request: Request):
    """
    获取 USB/IP 状态（与5000端口完全一致）

    通过检查多个维度来判断 USB/IP 连接状态：
    1. 检查当前客户端的连接状态记录
    2. 检查全局 USB/IP 设备来源记录（支持刷新页面后恢复状态）
    """
    client_id = get_client_id_from_request(request)

    # 方法1：检查当前客户端的连接状态
    with global_state.usbip_states_lock:
        state_info = global_state.usbip_states.get(client_id, {'connected': False, 'timestamp': 0})
        connected = state_info['connected']

    # 方法2：如果当前客户端没有记录，检查是否有全局 USB/IP 设备记录
    # 这样可以支持刷新页面后恢复按钮状态
    if not connected:
        with global_state.usbip_devices_source_lock:
            # 如果有任何 USB/IP 设备记录，说明有 USB/IP 连接
            has_usbip_devices = len(global_state.usbip_devices_source) > 0
            if has_usbip_devices:
                connected = True

    logger.info(f"[USB/IP Status] client_id={client_id}, connected={connected}, device_count={len(global_state.usbip_devices_source)}")
    return JSONResponse(content={'connected': connected})

# ==================== VPN管理 ====================

@app.get("/api/vpn/check-sshd")
async def check_vpn_sshd():
    """检查VPN SSH服务 - 与Flask实现一致"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 执行命令检查sshd进程
            output, error, code = ssh_manager.execute_command(
                ssh,
                "ps aux | grep sshd | grep -v grep"
            )

            ssh_manager.return_connection(ssh)

            # 检查是否有输出
            running = len(output.strip()) > 0

            # 返回扁平结构（与Flask一致）
            return JSONResponse(content={
                'success': True,
                'running': running
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error checking VPN sshd: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/vpn/check-routing")
async def check_vpn_routing():
    """检查VPN路由（通过ping目标）- 与Flask实现一致"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 获取VPN目标列表
            vpn_target = config.get("vpn_target", [])
            if isinstance(vpn_target, str):
                vpn_target = [t.strip() for t in vpn_target.split(',')]

            if not vpn_target:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={
                    'success': True,
                    'message': '未配置VPN目标',
                    'results': []
                })

            results = []
            success_count = 0
            failed_targets = []

            # Ping每个目标
            for target in vpn_target:
                cmd = f"ping -c 1 -W 2 {target} 2>&1"
                output, error, code = ssh_manager.execute_command(ssh, cmd)

                # 检查ping是否成功
                is_reachable = '1 packets transmitted, 1 received' in output or '1 received' in output

                result = {
                    'target': target,
                    'reachable': is_reachable,
                    'output': output[:200]  # 截断输出
                }
                results.append(result)

                if is_reachable:
                    success_count += 1
                else:
                    failed_targets.append(target)

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'results': results,
                'summary': {
                    'total': len(vpn_target),
                    'success': success_count,
                    'failed': len(failed_targets),
                    'success_rate': f"{success_count}/{len(vpn_target)}"
                },
                'failed_targets': failed_targets
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error checking VPN routing: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/api/vpn/connect")
async def connect_vpn(
    req: Optional[VPNConnectRequest] = Body(default=None)
):
    """连接VPN（使用nmcli）

    请求体完全可选，兼容前端无参数调用
    """
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 使用nmcli连接VPN
            vpn_cmd = "sudo nmcli connection up hcq2"
            output, error, code = ssh_manager.execute_command(
                ssh,
                vpn_cmd,
                timeout=20
            )

            import time
            time.sleep(2)

            # 检查连接结果
            if code == 0:
                is_connected = True
                message = 'VPN 连接成功'
            elif 'already active' in (error or ''):
                is_connected = True
                message = 'VPN 已连接'
            elif 'unknown connection' in (error or ''):
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": "VPN 连接 hcq2 不存在，请先在 NetworkManager 中配置"
                    },
                    status_code=404
                )
            else:
                is_connected = False
                message = f'VPN 连接失败: {error or output}'

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": is_connected,
                "connected": is_connected,
                "message": message,
                "output": (output[:500] if output else '')
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error connecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/api/vpn/disconnect")
async def disconnect_vpn():
    """断开VPN（使用nmcli）"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 使用nmcli断开VPN
            disconnect_cmd = "sudo nmcli connection down hcq2"
            output, error, code = ssh_manager.execute_command(
                ssh,
                disconnect_cmd,
                timeout=10
            )

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": True,
                "message": "VPN 已断开"
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error disconnecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/vpn/status")
async def get_vpn_status():
    """获取VPN连接状态"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            vpn_target = config.get('vpn_target', ['www.google.com'])[0]
            if isinstance(vpn_target, list):
                vpn_target = vpn_target[0] if vpn_target else 'www.google.com'

            output, error, code = ssh_manager.execute_command(
                ssh,
                f"ping -c 1 -W 2 {vpn_target} 2>&1",
                timeout=10
            )

            ssh_manager.return_connection(ssh)

            # 检查ping结果
            connected = '1 packets transmitted, 1 received' in output or '1 received' in output

            return JSONResponse(content={
                "success": True,
                "connected": connected
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error getting VPN status: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== 文件上传 ====================

@app.post("/api/upload/file")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form("")
):
    """
    文件上传 - 与Flask版本一致，上传到远程服务器

    接收浏览器上传的文件，保存到临时目录，然后通过SFTP上传到远程测试主机
    """
    import tempfile

    try:
        # 检查文件
        if not file or file.filename == '':
            return JSONResponse(
                content={'success': False, 'error': 'No file selected'},
                status_code=400
            )

        config = config_manager.load_config()

        # 创建临时目录
        upload_dir = os.path.join(tempfile.gettempdir(), 'gms_uploads')
        os.makedirs(upload_dir, exist_ok=True)

        # 保存到临时文件
        temp_path = os.path.join(upload_dir, file.filename)
        with open(temp_path, 'wb') as f:
            content = await file.read()
            f.write(content)

        try:
            # 连接远程服务器
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                os.remove(temp_path)
                return JSONResponse(
                    content={'success': False, 'error': 'SSH connection failed'},
                    status_code=500
                )

            # 上传到远程服务器
            remote_path = f"/home/{config['ubuntu_user']}/{file.filename}"
            sftp = ssh.open_sftp()
            sftp.put(temp_path, remote_path)
            sftp.close()
            ssh_manager.return_connection(ssh)

            # 清理临时文件
            os.remove(temp_path)

            return JSONResponse(content={
                'success': True,
                'remote_path': remote_path,
                'message': f'文件已上传到 {remote_path}'
            })
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)
            if 'ssh' in locals():
                ssh_manager.return_connection(ssh)
            raise e

    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

@app.post("/api/upload")
async def upload_files(files: List[UploadFile] = File(...), file_path: str = Form(None)):
    """
    文件上传 - 支持两种模式
    1. 多文件上传：接收文件对象列表
    2. 本地路径上传：通过file_path参数指定本地文件路径
    """
    try:
        config = config_manager.load_config()

        # 模式1：从本地路径上传（与Flask版本一致）
        if file_path:
            if not file_path or not os.path.exists(file_path):
                return JSONResponse(
                    content={'success': False, 'error': 'No file path provided or file not found'},
                    status_code=400
                )

            # 连接远程服务器
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH connection failed'},
                    status_code=500
                )

            try:
                filename = os.path.basename(file_path)
                remote_path = f"/home/{config['ubuntu_user']}/{filename}"

                # 使用SFTP上传
                sftp = ssh.open_sftp()
                sftp.put(file_path, remote_path)
                sftp.close()
                ssh_manager.return_connection(ssh)

                return JSONResponse(content={
                    'success': True,
                    'remote_path': remote_path,
                    'message': f'文件已上传到 {remote_path}'
                })
            except Exception as e:
                if 'ssh' in locals():
                    ssh_manager.return_connection(ssh)
                raise e

        # 模式2：多文件上传（保存到本地）
        upload_dir = '/tmp/uploads'
        os.makedirs(upload_dir, exist_ok=True)

        uploaded_files = []
        for file in files:
            file_path = os.path.join(upload_dir, file.filename)
            with open(file_path, 'wb') as f:
                content = await file.read()
                f.write(content)

            uploaded_files.append({
                'filename': file.filename,
                'path': file_path,
                'size': len(content)
            })

        return JSONResponse(content={
            "success": True,
            "files": uploaded_files,
            "count": len(uploaded_files)
        })
    except Exception as e:
        logger.error(f"Error uploading files: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

@app.post("/api/upload/progress")
async def get_upload_progress(req: dict):
    """获取上传进度"""
    try:
        upload_id = req.get('upload_id')

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

# ==================== 固件管理 ====================

@app.post("/api/firmware/burn")
async def burn_firmware(request: Request):
    """
    固件烧写 - 支持文件上传

    使用 upgrade_tool 烧写固件到选定的设备
    """
    try:
        # 获取客户端ID
        client_id = get_client_id_from_request(request)

        # 从URL参数获取设备列表（优先，这样可以在文件上传前先锁定）
        devices_param = request.query_params.get('devices')
        if devices_param:
            devices = devices_param.split(',')
            logger.info(f"[Firmware Burn] 设备列表从URL参数获取: {devices}")
        else:
            # 兼容旧版本：从FormData获取
            form = await request.form()
            devices_str = form.get('devices')
            devices = devices_str.split(',') if devices_str else []
            logger.info(f"[Firmware Burn] 设备列表从FormData获取: {devices}")

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'}
            )

        # 获取用户名并立即锁定设备（在等待FormData之前）
        config = config_manager.load_config()
        username = config.get('client_username', 'unknown')

        # 锁定设备
        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({'device_id': device_id, 'error': message})

        logger.info(f"[Device Lock] 锁定完成: 成功 {len(locked_devices)} 台, 失败 {len(failed_devices)} 台")
        logger.info(f"[Device Lock] 锁定的设备: {locked_devices}")

        # 如果有设备锁定失败，释放已锁定的设备并返回错误
        if failed_devices:
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)

            error_msg = "以下设备已被其他用户占用：\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"

            return JSONResponse(
                content={
                    'success': False,
                    'error': error_msg.strip(),
                    'failed_devices': failed_devices
                },
                status_code=409
            )

        # 广播设备锁定状态（立即显示）
        logger.info(f"[Device Lock] 开始广播锁定状态到 {len(global_state.websocket_connections)} 个客户端")
        await broadcast_device_lock_update(locked_devices)
        logger.info(f"[Device Lock] 锁定状态广播完成")

        # 现在才开始等待FormData（此时设备已经锁定并显示）
        form = await request.form()
        firmware_file = form.get('firmware_file')
        firmware_path = form.get('firmware_path', '').strip()

        # 检查固件来源
        if not firmware_file and not firmware_path:
            # 释放设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)
            await broadcast_device_lock_update(locked_devices)

            return JSONResponse(
                content={'success': False, 'error': 'Please upload a firmware file or provide a firmware path'}
            )

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            # 释放设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            # 1. 上传 upgrade_tool 到测试主机
            logger.info("[Firmware Burn] Uploading upgrade_tool...")
            local_tool = os.path.join(os.path.dirname(__file__), "tools", "upgrade_tool")
            remote_tool = f"/home/{config['ubuntu_user']}/GMS-Suite/upgrade_tool"

            if not os.path.exists(local_tool):
                logger.error(f"[Firmware Burn] upgrade_tool not found: {local_tool}")
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': f'upgrade_tool not found: {local_tool}'}
                )

            # 使用 SCP 上传 upgrade_tool
            import scp
            scp_client = scp.SCPClient(ssh.get_transport())
            scp_client.put(local_tool, remote_tool)
            scp_client.close()
            logger.info("[Firmware Burn] upgrade_tool uploaded successfully")

            # 2. 处理固件文件
            import tempfile

            if firmware_file:
                # 用户上传了文件 - 直接在内存中处理，不写磁盘
                logger.info(f"[Firmware Burn] Processing uploaded file: {firmware_file.filename}")

                # 使用BytesIO在内存中处理文件，避免写磁盘
                import io
                firmware_content = await firmware_file.read()
                firmware_size = len(firmware_content)
                firmware_bytes = io.BytesIO(firmware_content)

                logger.info(f"[Firmware Burn] File loaded into memory: {firmware_size} bytes")

                # 直接上传到测试主机
                firmware_name = firmware_file.filename
                remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_name}"

                logger.info(f"[Firmware Burn] Directly uploading to test host: {remote_firmware}")

                # 用于存储上传进度的全局变量（供回调访问）
                upload_progress_data = {'current_percentage': 0}

                # 自定义SCP进度回调
                def upload_progress(filename, size, sent):
                    percentage = (sent / size) * 100 if size > 0 else 0
                    upload_progress_data['current_percentage'] = percentage
                    logger.info(f"[Firmware Burn] Upload progress: {percentage:.2f}%")

                # 使用线程执行SCP上传
                import threading
                upload_complete = threading.Event()
                upload_error = [None]

                def upload_file_thread():
                    try:
                        # 使用SCP的putfo方法直接从内存上传
                        scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
                        scp_client.putfo(firmware_bytes, remote_firmware)
                        scp_client.close()
                        logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                    except Exception as e:
                        logger.error(f"[Firmware Burn] Upload error: {e}")
                        upload_error[0] = str(e)
                    finally:
                        upload_complete.set()

                # 发送上传开始消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'file_upload_progress',
                            'filename': firmware_name,
                            'percentage': 0,
                            'total_size': firmware_size,
                            'uploaded_size': 0
                        })
                    except:
                        pass

                # 启动上传线程
                upload_thread = threading.Thread(target=upload_file_thread)
                upload_thread.start()

                # 定期更新进度到前端
                last_percentage = 0
                while not upload_complete.is_set():
                    await asyncio.sleep(0.5)
                    current_percentage = upload_progress_data.get('current_percentage', 0)

                    # 只有当百分比变化时才发送更新
                    if abs(current_percentage - last_percentage) > 0.1:
                        if client_id in global_state.websocket_connections:
                            try:
                                sent_size = int((current_percentage / 100) * firmware_size)
                                await global_state.websocket_connections[client_id].send_json({
                                    'type': 'file_upload_progress',
                                    'filename': firmware_name,
                                    'percentage': round(current_percentage, 2),
                                    'total_size': firmware_size,
                                    'uploaded_size': sent_size
                                })
                            except:
                                pass
                        last_percentage = current_percentage

                # 等待线程完成
                upload_thread.join(timeout=300)  # 5分钟超时

                # 检查上传是否成功
                if upload_error[0]:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(
                        content={'success': False, 'error': f'Upload failed: {upload_error[0]}'}
                    )

                # 发送上传完成消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'file_upload_progress',
                            'filename': firmware_name,
                            'percentage': 100,
                            'total_size': firmware_size,
                            'uploaded_size': firmware_size
                        })
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': '✅ 固件文件上传完成',
                            'log_type': 'success'
                        })
                    except:
                        pass

                logger.info(f"[Firmware Burn] Firmware uploaded successfully, skipping local file check")

            # 如果没有上传文件，处理其他情况（远程文件或本地文件）
            else:
                # 现在处理固件文件（远程路径或本地文件）
                logger.info(f"[Firmware Burn] Processing firmware: {firmware_path}")
                firmware_name = os.path.basename(firmware_path)
                remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_name}"

                # 判断是本地文件还是远程文件
                if os.path.exists(firmware_path):
                    # 本地文件，需要上传
                    file_size = os.path.getsize(firmware_path)
                    logger.info(f"[Firmware Burn] Uploading local file: {firmware_path} ({file_size} bytes)")

                    # 用于存储上传进度的全局变量（供回调访问）
                    upload_progress_data = {'current_percentage': 0}

                    # 自定义SCP进度回调
                    def upload_progress(filename, size, sent):
                        percentage = (sent / size) * 100 if size > 0 else 0
                        upload_progress_data['current_percentage'] = percentage
                        logger.info(f"[Firmware Burn] Upload progress: {percentage:.2f}%")

                    # 使用线程执行SCP上传
                    import threading
                    upload_complete = threading.Event()
                    upload_error = [None]

                    def upload_file_thread():
                        try:
                            scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
                            scp_client.put(firmware_path, remote_firmware)
                            scp_client.close()
                            logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                        except Exception as e:
                            logger.error(f"[Firmware Burn] Upload error: {e}")
                            upload_error[0] = str(e)
                        finally:
                            upload_complete.set()

                    # 发送上传开始消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'file_upload_progress',
                                'filename': firmware_name,
                                'percentage': 0,
                                'total_size': file_size,
                                'uploaded_size': 0
                            })
                        except:
                            pass

                    # 启动上传线程
                    upload_thread = threading.Thread(target=upload_file_thread)
                    upload_thread.start()

                    # 定期更新进度到前端
                    last_percentage = 0
                    while not upload_complete.is_set():
                        await asyncio.sleep(0.5)
                        current_percentage = upload_progress_data.get('current_percentage', 0)

                        # 只有当百分比变化时才发送更新
                        if abs(current_percentage - last_percentage) > 0.1:
                            if client_id in global_state.websocket_connections:
                                try:
                                    sent_size = int((current_percentage / 100) * file_size)
                                    await global_state.websocket_connections[client_id].send_json({
                                        'type': 'file_upload_progress',
                                        'filename': firmware_name,
                                        'percentage': round(current_percentage, 2),
                                        'total_size': file_size,
                                        'uploaded_size': sent_size
                                    })
                                except:
                                    pass
                            last_percentage = current_percentage

                    # 等待线程完成
                    upload_thread.join(timeout=300)

                    # 检查上传是否成功
                    if upload_error[0]:
                        ssh_manager.return_connection(ssh)
                        return JSONResponse(
                            content={'success': False, 'error': f'Upload failed: {upload_error[0]}'}
                        )

                    # 发送上传完成消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'file_upload_progress',
                                'filename': firmware_name,
                                'percentage': 100,
                                'total_size': file_size,
                                'uploaded_size': file_size
                            })
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': '✅ 固件文件上传完成',
                                'log_type': 'success'
                            })
                        except:
                            pass

                    logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                elif firmware_path.startswith('/') or firmware_path.startswith('./'):
                    # 远程文件路径
                    logger.info(f"[Firmware Burn] Using remote file: {firmware_path}")
                    remote_firmware = firmware_path
                else:
                    # 可能只是文件名，尝试在 GMS-Suite 目录中查找
                    logger.info(f"[Firmware Burn] Searching for file in GMS-Suite: {firmware_path}")
                    check_cmd = f"ls /home/{config['ubuntu_user']}/GMS-Suite/{firmware_path} 2>/dev/null && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

                    if 'found' in output:
                        remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_path}"
                        logger.info(f"[Firmware Burn] File found: {remote_firmware}")
                    else:
                        ssh_manager.return_connection(ssh)
                        return JSONResponse(
                            content={'success': False, 'error': f'Firmware file not found: {firmware_path}. Please use a full path or upload the file first.'}
                        )

            # 3. 让设备进入 Loader 模式
            logger.info("[Firmware Burn] Entering Loader mode...")
            for device in devices:
                cmd = f"adb -s {device} reboot loader"
                ssh_manager.execute_command(ssh, cmd, timeout=5)
                logger.info(f"[Firmware Burn] Device {device} sent to Loader mode")

            logger.info("[Firmware Burn] Waiting for devices to enter Loader mode...")
            await asyncio.sleep(8)

            # 4. 检查 Loader 设备
            gms_suite_dir = f"/home/{config['ubuntu_user']}/GMS-Suite"
            check_cmd = f"cd {gms_suite_dir} && ./upgrade_tool ld"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

            if "List of rockusb connected" not in output:
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': 'No Loader devices detected'}
                )

            logger.info(f"[Firmware Burn] Loader devices detected:\n{output}")

            # 5. 烧写固件（upgrade_tool 会自动处理所有设备）
            logger.info("[Firmware Burn] Starting firmware burning...")
            burn_cmd = f"cd {gms_suite_dir} && ./upgrade_tool uf {shlex.quote(firmware_name)}"

            # 发送开始消息
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': '🔥 开始烧写固件...',
                        'log_type': 'info'
                    })
                except:
                    pass

            # 执行烧写并获取实时输出
            stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=300)

            # ANSI转义码过滤函数
            def strip_ansi_codes(text):
                """移除ANSI转义码"""
                import re
                # 移除ANSI转义序列
                ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
                return ansi_escape.sub('', text)

            # 实时读取输出并发送到前端
            import select
            output_buffer = []

            # 进度条状态
            firmware_burn_start = False
            current_progress = 0
            last_progress_time = 0

            while not stdout.channel.exit_status_ready():
                current_time = asyncio.get_event_loop().time()

                # 如果有数据可读，读取并处理
                if stdout.channel.recv_ready():
                    chunk = stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                    output_buffer.append(chunk)

                    # 清理ANSI转义码
                    clean_chunk = strip_ansi_codes(chunk)

                    # 检测烧写状态开始
                    if 'Download Firmware Start' in clean_chunk and not firmware_burn_start:
                        firmware_burn_start = True
                        current_progress = 0
                        last_progress_time = current_time
                        logger.info("[Firmware Burn] 检测到固件烧写开始，启动进度条")

                    # 发送所有非空输出到前端
                    if client_id in global_state.websocket_connections:
                        try:
                            for line in clean_chunk.split('\n'):
                                line = line.strip()
                                if line:
                                    # 固件烧写期间不显示日志（保持日志区域干净）
                                    # 除非是错误信息
                                    if firmware_burn_start:
                                        # 只显示错误信息
                                        if any(keyword in line.lower() for keyword in ['error', 'failed', 'fail', '错误', '失败']):
                                            await global_state.websocket_connections[client_id].send_json({
                                                'type': 'log_update',
                                                'log': line,
                                                'log_type': 'error'
                                            })
                                        continue

                                    # 其他正常日志
                                    await global_state.websocket_connections[client_id].send_json({
                                        'type': 'log_update',
                                        'log': line,
                                        'log_type': 'info'
                                    })
                        except Exception as e:
                            logger.error(f"[Firmware Burn] 发送日志失败: {e}")

                # 如果固件烧写开始，每0.5秒更新一次进度
                if firmware_burn_start and (current_time - last_progress_time > 0.5):
                    # 进度条从0%到95%，每0.5秒增加5%
                    current_progress = min(current_progress + 5, 95)
                    last_progress_time = current_time

                    # 发送进度更新到前端（只更新进度条，不显示在日志）
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'firmware_progress',
                                'percentage': current_progress
                            })
                        except Exception as e:
                            logger.error(f"[Firmware Burn] 发送进度失败: {e}")

                # 短暂休眠避免CPU占用过高
                await asyncio.sleep(0.1)

            # 获取最终输出
            final_output = ''.join(output_buffer)
            exit_status = stdout.channel.recv_exit_status()

            if exit_status == 0:
                logger.info(f"[Firmware Burn] Success:\n{final_output}")
                ssh_manager.return_connection(ssh)

                # 发送100%完成进度
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'firmware_progress',
                            'percentage': 100
                        })
                    except:
                        pass

                # 发送完成消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': '✅ 固件烧写完成！',
                            'log_type': 'success'
                        })
                    except:
                        pass

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                for device_id in locked_devices:
                    device_lock_manager.unlock_device(device_id, client_id)
                logger.info(f"[Device Lock] 设备解锁完成")

                # 广播设备解锁状态（立即移除）
                logger.info(f"[Device Lock] 开始广播解锁状态")
                await broadcast_device_lock_update(locked_devices)
                logger.info(f"[Device Lock] 解锁状态广播完成")

                return JSONResponse(
                    content={'success': True, 'message': 'Firmware burn completed successfully'}
                )
            else:
                logger.error(f"[Firmware Burn] Failed with exit code {exit_status}")

                # 从输出缓冲区获取错误信息
                final_output = ''.join(output_buffer)
                error_output = final_output or stderr.read().decode('utf-8', errors='ignore')
                ssh_manager.return_connection(ssh)

                # 发送失败消息（显示详细错误）
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': f'❌ 固件烧写失败 (exit code: {exit_status})',
                            'log_type': 'error'
                        })
                        # 如果有详细错误信息，也发送
                        if error_output and len(error_output) < 500:  # 限制长度
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'错误详情: {error_output[:200]}',
                                'log_type': 'error'
                            })
                    except:
                        pass

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                for device_id in locked_devices:
                    device_lock_manager.unlock_device(device_id, client_id)
                logger.info(f"[Device Lock] 设备解锁完成")

                # 广播设备解锁状态（立即移除）
                logger.info(f"[Device Lock] 开始广播解锁状态")
                await broadcast_device_lock_update(locked_devices)
                logger.info(f"[Device Lock] 解锁状态广播完成")

                return JSONResponse(
                    content={'success': False, 'error': error_output or 'Firmware burn failed'}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[Firmware Burn] Error: {e}")

            # 释放设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)

            # 广播设备更新（移除锁定状态）
            await broadcast_device_lock_update(locked_devices)

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        logger.error(f"Error in burn_firmware: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/gsi/burn")
async def burn_gsi(request: Request):
    """
    GSI 烧写 - 按照GUI版本实现

    使用 run_GSI_Burn.sh 脚本烧写GSI镜像到选定的设备
    """
    try:
        # 获取客户端ID
        client_id = get_client_id_from_request(request)

        # 解析请求体
        req_data = await request.json()
        devices = req_data.get('devices', [])
        script_path = req_data.get('script_path', '').strip()
        system_img = req_data.get('system_img', '').strip()
        vendor_img = req_data.get('vendor_img', '').strip()

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'}
            )

        # 检查脚本路径
        if not script_path:
            return JSONResponse(
                content={'success': False, 'error': 'Script path is required'}
            )

        # 检查system镜像
        if not system_img:
            return JSONResponse(
                content={'success': False, 'error': 'System image path is required'}
            )

        # 获取用户名
        config = config_manager.load_config()
        username = config.get('client_username', 'unknown')

        # 锁定设备
        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({'device_id': device_id, 'error': message})

        logger.info(f"[Device Lock] 锁定完成: 成功 {len(locked_devices)} 台, 失败 {len(failed_devices)} 台")
        logger.info(f"[Device Lock] 锁定的设备: {locked_devices}")

        # 如果有设备锁定失败，释放已锁定的设备并返回错误
        if failed_devices:
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)

            error_msg = "以下设备已被其他用户占用：\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"

            return JSONResponse(
                content={
                    'success': False,
                    'error': error_msg.strip(),
                    'failed_devices': failed_devices
                },
                status_code=409
            )

        # 广播设备锁定状态（立即显示）
        logger.info(f"[Device Lock] 开始广播锁定状态到 {len(global_state.websocket_connections)} 个客户端")
        await broadcast_device_lock_update(locked_devices)
        logger.info(f"[Device Lock] 锁定状态广播完成")

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            # 释放设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            import scp

            # 1. 上传必要文件到测试主机
            logger.info("[GSI Burn] Uploading necessary files...")

            # 上传脚本（从tools目录）
            local_script = os.path.join(os.path.dirname(__file__), "tools", "run_GSI_Burn.sh")
            remote_script = f"/home/{config['ubuntu_user']}/GMS-Suite/run_GSI_Burn.sh"

            if os.path.exists(local_script):
                logger.info(f"[GSI Burn] Uploading script from: {local_script}")
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_script, remote_script)
                scp_client.close()
                # 设置可执行权限
                ssh_manager.execute_command(ssh, f"chmod +x {remote_script}")
                logger.info(f"[GSI Burn] Script uploaded to: {remote_script}")
            else:
                logger.error(f"[GSI Burn] Script not found: {local_script}")
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': f'GSI burn script not found: {local_script}'}
                )

            # 上传 misc.img（从tools目录）
            local_misc = os.path.join(os.path.dirname(__file__), "tools", "misc.img")
            remote_misc = f"/home/{config['ubuntu_user']}/GMS-Suite/misc.img"

            if os.path.exists(local_misc):
                logger.info(f"[GSI Burn] Uploading misc.img from: {local_misc}")
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_misc, remote_misc)
                scp_client.close()
                logger.info(f"[GSI Burn] misc.img uploaded to: {remote_misc}")
            else:
                logger.warning(f"[GSI Burn] misc.img not found: {local_misc}, skipping...")

            # 2. 处理 vendor 镜像（如果提供）
            remote_vendor = ""
            if vendor_img:
                if os.path.exists(vendor_img):
                    # 本地文件，需要上传
                    vendor_name = os.path.basename(vendor_img)
                    remote_vendor = f"/home/{config['ubuntu_user']}/GMS-Suite/{vendor_name}"
                    scp_client = scp.SCPClient(ssh.get_transport())
                    scp_client.put(vendor_img, remote_vendor)
                    scp_client.close()
                else:
                    # 远程文件，直接使用路径
                    remote_vendor = vendor_img

            # 3. 对每个设备执行烧写
            logger.info("[GSI Burn] Starting GSI burning...")
            results = []

            # 发送开始消息
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': f'🔥 开始烧写GSI镜像到 {len(devices)} 台设备...',
                        'log_type': 'info'
                    })
                except:
                    pass

            for device in devices:
                # 构建烧写命令
                img_args = f"--system {system_img}"
                if remote_vendor:
                    img_args += f" --vendor {remote_vendor}"

                burn_cmd = f"{remote_script} {device} {img_args}"

                logger.info(f"[GSI Burn] Executing for {device}: {burn_cmd}")

                # 发送设备开始消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': f'📱 正在烧写设备: {device}',
                            'log_type': 'info'
                        })
                    except:
                        pass

                # 执行命令并实时读取输出
                stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=600)

                # ANSI转义码过滤函数
                def strip_ansi_codes(text):
                    """移除ANSI转义码"""
                    import re
                    ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
                    return ansi_escape.sub('', text)

                # 实时读取输出并发送到前端
                output_buffer = []

                while not stdout.channel.exit_status_ready():
                    if stdout.channel.recv_ready():
                        chunk = stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                        output_buffer.append(chunk)

                        # 清理ANSI转义码
                        clean_chunk = strip_ansi_codes(chunk)

                        # 发送进度更新到前端（逐行显示）
                        if client_id in global_state.websocket_connections:
                            try:
                                for line in clean_chunk.split('\n'):
                                    line = line.strip()
                                    if line and len(line) > 0:
                                        await global_state.websocket_connections[client_id].send_json({
                                            'type': 'log_update',
                                            'log': line,
                                            'log_type': 'info'
                                        })
                            except:
                                pass
                    else:
                        await asyncio.sleep(0.5)

                # 获取最终输出
                final_output = ''.join(output_buffer)
                exit_status = stdout.channel.recv_exit_status()

                # 读取stderr
                error_output = ''
                if stderr.channel.recv_ready():
                    error_output = stderr.read().decode('utf-8', errors='ignore')

                logger.info(f"[GSI Burn] Device {device} - Exit code: {exit_status}")
                logger.info(f"[GSI Burn] Device {device} - Output: {final_output[:500] if final_output else 'Empty'}")
                if error_output:
                    logger.error(f"[GSI Burn] Device {device} - Error: {error_output[:500]}")

                if exit_status == 0:
                    logger.info(f"[GSI Burn] Success for {device}")
                    results.append({
                        'device': device,
                        'success': True,
                        'output': final_output
                    })

                    # 发送成功消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'✅ 设备 {device} GSI烧写完成',
                                'log_type': 'success'
                            })
                        except:
                            pass
                else:
                    logger.error(f"[GSI Burn] Failed for {device}: {error_output}")
                    results.append({
                        'device': device,
                        'success': False,
                        'error': error_output,
                        'output': final_output
                    })

                    # 发送失败消息（显示详细错误）
                    if client_id in global_state.websocket_connections:
                        try:
                            error_msg = error_output or "未知错误"
                            # 如果输出中有错误信息，显示最后几行
                            if final_output:
                                lines = final_output.strip().split('\n')
                                if len(lines) > 0:
                                    last_lines = lines[-3:]  # 取最后3行
                                    error_detail = ' '.join(last_lines)
                                    if error_detail and len(error_detail) < 200:
                                        error_msg = error_detail

                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'❌ 设备 {device} GSI烧写失败: {error_msg}',
                                'log_type': 'error'
                            })
                        except:
                            pass

            ssh_manager.return_connection(ssh)

            # 释放所有设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)

            # 广播设备更新（移除锁定状态）
            await broadcast_device_lock_update(locked_devices)

            # 检查是否全部成功
            all_success = all(r['success'] for r in results)
            if all_success:
                return JSONResponse(
                    content={'success': True, 'message': 'GSI burn completed successfully', 'results': results}
                )
            else:
                return JSONResponse(
                    content={'success': False, 'error': 'Some devices failed', 'results': results}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[GSI Burn] Error: {e}")

            # 释放所有设备锁
            for device_id in locked_devices:
                device_lock_manager.unlock_device(device_id, client_id)

            # 广播设备更新（移除锁定状态）
            await broadcast_device_lock_update(locked_devices)

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/sn/burn")
async def burn_sn(req: SNBurnRequest):
    """
    SN烧录 - 与Flask版本一致

    烧写序列号到选定的设备
    注意：SN烧写通常需要在loader模式下使用upgrade_tool
    当前实现是占位符，需要特定工具支持
    """
    try:
        devices = req.devices
        sn_code = req.sn_code

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'},
                status_code=400
            )

        # 检查SN码
        if not sn_code:
            return JSONResponse(
                content={'success': False, 'error': 'SN code is required'},
                status_code=400
            )

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'},
                status_code=500
            )

        try:
            results = []

            for device_id in devices:
                # SN烧写通常需要在loader模式下使用upgrade_tool
                # 当前实现是占位符
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': 'SN burning requires device in loader mode. This feature needs to be implemented with specific tool support.'
                })

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={'success': True, 'results': results})
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burning SN: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

# ==================== 其他功能 ====================

@app.post("/api/files/list")
async def list_files(req: dict):
    """文件列表 - 通过SSH连接到远程主机"""
    try:
        path = req.get('path', '')
        config = config_manager.load_config()

        if not path:
            # Default to user home directory
            path = f"/home/{config.get('ubuntu_user', 'hcq')}"

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            # Check if path exists
            check_cmd = f"test -e '{path}' && echo 'exists' || echo 'not_found'"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd)

            if 'not_found' in output:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': f'Path not found: {path}'}, status_code=404)

            # List files with details (name, type, size, modified time)
            # Using ls -la to get detailed information
            list_cmd = f"ls -la '{path}' 2>/dev/null || echo 'ERROR'"
            output, error, code = ssh_manager.execute_command(ssh, list_cmd)

            if 'ERROR' in output or code != 0:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': 'Failed to list directory'}, status_code=500)

            files = []
            for line in output.split('\n'):
                if line.startswith('total') or not line.strip():
                    continue

                # Parse ls -la output
                parts = line.split()
                if len(parts) >= 9:
                    permissions = parts[0]
                    name = ' '.join(parts[8:])
                    is_dir = permissions.startswith('d')
                    size = parts[4] if not is_dir else '0'

                    # Skip . and ..
                    if name in ['.', '..']:
                        continue

                    files.append({
                        'name': name,
                        'type': 'directory' if is_dir else 'file',
                        'size': int(size),
                        'permissions': permissions
                    })

            # Sort: directories first, then files, alphabetically
            files.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'path': path,
                'files': files
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

# ==================== USB/IP辅助函数（与5000端口一致）====================

def is_windows_host(ssh):
    """检查SSH主机是否为Windows"""
    try:
        stdin, stdout, stderr = ssh.exec_command('ver 2>&1', timeout=3)
        output = stdout.read().decode('utf-8', errors='ignore').lower()
        return 'microsoft' in output or 'windows' in output
    except:
        return False


def find_device_host_password(config, device_host):
    """从 client_ssh_credentials 中查找对应 device_host 的密码"""
    if '@' not in device_host:
        return None

    username, hostname = device_host.split('@', 1)

    # 从 client_ssh_credentials 中查找匹配的凭据
    for cred in config.get('client_ssh_credentials', []):
        if cred.get('username') == username:
            logger.info(f"[USB/IP] Found SSH credential for username={username}")
            return cred.get('password')

    logger.info(f"[USB/IP] No SSH credential found for {device_host}")
    return None


def create_device_ssh_connection(config):
    """创建设备主机的SSH连接（Windows）"""
    device_host = config.get('device_host', '')
    if not device_host:
        return None

    if '@' not in device_host:
        logger.error("[USB/IP] Device host format should be user@host")
        return None

    username, hostname = device_host.split('@', 1)
    password = config.get('device_pswd', '')

    if not password:
        logger.error("[USB/IP] No SSH password configured for device host")
        return None

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=hostname, username=username, password=password, timeout=10)
        return ssh
    except Exception as e:
        logger.error(f"[USB/IP] Failed to connect to device host: {e}")
        return None

@app.post("/api/screen/start")
async def start_screen_recording(req: Optional[dict] = Body(default=None)):
    """启动屏幕镜像（请求体可选，兼容前端无参数调用）"""
    try:
        # 如果没有提供参数或为空字典，使用默认行为
        if req is None:
            req = {}

        devices = req.get('devices', [])

        config = config_manager.load_config()
        ubuntu_user = config.get('ubuntu_user', 'hcq')
        ubuntu_host = config.get('ubuntu_host', '')

        if not devices:
            # 尝试从已连接设备列表获取
            ssh = ssh_manager.get_connection(config)
            if ssh:
                try:
                    stdout, stderr, code = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
                    ssh_manager.return_connection(ssh)
                    if code == 0 and stdout:
                        # 解析设备列表
                        lines = stdout.strip().split('\n')[1:]  # 跳过第一行 "List of devices attached"
                        devices = [line.split()[0] for line in lines if line.strip() and '\tdevice' in line]
                except:
                    pass

        if not devices:
            return JSONResponse(content={'success': False, 'error': 'No devices selected'}, status_code=400)

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            client_id = client_manager.get_client_id('127.0.0.1')

            # Check VNC service status
            vnc_check_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
            vnc_output, _, _ = ssh_manager.execute_command(ssh, vnc_check_cmd, timeout=5)
            vnc_available = vnc_output.strip() == '200'

            # Check scrcpy availability (matching Flask version logic)
            scrcpy_path = config.get("scrcpy_path", "")
            if scrcpy_path:
                # Substitute ubuntu_user in path
                scrcpy_path = scrcpy_path.replace('${ubuntu_user}', ubuntu_user)
                scrcpy_check_cmd = f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(ssh, scrcpy_check_cmd)

                if "not_found" in scrcpy_output:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': False,
                        'error': f'scrcpy未找到: {scrcpy_path}',
                        'instructions': '请检查配置文件中的 scrcpy_path 路径'
                    }, status_code=404)
            else:
                # Fallback to checking PATH
                scrcpy_check_cmd = "which scrcpy"
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(ssh, scrcpy_check_cmd)

                if scrcpy_code != 0:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': False,
                        'error': 'scrcpy未安装',
                        'instructions': 'sudo apt-get install -y scrcpy'
                    }, status_code=404)
                scrcpy_path = "scrcpy"  # Use command from PATH

            # 启动scrcpy - 参考Flask版本实现
            results = []
            vnc_sessions = []

            # 使用5000端口的智能位置计算函数
            positions = calculate_window_positions(devices)

            for idx, device_id in enumerate(sorted(devices)):
                # 使用智能计算的窗口位置（与5000端口一致）
                x_offset = positions['start_x'] + idx * (positions['window_width'] + positions['horizontal_gap'])
                y_offset = positions['start_y']
                window_width = positions['window_width']
                window_height = positions['window_height']

                # 使用nohup确保scrcpy在SSH连接关闭后继续运行
                cmd = (
                    f"export DISPLAY=:0 && "
                    f"if [ -f /run/user/1000/gdm/Xauthority ]; then "
                    f"export XAUTHORITY=/run/user/1000/gdm/Xauthority; "
                    f"else "
                    f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority; "
                    f"fi && "
                    f"(nohup {scrcpy_path} -s {device_id} "
                    f"--max-size 800 "
                    f"--stay-awake "
                    f"--window-title '{device_id}' "
                    f"--window-x {x_offset} "
                    f"--window-y {y_offset} "
                    f"--window-width {window_width} "
                    f"--window-height {window_height} "
                    f"> /tmp/scrcpy_{device_id}.log 2>&1 &)"
                )

                ssh_manager.execute_command(ssh, cmd, timeout=10)

                # 验证scrcpy是否成功启动
                time.sleep(0.3)
                check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
                check_output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)
                is_started = 'RUNNING' in check_output

                results.append({
                    'device': device_id,
                    'started': is_started,
                    'position': {'x': x_offset, 'y': y_offset, 'width': window_width, 'height': window_height}
                })

                vnc_sessions.append({
                    'device': device_id,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true" if vnc_available else None,
                    'message': 'VNC查看可用' if vnc_available else '仅本地显示'
                })

            ssh_manager.return_connection(ssh)

            # 构建详细消息
            newly_started = [r['device'] for r in results if r.get('started')]
            failed_devices = [r['device'] for r in results if not r.get('started')]

            message_parts = []
            if newly_started:
                message_parts.append(f"✅ 已启动{len(newly_started)}个投屏设备: {', '.join(newly_started)}")
            if failed_devices:
                message_parts.append(f"❌ {len(failed_devices)}个设备启动失败: {', '.join(failed_devices)}")

            message = '\n'.join(message_parts) if message_parts else '投屏启动完成'

            return JSONResponse(content={
                'success': len(failed_devices) == 0,
                'message': message,
                'results': results,
                'vnc_sessions': vnc_sessions,
                'desktop_url': '/desktop',
                'note': '点击"主机桌面"查看屏幕' if vnc_available else 'VNC未启动，屏幕仅在本地显示'
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error starting screen recording: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== WebSocket ====================

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()
    global_state.websocket_connections[client_id] = websocket
    logger.info(f"WebSocket client connected: {client_id}")

    try:
        while True:
            # 接收消息
            data = await websocket.receive_json()
            message_type = data.get('type')

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

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
    finally:
        # 清理WebSocket连接
        if client_id in global_state.websocket_connections:
            del global_state.websocket_connections[client_id]

        # 清理终端SSH会话（如果存在）
        with global_state.terminal_lock:
            if client_id in global_state.terminal_ssh_sessions:
                session_info = global_state.terminal_ssh_sessions[client_id]
                try:
                    # 只有SSH模式才关闭SSH连接,ADB模式使用的是共享连接
                    if session_info.get('mode') != 'adb':
                        session_info['ssh'].close()
                        logger.info(f"[TERMINAL] Closed SSH connection for {client_id}")
                    else:
                        # ADB模式:只关闭channel,不关闭SSH连接
                        try:
                            session_info['channel'].close()
                            logger.info(f"[TERMINAL] Closed ADB channel for {client_id}")
                        except:
                            pass
                        # 归还SSH连接到连接池
                        ssh_manager.return_connection(session_info['ssh'])
                except Exception as e:
                    logger.error(f"[TERMINAL] Error closing session for {client_id}: {e}")
                del global_state.terminal_ssh_sessions[client_id]

async def refresh_devices_websocket(client_id: str, websocket: WebSocket):
    """WebSocket刷新设备列表"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)

        if ssh:
            try:
                stdout, stderr, code = ssh_manager.execute_command(
                    ssh, "adb devices", timeout=5
                )
                if code == 0:
                    lines = stdout.strip().split('\n')[1:]
                    devices_info = []
                    for line in lines:
                        if line.strip():
                            parts = line.split('\t')
                            if len(parts) >= 2:
                                device_id = parts[0]
                                status = parts[1]
                                device_data = {
                                    'id': device_id,
                                    'status': status
                                }

                                # 添加锁定状态
                                lock_status = device_lock_manager.get_lock_status(device_id)
                                if lock_status:
                                    device_data['locked'] = True
                                    device_data['locked_by'] = lock_status['locked_by']

                                devices_info.append(device_data)

                    await websocket.send_json({
                        'type': 'devices_updated',
                        'devices': devices_info
                    })
            except Exception as e:
                logger.error(f"Error refreshing devices: {e}")
            finally:
                ssh_manager.return_connection(ssh)
    except Exception as e:
        logger.error(f"Error in refresh_devices_websocket: {e}")
        await websocket.send_json({
            'type': 'error',
            'message': str(e)
        })

async def handle_adb_shell_connect(client_id: str, websocket: WebSocket, serial_no: str, config: dict):
    """处理ADB Shell连接 - 通过SSH执行adb shell命令"""
    try:
        # 先连接到SSH
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await websocket.send_json({
                'type': 'terminal_error',
                'error': 'SSH连接失败'
            })
            return

        # 创建shell通道
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        # 设置初始终端大小
        channel.resize_pty(width=120, height=30)

        # 发送清屏和adb shell命令
        # 使用多个换行和clear命令来清除banner
        channel.send('\n\n\n')  # 发送换行跳过banner
        channel.send('clear\n')  # 清屏
        channel.send(f'adb -s {serial_no} shell\n')  # 执行 adb shell

        # 保存会话
        loop = asyncio.get_event_loop()
        session_id = client_id

        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    old_session = global_state.terminal_ssh_sessions[session_id]
                    # 只有SSH模式才关闭SSH连接
                    if old_session.get('mode') != 'adb':
                        old_session['ssh'].close()
                    else:
                        # ADB模式:只关闭channel
                        try:
                            old_session['channel'].close()
                        except:
                            pass
                        ssh_manager.return_connection(old_session['ssh'])
                except:
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,  # 这里保存的是SSH连接对象
                'channel': channel,
                'host': config.get('ubuntu_host'),
                'user': config.get('ubuntu_user'),
                'mode': 'adb',
                'serial_no': serial_no,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        logger.info(f"[TERMINAL] ADB Shell session created for device {serial_no}")
        await websocket.send_json({
            'type': 'terminal_connected',
            'mode': 'adb',
            'serial_no': serial_no
        })

        # 启动后台读取线程
        def read_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] ADB Session {session_id} no longer exists")
                        break

                    try:
                        # 读取数据
                        data_chunk = global_state.terminal_ssh_sessions[session_id]['channel'].recv(1024)
                        if not data_chunk:
                            logger.info(f"[TERMINAL] No data received, ADB connection closed")
                            break

                        # 解码并发送
                        try:
                            text = data_chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data_chunk.decode('utf-8', errors='ignore')

                        # 使用保存的事件循环通过WebSocket发送
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                websocket.send_json({
                                    'type': 'terminal_data',
                                    'data': text
                                }),
                                loop
                            )
                            # 等待发送完成
                            future.result(timeout=5)
                        except Exception as e:
                            logger.error(f"[TERMINAL] Error sending ADB data: {e}")
                            break

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] ADB read error: {e}")
                        break

                    time.sleep(0.01)  # 防止CPU占用过高

            except Exception as e:
                logger.error(f"[TERMINAL] ADB read thread error: {e}")
            finally:
                # 清理连接
                logger.info(f"[TERMINAL] ADB read thread exiting for {session_id}")

        # 在后台线程中启动读取
        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()

        logger.info(f"[TERMINAL] ADB Shell connected for device {serial_no}")

    except Exception as e:
        logger.error(f"[TERMINAL] ADB Shell connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'ADB Shell连接失败: {str(e)}'
        })

async def handle_terminal_connect(client_id: str, websocket: WebSocket, data: dict):
    """处理终端SSH连接"""
    try:
        config = config_manager.load_config()
        host = data.get('host', config.get('ubuntu_host'))
        user = data.get('user', config.get('ubuntu_user'))
        password = data.get('password', config.get('ubuntu_pswd', ''))
        mode = data.get('mode', 'ssh')  # 'ssh' 或 'adb'
        serial_no = data.get('serial_no', '')

        # 使用client_id作为会话ID（每个WebSocket连接独立）
        session_id = client_id

        # ADB Shell 模式
        if mode == 'adb':
            logger.info(f"[TERMINAL] ADB Shell connection request for device {serial_no}")
            await handle_adb_shell_connect(client_id, websocket, serial_no, config)
            return

        logger.info(f"[TERMINAL] SSH Connection request from {session_id} to {user}@{host}")

        # 创建SSH客户端
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # 优化的SSH连接参数
        ssh_connect_timeout = 5
        ssh_banner_timeout = 3

        connected = False
        last_error = None

        # 尝试密钥认证（如果启用）
        use_key_auth = config.get('use_key_auth', False)
        if use_key_auth:
            try:
                key_path = os.path.expanduser(config.get('private_key_path', '~/.ssh/id_rsa'))
                key = paramiko.RSAKey.from_private_key_file(key_path)
                ssh.connect(
                    host,
                    username=user,
                    pkey=key,
                    timeout=ssh_connect_timeout,
                    banner_timeout=ssh_banner_timeout,
                    compress=True
                )
                connected = True
                logger.info(f"[TERMINAL] Connected using key authentication")
            except Exception as e:
                last_error = e
                logger.warning(f"[TERMINAL] Key auth failed: {e}")

        # 尝试密码认证
        if not connected and password:
            try:
                ssh.connect(
                    host,
                    username=user,
                    password=password,
                    timeout=ssh_connect_timeout,
                    banner_timeout=ssh_banner_timeout,
                    compress=True
                )
                connected = True
                logger.info(f"[TERMINAL] Connected using password authentication")
            except Exception as e:
                last_error = e
                logger.warning(f"[TERMINAL] Password auth failed: {e}")

        if not connected:
            error_msg = f'SSH连接失败：{str(last_error) if last_error else "请检查用户名、密码或密钥配置"}'
            await websocket.send_json({
                'type': 'terminal_error',
                'error': error_msg
            })
            return

        # 创建shell通道
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        # 设置初始终端大小
        channel.resize_pty(width=120, height=30)

        # 保存SSH会话和当前事件循环
        loop = asyncio.get_event_loop()
        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    global_state.terminal_ssh_sessions[session_id]['ssh'].close()
                except:
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'host': host,
                'user': user,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop  # 保存事件循环引用
            }

        logger.info(f"[TERMINAL] Terminal session created for {session_id}")
        await websocket.send_json({
            'type': 'terminal_connected'
        })

        # 启动后台读取线程
        def read_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] Session {session_id} no longer exists")
                        break

                    try:
                        # 读取数据
                        data_chunk = global_state.terminal_ssh_sessions[session_id]['channel'].recv(1024)
                        if not data_chunk:
                            logger.info(f"[TERMINAL] No data received, connection closed")
                            break

                        # 解码并发送
                        try:
                            text = data_chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data_chunk.decode('utf-8', errors='ignore')

                        # 使用保存的事件循环通过WebSocket发送
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                websocket.send_json({
                                    'type': 'terminal_data',
                                    'data': text
                                }),
                                loop
                            )
                            # 等待发送完成
                            future.result(timeout=5)
                        except Exception as e:
                            logger.error(f"[TERMINAL] Error sending data: {e}")
                            break

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] Read error: {e}")
                        break

                    time.sleep(0.01)  # 防止CPU占用过高

            except Exception as e:
                logger.error(f"[TERMINAL] Read thread error: {e}")
            finally:
                # 清理连接
                with global_state.terminal_lock:
                    if session_id in global_state.terminal_ssh_sessions:
                        try:
                            global_state.terminal_ssh_sessions[session_id]['ssh'].close()
                        except:
                            pass
                        del global_state.terminal_ssh_sessions[session_id]
                        logger.info(f"[TERMINAL] Cleaned up session {session_id}")

                # 通知客户端断开连接
                try:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            'type': 'terminal_error',
                            'error': '连接已断开'
                        }),
                        loop
                    )
                except:
                    pass

        # 启动读取线程
        thread = threading.Thread(target=read_output, daemon=True, name=f"terminal_read_{session_id}")
        thread.start()

    except paramiko.AuthenticationException:
        await websocket.send_json({
            'type': 'terminal_error',
            'error': 'SSH认证失败：用户名或密码错误'
        })
    except paramiko.SSHException as e:
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'SSH连接错误：{str(e)}'
        })
    except Exception as e:
        logger.error(f"[TERMINAL] Connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'连接失败：{str(e)}'
        })

async def handle_terminal_input(client_id: str, websocket: WebSocket, data: dict):
    """处理终端输入"""
    session_id = client_id

    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                input_data = data.get('input', data.get('data', ''))
                global_state.terminal_ssh_sessions[session_id]['channel'].send(input_data)
            except Exception as e:
                logger.error(f"[TERMINAL] Input error for {session_id}: {e}")
                await websocket.send_json({
                    'type': 'terminal_error',
                    'error': f'发送数据失败：{str(e)}'
                })

async def handle_terminal_resize(client_id: str, websocket: WebSocket, data: dict):
    """处理终端大小调整"""
    session_id = client_id

    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                global_state.terminal_ssh_sessions[session_id]['channel'].resize_pty(width=cols, height=rows)
                logger.info(f"[TERMINAL] Terminal resized for session {session_id}: {cols}x{rows}")
            except Exception as e:
                logger.error(f"[TERMINAL] Resize error for session {session_id}: {e}")

# ==================== 主程序 ====================

if __name__ == "__main__":
    logger.info("Starting GMS Auto Test FastAPI Server on port 5001...")
    logger.info("=" * 60)
    logger.info("  GMS Auto Test - FastAPI Server (Port 5001)")
    logger.info("  Framework: FastAPI (Pure)")
    logger.info("  Version: 4.0.0")
    logger.info("  Complete Migration from Flask")
    logger.info("=" * 60)
    logger.info("")

    # 运行FastAPI应用
    uvicorn.run(
        app,
        host='0.0.0.0',
        port=5001,
        log_level='info'
    )
