#!/usr/bin/env python3
"""
GMS Auto Test - FastAPI Full Application (Port 5001)

完整的 GMS 自动测试平台 FastAPI 应用，提供以下功能:
- 设备管理 (Devices): 设备列表、信息查询、重启、remount 等
- 测试管理 (Tests): 测试启动、停止、状态查询、日志流等
- 报告管理 (Reports): 报告列表、下载、分析、删除等
- 固件烧写 (Burning): 固件上传、GSI 烧写、序列号烧写等
- USB/IP 连接: USB/IP 设备连接、断开、状态查询
- 桌面 VNC: VNC 会话管理、桌面验证
- SSH 管理: SSH 连接测试、路由检查、SSHD 管理
- 文件管理: 文件上传、进度查询、列表
- VPN 管理: VPN 连接、断开、状态查询

Author: GMS Team
Version: 1.0.0
Port: 5001
"""

import os
import sys
import logging
import subprocess
import socket
import select
import pty
import fcntl
import termios
import struct
import signal
import uuid
import paramiko
import threading
import time
import re
import json
import shlex
import queue
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from urllib.parse import urlparse
import hashlib
import base64
import mimetypes
from typing import Dict, Any, List, Optional, Union, Tuple
from contextlib import asynccontextmanager
from collections import deque
from enum import Enum
import asyncio
import aiohttp
import tempfile
import shutil
import xml.etree.ElementTree as ET

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Body, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.websockets import WebSocketState

# 导入API文档列表
from core.api_docs_list import API_DOCS_LIST

# ==================== 枚举定义 ====================

class VerifiedBootState(str, Enum):
    """设备启动验证状态"""
    LOCKED = 'green'
    UNLOCKED_ORANGE = 'orange'
    UNLOCKED_YELLOW = 'yellow'

    @property
    def is_locked(self) -> bool:
        """返回是否已锁定"""
        return self == self.LOCKED

    @property
    def display_text(self) -> str:
        """返回显示文本"""
        return {
            'green': '已锁定 (GREEN)',
            'orange': '未锁定 (ORANGE)',
            'yellow': '未锁定 (YELLOW)',
        }[self.value]

class LogLevel(str, Enum):
    """日志级别"""
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'
    SUCCESS = 'success'

class AnalysisMode(str, Enum):
    """报告分析模式"""
    UPLOAD = "upload"
    SAVED = "saved"
    AI = "ai"

# ==================== 常量定义 ====================

# 上传进度相关常量
UPLOAD_PROGRESS_QUERY_TIMEOUT = 5  # 查询超时（秒）
UPLOAD_PROGRESS_EXPIRATION = 10  # 进度过期时间（秒）
UPLOAD_PROGRESS_CLEANUP_INTERVAL = 60  # 清理间隔（秒）

# GSI 固件烧写进度轮询配置
GSI_PROGRESS_POLL_INTERVAL = 0.5  # 服务器端进度更新间隔（秒）

# Favicon 获取配置
DEFAULT_FAVICON_TIMEOUT = 10  # 默认超时时间（秒）
MAX_BATCH_SIZE = 20  # 批量请求最大数量

# ==================== 统一响应格式 ====================

def success_response(data: Any = None, message: str = "Success") -> JSONResponse:
    """成功响应"""
    content = {'success': True, 'message': message}
    if data is not None:
        content['data'] = data
    return JSONResponse(content=content)

def error_response(error: str, status_code: int = 500, detail: Any = None) -> JSONResponse:
    """错误响应"""
    content = {'success': False, 'error': error}
    if detail is not None:
        content['detail'] = detail
    return JSONResponse(content=content, status_code=status_code)

def validation_error_response(errors: List[str]) -> JSONResponse:
    """验证错误响应"""
    return error_response(
        error="Validation failed",
        status_code=422,
        detail={"errors": errors}
    )
GSI_PROGRESS_INCREMENT = 5  # 每次增加的百分比
GSI_PROGRESS_MAX = 95  # 最大进度百分比（等待完成前）

# TRADEFED二进制文件映射
TRADEFED_BINARY_MAP = {
    'cts': 'cts-tradefed',
    'gsi': 'cts-tradefed',
    'gts': 'gts-tradefed',
    'sts': 'sts-tradefed',
    'vts': 'vts-tradefed',
    'xts': 'xts-tradefed'
}

# 特殊测试类型（不在主映射中）
SPECIAL_TEST_TYPES = {
    'cts-v-host-tradefed': 'cts-v',
    'apts-tradefed': 'apts',
    'gts-root-tradefed': 'gts-root',
}

# 预计算反向映射，避免每次调用函数时重复创建
TRADEFED_BINARY_REVERSE_MAP = {v: k for k, v in TRADEFED_BINARY_MAP.items()}

# 预编译正则表达式，避免重复编译
SUITE_TYPE_PATTERN = re.compile(r'/android-([a-z]+)')

# 预计算tradefed文件列表，避免在循环中重复计算
TRADEFED_BINARY_LIST = list(set(TRADEFED_BINARY_MAP.values()))

# 测试类型检测优先级（VTS优先于CTS，避免误匹配）
TEST_TYPE_DETECTION_PRIORITY = ['vts', 'gts', 'sts', 'cts']

def get_test_type_from_binary(binary_name: str) -> str:
    """从二进制文件名获取测试类型"""
    if result := SPECIAL_TEST_TYPES.get(binary_name):
        return result
    if result := TRADEFED_BINARY_REVERSE_MAP.get(binary_name):
        return result
    return binary_name.replace('-tradefed', '')

def detect_test_type_from_suite_path(suite_path: str) -> Optional[str]:
    """从测试套件路径检测测试类型

    Args:
        suite_path: 测试套件路径，如 /path/to/android-gts/tools

    Returns:
        检测到的测试类型（小写），如 'gts'，如果无法检测则返回 None
    """
    if not suite_path:
        return None

    suite_match = SUITE_TYPE_PATTERN.search(suite_path.lower())
    if suite_match:
        detected_type = suite_match.group(1)
        # 验证是否为已知的测试类型
        if detected_type in TRADEFED_BINARY_MAP or detected_type in SPECIAL_TEST_TYPES:
            return detected_type
    return None

def detect_test_type_from_dir_path(dir_path: str) -> Optional[str]:
    """从目录路径检测测试类型（用于重试目录）

    Args:
        dir_path: 目录路径，如 /path/to/20240101_120000/android-vts-results

    Returns:
        检测到的测试类型（小写），如 'vts'，如果无法检测则返回 None
    """
    if not dir_path:
        return None

    dir_lower = dir_path.lower()
    # 按优先级检测（VTS优先于CTS，避免误匹配）
    for test_type in TEST_TYPE_DETECTION_PRIORITY:
        if test_type in dir_lower:
            # 特殊处理：CTS不能匹配到GTS
            if test_type == 'cts' and 'gts' in dir_lower:
                continue
            return test_type
    return None

# ==================== Lifespan 事件处理 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info("Application startup")
    logger.info("=" * 60)

    # 启动定期清理任务
    async def periodic_cleanup_with_retry():
        """定期清理旧用户状态和测试日志（带错误恢复）"""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                global_state.cleanup_old_user_states()
                logger.info("Periodic cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup failed, retrying in 5 minutes: {e}")
                await asyncio.sleep(300)

    cleanup_task = asyncio.create_task(periodic_cleanup_with_retry())

    usb_dispatch_task = None
    public_tunnel_started = False

    try:
        await public_client_tunnel.start()
        public_tunnel_started = True
    except Exception as e:
        logger.error(f"Failed to start public client tunnel listeners: {e}")

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
                with global_state.device_cache_lock:
                    global_state.device_cache = {'devices': [], 'timestamp': 0}
                app.state.usb_event_queue.put({
                    'type': 'devices_changed',
                    'devices': devices,
                    'timestamp': datetime.now().isoformat()
                })
                logger.info(f"USB device change event queued, current devices: {devices}")
            except Exception as e:
                logger.error(f"Error queuing device change event: {e}")

        async def dispatch_usb_events():
            """后台分发USB事件，避免依赖状态轮询接口顺带消费队列。"""
            while True:
                try:
                    event = app.state.usb_event_queue.get_nowait()
                    with global_state.websocket_connections_lock:
                        clients = list(global_state.websocket_connections.items())

                    for client_id, ws in clients:
                        try:
                            if ws.client_state == WebSocketState.CONNECTED:
                                await ws.send_json(event)
                                logger.info(f"Sent USB event to client {client_id}: {event.get('type')}")
                        except Exception as e:
                            logger.debug(f"Error sending USB event to client {client_id}: {e}")
                except queue.Empty:
                    await asyncio.sleep(0.2)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"USB event dispatcher error: {e}")
                    await asyncio.sleep(1)

        # 初始化并启动USB监控
        init_usb_monitor(
            device_getter=get_devices,
            on_devices_changed=on_usb_devices_changed,
            check_interval=2.0,
            use_udev=True
        )
        start_usb_monitor()
        usb_dispatch_task = asyncio.create_task(dispatch_usb_events())
        logger.info("USB monitor started successfully")
    except Exception as e:
        logger.error(f"Failed to start USB monitor: {e}")

    yield

    # 关闭时执行
    logger.info("Application shutdown")

    # 取消清理任务
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    if usb_dispatch_task:
        usb_dispatch_task.cancel()
        try:
            await usb_dispatch_task
        except asyncio.CancelledError:
            pass

    if public_tunnel_started:
        try:
            await public_client_tunnel.stop()
        except Exception as e:
            logger.error(f"Error stopping public client tunnel: {e}")

    # 停止USB监控
    try:
        stop_usb_monitor()
        logger.info("USB monitor stopped")
    except Exception as e:
        logger.error(f"Error stopping USB monitor: {e}")

    logger.info("=" * 60)

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入核心模块
from core.config import config_manager
from core.ssh import ssh_manager, SSHD_INSTALL_GUIDE
from core.file_utils import FileUtils
from core.device import device_manager
from core.test_report import test_report_manager
from core.vnc import vnc_manager, calculate_window_positions
from core.adb_forward import adb_forward_manager
from core.usbip import usbip_manager, split_host_port, USBIPD_INSTALL_CMD, USBIPD_INSTALL_GUIDE
from core.common_utils import CommonUtils
from core.device_utils import DeviceUtils
from core.report_analyzer import ReportAnalyzer
from core.security_audit import classify_request_source, security_audit_logger
from core.test_report_db import test_report_db
from core.reverse_tunnel import public_client_tunnel

# 导入管理模块
from modules.client_manager import client_manager
from modules.device_lock_manager import device_lock_manager
from modules.test_logs_manager import test_logs_manager
from modules.icon_fetcher import IconFetcher

# 导入USB监控模块
from core.usb_monitor import init_usb_monitor, start_usb_monitor, stop_usb_monitor

# Redmine URL 模式常量
REDMINE_ISSUE_PATTERN = r'/issues/(\d+)'
REDMINE_ATTACHMENT_PATTERN = r'/attachments/(?:download/)?(\d+)'

# 预编译正则表达式以提高性能
COMPILED_REDMINE_ISSUE_PATTERN = re.compile(REDMINE_ISSUE_PATTERN)
COMPILED_REDMINE_ATTACHMENT_PATTERN = re.compile(REDMINE_ATTACHMENT_PATTERN)
COMPILED_REPORT_NAME_PATTERN = re.compile(r'Redmine-(\d+)-(.+)')

def create_basic_auth_header(username: str, password: str) -> Dict[str, str]:
    """创建 Basic Authentication 头"""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {'Authorization': f'Basic {credentials}'}

def build_redmine_download_url(base_url: str, attachment_id: str) -> str:
    """构建 Redmine 附件下载 URL"""
    return f"{base_url}/attachments/download/{attachment_id}/"

async def prepare_redmine_headers() -> Dict[str, str]:
    """
    准备带有认证的 Redmine API 请求头

    Returns:
        包含认证信息的请求头字典，如果未配置凭证则返回空字典
    """
    stored_creds = await load_redmine_credentials()
    if stored_creds:
        username = stored_creds.get('username')
        password = stored_creds.get('password')
        return create_basic_auth_header(username, password)
    return {}

def extract_filename_from_content_disposition(content_disposition: str) -> Optional[str]:
    """从 Content-Disposition header 中提取文件名"""
    if not content_disposition:
        return None
    # 匹配 filename*=UTF-8''xxx 或 filename="xxx" 或 filename=xxx
    filename_match = re.search(r"filename\*=UTF-8''([^\;]+)|filename=\"([^\"]+)\"|filename=([^\s;]+)", content_disposition)
    if filename_match:
        filename = filename_match.group(1) or filename_match.group(2) or filename_match.group(3)
        return urllib.parse.unquote(filename) if filename else None
    return None

def extract_report_name_from_upload(files: List[UploadFile]) -> str:
    """
    从上传的文件中提取报告名称

    Args:
        files: 上传的文件列表

    Returns:
        报告名称字符串
    """
    if not files or not files[0].filename:
        return 'Unknown Report'

    if len(files) == 1:
        return files[0].filename

    # 多文件上传 - 使用文件夹名称
    first_file = files[0].filename
    folder_name = os.path.dirname(first_file) or os.path.basename(first_file)
    return folder_name


def normalize_upload_relative_path(filename: Optional[str], allow_nested: bool = True) -> str:
    """Normalize browser-provided upload names and keep them relative."""
    raw_name = (filename or '').replace('\\', '/').strip()
    if not raw_name:
        raise ValueError("文件名无效")

    normalized = os.path.normpath(raw_name).replace('\\', '/')
    if normalized in ('.', '..') or normalized.startswith('../') or os.path.isabs(normalized):
        raise ValueError("非法文件路径")

    if not allow_nested:
        normalized = os.path.basename(normalized)

    if not normalized or normalized in ('.', '..'):
        raise ValueError("文件名无效")
    return normalized


def safe_upload_target_path(base_dir: str, filename: Optional[str], allow_nested: bool = True) -> str:
    """Build a safe destination path for an uploaded file."""
    relative_path = normalize_upload_relative_path(filename, allow_nested=allow_nested)
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, relative_path))
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError("非法文件路径")
    return target_abs


def copy_fileobj_to_path(source, destination: str, max_size: Optional[int] = None, chunk_size: int = 1024 * 1024) -> int:
    """Copy a file-like object to disk in chunks and return bytes written."""
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as target:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            bytes_written += len(chunk)
            if max_size is not None and bytes_written > max_size:
                raise ValueError(f"文件过大，最大支持 {max_size // (1024 * 1024)}MB")
            target.write(chunk)
    return bytes_written


async def save_upload_to_path(upload_file: UploadFile, destination: str, max_size: Optional[int] = None) -> int:
    """Persist an UploadFile without loading the whole file into memory."""
    await upload_file.seek(0)
    return await asyncio.to_thread(copy_fileobj_to_path, upload_file.file, destination, max_size)


def merge_files_to_path(source_paths: List[str], destination: str, chunk_size: int = 1024 * 1024) -> int:
    """Merge files into destination using bounded memory."""
    bytes_written = 0
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, 'wb') as outfile:
        for source_path in source_paths:
            with open(source_path, 'rb') as infile:
                while True:
                    chunk = infile.read(chunk_size)
                    if not chunk:
                        break
                    outfile.write(chunk)
                    bytes_written += len(chunk)
    return bytes_written


# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== 服务器配置常量 ====================
SERVER_PORT = 5001
GMS_ENV = os.getenv('GMS_ENV', 'development').strip().lower()
SERVER_HOST = os.getenv('GMS_SERVER_HOST', '127.0.0.1' if GMS_ENV == 'production' else '0.0.0.0')
# 用于文档和示例的默认URL（使用占位符而非硬编码IP）
DEFAULT_SERVER_URL = os.getenv('GMS_SERVER_URL', 'http://server:5001')
PROXY_HEADERS_ENABLED = os.getenv(
    'GMS_PROXY_HEADERS',
    'true' if GMS_ENV == 'production' else 'false'
).strip().lower() == 'true'
FORWARDED_ALLOW_IPS = os.getenv('GMS_FORWARDED_ALLOW_IPS', '127.0.0.1')


def get_default_suites_path(config: Dict[str, Any]) -> str:
    """Get default suites path from config or environment"""
    ubuntu_user = config_manager.get_ubuntu_user(config)
    return config.get('suites_path', f"/home/{ubuntu_user}/GMS-Suite")


def is_config_host_local(config: Dict[str, Any]) -> bool:
    return CommonUtils.is_local_host(config_manager.get_ubuntu_host(config))


def get_effective_local_server(client_id: str, requested_local_server: str = "") -> str:
    """Resolve the callback host for the current client."""
    if requested_local_server:
        return requested_local_server

    dynamic_config = config_manager._load_dynamic_config() or {}
    dynamic_local_server = dynamic_config.get('local_server')
    if dynamic_local_server:
        return dynamic_local_server

    return client_id


def build_suite_info(full_path: str) -> Optional[Dict[str, str]]:
    """Build suite info from tradefed binary path.

    For cts-v, the tools_path should be the android-cts-verifier directory,
    not the deep nested android-cts-v-host/tools path.
    """
    full_path = full_path.strip()
    if not full_path:
        return None

    parts = full_path.split('/')
    tradefed_name = parts[-1]
    test_type = get_test_type_from_binary(tradefed_name)

    tools_dir = '/'.join(parts[:-1])

    # 特殊处理 cts-v：找到 android-cts-verifier 目录作为 tools_path
    if test_type == 'cts-v':
        # 从后向前查找 android-cts-verifier 目录
        for i, part in enumerate(parts):
            if part == 'android-cts-verifier':
                tools_dir = '/'.join(parts[:i+1])
                break

    version_dir = next(
        (
            p for p in parts
            if p.startswith('android-')
            and (
                test_type in p
                or (test_type == 'gsi' and 'cts' in p)
                or (test_type == 'gts-root' and 'gts' in p)
            )
        ),
        ""
    )
    if test_type == 'gsi' and version_dir:
        test_type = 'cts'
    if test_type == 'gts-root' and version_dir:
        test_type = 'gts'

    return {
        'test_type': test_type,
        'version': version_dir,
        'tools_path': tools_dir,
        'full_path': full_path,
        'binary': tradefed_name
    }


def list_local_test_suites(base_path: str) -> List[Dict[str, str]]:
    suites = []
    if not os.path.isdir(base_path):
        logger.info(f"[TestSuites] Local base path not found: {base_path}")
        return suites

    max_depth = 8  # 增加深度以支持 verifier 等嵌套较深的套件
    base_depth = base_path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(base_path):
        if root.rstrip(os.sep).count(os.sep) - base_depth >= max_depth:
            dirs[:] = []
        for file_name in files:
            if not file_name.endswith('-tradefed'):
                continue
            full_path = os.path.join(root, file_name)
            if not os.access(full_path, os.X_OK):
                continue
            suite = build_suite_info(full_path)
            if suite:
                suites.append(suite)

    suites.sort(key=lambda item: (item['test_type'], item['version'], item['full_path']))
    return suites


def _parse_csv_env(env_name: str, default: str) -> List[str]:
    """Parse CSV environment variable into a normalized non-empty list."""
    raw = os.getenv(env_name, default)
    items = [item.strip() for item in raw.split(',') if item.strip()]
    return items or [default]

# ==================== APK分析配置常量 ====================
JADX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'jadx', 'bin', 'jadx')
APK_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'apk_uploads')
APK_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
APK_MAX_SOURCE_FILE_SIZE = 2 * 1024 * 1024  # 2MB - 单个源码文件查看上限
APK_MAX_TASKS = 50  # 最大保存任务数
JADX_TIMEOUT = 600  # jadx 反编译超时(秒)

# ==================== FastAPI应用 ====================

# 自定义JSONResponse类，确保UTF-8编码
class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")

# Redmine 问题ID缓存（用于保持附件拖拽的一致性）
from collections import OrderedDict
REDMINE_ISSUE_ID_CACHE = OrderedDict()
REDMINE_ISSUE_ID_CACHE_MAX_SIZE = 100  # 最多缓存100个附件ID到问题ID的映射

app = FastAPI(
    title="GMS Auto Test - FastAPI Server (Port 5001)",
    description="完整的测试管理服务（替代Flask版本）",
    version="4.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse
)

# CORS中间件
CORS_ORIGINS = _parse_csv_env('CORS_ORIGINS', '*')
ALLOW_CREDENTIALS = os.getenv('CORS_ALLOW_CREDENTIALS', 'false').strip().lower() == 'true'
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

TRUSTED_HOSTS = _parse_csv_env('TRUSTED_HOSTS', '*')
if TRUSTED_HOSTS != ['*']:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)

# 性能优化中间件：HTTP连接池和响应头优化
from starlette.middleware.gzip import GZipMiddleware

# 添加GZip压缩中间件（最小500字节启动压缩，提升传输效率）
app.add_middleware(GZipMiddleware, minimum_size=500)


AUDIT_SKIP_PREFIXES = (
    '/static/',
    '/novnc/',
    '/api/security-audit/page-view',
    '/api/security-audit/logs',
    '/api/security-audit/detail',
    '/api/security-audit/export',
    '/api/notifications',
    '/api/public-client/',
)
AUDIT_SKIP_PATHS = {'/favicon.ico'}
AUDIT_WEB_READONLY_NOISE_PATHS = {
    '/',
    '/templates/architecture.html',
    '/api/config/ai',
    '/api/config/opengrok',
    '/api/config/read',
    '/api/config/redmine',
    '/api/desktop/vnc/status',
    '/api/devices/list',
    '/api/devices/management',
    '/api/devices/user-locked',
    '/api/reports/list',
    '/api/ssh/sshd',
    '/api/system/docs',
    '/api/system/health',
    '/api/system/help',
    '/api/system/skills',
    '/api/test/logs/get',
    '/api/test/logs/list',
    '/api/test/status',
    '/api/test/suites',
    '/api/test/suites/files',
    '/api/tools/load',
    '/api/usbip/status',
    '/api/users/current',
    '/api/users/list',
    '/api/vpn/status',
    '/api/public-client/status',
}
AUDIT_WEB_READONLY_NOISE_PREFIXES = (
    '/api/favicon/',
)
AUDIT_PAGE_VIEW_SKIP_PAGES = {'security-audit'}


def can_audit_path(path: str) -> bool:
    if path in AUDIT_SKIP_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in AUDIT_SKIP_PREFIXES)


def should_audit_request(path: str, source: str, method: str) -> bool:
    if not can_audit_path(path):
        return False

    method_upper = method.upper()
    if source == 'web' and method_upper in {'GET', 'HEAD'}:
        if path in AUDIT_WEB_READONLY_NOISE_PATHS:
            return False
        if any(path.startswith(prefix) for prefix in AUDIT_WEB_READONLY_NOISE_PREFIXES):
            return False

    return True


def get_audit_operation(path: str, method: str) -> str:
    if path == '/':
        return '打开Web首页'
    if path.startswith('/api/'):
        return f"{method} {path}"
    return f"{method} {path}"


MAX_AUDIT_REQUEST_BODY_BYTES = 64 * 1024
MAX_AUDIT_RESPONSE_BODY_BYTES = 128 * 1024


def _safe_int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


async def summarize_audit_request(request: Request, should_audit: bool) -> Dict[str, Any]:
    """Build a safe request summary without recording file contents or secrets."""
    if not should_audit:
        return {}

    content_type = (request.headers.get('content-type') or '').lower()
    content_length = _safe_int(request.headers.get('content-length'))
    summary = {
        'content_type': content_type.split(';')[0] if content_type else '',
        'content_length': content_length,
        'query': security_audit_logger.sanitize_mapping(dict(request.query_params)),
    }

    if request.method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return summary

    if content_length > MAX_AUDIT_REQUEST_BODY_BYTES:
        if 'multipart/form-data' in content_type:
            summary['body'] = {
                'body_type': 'multipart',
                'captured': False,
                'reason': '文件上传内容不记录',
            }
        else:
            summary['body'] = {
                'captured': False,
                'reason': f'请求体超过 {MAX_AUDIT_REQUEST_BODY_BYTES} 字节',
            }
        return summary

    if 'application/json' not in content_type and 'application/x-www-form-urlencoded' not in content_type:
        if 'multipart/form-data' in content_type:
            summary['body'] = {
                'body_type': 'multipart',
                'captured': False,
                'reason': '文件上传内容不记录',
            }
        return summary

    body = await request.body()
    body_sent = False

    async def replay_body():
        nonlocal body_sent
        if body_sent:
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        body_sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    request._receive = replay_body

    if 'application/json' in content_type:
        summary['body'] = security_audit_logger.summarize_json_body(body)
    else:
        summary['body'] = security_audit_logger.summarize_form_body(body)
    return summary


async def summarize_audit_response(response) -> Tuple[Any, Dict[str, Any]]:
    """Capture small JSON responses for audit detail and rebuild the response."""
    if response is None:
        return response, {}

    content_type = (response.headers.get('content-type') or '').lower()
    content_encoding = (response.headers.get('content-encoding') or '').lower()
    content_length = _safe_int(response.headers.get('content-length'))

    summary = {
        'content_type': content_type.split(';')[0] if content_type else '',
        'content_length': content_length,
    }

    if 'application/json' not in content_type or content_encoding:
        return response, summary
    if content_length > MAX_AUDIT_RESPONSE_BODY_BYTES:
        summary.update({'captured': False, 'reason': '响应体过大'})
        return response, summary
    if not hasattr(response, 'body_iterator'):
        return response, summary

    body_parts = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, str):
            chunk = chunk.encode('utf-8')
        body_parts.append(chunk)

    body = b''.join(body_parts)
    summary['content_length'] = len(body)
    try:
        parsed = json.loads(body.decode('utf-8'))
        if isinstance(parsed, dict):
            summary['body'] = security_audit_logger.sanitize_mapping(parsed)
        else:
            summary['body'] = security_audit_logger.sanitize_value('response', parsed)
    except Exception as e:
        summary['parse_error'] = str(e)
        summary['preview'] = body[:300].decode('utf-8', errors='replace')

    headers = dict(response.headers)
    headers.pop('content-length', None)
    rebuilt = Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type
    )
    return rebuilt, summary


@app.middleware("http")
async def add_headers_middleware(request, call_next):
    """轻量级HTTP中间件 - 添加响应头"""
    path = request.url.path
    request_source = classify_request_source(request.headers.get('user-agent', ''), path)
    should_audit = should_audit_request(path, request_source, request.method)
    start_time = time.perf_counter()
    response = None
    error_text = None
    request_summary = {}
    response_summary = {}

    try:
        try:
            request_summary = await summarize_audit_request(request, should_audit)
        except Exception as request_audit_error:
            logger.debug(f"Security audit request summary failed: {request_audit_error}")
            request_summary = {'captured': False, 'reason': '请求摘要采集失败'}
        response = await call_next(request)
    except Exception as e:
        error_text = str(e)
        raise
    finally:
        status_code = response.status_code if response is not None else 500
        final_should_audit = should_audit or (status_code >= 400 and can_audit_path(path))

        if final_should_audit and response is not None:
            try:
                response, response_summary = await summarize_audit_response(response)
            except Exception as response_audit_error:
                logger.debug(f"Security audit response summary failed: {response_audit_error}")

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

        if final_should_audit:
            try:
                client_id = get_client_id_from_request(request)
                username, client_ip = parse_client_id(client_id)
            except Exception:
                client_ip = get_client_ip(request)
                username = 'unknown'
                client_id = f'{username}@{client_ip}'

            try:
                security_audit_logger.log_event({
                    'action_type': 'api' if path.startswith('/api/') else 'page_visit',
                    'source': request_source,
                    'operation': get_audit_operation(path, request.method),
                    'method': request.method,
                    'path': path,
                    'query': security_audit_logger.sanitize_mapping(dict(request.query_params)),
                    'request_summary': request_summary,
                    'response_summary': response_summary,
                    'status_code': status_code,
                    'duration_ms': duration_ms,
                    'client_ip': client_ip,
                    'client_id': client_id,
                    'username': username,
                    'user_agent': request.headers.get('user-agent', '')[:300],
                    'content_length': request.headers.get('content-length', ''),
                    'error': error_text,
                })
            except Exception as audit_error:
                logger.debug(f"Security audit log failed: {audit_error}")

    # 安全响应头
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"

    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400, immutable"
    elif path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"

    return response

# 挂载静态文件（启用缓存控制）
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

# ==================== 常量定义 ====================
MAX_LOG_ENTRIES = 1000  # 最大日志条目数
CLEANUP_INTERVAL_SECONDS = 3600  # 定期清理间隔（1小时）
USER_STATE_MAX_AGE_HOURS = 24  # 用户状态最大存活时间
UPLOAD_PROGRESS_MAX_AGE_SECONDS = 600  # 上传进度过期时间（10分钟）
USBIP_STATE_MAX_AGE_SECONDS = 86400  # USB/IP状态过期时间（24小时）

# ==================== 全局状态管理 ====================

class GlobalState:
    """全局状态管理"""
    def __init__(self):
        self.running_tests = {}  # {client_id: test_info}
        self.test_logs = {}      # {client_id: log_entries}
        self.ssh_connections = {}  # {client_id: ssh_connection}
        self.scrcpy_sessions = {}  # {device_id: session_info}
        self.device_cache = {'devices': [], 'timestamp': 0}  # 3秒TTL
        self.device_cache_lock = threading.Lock()  # 设备缓存锁
        self.websocket_connections = {}  # {client_id: websocket}
        self.websocket_connections_lock = threading.Lock()  # WebSocket连接锁
        self.firmware_upload_progress = {}  # {client_id: {'progress': float, 'filename': str, 'uploaded_size': int, 'total_size': int, 'timestamp': float}}
        self.firmware_upload_progress_lock = threading.Lock()  # 上传进度锁
        self.usbip_states = {}  # {client_id: {'connected': bool, 'timestamp': float}}
        self.usbip_devices_source = {}  # {device_id: {'source': device_host, 'timestamp': float}}
        self.terminal_ssh_sessions = {}  # {session_id: {'ssh': ssh, 'channel': channel, 'websocket': websocket}}
        self.terminal_lock = threading.Lock()  # 终端会话锁
        self.user_states = {}  # {client_id: {running, devices, logs, created_at, last_seen}}
        self.user_states_lock = threading.Lock()  # 用户状态锁
        self.usbip_states_lock = threading.Lock()  # USB/IP状态锁（与Flask一致）
        self.usbip_devices_source_lock = threading.Lock()  # USB/IP设备来源锁（与Flask一致）
        self.test_logs_lock = threading.Lock()  # 测试日志锁
        self.last_saved_log_file = {}  # {client_id: log_file_path}
        self.notifications = {}  # {client_id: deque([notification])}
        self.notifications_lock = threading.Lock()
        self.device_ssh_pools = {}
        self.device_ssh_pools_lock = threading.Lock()
        self.device_ssh_pools_max = 10  # 最大设备SSH连接池数量
        self.apk_analysis_tasks = {}  # {task_id: {'status': str, 'progress': int, 'apk_path': str, 'output_dir': str, 'filename': str, 'timestamp': float, 'error': str or None}}
        self.apk_analysis_tasks_lock = threading.Lock()
        self.apk_upload_locks = {}
        self.apk_upload_locks_lock = threading.Lock()

    def _close_ssh_safely(self, ssh):
        """安全关闭SSH连接"""
        try:
            if ssh and ssh.get_transport() is not None:
                ssh.close()
        except Exception:
            pass

    @staticmethod
    def _cleanup_expired(data_dict, lock, max_age_seconds):
        """通用过期清理辅助方法"""
        now_ts = time.time()
        with lock:
            expired = [k for k, v in data_dict.items()
                       if now_ts - v.get('timestamp', 0) > max_age_seconds]
            for k in expired:
                del data_dict[k]
        return expired

    def cleanup_old_user_states(self):
        """清理超过指定时间的旧用户状态，防止内存泄漏"""
        try:
            to_remove = []
            now = datetime.now()

            # 收集需要清理的client_id（在锁内快速遍历）
            with self.user_states_lock:
                for client_id, state in self.user_states.items():
                    if 'last_seen' in state:
                        try:
                            last_seen = datetime.fromisoformat(state['last_seen'])
                            if (now - last_seen) > timedelta(hours=USER_STATE_MAX_AGE_HOURS):
                                to_remove.append(client_id)
                        except (ValueError, TypeError):
                            to_remove.append(client_id)

                # 删除用户状态
                for client_id in to_remove:
                    del self.user_states[client_id]

            # 清理相关的测试日志（在user_states_lock外执行，避免嵌套锁）
            if to_remove:
                with self.test_logs_lock:
                    for client_id in to_remove:
                        self.test_logs.pop(client_id, None)
                        # 同时清理last_saved_log_file中的旧条目
                        self.last_saved_log_file.pop(client_id, None)

            # 以下清理逻辑独立于用户状态清理，每次都执行

            # 清理上传进度（过期超过10分钟的）
            expired_progress = self._cleanup_expired(
                self.firmware_upload_progress, self.firmware_upload_progress_lock, UPLOAD_PROGRESS_MAX_AGE_SECONDS)

            # 清理断开的终端SSH会话
            with self.terminal_lock:
                expired_sessions = []
                for sid, session in list(self.terminal_ssh_sessions.items()):
                    ws = session.get('websocket')
                    if ws is None or (hasattr(ws, 'client_state') and ws.client_state == WebSocketState.DISCONNECTED):
                        expired_sessions.append(sid)
                for sid in expired_sessions:
                    session = self.terminal_ssh_sessions.pop(sid)
                    self._close_ssh_safely(session.get('ssh'))
                    channel = session.get('channel')
                    if channel:
                        try:
                            channel.close()
                        except Exception:
                            pass

            # 清理USB/IP过期状态（超过24小时的）
            expired_usbip = self._cleanup_expired(
                self.usbip_states, self.usbip_states_lock, USBIP_STATE_MAX_AGE_SECONDS)

            if to_remove or expired_progress or expired_sessions or expired_usbip:
                logger.info(f"Cleanup: {len(to_remove)} user states, {len(expired_progress)} upload progress, "
                           f"{len(expired_sessions)} SSH sessions, {len(expired_usbip)} USB/IP states")
        except Exception as e:
            logger.error(f"Error cleaning up user states: {e}")

    def device_ssh_pool_get(self, pool_key: str, config: dict, pool_size: int = 3):
        """
        从设备SSH连接池获取或创建连接

        使用FIFO策略清理最老的连接池,防止内存泄漏
        """
        with self.device_ssh_pools_lock:
            # 限制连接池数量,防止内存泄漏
            if pool_key not in self.device_ssh_pools:
                if len(self.device_ssh_pools) >= self.device_ssh_pools_max:
                    # 清理最老的连接池
                    oldest_key = next(iter(self.device_ssh_pools))
                    old_pool = self.device_ssh_pools.pop(oldest_key)
                    while not old_pool.empty():
                        ssh = old_pool.get_nowait()
                        self._close_ssh_safely(ssh)
                    logger.info(f"[Device SSH Pool] Cleaned oldest pool: {oldest_key}")

                self.device_ssh_pools[pool_key] = queue.Queue(maxsize=pool_size)

            pool = self.device_ssh_pools[pool_key]

        # 尝试从池中获取有效连接（最多尝试pool_size次）
        max_attempts = pool.maxsize
        for attempt in range(max_attempts):
            try:
                ssh = pool.get_nowait()
                # 健康检查
                try:
                    transport = ssh.get_transport() if ssh else None
                    if transport and transport.is_active():
                        logger.debug(f"[Device SSH Pool] Reused connection for {pool_key}")
                        return ssh
                    else:
                        logger.debug(f"[Device SSH Pool] Connection {attempt+1}/{max_attempts} is inactive")
                        self._close_ssh_safely(ssh)
                except Exception as e:
                    logger.debug(f"[Device SSH Pool] Connection {attempt+1}/{max_attempts} check failed: {e}")
                    self._close_ssh_safely(ssh)
            except queue.Empty:
                break

        # 池为空或所有连接都失效，创建新连接
        logger.debug(f"[Device SSH Pool] Creating new connection for {pool_key}")
        return self._create_device_ssh_connection(pool_key, config)

    def device_ssh_pool_return(self, pool_key: str, ssh):
        """
        归还连接到设备SSH连接池

        Args:
            pool_key: 连接池键值
            ssh: SSHClient 对象
        """
        with self.device_ssh_pools_lock:
            if pool_key in self.device_ssh_pools:
                try:
                    self.device_ssh_pools[pool_key].put_nowait(ssh)
                except queue.Full:
                    # 池已满，关闭连接
                    self._close_ssh_safely(ssh)
            else:
                # 池不存在，关闭连接
                self._close_ssh_safely(ssh)

    def _create_device_ssh_connection(self, pool_key: str, config: dict):
        """
        创建设备SSH连接

        Args:
            pool_key: 连接池键值（通常是 device_host）
            config: 配置字典

        Returns:
            SSHClient 对象，失败返回 None
        """
        device_host = config.get('device_host', pool_key)
        if not device_host:
            logger.error("[Device SSH Pool] No device host in config")
            return None

        if '@' not in device_host:
            logger.error(f"[Device SSH Pool] Device host format should be user@host: {device_host}")
            return None

        username, hostname = CommonUtils.parse_host_address(device_host)
        hostname, port = split_host_port(hostname)
        password = config.get('device_pswd', '')

        if not password:
            logger.error(f"[Device SSH Pool] No SSH password configured for {pool_key}")
            return None

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=hostname, port=port, username=username, password=password, timeout=10)
            logger.info(f"[Device SSH Pool] Connected to {pool_key}")
            return ssh
        except Exception as e:
            logger.error(f"[Device SSH Pool] Failed to connect to {pool_key}: {e}")
            return None

global_state = GlobalState()

DEVICE_CACHE_TTL = 3
DEVICE_SSH_POOLS_MAX = 10  # 最大设备SSH连接池数量

# ==================== 通用工具函数 ====================

# Pre-compiled regex patterns for efficiency
_ANSI_ESCAPE_PATTERN = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
_IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
_PING_RTT_PATTERN = re.compile(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms')
_PING_AVG_PATTERN = re.compile(r'avg[=\s]+([\d.]+)', re.IGNORECASE)
_PING_LOSS_PATTERN = re.compile(r'(\d+)% packet loss')

def strip_ansi_codes(text: str) -> str:
    """
    移除ANSI转义码

    Args:
        text: 包含ANSI转义码的文本

    Returns:
        清理后的文本
    """
    return _ANSI_ESCAPE_PATTERN.sub('', text)

async def release_device_locks(client_id: str, device_ids: List[str], broadcast: bool = True):
    """
    批量释放设备锁并广播更新

    Args:
        client_id: 客户端ID
        device_ids: 要释放的设备ID列表
        broadcast: 是否广播设备锁更新
    """
    if not device_ids:
        return

    for device_id in device_ids:
        device_lock_manager.unlock_device(device_id, client_id)

    if broadcast:
        await broadcast_device_lock_update(device_ids)

# ==================== SSH 连接上下文管理器 ====================

class SSHConnection:
    """SSH连接上下文管理器，自动处理连接获取和归还"""

    def __init__(self, config=None):
        self.config = config or config_manager.load_config()
        self.ssh = None

    def __enter__(self):
        self.ssh = ssh_manager.get_connection(self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail="SSH连接失败"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh:
            try:
                ssh_manager.return_connection(self.ssh)
            except Exception as e:
                logger.error(f"Failed to return SSH connection: {e}")

# ==================== WebSocket 辅助函数 ====================

async def safe_websocket_send(client_id: str, message: dict):
    """线程安全地发送WebSocket消息（带背压检查）"""
    # 先获取WebSocket连接（使用锁）
    with global_state.websocket_connections_lock:
        ws = global_state.websocket_connections.get(client_id)

    # 在锁外执行异步操作
    if ws:
        try:
            # 检查WebSocket状态
            if ws.client_state == WebSocketState.DISCONNECTED:
                logger.debug(f"WebSocket {client_id} already disconnected")
                return

            # 检查缓冲区大小（防止内存泄漏）
            if hasattr(ws, '_queue') and ws._queue.qsize() > 100:
                logger.warning(f"WebSocket buffer full for {client_id}, dropping message")
                return

            await ws.send_json(message)
        except (WebSocketDisconnect, ConnectionError):
            logger.debug(f"WebSocket {client_id} disconnected during send")
        except Exception as e:
            logger.debug(f"Failed to send WebSocket message to {client_id}: {e}")


VALID_NOTIFICATION_LEVELS = {'info', 'success', 'warning', 'error'}
MAX_NOTIFICATIONS_PER_CLIENT = 200


def store_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存通知到内存历史，供页面通知中心读取。"""
    normalized_level = level if level in VALID_NOTIFICATION_LEVELS else 'info'
    record = {
        'id': str(uuid.uuid4()),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'title': str(title or '通知')[:120],
        'message': str(message or '')[:600],
        'level': normalized_level,
        'category': str(category or 'system')[:50],
        'read': False,
        'data': data or {},
    }
    key = client_id or 'unknown'
    with global_state.notifications_lock:
        if key not in global_state.notifications:
            global_state.notifications[key] = deque(maxlen=MAX_NOTIFICATIONS_PER_CLIENT)
        global_state.notifications[key].append(record)
    return record


async def push_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存并通过 WebSocket 推送通知。"""
    record = store_notification(client_id, title, message, level, category, data)
    await safe_websocket_send(client_id, {
        'type': 'notification',
        'notification': record
    })
    return record


def list_client_notifications(client_id: str, limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 100), MAX_NOTIFICATIONS_PER_CLIENT))
    key = client_id or 'unknown'
    with global_state.notifications_lock:
        records = list(global_state.notifications.get(key, []))
    records = list(reversed(records))[:limit]
    unread_count = sum(1 for record in records if not record.get('read'))
    return {'records': records, 'unread_count': unread_count}


def mark_client_notifications_read(client_id: str, ids: Optional[List[str]] = None) -> Dict[str, Any]:
    key = client_id or 'unknown'
    id_set = set(ids or [])
    updated = 0
    with global_state.notifications_lock:
        records = global_state.notifications.get(key, deque())
        for record in records:
            if not id_set or record.get('id') in id_set:
                if not record.get('read'):
                    record['read'] = True
                    updated += 1
        unread_count = sum(1 for record in records if not record.get('read'))
    return {'updated': updated, 'unread_count': unread_count}


def clear_client_notifications(client_id: str) -> Dict[str, Any]:
    key = client_id or 'unknown'
    with global_state.notifications_lock:
        removed = len(global_state.notifications.get(key, []))
        global_state.notifications[key] = deque(maxlen=MAX_NOTIFICATIONS_PER_CLIENT)
    return {'removed': removed, 'unread_count': 0}


async def broadcast_device_change(devices: List[str], disconnected: List[str] = None, connected: List[str] = None, source: str = 'usb_monitor'):
    """广播设备变化事件到所有 WebSocket 客户端

    Args:
        devices: 当前设备列表
        disconnected: 断开的设备列表（可选）
        connected: 连接的设备列表（可选）
        source: 事件来源 ('usb_monitor' 或 'usbip_disconnect')
    """
    message = {
        'type': 'devices_changed',
        'devices': devices,
        'source': source,
        'timestamp': datetime.now().isoformat()
    }
    if disconnected:
        message['disconnected'] = disconnected
    if connected:
        message['connected'] = connected

    logger.info(f"[Broadcast] Notifying {len(global_state.websocket_connections)} clients about device change (source: {source})")

    with global_state.websocket_connections_lock:
        clients = list(global_state.websocket_connections.items())

    notification_level = ''
    notification_title = ''
    notification_message = ''
    if disconnected:
        notification_level = 'warning'
        notification_title = 'USB设备断开'
        notification_message = '断开：' + ', '.join(disconnected)
    elif connected:
        notification_level = 'success'
        notification_title = 'USB设备已连接'
        notification_message = '连接：' + ', '.join(connected)

    for client_id, ws in clients:
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                client_message = dict(message)
                if notification_title:
                    client_message['notification'] = store_notification(
                        client_id,
                        notification_title,
                        notification_message,
                        notification_level,
                        'device',
                        {
                            'connected': connected or [],
                            'disconnected': disconnected or [],
                            'source': source,
                        }
                    )
                await ws.send_json(client_message)
        except Exception as e:
            logger.debug(f"Failed to broadcast to {client_id}: {e}")

async def notify_device_change(devices_to_remove: List[str], context: str = "USB/IP Stop"):
    """通知设备变化到 WebSocket 客户端

    Args:
        devices_to_remove: 已断开的设备列表
        context: 日志上下文标识
    """
    if not devices_to_remove:
        return

    try:
        from core.device_manager import get_device_manager

        dm = get_device_manager()
        # 使用 asyncio.to_thread 避免阻塞事件循环
        current_devices = await asyncio.to_thread(dm.get_connected_devices)
        logger.info(f"[{context}] Notifying device change: {current_devices}")
        await broadcast_device_change(current_devices, disconnected=devices_to_remove)
    except Exception as e:
        logger.warning(f"[{context}] Failed to notify device change: {e}")

def format_device_list_info(devices: List[str]) -> str:
    """格式化设备列表用于显示

    Args:
        devices: 设备列表

    Returns:
        格式化后的字符串，如 " (device1, device2)" 或空字符串
    """
    return f" ({', '.join(devices)})" if devices else ""

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
        return JSONResponse(
            content=response,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )

    @staticmethod
    def error(error_message, status_code=500, **extra_fields):
        """错误响应（与Flask格式一致）"""
        response = {'success': False, 'error': error_message}
        response.update(extra_fields)
        return JSONResponse(
            content=response,
            status_code=status_code,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )

    @staticmethod
    def device_results(results, operation_name):
        """设备批量操作结果"""
        success_count = sum(1 for r in results if r.get('success', False))
        fail_count = len(results) - success_count
        return ApiResponse.success({
            'results': results,
            'summary': {'total': len(results), 'success': success_count, 'failed': fail_count}
        }, f"{operation_name}完成: 成功 {success_count} 台, 失败 {fail_count} 台")

# ==================== 工具函数 ====================

from functools import wraps, lru_cache

def handle_api_errors(func):
    """统一API错误处理装饰器 - 支持同步和异步函数"""
    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return ApiResponse.error(str(e), status_code=500)

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return ApiResponse.error(str(e), status_code=500)

    # 检查函数是否是协程函数
    if asyncio.iscoroutinefunction(func):
        return async_wrapper
    else:
        return sync_wrapper

async def execute_on_devices_parallel(devices: List[str], operation_func, ssh, **kwargs) -> List[Dict]:
    """
    并行执行设备操作，替代串行循环

    Args:
        devices: 设备ID列表
        operation_func: 单设备操作函数，签名为 async def func(device_id, ssh, **kwargs) -> dict
        ssh: SSH连接对象
        **kwargs: 传递给operation_func的额外参数

    Returns:
        操作结果列表
    """
    async def process_device(device_id: str) -> Dict:
        try:
            result = await operation_func(device_id, ssh, **kwargs)
            result['device'] = device_id
            result['success'] = True
        except Exception as e:
            logger.error(f"Error processing device {device_id}: {e}")
            result = {'device': device_id, 'success': False, 'error': str(e)}
        return result

    # 并行执行所有设备操作
    tasks = [process_device(device_id) for device_id in devices]
    return await asyncio.gather(*tasks)

async def get_device_properties_optimized(device_id: str, ssh) -> Dict[str, str]:
    """获取设备属性 - 一次SSH调用获取所有属性"""
    cmd = f"""adb -s {device_id} shell "
    getprop ro.boot.verifiedbootstate;
    getprop | grep api_level;
    getprop sys.gmali.version;
    getprop persist.sys.timezone;
    getprop persist.sys.locale;
    cat /proc/meminfo | grep -E 'MemTotal|MemFree';
    cat vendor/etc/fstab.rk30board 2>/dev/null | grep userdata || echo 'N/A'
" """

    stdout, stderr, code = ssh_manager.execute_command(ssh, cmd, timeout=15)
    lines = stdout.strip().split('\n')

    properties = {}
    for line in lines:
        line = line.strip()
        if 'verifiedbootstate' in line or line in ['green', 'orange', 'yellow']:
            properties['boot_state'] = line
        elif 'api_level' in line:
            properties['api_level'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'sys.gmali.version' in line:
            properties['mali_version'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'MemTotal' in line:
            properties['mem_total'] = line.split()[-2] if len(line.split()) > 1 else line
        elif 'MemFree' in line:
            properties['mem_free'] = line.split()[-2] if len(line.split()) > 1 else line
        elif 'persist.sys.timezone' in line:
            properties['timezone'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'persist.sys.locale' in line:
            properties['locale'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'userdata' in line:
            properties['data_partition'] = line.split()[-1] if len(line.split()) > 0 else line

    return properties

@lru_cache(maxsize=128)
def cached_xml_analysis(xml_path: str, mtime: float) -> Dict:
    """带缓存的XML分析结果"""
    from core.report_analyzer import ReportAnalyzer
    return ReportAnalyzer().analyze_file(xml_path)

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
            report_info['user'] = parse_client_id(client_id)[0]

        # 解析XML获取测试结果统计（使用缓存）
        if os.path.exists(xml_path):
            try:
                stat = os.stat(xml_path)
                result = cached_xml_analysis(xml_path, stat.st_mtime)
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
    client_ip = get_client_ip(request)

    # 优先从配置文件读取client_hosts映射
    config = config_manager.load_config()
    client_hosts = config.get('client_hosts', {})

    # 如果client_hosts中有该IP的映射，使用映射的用户名
    if client_ip in client_hosts:
        username = client_hosts[client_ip]
    else:
        # 其次从请求头获取用户名
        username = request.headers.get('X-Client-Username')
        if username and request.headers.get('X-Client-Username-Encoding') == 'percent':
            username = urllib.parse.unquote(username)
        if not username or username == 'unknown':
            username = 'unknown'

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
            for device_id in device_ids:
                if device_id in all_locks:
                    lock_info = all_locks[device_id]
                    locked_by = lock_info['client_id']
                    device_updates.append({
                        'device_id': device_id,
                        'locked': True,
                        'locked_by': locked_by,
                        'locked_at': lock_info['timestamp']
                    })
                else:
                    device_updates.append({
                        'device_id': device_id,
                        'locked': False
                    })
        else:
            # 更新所有锁定的设备
            for device_id, lock_info in all_locks.items():
                locked_by = lock_info['client_id']
                device_updates.append({
                    'device_id': device_id,
                    'locked': True,
                    'locked_by': locked_by,
                    'locked_at': lock_info['timestamp']
                })

        # 广播到所有连接的客户端
        for client_id, ws in global_state.websocket_connections.items():
            try:
                await ws.send_json({
                    'type': 'device_lock_update',
                    'devices': device_updates
                })
            except Exception as e:
                logger.warning(f"[Broadcast Device Lock] 发送到客户端 {client_id} 失败: {e}")

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
    test_type: str = ""  # 改为空字符串，后面会自动检测
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

class USBIPDisconnectRequest(BaseModel):
    """USB/IP断开请求"""
    device_host: Optional[str] = None

class VPNConnectRequest(BaseModel):
    """VPN 连接请求（所有字段可选，兼容前端无参数调用）"""
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


class NotificationCreateRequest(BaseModel):
    """前端主动创建通知请求"""
    title: str = Field(..., max_length=120)
    message: str = Field(default="", max_length=600)
    level: str = Field(default="info", max_length=20)
    category: str = Field(default="system", max_length=50)
    data: Optional[Dict[str, Any]] = None


class NotificationReadRequest(BaseModel):
    """通知已读请求；ids 为空时表示全部标记已读。"""
    ids: Optional[List[str]] = None


class SecurityPageViewRequest(BaseModel):
    """前端页面访问审计请求"""
    page: str = Field(..., max_length=80)
    title: Optional[str] = Field(default="", max_length=160)
    hash: Optional[str] = Field(default="", max_length=160)

# ==================== 基础端点 ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板"""
    config = config_manager.load_config()

    response = templates.TemplateResponse(
        "index_fastapi.html",
        {
            "request": request,
            "config": config
        }
    )
    # HTML页面不缓存（确保用户获取最新版本）
    response.headers["Cache-Control"] = "no-cache"
    return response

@app.get("/api/system/health")
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


@app.get("/api/notifications")
@handle_api_errors
async def get_notifications(request: Request, limit: int = Query(100, ge=1, le=200)):
    """获取当前客户端通知列表。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(list_client_notifications(client_id, limit))


@app.post("/api/notifications")
@handle_api_errors
async def create_notification(req: NotificationCreateRequest, request: Request):
    """创建当前客户端本地通知，用于前端检测到的状态变化。"""
    client_id = get_client_id_from_request(request)
    record = store_notification(
        client_id,
        req.title,
        req.message,
        req.level,
        req.category,
        req.data
    )
    return ApiResponse.success({'notification': record})


@app.post("/api/notifications/mark-read")
@handle_api_errors
async def mark_notifications_read(req: NotificationReadRequest, request: Request):
    """将当前客户端通知标记为已读；未传 ids 时标记全部。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(mark_client_notifications_read(client_id, req.ids))


@app.post("/api/notifications/clear")
@handle_api_errors
async def clear_notifications(request: Request):
    """清空当前客户端通知。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(clear_client_notifications(client_id))


@app.post("/api/security-audit/page-view")
@handle_api_errors
async def record_security_page_view(req: SecurityPageViewRequest, request: Request):
    """记录前端子页面访问，用于补齐 hash 路由无法被后端直接感知的问题。"""
    if req.page in AUDIT_PAGE_VIEW_SKIP_PAGES:
        return ApiResponse.success({'skipped': True})

    client_id = get_client_id_from_request(request)
    username, client_ip = parse_client_id(client_id)
    record = security_audit_logger.log_event({
        'action_type': 'page_view',
        'source': 'web',
        'operation': f"访问页面 {req.page}",
        'page': req.page,
        'title': req.title or '',
        'hash': req.hash or '',
        'method': request.method,
        'path': '/#' + req.page,
        'status_code': 200,
        'duration_ms': 0,
        'client_ip': client_ip,
        'client_id': client_id,
        'username': username,
        'user_agent': request.headers.get('user-agent', '')[:300],
    })
    return ApiResponse.success({'id': record['id']})


@app.get("/api/security-audit/logs")
@handle_api_errors
async def list_security_audit_logs(
    limit: int = Query(200, ge=1, le=1000),
    source: Optional[str] = Query(None),
    action_type: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=120)
):
    """查询安全审计记录。"""
    if source and source not in {'web', 'cli'}:
        return ApiResponse.error("source 参数无效", status_code=400)
    if action_type and action_type not in {'api', 'page_view', 'page_visit'}:
        return ApiResponse.error("action_type 参数无效", status_code=400)
    result = await asyncio.to_thread(
        security_audit_logger.read_events,
        limit,
        source,
        action_type,
        q
    )
    return ApiResponse.success(result)


def get_related_logs_for_audit(record: Dict[str, Any], limit: int = 80) -> Dict[str, Any]:
    client_id = record.get('client_id') or ''
    related = {
        'client_id': client_id,
        'recent_client_logs': [],
        'saved_log_file': '',
        'saved_log_tail': [],
    }
    if not client_id:
        return related

    with global_state.test_logs_lock:
        related['recent_client_logs'] = list(global_state.test_logs.get(client_id, []))[-limit:]
        saved_log_file = global_state.last_saved_log_file.get(client_id, '')

    if saved_log_file and os.path.exists(saved_log_file):
        related['saved_log_file'] = saved_log_file
        try:
            with open(saved_log_file, 'r', encoding='utf-8', errors='replace') as f:
                related['saved_log_tail'] = list(deque(f, maxlen=limit))
        except Exception as e:
            related['saved_log_tail'] = [f'读取关联日志失败: {e}']

    return related


@app.get("/api/security-audit/detail/{event_id}")
@handle_api_errors
async def get_security_audit_detail(event_id: str):
    """获取单条安全审计详情，包括请求摘要、响应摘要和关联日志。"""
    record = await asyncio.to_thread(security_audit_logger.get_event, event_id)
    if not record:
        return ApiResponse.error("审计记录不存在", status_code=404)

    related_logs = await asyncio.to_thread(get_related_logs_for_audit, record)
    return ApiResponse.success({
        'record': record,
        'related_logs': related_logs,
    })


@app.get("/api/security-audit/export")
@handle_api_errors
async def export_security_audit_logs():
    """导出安全审计 JSONL 文件。"""
    if not os.path.exists(security_audit_logger.log_path):
        return ApiResponse.error("暂无审计记录", status_code=404)
    return FileResponse(
        security_audit_logger.log_path,
        media_type='application/x-ndjson',
        filename='security_audit.json'
    )

@app.get("/templates/architecture.html")
async def get_architecture():
    """获取系统架构图"""
    architecture_file = os.path.join(os.path.dirname(__file__), 'templates', 'architecture.html')
    if os.path.exists(architecture_file):
        with open(architecture_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return HTMLResponse(content=content)
    return JSONResponse(status_code=404, content={"error": "Architecture diagram not found"})

# ==================== 客户端管理 ====================

@app.get("/api/users/current")
async def get_client_info(request: Request):
    """获取客户端信息（返回client_id用于WebSocket连接）"""
    # 使用统一的client_id获取逻辑（优先从client_hosts读取）
    client_id = get_client_id_from_request(request)

    # 确保用户状态存在
    get_or_create_user_state(client_id)

    username, client_ip = parse_client_id(client_id)

    logger.info(f"[ClientInfo] GET - IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "ip": client_ip,
        "client_id": client_id,
        "username": username
    })


def get_client_ip(request: Request, fallback_ip: Optional[str] = None) -> str:
    """提取客户端真实IP地址（支持代理）"""
    if fallback_ip:
        return fallback_ip

    forwarded_for = request.headers.get('X-Forwarded-For', '').strip()
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()

    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        return real_ip

    return request.client.host if request.client else 'unknown'


def parse_client_id(client_id: str) -> tuple[str, str]:
    """解析 username@ip 格式，用户名里含 @ 时也能保留。"""
    if not client_id or '@' not in client_id:
        return client_id or 'unknown', 'unknown'
    username, client_ip = client_id.rsplit('@', 1)
    return username or 'unknown', client_ip or 'unknown'


def get_client_source(ip: str) -> Dict[str, str]:
    """按客户端 IP 判断来源类型。"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private:
            return {'source': 'internal', 'source_label': '内网'}
        return {'source': 'public', 'source_label': '公网'}
    except (ValueError, TypeError):
        return {'source': 'unknown', 'source_label': '未知'}


def is_public_origin_request(request: Optional[Request]) -> bool:
    """Return True when the browser is accessing through a public/ngrok host."""
    if not request:
        return False
    host = (request.headers.get('host') or '').lower()
    forwarded_host = (request.headers.get('x-forwarded-host') or '').lower()
    origin = (request.headers.get('origin') or '').lower()
    combined = ' '.join([host, forwarded_host, origin])
    return any(marker in combined for marker in (
        'ngrok-free.dev',
        'ngrok.app',
        'ngrok.io',
    ))


DEFAULT_ANDROID_USBIP_VID_PIDS = ('2207:0006',)


def parse_usbipd_android_busids(output: str, vid_pid: Optional[str] = None) -> List[str]:
    """从 usbipd list 输出中提取 Android 设备 BUSID。"""
    busids: List[str] = []
    in_connected = False
    vid_pids = {pid.lower() for pid in DEFAULT_ANDROID_USBIP_VID_PIDS}
    if vid_pid:
        vid_pids.add(vid_pid.lower())
    android_markers = (
        'android',
        'adb',
        'rk356',
        'rockchip',
    )

    for line in (output or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('Connected:'):
            in_connected = True
            continue
        if stripped.startswith('Persisted:'):
            break
        if not in_connected:
            continue

        lowered = stripped.lower()
        if any(pid in lowered for pid in vid_pids) or any(marker in lowered for marker in android_markers):
            parts = stripped.split()
            if parts and '-' in parts[0]:
                busids.append(parts[0])

    return busids


def parse_usbipd_busid_statuses(output: str) -> Dict[str, str]:
    """Parse usbipd list Connected section into BUSID -> status."""
    statuses: Dict[str, str] = {}
    in_connected = False
    for line in (output or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('Connected:'):
            in_connected = True
            continue
        if stripped.startswith('Persisted:'):
            break
        if not in_connected:
            continue
        parts = stripped.split()
        if parts and '-' in parts[0]:
            lowered = stripped.lower()
            if 'not shared' in lowered:
                statuses[parts[0]] = 'not_shared'
            elif 'attached' in lowered:
                statuses[parts[0]] = 'attached'
            elif 'shared' in lowered:
                statuses[parts[0]] = 'shared'
            else:
                statuses[parts[0]] = 'unknown'
    return statuses


def resolve_public_tunnel_device_host(request: Optional[Request], client_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (device_host, usbip_attach_host) for public-client agent mode."""
    if not public_client_tunnel.is_connected() or not is_public_origin_request(request):
        return None, None
    username, _ = parse_client_id(client_id)
    if not username or username == 'unknown':
        status = public_client_tunnel.get_status()
        username = status.get('username') or 'unknown'
    device_host = public_client_tunnel.get_ssh_device_host(username)
    if not device_host:
        return None, None
    return device_host, public_client_tunnel.get_usbip_attach_host()


def public_client_required_payload(request: Optional[Request]) -> Dict[str, Any]:
    base_url = get_request_base_url(request) if request else ''
    return {
        'public_client_required': True,
        'agent_connected': public_client_tunnel.is_connected(),
        'agent_install_url': f'{base_url}/api/public-client/install.ps1' if base_url else '/api/public-client/install.ps1',
        'error': '公网访问无法直接回连 Windows 客户端。请先在公网 Windows 上运行 GMS 公网客户端 agent，并保持该 PowerShell 窗口打开。',
    }


async def probe_public_client_usbipd() -> Dict[str, Any]:
    result = await public_client_tunnel.run_windows_command('usbipd --version', timeout=15)
    output = (result.get('stdout') or '').strip() or (result.get('stderr') or '').strip()
    return {
        'installed': result.get('returncode', -1) == 0 and bool(output),
        'version': output,
        'raw': result,
    }


async def probe_public_client_admin() -> bool:
    command = 'cmd /c net session >nul 2>&1'
    result = await public_client_tunnel.run_windows_command(command, timeout=15)
    return result.get('returncode', -1) == 0


def detach_ubuntu_usbip_ports(ssh, remote_host: Optional[str] = '127.0.0.1', detach_all: bool = False) -> List[str]:
    """Detach Ubuntu usbip ports that point to the public-client tunnel."""
    detached: List[str] = []
    stdout, stderr, code = usbip_manager.ssh_manager.execute_command(ssh, 'usbip port', timeout=10)
    if code != 0:
        logger.info(f"[USB/IP] usbip port returned {code}: {stderr or stdout}")
        return detached

    current_port: Optional[str] = None
    current_block: List[str] = []
    for line in (stdout or '').splitlines() + ['Port 999999:']:
        port_match = re.match(r'\s*Port\s+(\d+):', line)
        if port_match:
            block_text = '\n'.join(current_block)
            if current_port and (detach_all or (remote_host and remote_host in block_text)):
                detach_out, detach_err, detach_code = usbip_manager.ssh_manager.execute_command(
                    ssh,
                    f'sudo usbip detach -p {current_port}',
                    timeout=15
                )
                logger.info(
                    f"[USB/IP] Detached stale Ubuntu usbip port {current_port}: "
                    f"code={detach_code} out={detach_out} err={detach_err}"
                )
                detached.append(current_port)
            current_port = port_match.group(1)
            current_block = [line]
        elif current_port:
            current_block.append(line)

    if detached:
        time.sleep(2)
    return detached


def wait_for_adb_device_change(ssh, devices_before: set, timeout: int = 45) -> List[str]:
    """Wait until adb sees devices added after usbip attach."""
    deadline = time.time() + timeout
    last_devices = set(devices_before)
    while time.time() < deadline:
        stdout, stderr, code = usbip_manager.ssh_manager.execute_command(ssh, 'adb devices', timeout=10)
        if code == 0:
            devices_now = set(DeviceUtils.parse_adb_devices(stdout))
            last_devices = devices_now
            new_devices = list(devices_now - devices_before)
            if new_devices:
                logger.info(f"[USB/IP] ADB devices appeared via USB/IP: {new_devices}")
                return new_devices
        else:
            logger.info(f"[USB/IP] adb devices while waiting returned {code}: {stderr or stdout}")
        time.sleep(3)

    logger.warning(f"[USB/IP] Timed out waiting for ADB device. before={devices_before}, last={last_devices}")
    return []


def wait_for_adb_serial_ready(ssh, serial_no: str, timeout: int = 30) -> Dict[str, Any]:
    """Wait until a specific ADB serial is in device state and shell responds."""
    quoted_serial = shlex.quote(serial_no)
    deadline = time.time() + timeout
    last_output = ''
    last_error = ''

    usbip_manager.ssh_manager.execute_command(ssh, 'adb start-server', timeout=10)
    while time.time() < deadline:
        state_out, state_err, state_code = usbip_manager.ssh_manager.execute_command(
            ssh,
            f'adb -s {quoted_serial} get-state',
            timeout=8
        )
        state_text = (state_out or state_err or '').strip()
        last_output = state_out or ''
        last_error = state_err or ''

        if state_code == 0 and state_text == 'device':
            shell_out, shell_err, shell_code = usbip_manager.ssh_manager.execute_command(
                ssh,
                f"adb -s {quoted_serial} shell echo ready",
                timeout=10
            )
            last_output = shell_out or ''
            last_error = shell_err or ''
            if shell_code == 0 and 'ready' in shell_out:
                return {'ready': True}

        time.sleep(2)

    devices_out, devices_err, _ = usbip_manager.ssh_manager.execute_command(ssh, 'adb devices', timeout=8)
    return {
        'ready': False,
        'state': (last_output or last_error or '').strip(),
        'devices': (devices_out or devices_err or '').strip(),
    }


def attach_public_usbip_on_ubuntu(config: Dict[str, Any], attach_host: str, bound_busids: List[str]) -> Dict[str, Any]:
    """Run blocking Ubuntu-side USB/IP attach work outside the FastAPI event loop."""
    ubuntu_ssh = usbip_manager.ssh_manager.get_connection(config)
    if not ubuntu_ssh:
        return {'success': False, 'error': '无法连接Ubuntu主机', 'rollback': False}

    try:
        stdout_before, _, _ = usbip_manager.ssh_manager.execute_command(ubuntu_ssh, 'adb devices', timeout=10)
        devices_before = set(DeviceUtils.parse_adb_devices(stdout_before))
        logger.info(f"[USB/IP] Public devices before attach: {devices_before}")

        detach_ubuntu_usbip_ports(ubuntu_ssh, attach_host, detach_all=True)
        usbip_manager._ensure_vhci_driver(ubuntu_ssh)

        attached = []
        attach_errors = []
        for busid in bound_busids:
            cmd = f'sudo usbip attach -r {attach_host} -b {busid}'
            logger.info(f"[USB/IP] Public attaching {busid} from {attach_host}")
            out, err, code = usbip_manager.ssh_manager.execute_command(ubuntu_ssh, cmd, timeout=25)
            if code == 0:
                attached.append(busid)
            else:
                detail = err or out or f'attach command timed out or returned {code}'
                attach_errors.append(f'{busid}: {detail}')
                logger.warning(f"[USB/IP] Public attach returned {code} for {busid}: {detail}")

        device_list = wait_for_adb_device_change(ubuntu_ssh, devices_before, timeout=45)
        if not device_list:
            detach_ubuntu_usbip_ports(ubuntu_ssh, attach_host, detach_all=True)
            detail = '; '.join(attach_errors) if attach_errors else 'ADB 设备未在 45 秒内枚举出来'
            usbip_manager.ssh_manager.return_connection(ubuntu_ssh)
            return {'success': False, 'error': f'USB/IP 连接失败: {detail}', 'rollback': True}

        if not attached:
            attached = bound_busids

        usbip_manager.ssh_manager.return_connection(ubuntu_ssh)
        return {
            'success': True,
            'message': f'✅ 成功连接{len(attached)}个设备: {", ".join(attached)}',
            'devices': attached,
            'device_list': device_list,
        }
    except Exception as e:
        ubuntu_ssh.close()
        logger.exception("Error in public-client Ubuntu attach")
        return {'success': False, 'error': str(e) or repr(e), 'rollback': True}


def is_manual_username_fallback_error(error: Optional[str]) -> bool:
    """判断用户名识别失败是否属于可手动保存的网络不可达场景。"""
    if not error:
        return True
    error_lower = error.lower()
    return any(keyword in error_lower for keyword in (
        'network is unreachable',
        'no route to host',
        'connection refused',
        'timed out',
        'timeout',
        '连接超时',
        '连接被拒绝',
        '网络不可达',
        '无法访问',
    ))


@app.post("/api/users/detect")
async def detect_client(req: ClientInfoRequest, request: Request):
    """自动检测客户端用户名"""
    client_ip = get_client_ip(request, req.ip)

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
        manual_allowed = (
            not req.username
            or not req.password
            or is_manual_username_fallback_error(error)
        )
        return JSONResponse(content={
            "success": False,
            "error": error,
            "manual_allowed": manual_allowed
        }, status_code=200 if manual_allowed else 401)

@app.post("/api/users/set-username")
async def set_client_username(req: ClientInfoRequest, request: Request):
    """手动设置客户端用户名（不需要SSH密码）"""
    client_ip = get_client_ip(request, req.ip)
    username = req.username

    if not username or username == 'unknown':
        return JSONResponse(content={
            "success": False,
            "error": "用户名不能为空或unknown"
        }, status_code=400)

    # 加载现有动态配置
    existing_dynamic = config_manager._load_dynamic_config() or {}
    client_hosts = existing_dynamic.get('client_hosts', {})
    client_hosts[client_ip] = username

    # 只保存客户端相关配置
    dynamic_config = config_manager.prepare_client_config({'client_hosts': client_hosts})

    # 保存到配置文件
    if config_manager.save_dynamic_config(dynamic_config):
        # 更新内存中的映射
        client_manager.client_hosts = client_hosts

        # 同时更新 global_state.user_states 中的用户名
        old_client_id = f"unknown@{client_ip}"
        new_client_id = f"{username}@{client_ip}"

        with global_state.user_states_lock:
            # 如果存在 unknown@IP 的记录，更新为新用户名
            if old_client_id in global_state.user_states:
                old_state = global_state.user_states.pop(old_client_id)
                old_state['client_username'] = username
                global_state.user_states[new_client_id] = old_state
            # 或者更新已存在的 client_id 的用户名
            elif client_ip in [parse_client_id(k)[1] for k in global_state.user_states.keys()]:
                for key in list(global_state.user_states.keys()):
                    if key.endswith(f"@{client_ip}"):
                        global_state.user_states[key]['client_username'] = username
                        # 如果需要，也可以更新 client_id
                        if key != new_client_id:
                            state = global_state.user_states.pop(key)
                            global_state.user_states[new_client_id] = state
                        break

        logger.info(f"[Set Username] {client_ip} -> {username}")

        return JSONResponse(content={
            "success": True,
            "username": username,
            "ip": client_ip,
            "client_id": new_client_id
        })
    else:
        return JSONResponse(content={
            "success": False,
            "error": "保存配置失败"
        }, status_code=500)

@app.get("/api/users/list")
@handle_api_errors
async def list_users():
    """获取所有在线用户列表"""
    users = []
    now = datetime.now()

    # 本地地址列表，不显示在用户列表中
    local_addresses = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}

    # VPN网关地址列表（不显示在用户列表中）
    vpn_gateway_addresses = {'10.10.10.1'}

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
                except (ValueError, TypeError):
                    continue

            username_from_id, ip = parse_client_id(client_id)

            # 优先使用state中存储的username（更准确）
            username = state.get('client_username', username_from_id)
            if username == 'unknown':
                username = username_from_id

            # 过滤本地地址和VPN网关地址
            if ip in local_addresses or ip in vpn_gateway_addresses:
                continue
            # 过滤unknown用户（用户名识别失败的情况）

            # 如果同一个IP有多个用户记录，优先保留非unknown的用户
            if ip in temp_users:
                existing_user = temp_users[ip]
                if existing_user['username'] == 'unknown' and username != 'unknown':
                    # 用真实用户替换unknown用户
                    temp_users[ip] = {
                        'client_id': client_id,
                        'username': username,
                        'ip': ip,
                        **get_client_source(ip),
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
                    **get_client_source(ip),
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

# ==================== 辅助函数 ====================

def hide_sensitive_info(config: dict) -> dict:
    """隐藏配置中的敏感信息"""
    sensitive_fields = ['password', 'pswd', 'api_key', 'secret', 'token', 'private_key']

    if not isinstance(config, dict):
        return config

    safe_config = {}
    for key, value in config.items():
        # 检查是否是敏感字段
        is_sensitive = any(sensitive in key.lower() for sensitive in sensitive_fields)

        if is_sensitive and isinstance(value, str) and value:
            # 保留前4个字符，其余用*替代
            if len(value) > 4:
                safe_config[key] = value[:4] + '*' * (len(value) - 4)
            else:
                safe_config[key] = '****'
        elif isinstance(value, dict):
            # 递归处理嵌套字典
            safe_config[key] = hide_sensitive_info(value)
        elif isinstance(value, list):
            # 处理列表中的字典项
            safe_list = []
            for item in value:
                if isinstance(item, dict):
                    safe_list.append(hide_sensitive_info(item))
                else:
                    safe_list.append(item)
            safe_config[key] = safe_list
        else:
            safe_config[key] = value

    return safe_config

# ==================== 工具数据管理 ====================
TOOLS_DATA_FILE = os.path.join(os.path.dirname(__file__), 'data', 'user_tools_data.json')

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

@app.post("/api/tools/save")
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

@app.get("/api/tools/load")
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

@app.post("/api/tools/sync")
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

@app.get("/api/favicon/fetch")
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
        icon_result = await fetcher.fetch_icon_async(url)

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

@app.get("/api/favicon/proxy")
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

@app.post("/api/favicon/batch")
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
        results = await fetcher.batch_fetch_icons_async(urls)
        successful = sum(1 for r in results if r['success'])

        return success_response({
            'results': results,
            'total': len(urls),
            'successful': successful,
            'failed': len(results) - successful
        })
    finally:
        await fetcher.close()

# ==================== 配置管理 ====================

@app.get("/api/config/read")
async def get_config(request: Request):
    """获取配置 - 隐藏敏感信息后返回配置对象"""
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    config = config_manager.load_config()
    # local_server 是客户端回传地址；没有显式动态配置时，按当前请求用户/IP 展示。
    config['local_server'] = get_effective_local_server(client_id)

    # 隐藏敏感信息
    safe_config = hide_sensitive_info(config.copy())
    return JSONResponse(content=safe_config)

@app.get("/api/config/opengrok")
async def get_opengrok_config(request: Request):
    """获取OpenGrok配置 - 供前端源码链接使用"""
    config = config_manager.load_config()
    opengrok_config = config.get('opengrok', {})

    if not opengrok_config or 'base_url' not in opengrok_config:
        return error_response('OpenGrok未配置，请在configs/config.json中配置opengrok段', status_code=404)

    return JSONResponse(content={'success': True, 'data': opengrok_config})

@app.get("/api/config/ai")
async def get_ai_config(request: Request):
    """获取 AI 配置 - 供前端 AI 分析功能使用"""
    ai_config = config_manager.get_ai_config()

    if not ai_config:
        return error_response('AI 未配置或未启用，请在 configs/config.json 中配置 ai_models 段并设置 enabled: true', status_code=404)

    return JSONResponse(content={'success': True, 'data': hide_sensitive_info(ai_config.copy())})


@app.get("/api/ngrok/public-url")
async def get_ngrok_public_url(request: Request):
    """获取 ngrok 公网访问地址"""
    try:
        public_url = await asyncio.to_thread(_get_ngrok_public_url)
    except Exception as e:
        return error_response(f'无法获取 ngrok 信息：{str(e)}', status_code=503)

    if public_url:
        return JSONResponse(content={'success': True, 'public_url': public_url})

    return error_response('没有找到有效的 ngrok 公网地址', status_code=404)


def get_request_base_url(request: Request) -> str:
    scheme = request.headers.get('x-forwarded-proto') or request.url.scheme
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or request.url.netloc
    return f"{scheme}://{host}".rstrip('/')


@app.get("/api/public-client/status")
async def get_public_client_status():
    """返回公网客户端 agent / 反向隧道状态。"""
    return JSONResponse(content={'success': True, 'data': public_client_tunnel.get_status()})


@app.get("/api/public-client/agent.py")
async def download_public_client_agent():
    """下载公网 Windows 客户端 agent。"""
    agent_path = os.path.join(os.path.dirname(__file__), 'scripts', 'public_client_agent.py')
    return FileResponse(agent_path, media_type='text/x-python', filename='gms-public-client-agent.py')


@app.get("/api/public-client/install.ps1")
async def download_public_client_installer(request: Request, username: str = Query('', description='Windows SSH username')):
    """下载一键启动公网客户端 agent 的 PowerShell 脚本。"""
    base_url = get_request_base_url(request)
    username_arg = f' --username "{username}"' if username else ''
    script = f'''$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Server = "{base_url}"
$Agent = Join-Path $env:TEMP "gms-public-client-agent.py"

cmd /c net session > $null 2>&1
if ($LASTEXITCODE -ne 0) {{
    Write-Host "[GMS] Administrator privileges are required for usbipd bind. Relaunching as administrator ..."
    Start-Process powershell -Verb RunAs -ArgumentList @(
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`""
    )
    exit
}}

Get-CimInstance Win32_Process |
    Where-Object {{ $_.CommandLine -like "*gms-public-client-agent.py*" -and $_.ProcessId -ne $PID }} |
    ForEach-Object {{
        Write-Host ("[GMS] Stopping old agent process PID {{0}}" -f $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }}

Write-Host "[GMS] Downloading public client agent from $Server ..."
$Headers = @{{"ngrok-skip-browser-warning" = "true"}}
Invoke-WebRequest "$Server/api/public-client/agent.py" -Headers $Headers -OutFile $Agent

function Get-PythonCommand {{
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {{
        return [pscustomobject]@{{ Command = "py"; Args = @("-3", "-u") }}
    }}
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and $python.Source -and $python.Source -notmatch 'WindowsApps') {{
        return [pscustomobject]@{{ Command = $python.Source; Args = @("-u") }}
    }}
    $python3 = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3 -and $python3.Source -and $python3.Source -notmatch 'WindowsApps') {{
        return [pscustomobject]@{{ Command = $python3.Source; Args = @("-u") }}
    }}
    return $null
}}

$PythonCmd = Get-PythonCommand
if (-not $PythonCmd) {{
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {{
        throw "Python is not installed and winget is unavailable. Install Python 3.12, then rerun this script."
    }}
    Write-Host "[GMS] Python not found. Installing Python 3.12 with winget ..."
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $PythonCmd = Get-PythonCommand
}}

if (-not $PythonCmd) {{
    $Candidates = @(
        "$env:LocalAppData\\Programs\\Python\\Python312\\python.exe",
        "$env:ProgramFiles\\Python312\\python.exe",
        "${{env:ProgramFiles(x86)}}\\Python312\\python.exe"
    )
    foreach ($Candidate in $Candidates) {{
        if (Test-Path $Candidate) {{
            $PythonCmd = [pscustomobject]@{{ Command = $Candidate; Args = @("-u") }}
            break
        }}
    }}
}}

if (-not $PythonCmd) {{
    throw "Python was installed but was not found in PATH. Reopen PowerShell and rerun this script."
}}

if ($PythonCmd.Command -match 'WindowsApps') {{
    throw "Detected Windows Store python alias. Please install Python 3.12 from python.org or use py.exe, then rerun this script."
}}

Write-Host "[GMS] Starting agent. Keep this PowerShell window open."
Write-Host ("[GMS] Python command: {{0}}" -f $PythonCmd.Command)
& $PythonCmd.Command @($PythonCmd.Args) $Agent --server $Server{username_arg}
'''
    return PlainTextResponse(script, media_type='text/plain; charset=utf-8')


@app.websocket("/api/public-client/tunnel")
async def public_client_tunnel_endpoint(
    websocket: WebSocket,
    client_id: str = Query('public-client'),
    username: str = Query('unknown')
):
    """公网 Windows agent 反向 TCP 隧道。"""
    await public_client_tunnel.handle_agent(websocket, client_id, username)


ngrok_start_lock = asyncio.Lock()


def _get_ngrok_api_url() -> str:
    return os.environ.get("GMS_NGROK_API_URL", "http://127.0.0.1:4040").rstrip("/")


def _get_ngrok_public_url(timeout: int = 5) -> Optional[str]:
    """Read active ngrok public URL from the local ngrok API."""
    ngrok_api_url = _get_ngrok_api_url()

    with urllib.request.urlopen(f"{ngrok_api_url}/api/tunnels", timeout=timeout) as response:
        data = json.loads(response.read().decode())

    tunnels = data.get("tunnels") or []
    if not tunnels:
        return None

    # 优先查找 https 隧道，且指向本地 5001 端口的
    preferred = []
    fallback = []

    for tunnel in tunnels:
        public_url = tunnel.get("public_url") or ""
        if not public_url:
            continue
        config = tunnel.get("config") or {}
        addr = str(config.get("addr") or "")
        # 检查是否指向 5001 端口（GMS 服务端口）
        if addr.endswith(":5001") or ":5001" in addr:
            preferred.append(public_url)
        else:
            fallback.append(public_url)

    # 优先返回 https 地址
    candidates = preferred or fallback
    for url in candidates:
        if url.startswith("https://"):
            return url

    if candidates:
        return candidates[0]

    return None


def _run_setup_ngrok_script(timeout: int) -> subprocess.CompletedProcess:
    script_path = os.path.join(os.path.dirname(__file__), "scripts", "setup_ngrok.sh")
    if not os.path.exists(script_path):
        raise FileNotFoundError(f"setup_ngrok.sh not found: {script_path}")

    return subprocess.run(
        ["bash", script_path],
        cwd=os.path.dirname(__file__),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy()
    )


def _trim_process_output(text: str, limit: int = 4000) -> str:
    if not text:
        return ""
    return text[-limit:]


@app.post("/api/ngrok/ensure-public-url")
async def ensure_ngrok_public_url(request: Request):
    """确保 ngrok 正在运行，必要时启动脚本并返回公网访问地址。"""
    try:
        public_url = await asyncio.to_thread(_get_ngrok_public_url, 3)
        if public_url:
            return JSONResponse(content={
                'success': True,
                'public_url': public_url,
                'started': False
            })
    except Exception as e:
        logger.info(f"[ngrok] Local API not ready, will try to start ngrok: {e}")

    async with ngrok_start_lock:
        try:
            public_url = await asyncio.to_thread(_get_ngrok_public_url, 3)
            if public_url:
                return JSONResponse(content={
                    'success': True,
                    'public_url': public_url,
                    'started': False
                })
        except Exception:
            pass

        timeout = int(os.environ.get("GMS_NGROK_START_TIMEOUT", "45"))
        try:
            result = await asyncio.to_thread(_run_setup_ngrok_script, timeout)
        except subprocess.TimeoutExpired as e:
            return error_response(
                f'ngrok 启动超时（>{timeout}s）',
                status_code=504,
                detail={
                    'stdout': _trim_process_output(e.stdout if isinstance(e.stdout, str) else ''),
                    'stderr': _trim_process_output(e.stderr if isinstance(e.stderr, str) else '')
                }
            )
        except Exception as e:
            return error_response(f'执行 setup_ngrok.sh 失败：{str(e)}', status_code=500)

        if result.returncode != 0:
            return error_response(
                'setup_ngrok.sh 执行失败',
                status_code=503,
                detail={
                    'returncode': result.returncode,
                    'stdout': _trim_process_output(result.stdout),
                    'stderr': _trim_process_output(result.stderr)
                }
            )

        try:
            public_url = await asyncio.to_thread(_get_ngrok_public_url, 5)
        except Exception as e:
            return error_response(
                f'ngrok 已执行启动脚本，但无法读取公网地址：{str(e)}',
                status_code=503,
                detail={
                    'stdout': _trim_process_output(result.stdout),
                    'stderr': _trim_process_output(result.stderr)
                }
            )

        if not public_url:
            return error_response(
                'ngrok 已执行启动脚本，但没有找到有效的公网地址',
                status_code=503,
                detail={
                    'stdout': _trim_process_output(result.stdout),
                    'stderr': _trim_process_output(result.stderr)
                }
            )

        return JSONResponse(content={
            'success': True,
            'public_url': public_url,
            'started': True
        })


@app.post("/api/config/update")
async def update_config(req: dict):
    """更新配置 - 只修改动态配置，禁止修改config.json"""
    existing_dynamic = config_manager._load_dynamic_config() or {}

    # 动态配置字段（保存在 config_dynamic.json）
    # 只允许保存运行时动态配置
    # 注意：client_ip 和 client_username 是运行时状态，不应保存到配置文件
    dynamic_keys = {
        'client_hosts', 'client_ssh_credentials', 'local_server', 'sidebar_order'
    }

    # 检查是否有不允许修改的字段
    invalid_fields = set(req.keys()) - dynamic_keys
    if invalid_fields:
        raise HTTPException(
            status_code=400,
            detail=f"不允许修改以下字段: {', '.join(invalid_fields)}. 可修改的字段: {', '.join(dynamic_keys)}"
        )

    # 合并现有配置和请求配置（单次遍历）
    dynamic_updates = {
        k: req.get(k, existing_dynamic.get(k))
        for k in dynamic_keys
        if k in existing_dynamic or k in req
    }

    # 只保存客户端相关的动态配置
    if config_manager.save_dynamic_config(dynamic_updates):
        return JSONResponse(content={'success': True})
    else:
        raise HTTPException(status_code=500, detail="保存配置失败")


def normalize_sidebar_order(raw_order: Any) -> List[str]:
    """校验并去重侧边栏排序。"""
    if not isinstance(raw_order, list):
        raise HTTPException(status_code=400, detail="order 必须是数组")

    order = []
    seen = set()
    for item in raw_order:
        if not isinstance(item, str):
            continue
        page = item.strip()
        if page and page not in seen:
            order.append(page)
            seen.add(page)

    if not order:
        raise HTTPException(status_code=400, detail="order 不能为空")
    return order


@app.get("/api/sidebar-order")
async def get_sidebar_order():
    """获取侧边栏导航顺序。"""
    existing_dynamic = config_manager._load_dynamic_config() or {}
    order = existing_dynamic.get('sidebar_order', [])
    if not isinstance(order, list):
        order = []
    return JSONResponse(content={'success': True, 'order': order})


@app.post("/api/sidebar-order")
async def save_sidebar_order(req: dict = Body(default={})):
    """保存侧边栏导航顺序。"""
    order = normalize_sidebar_order(req.get('order'))
    existing_dynamic = config_manager._load_dynamic_config() or {}
    existing_dynamic['sidebar_order'] = order

    if config_manager.save_dynamic_config(existing_dynamic):
        return JSONResponse(content={'success': True, 'order': order})
    raise HTTPException(status_code=500, detail="保存侧边栏排序失败")

# ==================== 设备管理 ====================

@app.get("/api/devices/list")
@handle_api_errors
async def get_connected_devices(
    request: Request,
    help: bool = Query(False),
    force_refresh: bool = Query(False)
):
    """获取所有已连接的设备列表（与adb devices相同）"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("GET", "/api/devices/list")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 获取设备列表 - 与Flask一致，直接返回数组
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    # 先刷新设备列表（需要最新状态来清理记录）
    devices = await asyncio.to_thread(device_manager.get_connected_devices, force_refresh)

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
    if not force_refresh and now - global_state.device_cache['timestamp'] < DEVICE_CACHE_TTL:
        cached_devices = global_state.device_cache['devices']
        cached_device_set = {
            item.get('device_id')
            for item in cached_devices
            if isinstance(item, dict) and item.get('device_id')
        }
        if cached_device_set == current_device_set:
            return JSONResponse(content=cached_devices)

    devices_with_status = []

    for device_id in devices:
        device_info = {
            'device_id': device_id,
            'status': 'online',
            'locked': False
        }

        # 检查锁定状态
        client_ip = get_client_ip(request)
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

    # 更新缓存（使用专用锁确保原子性）
    with global_state.device_cache_lock:
        global_state.device_cache = {
            'devices': devices_with_status,
            'timestamp': now
        }

    # 直接返回数组（与Flask一致）
    return JSONResponse(content=devices_with_status)

async def _manage_bootloader_lock(devices: List[str], action: str) -> JSONResponse:
    """
    通用的 bootloader 锁定/解锁处理函数

    Args:
        devices: 设备ID列表
        action: 操作类型 ("lock" 或 "unlock")

    Returns:
        JSONResponse with operation results
    """
    try:
        if not devices:
            return ApiResponse.error("未选择设备", status_code=400)

        # 验证设备 ID 格式，防止命令注入（只允许字母、数字、横杠、冒号、点）
        valid_device_pattern = re.compile(r'^[a-zA-Z0-9.:-]+$')
        for device_id in devices:
            if not valid_device_pattern.match(device_id):
                return ApiResponse.error(f"无效的设备 ID 格式：{device_id}", status_code=400)

        config = config_manager.load_config()

        with ssh_manager.connection(config) as ssh:
            results = []

            # 本地脚本路径
            local_script = os.path.join(os.path.dirname(__file__), 'scripts', 'run_Device_Lock.sh')
            # 远程脚本路径
            remote_script = os.path.join(get_default_suites_path(config), 'run_Device_Lock.sh')

            # 检查本地脚本是否存在
            if not os.path.exists(local_script):
                return ApiResponse.error(f'脚本文件不存在: {local_script}', status_code=404)

            # 上传脚本到远程服务器
            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                # 设置执行权限
                ssh_manager.execute_command(ssh, f"chmod +x '{remote_script}'")
            except Exception as e:
                return ApiResponse.error(f'上传脚本失败: {str(e)}', status_code=500)

            # 对每个设备执行锁定/解锁操作
            for device_id in devices:
                try:
                    # 执行脚本，传递 action 参数
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
                    await asyncio.sleep(2)

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

            # 计算统计信息
            success_count = sum(1 for r in results if r.get('success', False))
            failed_count = len(results) - success_count

            response_data = {
                'results': results,
                'summary': {
                    'total': len(results),
                    'success': success_count,
                    'failed': failed_count
                }
            }

            action_text = "锁定" if action == "lock" else "解锁"
            return ApiResponse.success(response_data, f'设备{action_text}操作完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error managing device lock: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/bootloader-lock")
async def lock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None)
):
    """锁定设备Bootloader"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/devices/bootloader-lock")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 兼容两种请求格式：单设备（device_id）和批量（devices）
    devices = req.devices if req.devices else []
    if req.device_id:
        devices = [req.device_id]

    # 调用通用函数，执行锁定操作
    return await _manage_bootloader_lock(devices, "lock")

@app.post("/api/devices/bootloader-unlock")
async def unlock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None)
):
    """解锁设备Bootloader"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/devices/bootloader-unlock")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 兼容两种请求格式：单设备（device_id）和批量（devices）
    devices = req.devices if req.devices else []
    if req.device_id:
        devices = [req.device_id]

    # 调用通用函数，执行解锁操作
    return await _manage_bootloader_lock(devices, "unlock")

@app.post("/api/devices/bootloader-status")
async def check_bootloader_status(req: DeviceActionRequest):
    """检查设备Bootloader锁状态（GREEN=锁定, ORANGE=未锁定）"""
    try:
        with SSHConnection() as ssh:
            # 并行检查所有设备的锁定状态
            async def check_single_device(device_id: str) -> Dict:
                # Check verified boot state (GREEN = locked, ORANGE = unlocked)
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate"
                )
                state = output.strip()

                # 根据状态判断是否锁定（使用枚举）
                try:
                    boot_state = VerifiedBootState(state)
                    is_locked = boot_state.is_locked
                    status_text = boot_state.display_text
                except ValueError:
                    is_locked = False
                    status_text = f'未知状态 ({state})'

                return {
                    'device': device_id,
                    'locked': is_locked,
                    'state': state,
                    'status': status_text
                }

            results = await asyncio.gather(*[check_single_device(d) for d in req.devices])

            return ApiResponse.success({'results': results}, '锁定状态检查完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking lock status: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest):
    """获取设备详细信息"""
    try:
        with SSHConnection() as ssh:
            # 并行获取所有设备信息
            async def get_single_device_info(device_id: str) -> Dict:
                device_info = {'device': device_id, 'properties': {}}

                # 使用device_manager获取基本信息
                base_info = device_manager.get_device_info(device_id, ssh)

                # 添加base_info中的字段
                field_mapping = {
                    'serial_no': '设备序列号',
                    'model': '设备型号',
                    'android_version': 'Android版本',
                    'fingerprint': '系统指纹',
                    'build_type': '编译类型',
                    'build_tags': '编译标签',
                    'build_date': '编译时间',
                    'sdk_version': 'SDK版本',
                    'security_patch': '安全补丁'
                }

                for key, label in field_mapping.items():
                    if key in base_info:
                        device_info['properties'][label] = base_info[key]

                # 一次SSH调用获取所有额外属性
                extra_props = await get_device_properties_optimized(device_id, ssh)

                # 映射属性到中文标签
                prop_mapping = {
                    'boot_state': ('启动状态', lambda x: x if x else '未知'),
                    'api_level': ('API级别', lambda x: x.split('[')[-1].replace(']', '') if '[' in x else (x or '未知')),
                    'mali_version': ('Mali库版本', lambda x: x or '未知'),
                    'mem_total': ('总内存', lambda x: f"{x} KB" if x else '未知'),
                    'mem_free': ('可用内存', lambda x: f"{x} KB" if x else '未知'),
                    'timezone': ('时区', lambda x: x or '未知'),
                    'locale': ('语言', lambda x: x or '未知'),
                    'data_partition': ('DATA分区', lambda x: x.split()[-1] if x and 'userdata' in x else '未知')
                }

                for key, (label, formatter) in prop_mapping.items():
                    if key in extra_props:
                        device_info['properties'][label] = formatter(extra_props[key])

                return device_info

            # 并行执行所有设备的信息获取
            results = await asyncio.gather(*[get_single_device_info(d) for d in req.devices])

            return ApiResponse.success({'results': results}, '设备信息获取完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return ApiResponse.error(str(e), status_code=500)

def run_local_shell_command(command: str, timeout: int = 10) -> Tuple[str, str, int]:
    """Run a shell command on the web_app host and return stdout, stderr, exit code."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout if isinstance(e.stdout, str) else ""
        stderr = e.stderr if isinstance(e.stderr, str) else ""
        return stdout, stderr or f"Command timed out after {timeout}s", 124
    except Exception as e:
        return "", str(e), 1


def build_management_props_command(device_ids: List[str]) -> str:
    commands = []
    for device_id in device_ids:
        device_shell = (
            f'echo "===DEVICE:{device_id}===" && '
            'getprop ro.serialno && '
            'getprop ro.product.model && '
            'getprop ro.build.version.release && '
            'dumpsys battery | grep level | cut -d: -f2 | tr -d " "'
        )
        commands.append(f"adb -s {shlex.quote(device_id)} shell {shlex.quote(device_shell)}")
    return " ; ".join(commands)


def parse_management_device_props(props_output: str) -> Dict[str, Dict[str, str]]:
    device_data: Dict[str, Dict[str, str]] = {}
    current_device = None

    for line in props_output.split('\n'):
        line = line.strip()
        if line.startswith('===DEVICE:'):
            current_device = line.split('===DEVICE:')[1].split('===')[0]
            device_data[current_device] = {
                'serial_no': '',
                'model': '',
                'android_version': '',
                'battery_level': ''
            }
        elif current_device and line:
            if not device_data[current_device]['serial_no']:
                device_data[current_device]['serial_no'] = line
            elif not device_data[current_device]['model']:
                device_data[current_device]['model'] = line
            elif not device_data[current_device]['android_version']:
                device_data[current_device]['android_version'] = line
            elif not device_data[current_device]['battery_level']:
                device_data[current_device]['battery_level'] = line

    return device_data


def build_devices_management_payload(
    device_ids: List[str],
    device_data: Dict[str, Dict[str, str]],
    config: Dict[str, Any]
) -> Dict[str, Any]:
    client_id = client_manager.get_client_id('127.0.0.1')
    locks = device_lock_manager.get_all_locks()
    devices_info = []
    ubuntu_host = config.get("ubuntu_host") or get_ubuntu_host()
    ubuntu_user = config.get("ubuntu_user") or get_ubuntu_user()

    all_usbip_sources = {**global_state.usbip_devices_source, **usbip_manager.device_sources}
    current_device_set = set(device_ids)
    devices_to_remove = [dev_id for dev_id in all_usbip_sources if dev_id not in current_device_set]

    if devices_to_remove:
        logger.info(f"[Device Management] Cleaning up removed devices from memory: {devices_to_remove}")
        with global_state.usbip_devices_source_lock:
            for dev_id in devices_to_remove:
                global_state.usbip_devices_source.pop(dev_id, None)
        for dev_id in devices_to_remove:
            usbip_manager.device_sources.pop(dev_id, None)

    for device_id in device_ids:
        props = device_data.get(device_id, {})
        lock_info = locks.get(device_id, {})

        if device_id in all_usbip_sources:
            source_type = 'usbip'
            source_host = all_usbip_sources.get(device_id, {}).get('source', 'Unknown')
        else:
            source_type = 'local'
            source_host = f'{ubuntu_user}@{ubuntu_host}'

        devices_info.append({
            'device_id': device_id,
            'serial_no': props.get('serial_no', device_id),
            'model': props.get('model', ''),
            'android_version': props.get('android_version', ''),
            'battery_level': props.get('battery_level', ''),
            'source_type': source_type,
            'source_host': source_host,
            'status': 'online',
            'locked_by': lock_info.get('client_id', '') if device_id in locks else '',
            'locked_by_self': (lock_info.get('client_id') == client_id) if device_id in locks else False
        })

    return {'devices': devices_info}


@app.get("/api/devices/management")
async def devices_management():
    """设备管理页面（获取所有设备的详细管理信息）"""
    try:
        config = config_manager.load_config()

        if is_config_host_local(config):
            output, error, code = await asyncio.to_thread(run_local_shell_command, "adb devices", 5)
            if code != 0 and not output:
                logger.warning(f"[Device Management] Local adb devices failed: {error}")
                return JSONResponse(content={'devices': [], 'success': True, 'warning': error})

            device_ids = DeviceUtils.parse_adb_devices(output)
            if not device_ids:
                return JSONResponse(content={'devices': [], 'success': True, 'source': 'local'})

            props_cmd = build_management_props_command(device_ids)
            props_output, props_error, props_code = await asyncio.to_thread(
                run_local_shell_command, props_cmd, 15
            )
            if props_code != 0:
                logger.warning(f"[Device Management] Local device properties failed: {props_error}")

            payload = build_devices_management_payload(
                device_ids,
                parse_management_device_props(props_output),
                config
            )
            payload.update({'success': True, 'source': 'local'})
            return JSONResponse(content=payload)

        with SSHConnection(config) as ssh:
            output, _, _ = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = DeviceUtils.parse_adb_devices(output)

            if not device_ids:
                return JSONResponse(content={'devices': []})

            props_cmd = build_management_props_command(device_ids)
            props_output, _, _ = ssh_manager.execute_command(ssh, props_cmd, timeout=15)

            return JSONResponse(content=build_devices_management_payload(
                device_ids,
                parse_management_device_props(props_output),
                config
            ))

    except Exception as e:
        logger.error(f"Error getting devices management: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/devices/user-locked")
async def list_user_locks():
    """列出所有用户锁定设备（多用户环境下的设备占用状态）"""
    return JSONResponse(content={
        "success": True,
        "data": device_lock_manager.get_all_locks()
    })

@app.post("/api/devices/reboot")
@handle_api_errors
async def reboot_devices(req: DeviceActionRequest):
    """重启设备"""
    with SSHConnection() as ssh:
        # 并行重启所有设备
        async def reboot_single_device(device_id: str) -> Dict:
            result = device_manager.reboot_device(device_id, ssh)
            result['device'] = device_id
            return result

        results = await asyncio.gather(*[reboot_single_device(d) for d in req.devices])
        return ApiResponse.device_results(results, "设备重启")

@app.post("/api/devices/remount")
@handle_api_errors
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount设备"""
    # 获取client_id
    client_id = get_client_id_from_request(request)

    with SSHConnection() as ssh:
        # 并行remount所有设备
        async def remount_single_device(device_id: str) -> Dict:
            # 执行 adb root
            output, error, code = ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} root",
                timeout=15
            )

            # 发送输出到前端
            await safe_websocket_send(client_id, {
                'type': 'log_update',
                'log': f"[{device_id}] adb root: {output.strip()}",
                'log_type': 'info'
            })

            await asyncio.sleep(2)

            # 执行 remount
            output, error, code = ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} remount",
                timeout=15
            )

            # 发送输出到前端
            await safe_websocket_send(client_id, {
                'type': 'log_update',
                'log': f"[{device_id}] adb remount: {output.strip()}",
                'log_type': 'info'
            })

            # 使用device_manager的remount方法获取完整结果
            result = device_manager.remount_device(device_id, ssh)
            result['device'] = device_id
            return result

        results = await asyncio.gather(*[remount_single_device(d) for d in req.devices])
        return ApiResponse.device_results(results, "设备Remount")

@app.post("/api/devices/wifi")
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

@app.post("/api/devices/shell")
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

        # 验证设备是否在线。USB/IP 设备刚枚举出来时 adb shell 可能还没准备好，需要短暂等待。
        ready_result = await asyncio.to_thread(wait_for_adb_serial_ready, ssh, req.serial_no, 30)

        ssh_manager.return_connection(ssh)

        if ready_result.get('ready'):
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
            detail = ready_result.get('state') or ready_result.get('devices') or '无响应'
            logger.warning(f"[Device Shell] Device {req.serial_no} not ready: {detail}")
            return JSONResponse(
                content={"success": False, "message": f"设备 {req.serial_no} 不在线或无响应: {detail}"},
                status_code=400
            )
    except Exception as e:
        logger.error(f"Error opening device shell: {e}")
        return JSONResponse(
            content={"success": False, "message": f"打开Shell失败: {str(e)}"},
            status_code=500
        )

# ==================== OpenGrok源码搜索 ====================

@app.get("/api/opengrok/search")
@handle_api_errors
async def opengrok_search(
    query: str = Query(..., min_length=1, max_length=256, description="搜索关键词"),
    full: bool = Query(False, description="是否全文搜索"),
    max_results: int = Query(5, ge=1, le=20, description="最大结果数")
):
    """在源码中搜索代码，兼容 API 文档和技能入口。"""
    search_query = query.strip()
    if not search_query:
        return ApiResponse.error("搜索关键词不能为空", status_code=400)

    analyzer = ReportAnalyzer()
    results = await asyncio.to_thread(
        analyzer.rk_codesearch,
        search_query,
        None,
        max_results
    )
    return ApiResponse.success({
        "query": search_query,
        "full": full,
        "results": results,
        "count": len(results)
    })


# ==================== 测试管理 ====================

class TestParseArgsRequest(BaseModel):
    """测试参数解析请求 - 用于智能识别命令行参数"""
    params: List[str] = Field(default_factory=list, description="命令行参数列表")

class TestParseArgsResponse(BaseModel):
    """测试参数解析响应"""
    success: bool = True
    device: str = ""
    test_type: str = ""
    test_module: str = ""
    test_case: str = ""
    test_suite: str = ""
    retry_dir: str = ""
    warnings: List[str] = []
    help_text: str = ""

@app.post("/api/test/parse-args")
async def parse_test_args(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TestParseArgsRequest = Body(None)
):
    """解析测试启动参数 - 智能识别命令行参数

    支持两种模式：
    1. 直接测试模式：gms-rt-test-start <DEVICE> [TYPE] [MODULE/SUITE] [CASE/SUITE] [SUITE]
    2. 重试模式：gms-rt-test-start --retry <REPORT_TIMESTAMP> [DEVICE] [TYPE] [SUITE]

    智能识别规则：
    - 包含 '/' 的参数自动识别为路径（test_suite）
    - 其他参数按位置识别为 device, test_type, test_module, test_case
    """
    # 检查是否需要显示帮助
    if help or (req is None):
        help_text = """📖 API: /api/test/parse-args

🔹 功能：智能解析测试启动命令行参数

🔹 直接测试模式参数格式:
  params: ["DEVICE", "TYPE", "MODULE/SUITE", "CASE/SUITE", "SUITE"]

  示例:
  - ["RK3572GMS4", "CTS", "/path/to/android-cts/tools"]
  - ["RK3572GMS4", "CTS", "TestModuleName"]
  - ["RK3572GMS4", "CTS", "TestModuleName", "TestCaseName"]
  - ["RK3572GMS4", "CTS", "TestModuleName", "TestCaseName", "/path/to/suite"]

🔹 重试模式参数格式:
  params: ["--retry", "REPORT_TIMESTAMP", "DEVICE", "TYPE", "SUITE"]

  示例:
  - ["--retry", "2026.04.11_17.27.04.421_2920", "RF8TC2W4JNH", "GTS"]
  - ["--retry", "2026.04.11_17.27.04.421_2920", "RF8TC2W4JNH", "/path/to/suite"]

🔹 Supported Test Types:
  CTS, GTS, GTS-ROOT, STS, VTS, APTS, GSI
"""
        return PlainTextResponse(
            content=help_text,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "public, max-age=300"
            }
        )

    if req is None or not req.params:
        return JSONResponse(
            content={'success': False, 'error': 'Missing params'},
            status_code=400
        )

    params = req.params
    first_param = params[0] if params else ""

    result = {
        "success": True,
        "device": "",
        "test_type": "",
        "test_module": "",
        "test_case": "",
        "test_suite": "",
        "retry_dir": "",
        "warnings": []
    }

    # 重试模式
    if first_param == "--retry":
        if len(params) < 2:
            return JSONResponse(
                content={
                    'success': False,
                    'error': 'Report timestamp required for retry mode'
                },
                status_code=400
            )

        result["retry_dir"] = params[1]
        if len(params) > 2:
            result["device"] = params[2]

        # 处理第三个和第四个参数
        if len(params) > 3:
            third_param = params[3]
            if "/" in third_param:
                result["test_suite"] = third_param
                result["warnings"].append("Test type will be auto-detected from suite path")
            else:
                result["test_type"] = third_param

                # 检查第四个参数
                if len(params) > 4:
                    fourth_param = params[4]
                    if "/" in fourth_param:
                        result["test_suite"] = fourth_param
                    else:
                        result["warnings"].append(f"Fourth parameter ignored (expected suite path, got: {fourth_param})")
        else:
            result["warnings"].append("Neither test type nor suite specified")

        return TestParseArgsResponse(**result)

    # 直接测试模式
    result["device"] = params[0] if len(params) > 0 else ""
    result["test_type"] = params[1] if len(params) > 1 else ""

    # 智能识别参数 3, 4, 5
    param3 = params[2] if len(params) > 2 else ""
    param4 = params[3] if len(params) > 3 else ""
    param5 = params[4] if len(params) > 4 else ""

    # 参数 3: 可能是 test_module 或 test_suite 路径
    if param3:
        if "/" in param3:
            result["test_suite"] = param3
        else:
            result["test_module"] = param3

    # 参数 4: 根据已有参数判断
    if param4:
        if result["test_suite"]:
            # 已有 test_suite，param4 是 test_case
            result["test_case"] = param4
        else:
            # 还没有 test_suite，检查是否是路径
            if "/" in param4:
                result["test_suite"] = param4
            else:
                result["test_case"] = param4

    # 参数 5: 只在没有 test_suite 时检查
    if param5 and not result["test_suite"]:
        if "/" in param5:
            result["test_suite"] = param5
        else:
            if result["test_case"]:
                result["warnings"].append(f"Fifth parameter ignored (unexpected: {param5})")
            else:
                result["test_case"] = param5

    return TestParseArgsResponse(**result)



@app.post("/api/test/start")
async def start_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TestStartRequest = Body(None)
):
    """启动测试 - 与Flask版本逻辑一致（后台执行，立即返回）"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help 或 ?h=1 或 ?help=true）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/test/start")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 如果req为None（显示帮助时没有body），返回错误
    if req is None:
        return JSONResponse(
            content={'success': False, 'error': 'Missing request body'},
            status_code=400
        )

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
        await release_device_locks(client_id, locked_devices, broadcast=False)

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

    # 立即广播设备锁定状态（不等待后台任务）
    if locked_devices:
        logger.info(f"[TestStart] Broadcasting device lock for: {locked_devices}")
        await broadcast_device_lock_update(locked_devices)

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
    async def log_callback(message: str, log_type: Union[LogLevel, str] = LogLevel.INFO):
        # 构建时间戳
        timestamp_str = datetime.now().strftime('%H:%M:%S')

        # 使用与Flask版本一致的字符串格式
        log_str = f"[{timestamp_str}] {message}"

        # 处理log_type参数（兼容枚举和字符串）
        if isinstance(log_type, str):
            log_type_str = log_type
        else:
            log_type_str = log_type.value

        # 保存到全局状态（限制数量，防止内存溢出）
        with global_state.test_logs_lock:
            if client_id not in global_state.test_logs:
                global_state.test_logs[client_id] = deque(maxlen=MAX_LOG_ENTRIES)
            global_state.test_logs[client_id].append({
                'message': message,
                'type': log_type_str,
                'timestamp': datetime.now().isoformat()
            })

        # 保存到用户状态（限制数量，防止内存溢出）
        user_state = get_or_create_user_state(client_id)
        if 'logs' not in user_state:
            user_state['logs'] = deque(maxlen=MAX_LOG_ENTRIES)
        user_state['logs'].append(log_str)

        # 通过WebSocket推送
        await safe_websocket_send(client_id, {
            'type': 'log_update',
            'log': message,
            'log_type': log_type_str
        })

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
            # 释放设备锁
            await release_device_locks(client_id, locked_devices)
            return

        await log_callback("✅ SSH 连接成功", 'success')

        # 上传测试脚本
        local_script = os.path.realpath(
            os.path.join(os.path.dirname(__file__), 'scripts', 'run_GMS_Test_Auto.sh')
        )

        if os.path.exists(local_script):
            suites_path = config.get('suites_path') or get_default_suites_path(config)
            remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

            script_size = os.path.getsize(local_script)
            size_kb = script_size / 1024

            await log_callback(f"📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)", 'info')

            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)

                # 设置可执行权限
                stdin, stdout, stderr = ssh.exec_command(f"chmod +x '{remote_script}'")
                stdout.read()

                await log_callback(f"✅ 上传完成 ({size_kb:.2f}KB)", 'success')
            except Exception as e:
                await log_callback(f"⚠️ 脚本上传失败: {str(e)}", 'warning')
        else:
            await log_callback("⚠️ 本地脚本不存在，使用远程脚本", 'warning')

        # 构建测试命令
        test_type = test_params.get('test_type', '')
        test_module = test_params.get('test_module', '')
        test_case = test_params.get('test_case', '')
        retry_dir = test_params.get('retry_dir', '')
        test_suite = test_params.get('test_suite', '')

        # 统一将 test_type 转换为小写
        test_type_lower = test_type.lower() if test_type else ''

        # 修复：将testcases路径转换为tools路径（因为cts-tradefed在tools目录）
        if test_suite and 'testcases' in test_suite:
            test_suite_tools = test_suite.replace('/testcases', '/tools')
        else:
            test_suite_tools = test_suite

        # 如果没有指定test_type，尝试从test_suite路径中自动检测
        if not test_type_lower and test_suite_tools:
            test_type_lower = detect_test_type_from_suite_path(test_suite_tools)
            if test_type_lower:
                await log_callback(f"🔍 从套件路径检测到测试类型: {test_type_lower}", 'info')

        local_server = get_effective_local_server(
            client_id,
            test_params.get('local_server', '')
        )
        devices = test_params.get('devices', [])

        # 验证测试套件路径（非重试模式下必需）
        if not retry_dir and not test_suite_tools:
            await log_callback("❌ 缺少测试套件路径", 'error')
            await log_callback("💡 请使用 --test-suite 参数指定测试套件路径", 'info')
            await log_callback("💡 或在Web界面中选择测试套件", 'info')
            update_user_state_field(client_id, {'running': False})
            # 释放设备锁
            await release_device_locks(client_id, locked_devices)
            return

        suites_path = config.get('suites_path') or get_default_suites_path(config)
        remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

        # 构建命令参数
        cmd_parts = [remote_script]

        # 添加测试类型
        if retry_dir:
            timestamp = os.path.basename(retry_dir.strip().rstrip('/'))

            # 在重试模式下，如果没有提供test_type，按优先级检测：
            # 1. 从test_suite路径检测（最准确）
            # 2. 从数据库查找原始报告的测试类型
            # 3. 从retry_dir目录名检测
            if not test_type_lower and test_suite_tools:
                await log_callback("🔍 从test_suite路径检测测试类型...", 'info')
                test_type_lower = detect_test_type_from_suite_path(test_suite_tools)
                if test_type_lower:
                    await log_callback(f"✓ 从test_suite检测到测试类型: {test_type_lower}", 'info')

            # 如果仍然没有test_type，尝试从数据库查找
            if not test_type_lower:
                await log_callback(f"🔍 从数据库查找报告 {timestamp} 的测试类型...", 'info')
                try:
                    report = test_report_db.get_report_by_timestamp(timestamp)
                    if report and report.get('test_type'):
                        original_type = report['test_type'].lower()
                        test_type_lower = original_type
                        await log_callback(f"✓ 从报告检测到测试类型: {test_type_lower}", 'info')
                    else:
                        await log_callback(f"⚠️ 数据库中未找到报告 {timestamp}，尝试从目录名检测", 'warning')
                except Exception as e:
                    await log_callback(f"⚠️ 从数据库读取测试类型失败: {e}，尝试从目录名检测", 'warning')

            # 如果仍然没有test_type，尝试从retry_dir目录名检测
            if not test_type_lower and retry_dir:
                await log_callback("🔍 从目录名检测测试类型...", 'info')
                test_type_lower = detect_test_type_from_dir_path(retry_dir)
                if test_type_lower:
                    await log_callback(f"✓ 从路径检测到{test_type_lower.upper()}测试", 'info')

            # 如果仍然没有test_type，置空（让脚本自动检测或报错）
            if not test_type_lower:
                test_type_lower = ''
                await log_callback("⚠️ 未检测到测试类型，将由脚本自动检测", 'warning')

            cmd_parts.extend([test_type_lower, "retry", timestamp])
            await log_callback(f"🔄 Retry模式: test_type={test_type_lower or '(自动检测)'}, timestamp={timestamp}", 'info')

            # 在重试模式下，如果没有提供test_suite，且已知test_type，自动查找对应的测试套件
            if not test_suite_tools and test_type_lower:
                await log_callback(f"🔍 自动查找 {test_type_lower.upper()} 测试套件...", 'info')
                try:
                    # 在 suites_path 中查找对应的测试套件
                    import glob
                    # 查找匹配的套件目录，如 android-gts-*
                    suite_pattern = os.path.join(suites_path, f'android-{test_type_lower}-*')
                    # 只获取目录，排除文件（如zip文件）
                    suite_dirs = [d for d in glob.glob(suite_pattern) if os.path.isdir(d)]

                    if suite_dirs:
                        # 使用max()代替sort()获取最新的套件（O(n) vs O(n log n)）
                        suite_dir = max(suite_dirs, key=os.path.getmtime)
                        await log_callback(f"✓ 找到测试套件目录: {suite_dir}", 'info')
                        # 尝试找到 tools 目录，处理不同的目录结构
                        # 可能的结构: android-vts-*/android-vts/tools 或 android-vts-*/tools
                        possible_tools_dirs = [
                            os.path.join(suite_dir, f'android-{test_type_lower}', 'tools'),
                            os.path.join(suite_dir, 'tools'),
                            suite_dir  # 有时tools目录直接在套件目录下
                        ]

                        for tools_dir in possible_tools_dirs:
                            if os.path.isdir(tools_dir):
                                # 快速检查：先检查最常见的tradefed文件
                                tradefed_path = os.path.join(tools_dir, f'{test_type_lower}-tradefed')
                                if os.path.exists(tradefed_path):
                                    test_suite_tools = tools_dir
                                    await log_callback(f"✓ 找到tools目录: {test_suite_tools}", 'info')
                                    break
                                # 回退：检查所有tradefed可执行文件
                                has_tradefed = any(os.path.exists(os.path.join(tools_dir, tf)) for tf in TRADEFED_BINARY_LIST)
                                if has_tradefed or os.path.exists(os.path.join(tools_dir, 'test.xml')):
                                    test_suite_tools = tools_dir
                                    await log_callback(f"✓ 找到tools目录: {test_suite_tools}", 'info')
                                    break

                        if not test_suite_tools:
                            await log_callback(f"⚠️ 未找到有效的tools目录，已尝试: {possible_tools_dirs}", 'warning')
                    else:
                        await log_callback(f"⚠️ 未找到 {test_type_lower.upper()} 测试套件", 'warning')
                except Exception as e:
                    await log_callback(f"❌ 查找测试套件失败: {e}", 'error')
        else:
            cmd_parts.append(test_type_lower)
            if test_module:
                cmd_parts.append(test_module)
            if test_case:
                cmd_parts.append(test_case)

        # 添加设备参数
        if devices:
            device_args_list = []
            if len(devices) > 1:
                device_args_list.extend(["--shard-count", str(len(devices))])
            for device in devices:
                device_args_list.extend(["-s", device])

            device_args_str = " ".join(device_args_list)
            cmd_parts.extend(["--device-args", device_args_str])

        # 添加测试套件（使用tools路径）
        if test_suite_tools:
            cmd_parts.extend(["--test-suite", test_suite_tools])

        if local_server:
            cmd_parts.extend(["--local-server", local_server])
        else:
            await log_callback("⚠️ local_server为空，测试可能失败", 'warning')

        # 添加进程组ID（用于多用户隔离的精确进程停止）
        if process_group_id:
            cmd_parts.extend(["--pgid", process_group_id])

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
                except (WebSocketDisconnect, ConnectionError, KeyError):
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

        # 读取剩余的输出
        if stdout.channel.recv_ready():
            try:
                remaining_data = stdout.channel.recv(65536).decode('utf-8', errors='replace')
                if remaining_data:
                    lines = remaining_data.split('\n')
                    for line in lines:
                        if line.strip():
                            await log_callback(line.strip(), 'info')
            except Exception as e:
                logger.error(f"Error reading remaining stdout: {e}")

        # 读取剩余的错误输出
        if stderr.channel.recv_stderr_ready():
            try:
                remaining_error = stderr.channel.recv_stderr(65536).decode('utf-8', errors='replace')
                if remaining_error:
                    lines = remaining_error.split('\n')
                    for line in lines:
                        if line.strip():
                            await log_callback(line.strip(), 'error')
            except Exception as e:
                logger.error(f"Error reading remaining stderr: {e}")

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
            logger.exception("保存测试报告异常堆栈")

        # 清理资源
        if ssh:
            ssh_manager.return_connection(ssh)

        # 释放设备锁
        await release_device_locks(client_id, locked_devices)
        logger.info(f"[Device Lock] 测试完成，已广播设备解锁状态: {locked_devices}")

        # 更新状态为停止
        update_user_state_field(client_id, {'running': False, 'devices': []})

        notification = store_notification(
            client_id,
            '测试任务已结束',
            '测试执行已结束，请查看日志和报告确认结果。',
            'info',
            'test',
            {'devices': locked_devices}
        )

        # 发送 test_complete 事件
        await safe_websocket_send(client_id, {
            'type': 'test_complete',
            'notification': notification,
        })

@app.post("/api/test/stop")
async def stop_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """停止测试 - 与Flask版本逻辑一致"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/test/stop")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    client_id = get_client_id_from_request(request)
    user_state = get_or_create_user_state(client_id)
    process_group_id = user_state.get('process_group_id')

    # 检查是否有正在运行的测试
    running = user_state.get('running', False)
    devices_to_release = user_state.get('devices', [])

    if not running and not devices_to_release:
        return JSONResponse(
            content={'success': False, 'error': '没有正在运行的测试'},
            status_code=400
        )

    # 设置 running=False
    update_user_state_field(client_id, {'running': False})

    # 添加停止日志
    timestamp_str = datetime.now().strftime('%H:%M:%S')
    log_str = f"[{timestamp_str}] ⏹️ 用户请求停止测试..."
    if 'logs' not in user_state:
        user_state['logs'] = []
    user_state['logs'].append(log_str)

    # 释放设备锁
    if devices_to_release:
        logger.info(f"[TestStop] Releasing device locks for: {devices_to_release}")
        for device_id in devices_to_release:
            device_lock_manager.unlock_device(device_id, client_id)

        # 广播设备解锁状态更新
        logger.info(f"[TestStop] Broadcasting device unlock for: {devices_to_release}")
        await broadcast_device_lock_update(devices_to_release)

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
                await asyncio.sleep(1)

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

                await asyncio.sleep(1)
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 已终止 {killed_count} 个测试进程（命令行匹配）")
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={"success": True, "message": "测试已停止"})

        # 如果没有进程组ID或查找失败，记录警告但不强制终止（避免误杀手动测试）
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 未找到测试进程（可能已停止或手动测试）")
        ssh_manager.return_connection(ssh)
        return JSONResponse(content={"success": True, "message": "测试已停止（未找到运行中的测试进程）"})

    except Exception as e:
        ssh_manager.return_connection(ssh)
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 停止测试时出错: {str(e)}")
        logger.error(f"Error stopping test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/test/clean")
async def clean_test_logs(request: Request):
    """清理当前用户的测试日志"""
    try:
        client_id = get_client_id_from_request(request)

        # 清除当前用户的日志
        user_state = get_or_create_user_state(client_id)
        user_state['logs'] = []
        update_user_state_field(client_id, {'logs': []})

        logger.info(f"[Clean Logs] 用户 {client_id} 清除了测试日志")

        return JSONResponse(content={
            "success": True,
            "message": "日志已清除"
        })
    except Exception as e:
        logger.error(f"Error cleaning logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.get("/api/test/logs/get")
async def get_test_logs(request: Request):
    """获取测试日志（查看或下载）"""
    try:
        client_id = get_client_id_from_request(request)
        log_file = global_state.last_saved_log_file.get(client_id)

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
        logger.error(f"Error getting test logs: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.post("/api/test/logs/batch")
async def download_test_logs(req: dict):
    """批量下载测试日志（ZIP压缩包）"""
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

@app.post("/api/test/logs/save")
async def save_current_log(req: dict):
    """保存当前日志"""
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
            user_id = config_manager.get_ubuntu_user(config)
        else:
            user_id = parse_client_id(client_id)[0] if '@' in client_id else client_id

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

        global_state.last_saved_log_file[client_id] = str(log_file)

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


@app.get("/api/test/suites")
async def list_suites(base_path: str = None):
    """List all available test suites under the specified path

    Request:
        base_path: Optional - Path to search for test suites (defaults to config.suites_path)

    Response:
        success: bool
        suites: List of test suite info
            - test_type: str (cts, gts, vts, sts, gsi, apts)
            - version: str (e.g., android-cts-16_r4)
            - tools_path: str (path to tools directory)
            - full_path: str (full path to tradefed binary)
            - binary: str (e.g., cts-tradefed)
        count: int - Number of suites found
        base_path: str - The path that was searched
    """
    try:
        config = config_manager.load_config()
        # Use base_path from request or get from config
        base_path = base_path or config.get('suites_path') or get_default_suites_path(config)

        if is_config_host_local(config):
            suites = list_local_test_suites(base_path)
            return JSONResponse(content={
                'success': True,
                'suites': suites,
                'count': len(suites),
                'base_path': base_path,
                'source': 'local'
            })

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            # Find all *-tradefed executables
            find_cmd = f"find '{base_path}' -maxdepth 5 -type f -executable -name '*-tradefed' 2>/dev/null | sort"
            output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=30)

            suites = []
            stripped_output = output.strip()
            if stripped_output:
                for line in stripped_output.split('\n'):
                    suite = build_suite_info(line)
                    if suite:
                        suites.append(suite)

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'suites': suites,
                'count': len(suites),
                'base_path': base_path,
                'source': 'ssh'
            })

        except Exception:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing suites: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


def _normalize_suite_relative_path(path: Optional[str]) -> str:
    """Normalize a browser path so it always remains relative to a suite root."""
    rel_path = (path or '').replace('\\', '/').strip().strip('/')
    if not rel_path:
        return ''

    parts = [part for part in rel_path.split('/') if part and part != '.']
    if any(part == '..' for part in parts):
        raise ValueError("非法路径")
    return '/'.join(parts)


def _get_suite_root_from_path(suite_path: str, config: Dict[str, Any]) -> str:
    """Convert a selected suite tools path to the suite root directory."""
    raw_path = (suite_path or '').replace('\\', '/').strip().rstrip('/')
    if not raw_path or not raw_path.startswith('/'):
        raise ValueError("测试套件路径无效")

    suite_root = raw_path[:-len('/tools')] if raw_path.endswith('/tools') else raw_path
    suite_root = suite_root.rstrip('/')
    if not suite_root:
        raise ValueError("测试套件路径无效")

    base_path = (config.get('suites_path') or '').replace('\\', '/').strip().rstrip('/')
    if base_path.startswith('/') and not (suite_root == base_path or suite_root.startswith(base_path + '/')):
        raise ValueError("测试套件不在配置的套件目录内")

    return suite_root


def _build_suite_remote_path(suite_path: str, path: Optional[str], config: Dict[str, Any]) -> tuple:
    suite_root = _get_suite_root_from_path(suite_path, config)
    rel_path = _normalize_suite_relative_path(path)
    remote_path = suite_root if not rel_path else f"{suite_root}/{rel_path}"
    return suite_root, rel_path, remote_path


_SUITE_SCRIPT_PREAMBLE = r"""
import json, os, sys
root = os.path.realpath(sys.argv[1])
target = os.path.realpath(sys.argv[2])
def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))
if target != root and not target.startswith(root + os.sep):
    emit({"success": False, "error": "非法路径"})
    sys.exit(0)
"""

SUITE_FILE_LIST_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isdir(target):
    emit({"success": False, "error": "目录不存在"})
    sys.exit(0)

items = []
for name in sorted(os.listdir(target), key=lambda n: n.lower()):
    full_path = os.path.join(target, name)
    try:
        real_path = os.path.realpath(full_path)
        if real_path != root and not real_path.startswith(root + os.sep):
            continue
        st = os.stat(full_path)
        is_dir = os.path.isdir(full_path)
        rel = os.path.relpath(full_path, root)
        items.append({
            "name": name,
            "path": "" if rel == "." else rel,
            "type": "directory" if is_dir else "file",
            "size": 0 if is_dir else st.st_size,
            "modified": int(st.st_mtime),
            "is_apk": (not is_dir) and name.lower().endswith(".apk"),
        })
    except OSError:
        continue

items.sort(key=lambda item: (item["type"] != "directory", item["name"].lower()))
emit({
    "success": True,
    "path": "" if target == root else os.path.relpath(target, root),
    "root": root,
    "items": items,
})
"""

SUITE_FILE_INFO_SCRIPT = _SUITE_SCRIPT_PREAMBLE + r"""
if not os.path.isfile(target):
    emit({"success": False, "error": "文件不存在"})
    sys.exit(0)

st = os.stat(target)
emit({
    "success": True,
    "real_path": target,
    "name": os.path.basename(target),
    "size": st.st_size,
    "modified": int(st.st_mtime),
    "is_apk": target.lower().endswith(".apk"),
})
"""


def _run_suite_file_script(ssh, script: str, suite_root: str, remote_path: str, timeout: int = 20) -> Dict[str, Any]:
    cmd = f"python3 -c {shlex.quote(script)} {shlex.quote(suite_root)} {shlex.quote(remote_path)}"
    output, error, code = ssh_manager.execute_command(ssh, cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(error.strip() or output.strip() or "远程文件操作失败")
    try:
        return json.loads(output.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"远程文件响应解析失败: {e}")


@app.get("/api/test/suites/files")
@handle_api_errors
async def list_suite_files(
    suite_path: str = Query(..., description="测试套件 tools 目录路径"),
    path: str = Query('', description="套件内相对路径")
):
    """浏览测试套件目录内的文件。"""
    config = config_manager.load_config()
    try:
        suite_root, rel_path, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        payload = _run_suite_file_script(ssh, SUITE_FILE_LIST_SCRIPT, suite_root, remote_path)
        if not payload.get('success'):
            return ApiResponse.error(payload.get('error', '目录读取失败'), status_code=400)
        return ApiResponse.success({
            'suite_path': suite_path,
            'suite_root': suite_root,
            'path': payload.get('path', rel_path),
            'items': payload.get('items', []),
        })
    finally:
        ssh_manager.return_connection(ssh)


@app.get("/api/test/suites/download")
@handle_api_errors
async def download_suite_file(
    suite_path: str = Query(..., description="测试套件 tools 目录路径"),
    path: str = Query(..., description="套件内相对文件路径")
):
    """下载测试套件目录内的指定文件。"""
    config = config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(suite_path, path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get('success'):
            ssh_manager.return_connection(ssh)
            return ApiResponse.error(info.get('error', '文件不存在'), status_code=404)

        sftp = ssh.open_sftp()
        remote_file = sftp.open(info['real_path'], 'rb')
    except Exception:
        ssh_manager.return_connection(ssh)
        raise

    filename = info.get('name') or os.path.basename(remote_path) or 'download'
    ascii_filename = re.sub(r'[^A-Za-z0-9._-]+', '_', filename) or 'download'
    quoted_filename = urllib.parse.quote(filename)
    media_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'

    def iter_remote_file():
        try:
            while True:
                chunk = remote_file.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                remote_file.close()
            finally:
                try:
                    sftp.close()
                finally:
                    ssh_manager.return_connection(ssh)

    return StreamingResponse(
        iter_remote_file(),
        media_type=media_type,
        headers={
            'Content-Disposition': f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quoted_filename}',
            'Content-Length': str(info.get('size', 0)),
        }
    )


class SuiteApkAnalyzeRequest(BaseModel):
    suite_path: str
    path: str


@app.post("/api/test/suites/apk/analyze")
@handle_api_errors
async def create_suite_apk_analysis_task(req: SuiteApkAnalyzeRequest):
    """把测试套件中的 APK 复制为 APK 分析任务。"""
    config = config_manager.load_config()
    try:
        suite_root, _, remote_path = _build_suite_remote_path(req.suite_path, req.path, config)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return ssh_connection_failed_response()

    task_id = str(uuid.uuid4())
    sftp = None
    try:
        info = _run_suite_file_script(ssh, SUITE_FILE_INFO_SCRIPT, suite_root, remote_path)
        if not info.get('success'):
            return ApiResponse.error(info.get('error', '文件不存在'), status_code=404)
        if not info.get('is_apk'):
            return ApiResponse.error("仅支持 APK 文件反编译", status_code=400)
        if int(info.get('size', 0)) > APK_MAX_FILE_SIZE:
            return ApiResponse.error(f"文件过大，最大支持 {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

        filename = _normalize_apk_filename(info.get('name') or os.path.basename(remote_path))
        task_dir = _safe_join(APK_UPLOAD_DIR, task_id)
        os.makedirs(task_dir, exist_ok=True)
        apk_path = _safe_join(task_dir, filename)

        sftp = ssh.open_sftp()
        await asyncio.to_thread(sftp.get, info['real_path'], apk_path)

        if os.path.getsize(apk_path) > APK_MAX_FILE_SIZE:
            _cleanup_files([apk_path])
            return ApiResponse.error(f"文件过大，最大支持 {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

        _create_apk_task(task_id, apk_path, filename)
        return ApiResponse.success({
            'task_id': task_id,
            'filename': filename,
            'size': os.path.getsize(apk_path),
            'source_path': req.path,
        })
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass
        ssh_manager.return_connection(ssh)


class TradefedListResultsRequest(BaseModel):
    """Request model for tradefed list results"""
    suite_path: str
    tradefed_bin: Optional[str] = None


class TestSuiteDownloadRequest(BaseModel):
    """Request model for downloading test suite from URL"""
    url: str = Field(..., description="测试套件下载地址")
    save_dir: Optional[str] = Field(default=None, description="保存目录（默认：/home/hcq/GMS-Suite）")


class TestSuiteExtractRequest(BaseModel):
    """Request model for extracting test suite archive"""
    archive_path: str = Field(..., description="压缩包文件路径")
    extract_dir: Optional[str] = Field(default=None, description="解压目录（默认：/home/hcq/GMS-Suite）")


class TestSuiteAddLocalRequest(BaseModel):
    """Request model for adding local test suite path"""
    path: str = Field(..., description="本地测试套件路径")


@app.post("/api/test/suites/add-local")
@handle_api_errors
async def add_local_test_suite(req: TestSuiteAddLocalRequest):
    """添加本地测试套件路径到配置

    Request:
        path: str - 本地测试套件路径（如：/home/hcq/GMS-Suite/android-cts-verifier-16.1_r2/）

    Response:
        success: bool
        message: str - 添加状态信息
        path: str - 添加的路径
    """
    config = config_manager.load_config()

    if not req.path:
        return JSONResponse(
            content={'success': False, 'error': '路径不能为空'},
            status_code=400
        )

    # 检查路径是否存在
    if is_config_host_local(config):
        if not os.path.exists(req.path):
            return JSONResponse(
                content={'success': False, 'error': f'路径不存在：{req.path}'},
                status_code=404
            )
        # 检查是否为目录
        if not os.path.isdir(req.path):
            return JSONResponse(
                content={'success': False, 'error': f'路径不是目录：{req.path}'},
                status_code=400
            )
    else:
        # 远程 SSH 检查
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        # 检查远程路径是否存在
        check_cmd = f"[ -d '{req.path}' ] && echo 'exists' || echo 'not_exists'"
        output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=10)
        if output.strip() != 'exists':
            return JSONResponse(
                content={'success': False, 'error': f'路径不存在：{req.path}'},
                status_code=404
            )

    # 路径验证通过，返回成功
    # 注意：实际的套件识别由前端刷新 /api/test/suites 后自动完成
    return JSONResponse(content={
        'success': True,
        'message': f'已添加本地路径：{os.path.basename(req.path.rstrip("/"))}',
        'path': req.path
    })


@app.post("/api/test/suites/result")
async def list_tradefed_results(
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TradefedListResultsRequest = Body(None),
    force_refresh: bool = Query(False)  # 添加强制刷新参数
):
    """Execute tradefed list results command and return test results

    Request:
        suite_path: str - Path to test suite tools directory
        tradefed_bin: Optional[str] - Tradefed binary name (auto-detected if not provided)
        force_refresh: bool - Force cache refresh (default: False)

    Response:
        success: bool
        results: List of test result entries
            - session: str
            - pass: int
            - fail: int
            - modules: str
            - complete: str
            - result_directory: str
            - test_plan: str
            - device_serial: str
            - build_id: str
            - product: str
        raw_output: str - Raw command output
        cached: bool - Whether results were served from cache
    """
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/test/suites/result")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    if req is None:
        return JSONResponse(
            content={'success': False, 'error': 'Missing request body'},
            status_code=400
        )

    try:
        config = config_manager.load_config()
        suite_path = req.suite_path
        tradefed_bin = req.tradefed_bin
        logger.info(f"Querying test suite results for {suite_path} (no cache)")

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            # Auto-detect tradefed binary if not provided
            if not tradefed_bin:
                tradefed_bin = find_tradefed_binary(ssh, suite_path)
                if not tradefed_bin:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(
                        content={'success': False, 'error': f'No tradefed binary found in {suite_path}'},
                        status_code=404
                    )

            # Execute tradefed list results (使用优化后的函数)
            output, error, code = execute_tradefed_command(ssh, suite_path, tradefed_bin)

            ssh_manager.return_connection(ssh)

            if code != 0:
                return JSONResponse(
                    content={
                        'success': False,
                        'error': error or f'Command failed with exit code: {code}',
                        'raw_output': output
                    },
                    status_code=500
                )

            # Parse results using shared utility
            results = parse_tradefed_list_results(output)

            return JSONResponse(content={
                'success': True,
                'results': results,
                'count': len(results),
                'raw_output': output,
                'cached': False
            })

        except Exception:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing tradefed results: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


# ==================== 测试套件下载和解压 ====================

@app.post("/api/test/suites/download-url")
@handle_api_errors
async def download_test_suite_from_url(req: TestSuiteDownloadRequest):
    """从指定 URL 下载测试套件（CTS/VTS/GTS/STS 等）

    Request:
        url: str - 测试套件下载地址
        save_dir: Optional[str] - 保存目录（默认：/home/hcq/GMS-Suite）

    Response:
        success: bool
        message: str - 下载状态信息
        archive_path: str - 下载的压缩包路径
        file_size: int - 文件大小（字节）
    """
    config = config_manager.load_config()

    if not req.url:
        return JSONResponse(
            content={'success': False, 'error': '下载地址不能为空'},
            status_code=400
        )

    # 使用默认路径
    save_dir = req.save_dir or get_default_suites_path(config)

    # 创建保存目录
    os.makedirs(save_dir, exist_ok=True)

    # 从 URL 解析文件名
    parsed_url = urllib.parse.urlparse(req.url)
    filename = os.path.basename(parsed_url.path) or 'test-suite.zip'

    # 清理文件名
    filename = re.sub(r'[^\w\-_.\[\]]', '_', filename)
    archive_path = os.path.join(save_dir, filename)

    if is_config_host_local(config):
        # 本地下载
        import subprocess

        # 使用 curl 下载，支持进度显示，添加超时限制（30 分钟）
        cmd = ['curl', '-L', '--connect-timeout', '30', '--max-time', '1800',
               '-o', archive_path, req.url]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # 设置超时，避免无限等待
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1900)
            except asyncio.TimeoutError:
                process.kill()
                return JSONResponse(
                    content={'success': False, 'error': '下载超时（超过 30 分钟）'},
                    status_code=500
                )

            if process.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore')
                return JSONResponse(
                    content={'success': False, 'error': f'下载失败：{error_msg}'},
                    status_code=500
                )
        except Exception as e:
            return JSONResponse(
                content={'success': False, 'error': f'下载异常：{str(e)}'},
                status_code=500
            )

        # 获取文件大小
        file_size = os.path.getsize(archive_path)

        return JSONResponse(content={
            'success': True,
            'message': f'下载完成：{filename}',
            'archive_path': archive_path,
            'file_size': file_size,
            'download_method': 'local'
        })

    else:
        # 远程 SSH 下载
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            # 使用 curl 下载
            cmd = f"curl -L -o '{archive_path}' '{req.url}' 2>&1"
            output, exit_code, _ = ssh_manager.execute_command(ssh, cmd, timeout=600)

            if exit_code != 0:
                return JSONResponse(
                    content={'success': False, 'error': f'下载失败：{output}'},
                    status_code=500
                )

            # 获取文件大小
            size_cmd = f"stat -c%s '{archive_path}' 2>/dev/null || stat -f%z '{archive_path}' 2>/dev/null || echo 0"
            size_output, _, _ = ssh_manager.execute_command(ssh, size_cmd, timeout=10)
            file_size = int(size_output.strip())

            return JSONResponse(content={
                'success': True,
                'message': f'下载完成：{filename}',
                'archive_path': archive_path,
                'file_size': file_size,
                'download_method': 'ssh'
            })

        finally:
            ssh_manager.return_connection(ssh)


@app.post("/api/test/suites/extract")
@handle_api_errors
async def extract_test_suite_archive(req: TestSuiteExtractRequest):
    """解压测试套件压缩包

    Request:
        archive_path: str - 压缩包文件路径
        extract_dir: Optional[str] - 解压目录（默认：/home/hcq/GMS-Suite）

    Response:
        success: bool
        message: str - 解压状态信息
        extracted_path: str - 解压后的目录路径
        files_count: int - 解压的文件数量
    """
    config = config_manager.load_config()

    if not req.archive_path:
        return JSONResponse(
            content={'success': False, 'error': '压缩包路径不能为空'},
            status_code=400
        )

    # 使用默认路径
    extract_dir = req.extract_dir or get_default_suites_path(config)

    # 检查文件是否存在（本地）
    if is_config_host_local(config) and not os.path.exists(req.archive_path):
        return JSONResponse(
            content={'success': False, 'error': f'压缩包不存在：{req.archive_path}'},
            status_code=404
        )

    # 创建解压目录
    os.makedirs(extract_dir, exist_ok=True)

    if is_config_host_local(config):
        # 本地解压
        import subprocess
        import tarfile
        import zipfile

        try:
            # 根据文件类型选择解压方式
            if req.archive_path.endswith('.zip'):
                with zipfile.ZipFile(req.archive_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                    files_count = len(zip_ref.namelist())
            elif req.archive_path.endswith(('.tar.gz', '.tgz')):
                with tarfile.open(req.archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(extract_dir)
                    files_count = len(tar_ref.getnames())
            elif req.archive_path.endswith('.tar'):
                with tarfile.open(req.archive_path, 'r') as tar_ref:
                    tar_ref.extractall(extract_dir)
                    files_count = len(tar_ref.getnames())
            elif req.archive_path.endswith('.tar.bz2'):
                with tarfile.open(req.archive_path, 'r:bz2') as tar_ref:
                    tar_ref.extractall(extract_dir)
                    files_count = len(tar_ref.getnames())
            else:
                # 尝试使用 tar 命令
                cmd = ['tar', '-xf', req.archive_path, '-C', extract_dir]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await process.communicate()

                if process.returncode != 0:
                    error_msg = stderr.decode('utf-8', errors='ignore')
                    return JSONResponse(
                        content={'success': False, 'error': f'解压失败：{error_msg}'},
                        status_code=500
                    )
                files_count = 0  # 未知

            # 获取解压后的目录
            extracted_name = os.path.splitext(os.path.basename(req.archive_path))[0]
            # 移除可能的.tar/.gz 等后缀
            for ext in ['.tar', '.gz', '.bz2', '.zip', '.tgz']:
                if extracted_name.endswith(ext):
                    extracted_name = extracted_name[:-len(ext)]

            extracted_path = os.path.join(extract_dir, extracted_name)

            return JSONResponse(content={
                'success': True,
                'message': f'解压完成：{extracted_name}',
                'extracted_path': extracted_path,
                'files_count': files_count,
                'extract_method': 'local'
            })

        except Exception as e:
            return JSONResponse(
                content={'success': False, 'error': f'解压失败：{str(e)}'},
                status_code=500
            )

    else:
        # 远程 SSH 解压
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            # 使用 tar 命令解压
            cmd = f"tar -xf '{req.archive_path}' -C '{req.extract_dir}' 2>&1"
            output, exit_code, _ = ssh_manager.execute_command(ssh, cmd, timeout=300)

            if exit_code != 0:
                return JSONResponse(
                    content={'success': False, 'error': f'解压失败：{output}'},
                    status_code=500
                )

            # 获取解压后的目录名
            archive_name = os.path.basename(req.archive_path)
            extracted_name = os.path.splitext(archive_name)[0]
            for ext in ['.tar', '.gz', '.bz2', '.zip', '.tgz']:
                if extracted_name.endswith(ext):
                    extracted_name = extracted_name[:-len(ext)]

            extracted_path = os.path.join(req.extract_dir, extracted_name)

            return JSONResponse(content={
                'success': True,
                'message': f'解压完成：{extracted_name}',
                'extracted_path': extracted_path,
                'extract_method': 'ssh'
            })

        finally:
            ssh_manager.return_connection(ssh)


@app.get("/api/test/status")
async def get_status(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """获取测试状态"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("GET", "/api/test/status")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )
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
                    # 返回所有日志
                    response['logs'] = logs
                    response['log_count'] = len(logs)
            else:
                # 返回所有日志
                response['logs'] = logs
                response['log_count'] = len(logs)

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.get("/api/test/logs/stream")
async def stream_test_logs(request: Request):
    """
    流式输出测试日志（纯文本格式）

    提供实时日志流，适合:
    - 命令行工具（curl, wget等）
    - 脚本自动化
    - 日志收集系统

    返回格式: 纯文本流，每行一条日志
    """
    client_id = get_client_id_from_request(request)

    async def log_stream():
        """生成纯文本日志流"""
        try:
            last_log_count = 0

            while True:
                user_state = get_or_create_user_state(client_id)
                running = user_state.get('running', False)
                logs = user_state.get('logs', [])
                current_log_count = len(logs)

                # 发送新日志
                if current_log_count > last_log_count:
                    for i in range(last_log_count, current_log_count):
                        log_entry = logs[i]
                        # 直接输出日志内容，每行一个日志
                        yield f"{log_entry}\n"
                    last_log_count = current_log_count

                # 如果测试结束，退出
                if not running and last_log_count > 0:
                    yield "=== 测试完成 ===\n"
                    break

                # 等待一段时间再检查
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Error in stream: {e}")
            yield f"错误: {str(e)}\n"

    return StreamingResponse(
        log_stream(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no"  # 禁用nginx缓冲
        }
    )

# ==================== 报告管理 ====================
@app.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False):
    """
    从数据库获取测试报告列表

    Args:
        user_only: 是否只显示当前用户的报告，默认 False 显示所有用户的报告
    """
    import time
    start_time = time.time()

    try:
        # 如果要求只显示当前用户的报告，先获取 client_id 用于数据库过滤
        client_id_filter = None
        if user_only:
            client_id = get_client_id_from_request(request)

            # 对于本地访问（127.0.0.1 或::1），也显示配置文件中 client_ip 对应的报告
            config = config_manager.load_config()
            configured_ip = config.get('client_ip', '')
            username = config.get('client_username', 'unknown')

            # 构建可能的 client_id
            if configured_ip and ('@127.0.0.1' in client_id or '@::1' in client_id or '@localhost' in client_id):
                client_id_filter = f"{username}@{configured_ip}"
            else:
                client_id_filter = client_id

        # 从数据库获取报告（使用索引过滤，限制返回数量）
        db_start = time.time()
        all_reports = test_report_db.get_reports(limit=30, user_only=client_id_filter)
        db_time = (time.time() - db_start) * 1000

        # 返回报告列表
        total_time = (time.time() - start_time) * 1000
        logger.info(f"[API] /api/reports/list completed: {len(all_reports)} reports, DB: {db_time:.2f}ms, Total: {total_time:.2f}ms")

        return JSONResponse(content={'reports': all_reports})

    except Exception as e:
        logger.error(f"获取报告列表失败: {e}")
        return JSONResponse(content={'reports': []})

@app.get("/api/reports/download")
async def download_report(
    request: Request,
    report_timestamp: str = Query(None, description="报告时间戳"),
    download: bool = Query(False, description="是否下载ZIP文件（默认返回JSON列表）"),
    path: str = Query(None, description="文件路径（查看单个文件内容）")
):
    """
    统一的报告接口

    支持三种模式：
    1. 获取报告文件列表（JSON）：?report_timestamp=xxx
    2. 下载报告ZIP文件：?report_timestamp=xxx&download=true
    3. 查看单个文件内容（JSON）：?path=/xxx/xxx/invocation_summary.txt
    """
    try:
        # 模式1&2：处理报告相关请求
        if report_timestamp:
            # 从数据库获取报告信息
            report = test_report_db.get_report_by_timestamp(report_timestamp)

            if not report:
                logger.error(f"[DOWNLOAD] 报告不存在: {report_timestamp}")
                return JSONResponse(
                    content={'success': False, 'error': f'报告不存在: {report_timestamp}'},
                    status_code=404
                )

            report_dir = report.get('result_dir')

            if not report_dir or not os.path.exists(report_dir):
                logger.error(f"[DOWNLOAD] 报告目录不存在: {report_dir}")
                return JSONResponse(
                    content={'success': False, 'error': f'报告目录不存在: {report_dir}'},
                    status_code=404
                )

            # 推导 logs 目录路径 (result_dir 向上两级到 android_suite_dir，再构建 logs 路径)
            android_suite_dir = os.path.dirname(os.path.dirname(report_dir))
            logs_dir = os.path.join(android_suite_dir, 'logs', report_timestamp)

            # 检查logs目录是否存在
            has_logs = os.path.exists(logs_dir)
            if has_logs:
                logger.info(f"[DOWNLOAD] 找到logs目录: {logs_dir}")
            else:
                logger.info(f"[DOWNLOAD] 未找到logs目录: {logs_dir}")

            # 模式 2：下载 ZIP 文件（回退方案）
            if download:
                logger.info(f"[DOWNLOAD] 请求下载报告 ZIP: timestamp='{report_timestamp}'")

                # 构建目录映射：{目录路径：ZIP 中的前缀}
                dir_mapping = {report_dir: ''}  # results 目录文件放在 ZIP 根目录
                if has_logs:
                    dir_mapping[logs_dir] = 'logs'  # logs 目录文件放在 ZIP 的 logs/子目录下

                result = FileUtils.create_zip_from_multiple_directories(dir_mapping, zip_filename=f"{report_timestamp}.zip")

                if result is None:
                    logger.warning("[DOWNLOAD] 没有找到文件")
                    return JSONResponse(
                        content={'success': False, 'error': '没有找到文件'},
                        status_code=500
                    )

                zip_data, file_count = result
                logger.info(f"[DOWNLOAD] 创建 ZIP 成功：{report_timestamp}.zip, {file_count} 个文件")

                # 返回 ZIP 文件
                return Response(
                    content=zip_data,
                    media_type="application/zip",
                    headers={
                        "Content-Disposition": f"attachment; filename=\"{report_timestamp}.zip\""
                    }
                )


            logger.info(f"[DOWNLOAD] 请求获取报告文件列表: timestamp='{report_timestamp}'")

            # 收集所有文件（results和logs）
            all_files = []

            # 添加results目录文件，results目录下的文件相对路径直接从result_dir开始
            result_files = FileUtils.list_directory_files(report_dir, max_files=100, relative_to=report_dir)
            all_files.extend(result_files)

            # 添加logs目录文件（如果存在），添加logs/前缀
            if has_logs:
                log_files = FileUtils.list_directory_files(logs_dir, max_files=100, relative_to=logs_dir)
                # 为logs文件添加logs/前缀
                for file_info in log_files:
                    file_info['relative_path'] = os.path.join('logs', file_info['relative_path'])
                all_files.extend(log_files)

            logger.info(f"[DOWNLOAD] 找到 {len(all_files)} 个文件 (results: {len(result_files)}, logs: {len(log_files) if has_logs else 0})")

            return JSONResponse(content={'success': True, 'files': all_files})

        # 模式3：查看单个文件内容
        elif path:
            logger.info(f"[DOWNLOAD] 请求查看文件内容: path='{path}'")

            config = config_manager.load_config()
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH connection failed'},
                    status_code=500
                )

            try:
                # 读取文件内容
                cat_cmd = f"cat '{path}' 2>/dev/null"
                output, error, code = ssh_manager.execute_command(ssh, cat_cmd, timeout=30)

                ssh_manager.return_connection(ssh)

                # 确定内容类型
                file_ext = os.path.splitext(path)[1].lower()
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

            except Exception:
                ssh_manager.return_connection(ssh)
                raise

        else:
            return JSONResponse(
                content={'success': False, 'error': '请提供 report_timestamp 或 path 参数'},
                status_code=400
            )

    except Exception as e:
        logger.error(f"[DOWNLOAD] 处理请求失败: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/reports/analyze-url")
async def analyze_report_from_url(request: Request):
    """
    从 URL 下载并分析测试报告（支持 Redmine 附件自动下载）

    参数：
        url: 附件的 URL 地址
        redmine_username: Redmine 用户名（可选）
        redmine_password: Redmine 密码（可选）

    功能：
        1. 自动从 Redmine 下载附件（使用存储的或提供的凭证）
        2. 支持一次配置，永久自动下载
        3. 凭证加密存储
    """
    try:
        # 解析请求参数
        body = await request.json()
        url = body.get('url', '').strip()
        redmine_username = body.get('redmine_username', '').strip()
        redmine_password = body.get('redmine_password', '').strip()

        if not url:
            return JSONResponse(
                content={'success': False, 'error': '缺少 URL 参数'},
                status_code=400
            )

        logger.info(f"[Report Analysis] 收到 URL 分析请求: {url}")

        # 从 URL 中提取文件名
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path) or 'downloaded_file.zip'

        # 检测是否为 Redmine URL (获取配置一次，复用整个请求)
        redmine_config = None
        is_redmine = False
        try:
            redmine_config = config_manager.get_redmine_config()
            is_redmine = redmine_config['domain'] in url.lower()
        except ValueError:
            # 如果Redmine未配置，不进行Redmine处理
            pass

        # Redmine URL 处理
        original_issue_id = None  # 用户访问的原始问题ID（用于报告命名）
        attachment_owner_issue_id = None  # 附件实际所属的问题ID
        if is_redmine:
            issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
            if issue_match and '/attachments/' not in url:
                # 是问题页面，需要提取附件
                issue_id = issue_match.group(1)
                original_issue_id = issue_id  # 保存用户访问的原始问题ID
                logger.info(f"[Report Analysis] 检测到 Redmine 问题页面: {issue_id}")

                # 调用提取附件的逻辑
                try:
                    # 复用已获取的配置，避免重复调用
                    if redmine_config:
                        base_url = redmine_config['base_url']
                    else:
                        logger.warning("[Report Analysis] Redmine配置不可用")
                        return JSONResponse(
                            content={'success': False, 'error': 'Redmine 配置不可用'},
                            status_code=404
                        )

                    stored_creds = await load_redmine_credentials()
                    if stored_creds:
                        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"
                        username = stored_creds.get('username')
                        password = stored_creds.get('password')
                        headers = {}
                        headers.update(create_basic_auth_header(username, password))
                        logger.info(f"[Report Analysis] 使用存储的 Redmine 凭证查询问题 {issue_id}")
                    else:
                        # 如果没有配置凭证，使用配置文件中的域名
                        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"
                        headers = {}
                        logger.warning("[Report Analysis] 未找到存储的 Redmine 凭证，使用配置文件中的域名和匿名查询")

                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                            if response.status != 200:
                                return JSONResponse(
                                    content={'success': False, 'error': f'无法访问问题页面: HTTP {response.status}'},
                                    status_code=400
                                )

                            # 检查响应内容类型
                            content_type = response.headers.get('Content-Type', '')
                            if 'application/json' not in content_type:
                                # 可能返回了HTML登录页面
                                text = await response.text()
                                logger.warning(f"[Report Analysis] 期望JSON但收到 {content_type}，可能是认证问题")
                                logger.warning(f"[Report Analysis] 响应前100字符: {text[:100]}")
                                return JSONResponse(
                                    content={'success': False, 'error': 'Redmine API 返回了非JSON响应，可能是认证失败或权限问题'},
                                    status_code=401
                                )

                            data = await response.json()
                            attachments = data.get('issue', {}).get('attachments', [])

                            if not attachments:
                                return JSONResponse(
                                    content={'success': False, 'error': f'问题 {issue_id} 没有附件'},
                                    status_code=404
                                )

                            # 获取第一个附件
                            first_attachment = attachments[0]
                            attachment_id = first_attachment.get('id')
                            filename = first_attachment.get('filename', f'attachment_{attachment_id}')

                            # 转换为附件下载 URL
                            url = build_redmine_download_url(base_url, attachment_id)
                            logger.info(f"[Report Analysis] 从问题页面提取附件: {filename} -> {url}")
                except Exception as extract_error:
                    logger.error(f"[Report Analysis] 提取附件失败: {extract_error}")
                    return JSONResponse(
                        content={'success': False, 'error': f'无法提取附件: {str(extract_error)}'},
                        status_code=500
                    )
            else:
                # 匹配 /attachments/数字 或 /attachments/download/数字 格式
                redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                if redmine_attach_match:
                    attachment_id = redmine_attach_match.group(1)
                    logger.info(f"[Report Analysis] 检测到 Redmine 附件 URL，ID: {attachment_id}")

                    # 首先检查缓存中是否有这个附件ID的记录
                    if attachment_id in REDMINE_ISSUE_ID_CACHE:
                        cached_issue_id = REDMINE_ISSUE_ID_CACHE[attachment_id]
                        logger.info(f"[Report Analysis] 从缓存中找到附件 {attachment_id} 的问题ID: {cached_issue_id}")
                        if not original_issue_id:
                            original_issue_id = cached_issue_id
                            logger.info(f"[Report Analysis] 使用缓存的问题ID作为报告前缀: {original_issue_id}")
                    else:
                        # 缓存未命中，尝试通过 Redmine API 搜索查询附件所属的问题
                        try:
                            base_url = redmine_config['base_url']
                            stored_creds = await load_redmine_credentials()

                            if stored_creds:
                                username = stored_creds.get('username')
                                password = stored_creds.get('password')
                                headers = {}
                                headers.update(create_basic_auth_header(username, password))
                            else:
                                headers = {}

                            # 使用搜索API查找包含此附件的问题
                            search_url = f"{base_url}/issues.json?attachment_id={attachment_id}"
                            logger.info(f"[Report Analysis] 搜索附件所属问题: {search_url}")

                            async with aiohttp.ClientSession() as session:
                                async with session.get(search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                                    if response.status == 200:
                                        # 检查响应内容类型
                                        content_type = response.headers.get('Content-Type', '')
                                        if 'application/json' not in content_type:
                                            logger.warning(f"[Report Analysis] 搜索API返回非JSON响应: {content_type}")
                                        else:
                                            search_data = await response.json()
                                            issues = search_data.get('issues', [])
                                            if issues:
                                                # 获取第一个相关问题的ID（附件实际所属问题）
                                                attachment_owner_issue_id = str(issues[0].get('id'))
                                                logger.info(f"[Report Analysis] 找到附件所属问题: {attachment_owner_issue_id}")
                                                # 对于直接附件URL，将附件所属问题ID设为原始问题ID
                                                if not original_issue_id:
                                                    original_issue_id = attachment_owner_issue_id
                                                    logger.info(f"[Report Analysis] 直接附件访问，使用所属问题ID作为报告前缀: {original_issue_id}")
                                                else:
                                                    # 如果附件来自不同问题，记录日志
                                                    if attachment_owner_issue_id != original_issue_id:
                                                        logger.info(f"[Report Analysis] 注意：附件来自问题 {attachment_owner_issue_id}，但用户访问的是问题 {original_issue_id}")
                                            else:
                                                logger.warning(f"[Report Analysis] 未找到附件 {attachment_id} 所属的问题")
                                    else:
                                        logger.warning(f"[Report Analysis] 搜索失败: HTTP {response.status}")
                        except Exception as search_error:
                            logger.warning(f"[Report Analysis] 搜索附件信息失败: {search_error}")

                        # 如果URL还不是下载URL格式，则转换
                        if '/attachments/download/' not in url:
                            url = build_redmine_download_url(redmine_config['base_url'], attachment_id)
                            logger.info(f"[Report Analysis] 转换为 Redmine 下载 URL: {url}")
                        else:
                            logger.info(f"[Report Analysis] URL已是下载格式，无需转换: {url}")

                    # 更新 filename 用于保存
                    filename = 'downloaded_file.zip'

        # 下载文件
        logger.info(f"[Report Analysis] 正在下载附件: {filename}")


        temp_dir = tempfile.mkdtemp(prefix='redmine_download_')
        temp_file_path = os.path.join(temp_dir, filename)

        try:
            # 构建 HTTP headers
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

            # 处理 Redmine 认证
            if is_redmine:
                # 如果提供了凭证，使用并保存
                if redmine_username and redmine_password:
                    headers.update(create_basic_auth_header(redmine_username, redmine_password))
                    logger.info(f"[Report Analysis] 使用提供的 Redmine 凭证: {redmine_username}")
                    # 保存凭证供下次使用
                    await save_redmine_credentials(redmine_username, redmine_password)
                else:
                    # 尝试从存储中获取凭证
                    stored_creds = await load_redmine_credentials()
                    if stored_creds:
                        redmine_username = stored_creds.get('username')
                        redmine_password = stored_creds.get('password')
                        headers.update(create_basic_auth_header(redmine_username, redmine_password))
                        logger.info(f"[Report Analysis] 使用存储的 Redmine 凭证: {redmine_username}")
                    else:
                        logger.warning("[Report Analysis] 未找到 Redmine 凭证，返回需要认证标志")
                        return JSONResponse(
                            content={
                                'success': False,
                                'error': '未配置 Redmine 凭证，请输入用户名和密码',
                                'requires_auth': True,
                                'is_redmine': True
                            },
                            status_code=401
                        )

            # 下载文件
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=120), headers=headers) as response:
                    # 处理响应
                    if response.status == 401 or response.status == 403:
                        if is_redmine:
                            return JSONResponse(
                                content={
                                    'success': False,
                                    'error': 'Redmine 认证失败。请检查用户名和密码。',
                                    'requires_auth': True,
                                    'is_redmine': True
                                },
                                status_code=403
                            )
                        else:
                            return JSONResponse(
                                content={'success': False, 'error': f'下载失败，HTTP {response.status}'},
                                status_code=400
                            )
                    elif response.status != 200:
                        logger.error(f"[Report Analysis] 下载失败: HTTP {response.status}")
                        return JSONResponse(
                            content={'success': False, 'error': f'下载失败，HTTP {response.status}'},
                            status_code=400
                        )

                    # 下载文件内容
                    downloaded_size = 0
                    # 从 Content-Disposition header 获取真实文件名
                    real_filename = extract_filename_from_content_disposition(
                        response.headers.get('Content-Disposition', '')
                    ) or filename
                    if real_filename != filename:
                        logger.info(f"[Report Analysis] 从 Content-Disposition 获取文件名：{real_filename}")

                    # 智能检查：如果文件名已包含Redmine前缀，提取问题ID以保持一致性
                    redmine_prefix_match = COMPILED_REPORT_NAME_PATTERN.match(real_filename)
                    if redmine_prefix_match:
                        extracted_issue_id = redmine_prefix_match.group(1)
                        logger.info(f"[Report Analysis] 文件名已包含Redmine前缀，提取问题ID: {extracted_issue_id}")
                        # 如果当前没有原始问题ID，使用文件名中的ID
                        if not original_issue_id:
                            original_issue_id = extracted_issue_id
                            logger.info(f"[Report Analysis] 使用文件名中的问题ID作为报告前缀: {original_issue_id}")
                        # 如果文件名中的ID与当前ID不同，记录警告但优先使用文件名中的ID（保持用户期望的一致性）
                        elif original_issue_id != extracted_issue_id:
                            logger.info(f"[Report Analysis] 注意：文件名中的问题ID ({extracted_issue_id}) 与当前问题ID ({original_issue_id}) 不同，使用文件名中的ID以保持一致性")
                            original_issue_id = extracted_issue_id

                    with open(temp_file_path, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            f.write(chunk)
                            downloaded_size += len(chunk)

                    logger.info(f"[Report Analysis] 下载完成：{downloaded_size} bytes")
                    # 更新 filename 为真实文件名
                    filename = real_filename


            # 分析报告
            logger.info(f"[Report Analysis] 分析报告文件: {temp_file_path}")
            result = await analyze_report_file(temp_file_path, temp_dir)
            if result:
                logger.info(f"[Report Analysis] 分析完成 - 失败用例数: {len(result.get('failures', []))}")
                if result.get('failures'):
                    for i, failure in enumerate(result['failures'][:3]):
                        logger.info(f"[Report Analysis]   失败 {i+1}: {failure.get('name', 'unknown')}")
            else:
                logger.warning("[Report Analysis] 分析结果为空")

            # 清理临时文件
            try:
                shutil.rmtree(temp_dir)
            except:
                pass

            if result:
                # 从 URL 中提取报告名称
                report_name = filename

                # 如果是从Redmine下载的报告，添加前缀
                if is_redmine:
                    # 优先使用用户访问的原始问题ID（original_issue_id），而不是附件实际所属的问题ID
                    # 这样可以确保报告名称与用户访问的问题页面保持一致
                    if original_issue_id:
                        # 添加Redmine前缀：Redmine-{用户访问的问题ID}-{原文件名}
                        report_name = f"Redmine-{original_issue_id}-{filename}"
                        logger.info(f"[Report Analysis] Redmine报告添加前缀: {report_name}")
                        # 如果附件来自不同问题，记录信息
                        if attachment_owner_issue_id and attachment_owner_issue_id != original_issue_id:
                            logger.info(f"[Report Analysis] 附件来自问题 {attachment_owner_issue_id}，使用用户访问的问题ID {original_issue_id} 作为报告前缀")
                    else:
                        # 尝试从URL中提取issue_id
                        issue_match = COMPILED_REDMINE_ISSUE_PATTERN.search(url)
                        if issue_match:
                            issue_id = issue_match.group(1)
                            report_name = f"Redmine-{issue_id}-{filename}"
                            logger.info(f"[Report Analysis] Redmine报告添加前缀: {report_name}")
                        else:
                            # 如果没有issue_id，检查是否是从附件URL直接下载
                            redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                            if redmine_attach_match:
                                attachment_id = redmine_attach_match.group(1)
                                # 尝试从原始URL（如果有的话）中查找问题ID
                                # 注意：这里我们显示附件ID，但建议用户从问题页面访问
                                report_name = f'Redmine附件{attachment_id}（建议从问题页面访问以获取完整信息）'
                                logger.info(f"[Report Analysis] 直接附件下载，建议从问题页面访问: {attachment_id}")
                            else:
                                report_name = 'Redmine 附件报告'
                else:
                    # 如果是默认文件名，尝试从 URL 中提取更有意义的名称
                    if filename == 'downloaded_file.zip':
                        # 从 URL 提取附件 ID 作为报告名称
                        redmine_attach_match = re.search(r'/attachments/(\d+)', url)
                        if redmine_attach_match:
                            report_name = f'Redmine 附件 #{redmine_attach_match.group(1)}'
                        else:
                            report_name = 'Redmine 附件报告'

                result['report_name'] = report_name

                # 更新缓存：将附件ID映射到最终使用的问题ID
                if is_redmine and original_issue_id:
                    # 从URL中提取附件ID（只解析一次）
                    redmine_attach_match = COMPILED_REDMINE_ATTACHMENT_PATTERN.search(url)
                    if redmine_attach_match:
                        attachment_id_for_cache = redmine_attach_match.group(1)
                        # 只在值变化时更新缓存
                        if REDMINE_ISSUE_ID_CACHE.get(attachment_id_for_cache) != original_issue_id:
                            # 管理缓存大小
                            if len(REDMINE_ISSUE_ID_CACHE) >= REDMINE_ISSUE_ID_CACHE_MAX_SIZE:
                                # OrderedDict的popitem(last=False)删除第一个（最旧）条目
                                REDMINE_ISSUE_ID_CACHE.popitem(last=False)
                                logger.info("[Report Analysis] 缓存已满，删除最旧条目")

                            # 存入缓存
                            REDMINE_ISSUE_ID_CACHE[attachment_id_for_cache] = original_issue_id
                            logger.info(f"[Report Analysis] 更新缓存：附件 {attachment_id_for_cache} -> 问题 {original_issue_id}")

                return JSONResponse(content={
                    'success': True,
                    'data': result,
                    'filename': filename,
                    'mode': 'url'
                })
            else:
                return JSONResponse(
                    content={'success': False, 'error': '报告分析失败'},
                    status_code=500
                )

        except Exception as download_error:
            # 清理临时文件
            try:
                shutil.rmtree(temp_dir)
            except:
                pass
            raise download_error

    except Exception as e:
        logger.error(f"[Report Analysis] URL 分析失败: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': f'下载或分析失败: {str(e)}'},
            status_code=500
        )

@app.get("/api/config/redmine")
async def get_redmine_config(request: Request):
    """获取 Redmine 配置信息 - 供前端使用"""
    try:
        redmine_config = config_manager.get_redmine_config()
        return JSONResponse(content={'success': True, 'data': redmine_config})
    except ValueError as e:
        return error_response(str(e), status_code=404)
    except Exception as e:
        return error_response(f'获取 Redmine 配置失败: {str(e)}', status_code=500)

@app.post("/api/reports/extract-redmine-attachment")
async def extract_redmine_attachment(request: Request):
    """
    从 Redmine 问题页面提取第一个附件 URL

    参数：
        issue_url: Redmine 问题页面 URL

    返回：
        attachment_url: 第一个附件的完整 URL
        filename: 附件文件名
    """
    try:
        body = await request.json()
        issue_url = body.get('issue_url', '').strip()

        if not issue_url:
            return JSONResponse(
                content={'success': False, 'error': '缺少 issue_url 参数'},
                status_code=400
            )

        # 提取问题 ID
        issue_match = re.search(REDMINE_ISSUE_PATTERN, issue_url)
        if not issue_match:
            return JSONResponse(
                content={'success': False, 'error': '无效的问题 URL'},
                status_code=400
            )

        issue_id = issue_match.group(1)
        logger.info(f"[Redmine Extract] 提取问题 {issue_id} 的附件")

        # 获取存储的凭证和配置
        stored_creds = await load_redmine_credentials()
        if not stored_creds:
            return JSONResponse(
                content={'success': False, 'error': '未配置 Redmine 凭证'},
                status_code=401
            )

        # 构建 Redmine API URL（必须包含 ?include=attachments 才能获取附件）
        try:
            redmine_config = config_manager.get_redmine_config()
            base_url = redmine_config['base_url']
        except ValueError as e:
            return JSONResponse(
                content={'success': False, 'error': str(e)},
                status_code=404
            )

        api_url = f"{base_url}/issues/{issue_id}.json?include=attachments"

        username = stored_creds.get('username')
        password = stored_creds.get('password')

        headers = create_basic_auth_header(username, password)
        headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    return JSONResponse(
                        content={'success': False, 'error': f'无法访问问题页面: HTTP {response.status}'},
                        status_code=400
                    )

                data = await response.json()
                attachments = data.get('issue', {}).get('attachments', [])

                if not attachments:
                    return JSONResponse(
                        content={'success': False, 'error': '该问题没有附件'},
                        status_code=404
                    )

                # 获取第一个附件
                first_attachment = attachments[0]
                attachment_id = first_attachment.get('id')
                filename = first_attachment.get('filename', f'attachment_{attachment_id}')

                # 构建附件 URL
                attachment_url = build_redmine_download_url(base_url, attachment_id)

                logger.info(f"[Redmine Extract] 找到附件: {filename} (ID: {attachment_id})")

                return JSONResponse(content={
                    'success': True,
                    'attachment_url': attachment_url,
                    'filename': filename,
                    'attachment_id': attachment_id
                })

    except Exception as e:
        logger.error(f"[Redmine Extract] 提取附件失败: {e}")
        return JSONResponse(
            content={'success': False, 'error': f'提取附件失败: {str(e)}'},
            status_code=500
        )


async def analyze_report_file(file_path: str, temp_dir: str = None) -> Optional[Dict]:
    """分析报告文件"""
    from core.report_analyzer import ReportAnalyzer

    # 使用独立的 temp_dir 创建新的分析器实例，避免全局实例的状态污染
    if temp_dir is None:
        temp_dir = os.path.dirname(file_path)

    analyzer = ReportAnalyzer(temp_dir=temp_dir)
    return await asyncio.to_thread(analyzer.analyze_file, file_path)

async def save_redmine_credentials(username: str, password: str):
    """加密保存 Redmine 凭证"""
    try:
        config_dir = os.path.join(os.path.dirname(__file__), 'configs')
        os.makedirs(config_dir, exist_ok=True)

        config_path = os.path.join(config_dir, 'redmine_auth.json')

        encryption_key = base64.urlsafe_b64encode(hashlib.sha256(b'gms_remote_test_redmine_2024').digest())
        cipher_suite = Fernet(encryption_key)
        encrypted_password = cipher_suite.encrypt(password.encode()).decode()

        with open(config_path, 'w') as f:
            json.dump({
                'username': username,
                'encrypted_password': encrypted_password,
                'updated_at': datetime.now().isoformat()
            }, f)

        logger.info(f"[Redmine Auth] 已保存用户 {username} 的认证信息")
        return True

    except Exception as e:
        logger.error(f"[Redmine Auth] 保存凭证失败: {e}")
        return False

async def load_redmine_credentials():
    """加载存储的 Redmine 凭证和配置"""
    try:
        config_path = os.path.join(os.path.dirname(__file__), 'configs', 'redmine_auth.json')

        if not os.path.exists(config_path):
            return None

        with open(config_path, 'r') as f:
            data = json.load(f)

        # 解密密码
        from cryptography.fernet import Fernet
        import hashlib

        encryption_key = base64.urlsafe_b64encode(hashlib.sha256(b'gms_remote_test_redmine_2024').digest())
        cipher_suite = Fernet(encryption_key)

        try:
            decrypted_password = cipher_suite.decrypt(data['encrypted_password'].encode()).decode()
            # 只返回凭证信息，域名配置由config_manager统一管理
            return {
                'username': data['username'],
                'password': decrypted_password
            }
        except Exception as e:
            logger.warning(f"[Redmine Auth] 解密失败: {e}")
            return None

    except Exception as e:
        logger.error(f"[Redmine Auth] 加载凭证失败: {e}")
        return None

@app.post("/api/reports/analyze")
async def analyze_reports(
    mode: AnalysisMode = Form(default=AnalysisMode.UPLOAD),
    report_timestamp: Optional[str] = Form(default=None),
    test_name: Optional[str] = Form(default=None),
    error_message: Optional[str] = Form(default=None),
    stack_trace: Optional[str] = Form(default=None),
    module: Optional[str] = Form(default=None),
    class_names: Optional[str] = Form(default=None),
    file: Optional[UploadFile] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    files_array: Optional[List[UploadFile]] = File(default=None, alias='files[]')
):
    """
    统一的报告分析 API

    参数：
        mode: 分析模式
            - 'upload': 上传并分析报告文件（默认）
            - 'saved': 分析已保存的报告
            - 'ai': AI分析失败用例

    各模式参数：
        mode='upload':
            - file: 单个文件上传（XML、ZIP、TAR.GZ）
            - files: 多文件上传（文件夹模式）
            - files[]: 多文件上传（HTML标准格式）

        mode='saved':
            - report_timestamp: 报告时间戳

        mode='ai':
            - test_name: 测试用例名称
            - error_message: 错误消息
            - stack_trace: 堆栈跟踪（可选）
            - module: 模块名（可选）
            - class_names: 类名列表JSON字符串（可选）

    Response:
        {
            "success": true,
            "data": {...},
            "mode": "upload|saved|ai"
        }
    """
    import tempfile
    import json

    try:
        # 模式1: 分析已保存的报告
        if mode == AnalysisMode.SAVED:
            if not report_timestamp:
                return JSONResponse(
                    status_code=400,
                    content={'success': False, 'error': '缺少 report_timestamp 参数'}
                )

            # 从数据库获取报告信息
            report = test_report_db.get_report_by_timestamp(report_timestamp)

            if not report:
                return JSONResponse(
                    content={'success': False, 'error': '报告不存在'},
                    status_code=404
                )

            result_dir = report.get('result_dir')
            if not result_dir:
                return JSONResponse(
                    content={'success': False, 'error': '报告目录不存在'},
                    status_code=404
                )

            # 直接检查 XML 文件是否存在（TOCTOU 修复：合并检查）
            result_xml = os.path.join(result_dir, 'test_result.xml')
            if not await asyncio.to_thread(os.path.exists, result_xml):
                return JSONResponse(
                    content={'success': False, 'error': 'test_result.xml 不存在'},
                    status_code=404
                )

            # 使用 test_report_manager.analyze_report 以获取完整的报告信息（包括 report_name）
            result = await asyncio.to_thread(test_report_manager.analyze_report, report_timestamp)

            if not result:
                return JSONResponse(
                    content={'success': False, 'error': '报告分析失败'},
                    status_code=500
                )

            return JSONResponse(content={
                'success': True,
                'data': result,
                'mode': 'saved'
            })

        # 模式2: AI分析失败用例
        elif mode == AnalysisMode.AI:
            if not test_name:
                return JSONResponse(
                    status_code=400,
                    content={'success': False, 'error': '缺少 test_name 参数'}
                )

            # 解析 class_names JSON 字符串
            parsed_class_names = []
            if class_names:
                try:
                    parsed_class_names = json.loads(class_names)
                except json.JSONDecodeError:
                    parsed_class_names = []

            # 调用AI分析（包含OpenGrok源码搜索）
            result = await asyncio.to_thread(
                analyze_with_ai,
                test_name,
                error_message or '',
                stack_trace or '',
                module or '',
                parsed_class_names
            )

            return JSONResponse(content={
                'success': True,
                'data': result,
                'mode': 'ai'
            })

        # 模式3: 上传并分析报告文件（默认）
        else:  # mode == 'upload'
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

            # 保存上传文件到临时位置
            with tempfile.TemporaryDirectory() as temp_dir:
                # 如果是单文件（XML、ZIP、TAR.GZ）
                if len(all_files) == 1:
                    uploaded_file = all_files[0]
                    try:
                        temp_file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=False)
                    except ValueError as e:
                        return JSONResponse(
                            status_code=400,
                            content={'success': False, 'error': str(e)}
                        )

                    await save_upload_to_path(uploaded_file, temp_file_path)

                    # 使用 ReportAnalyzer 分析报告
                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    result = await asyncio.to_thread(analyzer.analyze_file, temp_file_path)

                    if result:
                        result['report_name'] = extract_report_name_from_upload([uploaded_file])
                        return JSONResponse(content={
                            'success': True,
                            'data': result,
                            'mode': 'upload'
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
                            try:
                                file_path = safe_upload_target_path(temp_dir, uploaded_file.filename, allow_nested=True)
                            except ValueError as e:
                                return JSONResponse(
                                    status_code=400,
                                    content={'success': False, 'error': str(e)}
                                )
                            await save_upload_to_path(uploaded_file, file_path)

                    # 查找 test_result.xml 或 host_log（支持两种模式）
                    analyzer = ReportAnalyzer(temp_dir=temp_dir)
                    xml_path = await asyncio.to_thread(analyzer.file_handler.find_xml_file)

                    # 如果没有 test_result.xml，尝试使用日志分析器
                    if not xml_path:
                        logger.info("未找到 test_result.xml，尝试使用HostLog日志分析器")
                        result = await asyncio.to_thread(analyzer.analyze_log_dir, temp_dir)

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
                        result['report_name'] = extract_report_name_from_upload(all_files)
                        return JSONResponse(content={
                            'success': True,
                            'data': result,
                            'mode': 'upload'
                        })

                    # 分析报告（使用 analyze_file 方法来获得正确的字典格式）
                    result = await asyncio.to_thread(analyzer.analyze_file, xml_path)

                    if result:
                        result['report_name'] = extract_report_name_from_upload(all_files)

                        return JSONResponse(content={
                            'success': True,
                            'data': result,
                            'mode': 'upload'
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



@app.get("/api/system/skills")
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
        skills_base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skills')
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


@app.get("/api/system/install-sh")
async def download_install_sh(request: Request):
    """下载 install.sh 部署脚本

    Returns:
        install.sh 脚本文件
    """
    try:
        logger.info("[INSTALL_SH_DOWNLOAD] 请求下载 install.sh")

        install_sh_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'install.sh')

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


# ==================== 测试分析辅助函数 ====================

# ==================== IP和网络工具函数 ====================
import ipaddress

def extract_ip_from_host(host_string: str) -> str:
    """从user@host或host字符串中提取IP地址"""
    return CommonUtils.extract_ip_from_host(host_string)

def are_same_network(ip1: str, ip2: str, prefix_len: int = 24) -> bool:
    """检查两个IP是否在同一网段"""
    try:
        network1 = ipaddress.IPv4Network(f"{ip1}/{prefix_len}", strict=False)
        network2 = ipaddress.IPv4Network(f"{ip2}/{prefix_len}", strict=False)
        return network1 == network2
    except (ipaddress.AddressValueError, ValueError):
        # 如果IP格式无效，回退到字符串比较
        parts1 = ip1.split('.')
        parts2 = ip2.split('.')
        if len(parts1) == 4 and len(parts2) == 4:
            return parts1[:3] == parts2[:3]
        return False

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


def construct_source_search_url(search_term, search_type='full'):
    """
    构造OpenGrok源码搜索URL

    Args:
        search_term: 搜索词（通常是类名）
        search_type: 搜索类型 (full, path, symbol, def)

    Returns:
        str: OpenGrok搜索命令提示
    """
    # 返回OpenGrok搜索命令的提示信息
    # 实际搜索通过OpenGrok插件完成
    return f"使用OpenGrok搜索: {search_term} (字段: {search_type})"


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
                'url': construct_source_search_url('UserManagerService', 'full')
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



# ==================== OpenGrok源码搜索辅助函数 ====================

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


def analyze_with_ai(test_name, error_message, stack_trace='', module='', class_names=None):
    """
    调用大模型API分析测试失败（支持多个AI提供商，自动获取源码）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块名称
        class_names: 从堆栈中提取的类名列表

    Returns:
        dict: AI分析结果（包含源码分析）
    """
    logger = logging.getLogger(__name__)

    if class_names is None:
        class_names = []

    # 从堆栈跟踪中提取失败位置（使用可复用工具）
    from core.common_utils import StackTraceUtils
    failure_location = StackTraceUtils.extract_failure_location(stack_trace)
    if failure_location:
        logger.info(f"从堆栈提取失败位置: {failure_location['file_name']}.{failure_location['file_type']}:{failure_location['line_number']}")

    source_search_results = []
    # 优先使用通用AI分析器（内部会自动进行源码搜索，无需手动重复搜索）
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
            auto_fetch_source=True  # 启用自动获取源码
        )

        if result['success']:
            provider_name = result.get('provider', 'unknown')
            # 使用统一的配置接口获取 provider 配置
            provider_config = config_manager.get_ai_provider_config(provider_name)
            provider_display = provider_config.get('name', f'{provider_name.upper()} AI') if provider_config else f'{provider_name.upper()} AI'

            # 简化响应结构，直接使用AI返回的emoji格式
            response = {
                'root_cause': result.get('root_cause', ''),
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name,
                'stack_trace': stack_trace
            }

            # 添加源码信息（如果成功获取）
            if result.get('source_info'):
                source_info = result['source_info']
                response['source_code_fetched'] = True
                response['source_file_path'] = source_info.get('file_path', '')
                response['source_url'] = source_info.get('url', '')
                response['source_project'] = source_info.get('project', '')
                logger.info(f"成功获取源码信息: {source_info.get('file_path', 'unknown')}")

            # 添加源码搜索结果（供前端显示OpenGrok链接）
            if source_search_results:
                response['source_search_results'] = source_search_results

            return response
        else:
            logger.warning(f"AI分析失败: {result.get('error')}")
            raise Exception(result.get('error', 'AI分析失败'))

    except ImportError:
        logger.warning("通用AI分析器未安装，使用基于规则的分析")
    except Exception as e:
        logger.warning(f"通用AI分析失败: {str(e)}，使用基于规则的分析")

    # AI调用失败，返回基于规则的分析
    return rule_based_analysis(test_name, error_message, stack_trace, module)


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




# ==================== VNC管理 ====================
@app.get("/api/desktop/vnc/status")
async def get_desktop_vnc_status():
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

@app.post("/api/desktop/vnc/start")
async def start_desktop_vnc(req: Optional[VNCStartRequest] = Body(default=None)):
    """启动Ubuntu主机桌面VNC（Ubuntu桌面的VNC服务）"""
    config = config_manager.load_config()
    default_host = f"{config_manager.get_ubuntu_user(config)}@{config_manager.get_ubuntu_host(config) or 'localhost'}"

    if req is None:
        # 如果没有提供请求体，使用配置文件的默认值
        host = default_host
        password = config.get('ubuntu_pswd', '')
        vnc_password = ''
    else:
        host = req.host or default_host
        password = req.password or (config.get('ubuntu_pswd', '') if host == default_host else '')
        vnc_password = req.vnc_password or ''

    result = vnc_manager.start_vnc(host, password, vnc_password)
    return JSONResponse(content=result)

@app.post("/api/vnc/start")
async def start_desktop_vnc_legacy(req: Optional[VNCStartRequest] = Body(default=None)):
    """兼容旧 Flask 前端接口：/api/vnc/start。"""
    return await start_desktop_vnc(req)

@app.post("/api/desktop/vnc/stop")
async def stop_desktop_vnc():
    """停止Ubuntu主机桌面VNC"""
    result = vnc_manager.stop_vnc()
    return JSONResponse(content=result)


NOVNC_UPSTREAM_HTTP = os.getenv("GMS_NOVNC_UPSTREAM", "http://127.0.0.1:6080").rstrip("/")
NOVNC_UPSTREAM_WS = NOVNC_UPSTREAM_HTTP.replace("http://", "ws://", 1).replace("https://", "wss://", 1)


def build_novnc_upstream_url(path: str, query_string: bytes = b"") -> str:
    upstream_path = path.lstrip("/") or "vnc.html"
    url = f"{NOVNC_UPSTREAM_HTTP}/{upstream_path}"
    if query_string:
        url = f"{url}?{query_string.decode('utf-8', errors='ignore')}"
    return url


@app.websocket("/novnc/websockify")
@app.websocket("/novnc/novnc/websockify")
async def novnc_websockify_proxy(websocket: WebSocket):
    """Proxy noVNC websocket through the 5001 origin for HTTPS/ngrok access."""
    await websocket.accept()
    upstream_url = f"{NOVNC_UPSTREAM_WS}/websockify"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(upstream_url) as upstream:
                async def client_to_upstream():
                    while True:
                        message = await websocket.receive()
                        if message.get("type") == "websocket.disconnect":
                            await upstream.close()
                            break
                        if message.get("bytes") is not None:
                            await upstream.send_bytes(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send_str(message["text"])

                async def upstream_to_client():
                    async for message in upstream:
                        if message.type == aiohttp.WSMsgType.BINARY:
                            await websocket.send_bytes(message.data)
                        elif message.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(message.data)
                        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client())
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
    except Exception as e:
        logger.error(f"[noVNC] WebSocket proxy error: {e}")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@app.get("/novnc")
@app.get("/novnc/{path:path}")
async def novnc_http_proxy(request: Request, path: str = "vnc.html"):
    """Proxy noVNC static files through the 5001 origin."""
    upstream_url = build_novnc_upstream_url(path, request.scope.get("query_string", b""))
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(upstream_url) as upstream:
                body = await upstream.read()
                return Response(
                    content=body,
                    status_code=upstream.status,
                    media_type=upstream.headers.get("content-type", "application/octet-stream"),
                    headers={"Cache-Control": "no-store"}
                )
    except aiohttp.ClientConnectorError:
        return error_response("noVNC 服务未运行，请先启动 VNC", status_code=503)
    except Exception as e:
        logger.error(f"[noVNC] HTTP proxy error: {e}")
        return error_response(f"noVNC 代理失败：{str(e)}", status_code=502)


@app.post("/api/desktop/validate")
async def validate_desktop_host(req: dict = Body(...)):
    """验证Ubuntu主机桌面连接并检查VNC服务"""
    try:
        host_connection = req.get('host', '')
        password = req.get('password', '')

        if not host_connection or '@' not in host_connection:
            return JSONResponse(
                content={'success': False, 'error': '无效的主机格式 user@ip'},
                status_code=400
            )

        user, ip = CommonUtils.parse_host_address(host_connection)

        # 检查是否是本地主机
        is_local = CommonUtils.is_local_host(ip)

        if is_local:
            # 本地主机直接验证成功
            return JSONResponse(content={
                'success': True,
                'message': '本地主机验证成功',
                'needs_password': False,
                'local': True
            })

        # 远程主机验证
        ssh = None
        try:
            # 使用 ssh_manager 获取连接
            config = {
                'hostname': ip,
                'username': user,
                'password': password,
                'timeout': 10
            }
            ssh = ssh_manager.create_connection(config)
            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH连接失败', 'needs_password': True},
                    status_code=401
                )

            return JSONResponse(content={
                'success': True,
                'message': '主机验证成功',
                'needs_password': False,
                'password': password if password else ''
            })
        finally:
            # 确保SSH连接返回到连接池
            if ssh:
                try:
                    ssh_manager.return_connection(ssh)
                except Exception as e:
                    logger.warning(f"Failed to return SSH connection: {e}")

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

@app.post("/api/desktop/validate-host")
async def validate_desktop_host_legacy(req: dict = Body(...)):
    """兼容旧 Flask 前端接口：/api/desktop/validate-host。"""
    return await validate_desktop_host(req)

@app.post("/api/devices/scrcpy")
async def show_device_screens(req: DeviceActionRequest):
    """显示设备屏幕（启动scrcpy投屏）"""
    try:
        devices = req.devices

        config = config_manager.load_config()
        ubuntu_user = config_manager.get_ubuntu_user(config)
        ubuntu_host = config_manager.get_ubuntu_host(config)

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
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

        if not devices:
            return JSONResponse(content={'success': False, 'error': 'No devices selected'}, status_code=400)

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            # Check VNC service status
            vnc_check_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
            vnc_output, _, _ = ssh_manager.execute_command(ssh, vnc_check_cmd, timeout=5)
            vnc_available = vnc_output.strip() == '200'

            # Check scrcpy availability
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

            # 启动scrcpy
            results = []
            vnc_sessions = []

            # 检查请求的设备是否已有 scrcpy 进程在运行（高效单命令版本）
            existing_devices = []
            for device_id in devices:
                is_healthy, pid_or_error = DeviceUtils.check_scrcpy_healthy(ssh, device_id)

                if is_healthy and pid_or_error:
                    existing_devices.append(device_id)
                    logger.info(f'检测到已投屏设备：{device_id} (PID: {pid_or_error})')
                else:
                    # 进程不存在或无效，清理可能存在的僵尸进程
                    DeviceUtils.kill_process(ssh, f'scrcpy.*-s {device_id}')

            # 只处理新设备
            new_devices = [d for d in devices if d not in existing_devices]

            if not new_devices:
                # 所有设备都已运行
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={
                    'success': True,
                    'message': f'所有 {len(devices)} 个设备已在投屏中',
                    'results': [{'device': d, 'started': False, 'already_running': True} for d in devices],
                    'vnc_sessions': [{'device': d, 'message': '已在运行'} for d in devices],
                    'note': '所有设备已处于投屏状态'
                })

            # 计算窗口位置：考虑已有设备，新设备放在后面
            positions = calculate_window_positions(
                existing_devices + new_devices,
                max_window_width=350
            )

            # 启动新设备（跳过已运行的）
            for idx, device_id in enumerate(sorted(existing_devices + new_devices)):
                # 只启动新设备
                if device_id not in new_devices:
                    continue

                # 计算窗口位置
                x_offset = positions['start_x'] + idx * (positions['window_width'] + positions['horizontal_gap'])
                y_offset = positions['start_y']
                window_width = positions['window_width']
                window_height = positions['window_height']
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
                await asyncio.sleep(0.3)
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
        except Exception:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error showing device screens: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
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
@app.get("/api/usbip/status")
@handle_api_errors
async def get_usbip_status(request: Request, device_host: Optional[str] = None):
    """
    获取 USB/IP 状态（支持指定主机）

    Args:
        device_host: 可选，目标主机 (user@ip 或 ip)，不传则使用当前客户端
    """
    # 确定目标主机
    if device_host:
        client_id = device_host
    else:
        client_id = get_client_id_from_request(request)
        tunnel_host, _ = resolve_public_tunnel_device_host(request, client_id)
        if tunnel_host:
            client_id = tunnel_host

    # 方法1：检查当前客户端的连接状态
    with global_state.usbip_states_lock:
        state_info = global_state.usbip_states.get(client_id, {'connected': False, 'timestamp': 0})
        connected = state_info['connected']

    # 方法2：如果当前客户端没有记录，检查是否有来自该主机的 USB/IP 设备记录
    # 这样可以支持刷新页面后恢复按钮状态，但需要匹配设备来源
    if not connected:
        with global_state.usbip_devices_source_lock:
            # 检查是否有来自该主机的 USB/IP 设备
            has_devices_from_host = any(
                device_info.get('source') == client_id
                for device_info in global_state.usbip_devices_source.values()
            )
            if has_devices_from_host:
                connected = True

    logger.info(f"[USB/IP Status] client_id={client_id}, connected={connected}, device_count={len(global_state.usbip_devices_source)}")
    return JSONResponse(content={'connected': connected})

@app.post("/api/usbip/connect")
async def start_usbip(
    req: Optional[USBIPStartRequest] = Body(default=None),
    request: Request = None,
    help: bool = Query(False)
):
    """启动 USB/IP 转发（使用usbip_manager.start_usbip高级封装方法 - 与Flask版本一致）"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/usbip/connect")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"}
            )

    try:
        config = config_manager.load_config()
        client_id = get_client_id_from_request(request)

        # 从请求中获取参数
        request_data = req.model_dump() if req else {}

        usbip_attach_host = None
        tunnel_host = None
        public_client_mode = False

        # 获取device_host（优先级：请求参数 > 公网客户端隧道 > 配置文件 > client_id）
        explicit_device_host = request_data.get('device_host')
        if explicit_device_host:
            device_host = explicit_device_host
        else:
            tunnel_host, tunnel_usbip_host = resolve_public_tunnel_device_host(request, client_id)
            if tunnel_host:
                device_host = tunnel_host
                usbip_attach_host = tunnel_usbip_host
                public_client_mode = is_public_origin_request(request)
                logger.info(f"[USB/IP] Public client tunnel mode enabled: {device_host} attach={usbip_attach_host}")
            else:
                if is_public_origin_request(request):
                    payload = public_client_required_payload(request)
                    return ApiResponse.error(
                        payload['error'],
                        status_code=428,
                        public_client_required=True,
                        agent_install_url=payload['agent_install_url'],
                        agent_connected=False
                    )
                device_host = config.get('usbip_device_host') or config.get('device_host') or client_id

        logger.info(f"[USB/IP] Using device_host: {device_host}")

        # 保存原始 Windows 设备主机地址，用于记录设备来源
        windows_device_host = device_host

        if public_client_mode:
            tunnel_status = public_client_tunnel.get_status()
            if not tunnel_status.get('connected'):
                payload = public_client_required_payload(request)
                return ApiResponse.error(
                    payload['error'],
                    status_code=428,
                    public_client_required=True,
                    agent_install_url=payload['agent_install_url'],
                    agent_connected=False
                )
            windows_usbipd = await probe_public_client_usbipd()
            if not windows_usbipd.get('installed'):
                return JSONResponse(content={
                    'success': False,
                    'error': '公网客户端已连接，但 Windows 主机未安装 usbipd',
                    'install_guide': USBIPD_INSTALL_GUIDE.format(install_cmd=USBIPD_INSTALL_CMD),
                    'installed': False
                })
            if not await probe_public_client_admin():
                return ApiResponse.error(
                    '公网客户端 agent 没有管理员权限，无法执行 usbipd bind。请关闭当前 agent 窗口，用管理员 PowerShell 重新运行 GMS 公网客户端命令。',
                    status_code=403
                )

            taskkill_result = await public_client_tunnel.run_windows_command('taskkill /F /IM adb.exe /T', timeout=15)
            if taskkill_result.get('returncode', -1) not in (0, 128, 255):
                logger.warning(f"[USB/IP] taskkill adb failed: {taskkill_result}")

            list_result = await public_client_tunnel.run_windows_command('usbipd list', timeout=20)
            if list_result.get('returncode', -1) != 0:
                return ApiResponse.error(
                    f"获取 Windows usbipd 设备列表失败: {list_result.get('stderr') or list_result.get('stdout') or 'unknown error'}",
                    status_code=500
                )

            vid_pid = config.get('usbip_vid_pid')
            usbipd_list_output = '\n'.join([
                list_result.get('stdout') or '',
                list_result.get('stderr') or '',
            ]).strip()
            logger.info(f"[USB/IP] usbipd list output via public agent:\n{usbipd_list_output}")
            busids = parse_usbipd_android_busids(usbipd_list_output, vid_pid=vid_pid)
            busid_statuses = parse_usbipd_busid_statuses(usbipd_list_output)
            if not busids:
                return ApiResponse.error(
                    f'未找到Android设备。usbipd list 输出: {usbipd_list_output or "empty"}',
                    status_code=404
                )

            bound_busids = []
            newly_bound = False
            for busid in busids:
                if busid_statuses.get(busid) in ('shared', 'attached'):
                    logger.info(f"[USB/IP] {busid} is already {busid_statuses.get(busid)}, skip bind")
                    bound_busids.append(busid)
                    continue
                bind_result = await public_client_tunnel.run_windows_command(f'usbipd bind --busid {busid}', timeout=30)
                if bind_result.get('returncode', -1) != 0:
                    stderr = bind_result.get('stderr') or bind_result.get('stdout') or 'unknown error'
                    logger.warning(f"[USB/IP] bind failed for {busid}: {stderr}")
                    # 如果已经共享，继续；否则直接报错
                    if 'already' not in stderr.lower() and 'shared' not in stderr.lower():
                        if 'access denied' in stderr.lower() or 'administrator privileges' in stderr.lower():
                            return ApiResponse.error(
                                '公网客户端 agent 没有管理员权限，无法执行 usbipd bind。请关闭所有 GMS agent PowerShell 窗口，用管理员 PowerShell 重新运行 GMS 公网客户端命令。',
                                status_code=403
                            )
                        return ApiResponse.error(f'设备绑定失败: {busid} - {stderr}', status_code=500)
                else:
                    newly_bound = True
                bound_busids.append(busid)
            if newly_bound:
                await asyncio.sleep(2)

            result = await asyncio.to_thread(
                attach_public_usbip_on_ubuntu,
                config,
                usbip_attach_host or '127.0.0.1',
                bound_busids
            )
            if not result.get('success'):
                if result.get('rollback'):
                    logger.info("[USB/IP] Public attach failed; keeping Windows usbipd bindings for faster retry")
                return ApiResponse.error(result.get('error') or 'USB/IP 连接失败', status_code=500)
        else:
            # 获取密码
            device_password = request_data.get('device_password') or find_device_host_password(config, device_host) or config.get('device_pswd', '')

            if not device_password:
                return ApiResponse.error(
                    f'未找到 {device_host} 的SSH凭据，请先在登录页面输入SSH密码',
                    status_code=401,
                    need_password=True,
                    device_host=device_host
                )

            # 直接调用高级封装方法（简化实现，与Flask版本一致）
            result = usbip_manager.start_usbip(device_host, device_password, usbip_attach_host=usbip_attach_host)

        # 更新连接状态（使用线程锁 - 与Flask版本一致）
        if result.get('success'):
            with global_state.usbip_states_lock:
                global_state.usbip_states[device_host] = {'connected': True, 'timestamp': time.time()}
            logger.info(f"[USB/IP Start] Set connected=True for device_host={device_host}")

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

                # 持久化USB/IP设备来源到配置文件（修复长时间连接后来源类型丢失的问题）
                try:
                    existing_dynamic = config_manager._load_dynamic_config() or {}
                    usbip_sources = existing_dynamic.get('usbip_devices_source', {})

                    # 更新设备来源
                    for device_id in device_list:
                        usbip_sources[device_id] = {
                            'source': windows_device_host,
                            'timestamp': time.time()
                        }

                    # 保存到配置文件
                    existing_dynamic['usbip_devices_source'] = usbip_sources
                    if config_manager.save_dynamic_config(existing_dynamic):
                        logger.info(f"[USB/IP Start] Persisted device sources for {len(device_list)} devices")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to persist device sources: {e}")


        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting USB/IP: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/usbip/disconnect")
async def stop_usbip(request: Request, req: Optional[USBIPDisconnectRequest] = Body(default=None)):
    """停止 USB/IP 转发（支持指定主机）

    Args:
        req: 请求体，包含 device_host 参数（可选）
    """
    config = config_manager.load_config()
    client_id = get_client_id_from_request(request)
    public_client_mode = False

    # 使用提供的主机或当前客户端
    if req and req.device_host:
        config['device_host'] = req.device_host
    else:
        tunnel_host, _ = resolve_public_tunnel_device_host(request, client_id)
        if tunnel_host:
            config['device_host'] = tunnel_host
            public_client_mode = is_public_origin_request(request)
        else:
            config['device_host'] = client_id

    device_password = find_device_host_password(config, config['device_host'])
    if not device_password:
        device_password = config.get('device_pswd', '')

    if device_password:
        config['device_pswd'] = device_password

    devices_to_remove: List[str] = []

    try:
        if public_client_mode:
            if not public_client_tunnel.is_connected():
                return ApiResponse.error(
                    '公网客户端未连接，无法断开 USB/IP',
                    status_code=428,
                    public_client_required=True,
                    agent_connected=False,
                    agent_install_url=f"{get_request_base_url(request)}/api/public-client/install.ps1"
                )
            ubuntu_ssh = usbip_manager.ssh_manager.get_connection(config)
            if ubuntu_ssh:
                try:
                    detach_ubuntu_usbip_ports(ubuntu_ssh, '127.0.0.1', detach_all=True)
                    usbip_manager.ssh_manager.return_connection(ubuntu_ssh)
                except Exception as e:
                    ubuntu_ssh.close()
                    logger.warning(f"[USB/IP Stop] detach Ubuntu usbip ports failed: {e}")
            logger.info("[USB/IP Stop] Public mode keeps Windows usbipd bindings; only Ubuntu attach is detached")
            await asyncio.sleep(1)
            with global_state.usbip_devices_source_lock:
                devices_to_remove = [
                    device_id for device_id, device_info in global_state.usbip_devices_source.items()
                    if device_info.get('source') == config['device_host']
                ]
                for device_id in devices_to_remove:
                    del global_state.usbip_devices_source[device_id]
                    logger.info(f"[USB/IP Stop] Removed device source: {device_id} from {config['device_host']}")
        else:
            with DeviceSSHConnection(config) as win_ssh:
                ssh_manager.execute_command(win_ssh, 'usbipd unbind --all', timeout=10)
                await asyncio.sleep(2)

            # 清除来自该主机的USB/IP设备来源记录（从多个位置）
            with global_state.usbip_devices_source_lock:
                logger.info(f"[USB/IP Stop] Looking for devices from {config['device_host']} in usbip_devices_source")
                logger.info(f"[USB/IP Stop] Current sources: {list(global_state.usbip_devices_source.items())}")

                devices_to_remove = [
                    device_id for device_id, device_info in global_state.usbip_devices_source.items()
                    if device_info.get('source') == config['device_host']
                ]

                logger.info(f"[USB/IP Stop] Found {len(devices_to_remove)} devices to remove: {devices_to_remove}")

                for device_id in devices_to_remove:
                    del global_state.usbip_devices_source[device_id]
                    logger.info(f"[USB/IP Stop] Removed device source: {device_id} from {config['device_host']}")

            # 同时从 usbip_manager.device_sources 中清除
            for device_id in devices_to_remove:
                if device_id in usbip_manager.device_sources:
                    del usbip_manager.device_sources[device_id]
                    logger.info(f"[USB/IP Stop] Removed device source from usbip_manager: {device_id}")

            # 持久化更新的设备来源到配置文件
            if devices_to_remove:
                try:
                    existing_dynamic = config_manager._load_dynamic_config() or {}
                    usbip_sources = existing_dynamic.get('usbip_devices_source', {})

                    # 从配置文件中删除已断开的设备
                    for device_id in devices_to_remove:
                        if device_id in usbip_sources:
                            del usbip_sources[device_id]

                    # 保存更新后的配置
                    existing_dynamic['usbip_devices_source'] = usbip_sources
                    if config_manager.save_dynamic_config(existing_dynamic):
                        logger.info(f"[USB/IP Stop] Persisted device source removal for {len(devices_to_remove)} devices")
                except Exception as e:
                    logger.warning(f"[USB/IP Stop] Failed to persist device source removal: {e}")

        # 更新 USB/IP 连接状态
        with global_state.usbip_states_lock:
            global_state.usbip_states[config['device_host']] = {'connected': False, 'timestamp': time.time()}

        # 构建断开设备的详细信息
        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        # 主动触发设备列表刷新（避免等待 USB 监控器检测到变化，减少延迟）
        await notify_device_change(devices_to_remove, "USB/IP Stop")

        return JSONResponse(content={
            'success': True,
            'message': f'本地设备已断开{disconnected_devices_info}'
        })
    except HTTPException:
        # 无法连接到 Windows，只清除连接状态和设备来源记录
        with global_state.usbip_devices_source_lock:
            devices_to_remove = [
                device_id for device_id, device_info in global_state.usbip_devices_source.items()
                if device_info.get('source') == config['device_host']
            ]
            for device_id in devices_to_remove:
                del global_state.usbip_devices_source[device_id]
                logger.info(f"[USB/IP Stop] Removed device source: {device_id} from {config['device_host']}")

        # 同时从 usbip_manager.device_sources 中清除
        for device_id in devices_to_remove:
            if device_id in usbip_manager.device_sources:
                del usbip_manager.device_sources[device_id]
                logger.info(f"[USB/IP Stop] Removed device source from usbip_manager: {device_id}")

        # 持久化更新的设备来源到配置文件
        if devices_to_remove:
            try:
                existing_dynamic = config_manager._load_dynamic_config() or {}
                usbip_sources = existing_dynamic.get('usbip_devices_source', {})

                # 从配置文件中删除已断开的设备
                for device_id in devices_to_remove:
                    if device_id in usbip_sources:
                        del usbip_sources[device_id]

                # 保存更新后的配置
                existing_dynamic['usbip_devices_source'] = usbip_sources
                if config_manager.save_dynamic_config(existing_dynamic):
                    logger.info(f"[USB/IP Stop] Persisted device source removal for {len(devices_to_remove)} devices")
            except Exception as e:
                logger.warning(f"[USB/IP Stop] Failed to persist device source removal: {e}")

        with global_state.usbip_states_lock:
            global_state.usbip_states[config['device_host']] = {'connected': False, 'timestamp': time.time()}

        # 构建断开设备的详细信息
        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        # 主动触发设备列表刷新（避免等待 USB 监控器检测到变化，减少延迟）
        await notify_device_change(devices_to_remove, "USB/IP Stop")

        return JSONResponse(content={'success': True, 'message': f'本地设备已断开{disconnected_devices_info}'})

@app.post("/api/usbip/install")
async def install_usbipd(request: Request, device_host: Optional[str] = None):
    """Install usbipd to Windows host (supports specifying target host)

    Args:
        device_host: Optional target host (format: user@ip or ip). If not provided, uses current client
    """
    try:
        config = config_manager.load_config()
        client_id = get_client_id_from_request(request)

        # Use provided host or fallback to current client
        if device_host:
            config['device_host'] = device_host
        else:
            tunnel_host, _ = resolve_public_tunnel_device_host(request, client_id)
            if is_public_origin_request(request):
                tunnel_status = public_client_tunnel.get_status()
                if tunnel_status.get('connected'):
                    windows_usbipd = await probe_public_client_usbipd()
                    installed = bool(windows_usbipd.get('installed'))
                    logger.info(
                        f"[USB/IP Install] public-client agent status: connected={tunnel_status.get('connected')}, "
                        f"installed={installed}"
                    )
                    if installed:
                        return JSONResponse(content={
                            'success': True,
                            'installed': True,
                            'running': True,
                            'version': windows_usbipd.get('version') or '',
                            'message': f"usbipd 已安装{('，版本: ' + windows_usbipd.get('version')) if windows_usbipd.get('version') else ''}"
                        })
                    return JSONResponse(content={
                        'success': False,
                        'installed': False,
                        'running': False,
                        'install_guide': USBIPD_INSTALL_GUIDE.format(install_cmd=USBIPD_INSTALL_CMD),
                        'error': '公网客户端已连接，但 Windows 主机未安装 usbipd'
                    })

            config['device_host'] = tunnel_host or client_id

        # Auto-find password from client_ssh_credentials
        device_password = find_device_host_password(config, config['device_host'])
        if not device_password:
            device_password = config.get('device_pswd', '')

        if device_password:
            config['device_pswd'] = device_password

        # Connect to Windows host and install
        with DeviceSSHConnection(config) as win_ssh:
            result = usbip_manager.install_usbipd(win_ssh, config)
            return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Error installing usbipd: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== VPN管理 ====================
@app.get("/api/ssh/sshd")
@handle_api_errors
async def check_ssh_sshd(request: Request, device_host: Optional[str] = Query(None, description="设备主机地址 (user@ip 格式，如 user@192.168.1.100)")):
    """检查SSH服务状态（如未安装则返回安装指南）

    通过SSH连接到Windows客户端检查SSHD服务状态。
    支持查询参数 device_host 来检查指定主机的状态。
    注意：device_host 必须是 user@ip 格式，例如 user@192.168.1.100
    """
    def exec_ssh_cmd(ssh, cmd):
        """执行SSH命令并返回输出"""
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
        return stdout.read().decode('utf-8', errors='ignore').strip()

    config = config_manager.load_config()
    # 优先使用查询参数中的 device_host，否则使用请求中的客户端ID
    if not device_host:
        client_id = get_client_id_from_request(request)
        tunnel_host, _ = resolve_public_tunnel_device_host(request, client_id)
        if is_public_origin_request(request):
            tunnel_status = public_client_tunnel.get_status()
            windows_sshd = tunnel_status.get('windows_sshd') or {}
            if tunnel_status.get('connected') and windows_sshd:
                installed = bool(windows_sshd.get('installed'))
                running = bool(windows_sshd.get('running'))
                logger.info(
                    f"[SSHD Check] public-client agent status: connected={tunnel_status.get('connected')}, "
                    f"installed={installed}, running={running}"
                )
                return JSONResponse(content={
                    'success': True,
                    'installed': installed,
                    'running': running,
                    'install_guide': None if installed else SSHD_INSTALL_GUIDE
                })
            if tunnel_status.get('connected') and not windows_sshd:
                logger.warning("[SSHD Check] public-client agent connected but did not report windows_sshd status")
                return JSONResponse(content={
                    'success': True,
                    'installed': None,
                    'running': None,
                    'install_guide': None,
                    'error': '公网客户端已连接，但当前 agent 版本未上报 Windows SSHD 状态。请重新运行最新的安装脚本更新 agent。'
                })

        if not tunnel_host and is_public_origin_request(request):
            payload = public_client_required_payload(request)
            return JSONResponse(content={
                'success': False,
                'checkable': False,
                'installed': None,
                'running': None,
                'install_guide': None,
                **payload,
            }, status_code=428)
        device_host = tunnel_host or client_id

    # 验证 device_host 格式
    if device_host and '@' not in device_host:
        return JSONResponse(content={
            'success': False,
            'error': f'设备主机格式错误："{device_host}"。正确格式应为 user@ip，例如 user@192.168.1.100',
            'installed': False,
            'running': False
        }, status_code=400)

    config['device_host'] = device_host
    config['device_pswd'] = find_device_host_password(config, device_host) or config.get('device_pswd', '')

    try:
        with DeviceSSHConnection(config) as ssh:
            # 检查是否已安装（先找文件，再查服务）
            installed = bool(exec_ssh_cmd(ssh, "where sshd.exe 2>nul"))
            if not installed:
                installed = bool(exec_ssh_cmd(ssh, "sc query sshd 2>nul | findstr /C:\"RUNNING\" /C:\"STOPPED\""))

            # 检查是否运行中
            running = bool(exec_ssh_cmd(ssh, "sc query sshd | findstr /C:\"RUNNING\" 2>nul"))

            logger.info(f"[SSHD Check] {device_host}: installed={installed}, running={running}")

            return JSONResponse(content={
                'success': True,
                'installed': installed,
                'running': running,
                'install_guide': SSHD_INSTALL_GUIDE if not installed else None
            })
    except HTTPException:
        logger.warning(f"[SSHD Check] Cannot connect to {device_host}")
        return JSONResponse(content={
            "success": True,
            "installed": False,
            "running": False,
            "install_guide": SSHD_INSTALL_GUIDE,
            "error": "无法连接到SSH服务，请检查网络连接和Windows客户端状态"
        })
    except Exception as e:
        logger.error(f"[SSHD Check] Error: {e}")
        return JSONResponse(content={
            "success": True,
            "installed": False,
            "running": False,
            "install_guide": SSHD_INSTALL_GUIDE,
            "error": f"检查SSHD状态时发生错误: {str(e)}"
        })

@app.get("/api/ssh/route")
@handle_api_errors
async def check_ssh_route(request: Request):
    """检查网络路由 - 检查测试主机和设备主机是否在同一网段"""
    config = config_manager.load_config()

    ubuntu_host = config.get("ubuntu_host", "")
    client_ip = get_client_ip(request)

    if not ubuntu_host or client_ip == 'unknown':
        return JSONResponse(content={
            'success': False,
            'error': '无法获取主机IP地址'
        }, status_code=400)

    ubuntu_ip = extract_ip_from_host(ubuntu_host)
    device_ip = extract_ip_from_host(client_ip)

    same_network = are_same_network(ubuntu_ip, device_ip)
    need_route = not same_network

    # 先测试实际连通性
    connectivity_ok = False
    latency = None
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ['ping', '-c', '1', '-W', '2', ubuntu_ip],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            connectivity_ok = True
            # 提取延迟时间
            match = re.search(r'time=([\d.]+)', result.stdout)
            if match:
                latency = f"{match.group(1)}ms"
    except Exception as e:
        logger.warning(f"Ping test failed: {e}")

    # 只有网段不同且实际不通时才提示添加路由
    if need_route and not connectivity_ok:
        try:
            ubuntu_network_obj = ipaddress.IPv4Network(f"{ubuntu_ip}/24", strict=False)
            device_network_obj = ipaddress.IPv4Network(f"{device_ip}/24", strict=False)
            ubuntu_network = str(ubuntu_network_obj.network_address)
            device_network = str(device_network_obj.network_address)
        except (ipaddress.AddressValueError, ValueError):
            ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'
            device_network = '.'.join(device_ip.split('.')[:3]) + '.0'

        route_commands = {
            'windows': [
                f"route add {ubuntu_network} mask 255.255.255.0 {device_ip}",
                f"route add {device_network} mask 255.255.255.0 {ubuntu_ip}",
                "# 检查路由表: route print",
                f"# 删除路由表: route delete {ubuntu_network}",
                f"# 删除路由表: route delete {device_network}"
            ],
            'linux': [
                f"sudo ip route add {ubuntu_network}/24 via {device_ip}",
                f"sudo ip route add {device_network}/24 via {ubuntu_ip}",
                "# 检查路由表: ip route show",
                f"# 删除路由表: sudo ip route del {ubuntu_network}/24",
                f"# 删除路由表: sudo ip route del {device_network}/24"
            ]
        }

        return JSONResponse(content={
            'success': True,
            'same_network': False,
            'need_route': True,
            'connectivity_ok': False,
            'message': f'⚠️ 网段不同且无法连通: {ubuntu_ip} (网段: {ubuntu_network}/24) ↔ {device_ip} (网段: {device_network}/24)',
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'ubuntu_network': ubuntu_network,
            'device_network': device_network,
            'route_commands': route_commands,
            'warning': '测试主机和设备主机不在同一网段且无法连通，建议添加路由表'
        })
    elif need_route and connectivity_ok:
        # 网段不同但已连通，路由已配置
        try:
            ubuntu_network_obj = ipaddress.IPv4Network(f"{ubuntu_ip}/24", strict=False)
            ubuntu_network = str(ubuntu_network_obj.network_address)
        except (ipaddress.AddressValueError, ValueError):
            ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'

        return JSONResponse(content={
            'success': True,
            'same_network': False,
            'need_route': False,
            'connectivity_ok': True,
            'latency': latency,
            'message': f'✅ 网段不同但已连通: {ubuntu_ip} (延迟: {latency}) ↔ {device_ip}',
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'network': ubuntu_network,
            'note': '网段不同但路由已配置，网络通信正常'
        })
    else:
        # 同网段
        try:
            ubuntu_network_obj = ipaddress.IPv4Network(f"{ubuntu_ip}/24", strict=False)
            ubuntu_network = str(ubuntu_network_obj.network_address)
        except (ipaddress.AddressValueError, ValueError):
            ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'

        return JSONResponse(content={
            'success': True,
            'same_network': True,
            'need_route': False,
            'connectivity_ok': connectivity_ok,
            'latency': latency,
            'message': f'✅ 网段相同: {ubuntu_ip} ↔ {device_ip}' + (f' (延迟: {latency})' if latency else ''),
            'ubuntu_ip': ubuntu_ip,
            'device_ip': device_ip,
            'network': ubuntu_network
        })


def _validate_ip_address(ip: str) -> bool:
    """验证IPv4地址格式和范围"""
    try:
        ipaddress.IPv4Address(ip)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _extract_network(ip: str) -> str:
    """从IP地址提取网络地址"""
    try:
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return str(network.network_address)
    except (ipaddress.AddressValueError, ValueError):
        # Fallback to string manipulation for edge cases
        return '.'.join(ip.split('.')[:3]) + '.0'


def _parse_ping_output(ping_output: str, exit_status: int) -> tuple[bool, str]:
    """解析ping输出，返回(可达性, 延迟)"""
    if exit_status == 0:
        if "0% packet loss" in ping_output:
            # 无丢包
            time_match = _PING_RTT_PATTERN.search(ping_output)
            if time_match:
                return True, f"{time_match.group(1)}ms"
            else:
                avg_match = _PING_AVG_PATTERN.search(ping_output)
                if avg_match:
                    return True, f"{avg_match.group(1)}ms"
                else:
                    return True, '<10ms'
        elif "packet loss" in ping_output:
            # 部分丢包
            loss_match = _PING_LOSS_PATTERN.search(ping_output)
            if loss_match:
                loss_percent = int(loss_match.group(1))
                if loss_percent < 100:
                    return True, f'{loss_percent}% 丢包'
                else:
                    return False, 'N/A (100% 丢包)'
            else:
                return False, 'N/A'
        else:
            return True, 'N/A'
    else:
        # ping失败
        if "100% packet loss" in ping_output or "Network is unreachable" in ping_output:
            return False, 'N/A (不可达)'
        else:
            return False, 'N/A'

def _generate_route_commands(test_network: str, target_network: str, test_host_ip: str) -> dict:
    """生成路由命令

    网络拓扑说明（示例）：
    - 测试主机: 配置文件中的 ubuntu_host (运行GMS服务)
    - 客户端: 用户浏览器所在电脑的IP
    - 测试主机网关: 通常为测试主机网段的.1地址

    路由目的：让测试主机能够访问客户端网段
    """
    # 推测网关地址（通常是网段的第一个IP）
    test_gateway = '.'.join(test_network.split('.')[:3]) + '.1'

    return {
        'windows': [
            "# 在测试主机上执行以下命令:",
            "# 添加到客户端网段的路由（通过测试主机网关）",
            f"route add {target_network} mask 255.255.255.0 {test_gateway}",
            "# 检查路由表: route print",
            f"# 删除路由: route delete {target_network}"
        ],
        'linux': [
            "# 在测试主机上执行以下命令:",
            "# 添加到客户端网段的路由（通过测试主机网关）",
            f"sudo ip route add {target_network}/24 via {test_gateway}",
            "# 检查路由表: ip route show",
            f"# 删除路由: sudo ip route del {target_network}/24"
        ]
    }

@app.post("/api/ssh/ping")
async def ping_route_test(request: Request):
    """测试测试主机和客户端的网络连通性"""
    try:
        # 获取请求数据
        data = await request.json()
        test_host_ip = data.get('test_host_ip', '').strip()
        client_ip = data.get('client_ip', '').strip()

        # 验证IP格式
        if not _validate_ip_address(test_host_ip) or not _validate_ip_address(client_ip):
            return JSONResponse(
                content={'success': False, 'error': 'IP地址格式不正确'},
                status_code=400
            )

        # 检查是否在同一网段
        test_network = _extract_network(test_host_ip)
        client_network = _extract_network(client_ip)
        same_network = (test_network == client_network)

        # 尝试真正的ping测试（从测试主机ping客户端）
        reachable = False
        latency = None
        ping_output = ""

        if same_network:
            # 同一网段，理论上可达
            reachable = True
            latency = '<1ms (同一网段)'
        else:
            # 不同网段，需要从测试主机执行ping来验证连通性
            try:
                config = config_manager.load_config()
                ssh = ssh_manager.get_connection(config)
                if ssh:
                    # 从测试主机ping客户端IP
                    ping_cmd = f"ping -c 3 -W 2 {client_ip}"
                    stdin, stdout, stderr = ssh.exec_command(ping_cmd, timeout=10)

                    # 读取ping输出（限制大小防止内存溢出）
                    ping_output = stdout.read(8192).decode('utf-8', errors='ignore')   # 8KB sufficient for ping
                    stderr.read(2048).decode('utf-8', errors='ignore')   # 2KB sufficient for errors
                    exit_status = stdout.channel.recv_exit_status()

                    ssh_manager.return_connection(ssh)

                    # 解析ping结果
                    reachable, latency = _parse_ping_output(ping_output, exit_status)

                    logger.info(f"Ping test from {test_host_ip} to {client_ip}: reachable={reachable}, latency={latency}")

            except Exception as e:
                logger.warning(f"Ping test failed: {e}")
                reachable = False
                latency = 'N/A'

        # 准备路由命令（检查测试主机是否需要添加路由到客户端网段）
        route_commands = None
        test_client_different = (test_network != client_network)

        if test_client_different:
            # 测试主机和客户端不在同一网段，需要添加路由
            route_commands = _generate_route_commands(test_network, client_network, test_host_ip)

        return JSONResponse(content={
            'success': True,
            'reachable': reachable,
            'latency': latency,
            'same_network': same_network,
            'test_host_ip': test_host_ip,
            'client_ip': client_ip,
            'test_network': test_network,
            'client_network': client_network,
            'test_client_different': test_client_different,
            'route_commands': route_commands
        })

    except Exception as e:
        logger.error(f"Error in ping route test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/terminal/open")
async def get_ssh_terminal_info():
    """获取SSH终端连接信息

    返回测试主机的SSH连接信息，方便用户手动建立SSH连接
    """
    try:
        config = config_manager.load_config()

        # Cache config values to avoid redundant get() calls
        ssh_host = config_manager.get_ubuntu_host(config) or 'localhost'
        ssh_user = config_manager.get_ubuntu_user(config)
        connection_string = f"ssh {ssh_user}@{ssh_host}"

        return JSONResponse(content={
            'success': True,
            'host': ssh_host,
            'user': ssh_user,
            'port': 22,
            'connection_command': connection_string,
            'instructions': [
                f"1. 复制连接命令: {connection_string}",
                "2. 在终端中粘贴并执行连接命令",
                "3. 输入密码或使用SSH密钥认证",
                "4. 连接成功后，您将获得测试主机的终端访问权限"
            ]
        })

    except Exception as e:
        logger.error(f"Error getting SSH terminal info: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


def get_primary_vpn_target(config: Dict[str, Any]) -> str:
    vpn_target = config.get('vpn_target', 'www.google.com')
    if isinstance(vpn_target, list):
        vpn_target = vpn_target[0] if vpn_target else 'www.google.com'
    return str(vpn_target or 'www.google.com')


def check_local_vpn_connected(vpn_target: str) -> bool:
    for _ in range(2):
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '1', vpn_target],
                capture_output=True,
                text=True,
                timeout=3
            )
            output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 and ('1 received' in output or 'bytes from' in output):
                return True
        except Exception as e:
            logger.debug(f"[VPN Status] Local ping check failed: {e}")

    try:
        result = subprocess.run(
            ['nmcli', '-t', '-f', 'NAME,TYPE,STATE', 'connection', 'show', '--active'],
            capture_output=True,
            text=True,
            timeout=3
        )
        output = result.stdout.lower()
        return 'vpn' in output or 'tun' in output or 'tap' in output
    except Exception as e:
        logger.debug(f"[VPN Status] Local nmcli check failed: {e}")
        return False


def get_configured_vpn_connection_name(config: Dict[str, Any]) -> str:
    return str(config.get('vpn_connection_name') or os.getenv('GMS_VPN_CONNECTION_NAME', '')).strip()


def parse_first_vpn_connection(nmcli_output: str) -> str:
    for line in nmcli_output.splitlines():
        parts = line.split(':')
        if len(parts) >= 2 and 'vpn' in parts[1].lower():
            return parts[0].replace(r'\:', ':').strip()
    return ""


async def execute_config_host_command(config: Dict[str, Any], ssh, command: str, timeout: int) -> Tuple[str, str, int]:
    if is_config_host_local(config):
        return await asyncio.to_thread(run_local_shell_command, command, timeout)
    return ssh_manager.execute_command(ssh, command, timeout=timeout)


async def resolve_vpn_connection_name(config: Dict[str, Any], ssh=None, active_only: bool = False) -> str:
    vpn_name = get_configured_vpn_connection_name(config)
    if vpn_name:
        return vpn_name

    if active_only:
        cmd = "nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null"
    else:
        cmd = "nmcli -t -f NAME,TYPE connection show 2>/dev/null"
    output, _, _ = await execute_config_host_command(config, ssh, cmd, timeout=5)
    return parse_first_vpn_connection(output)


@app.get("/api/vpn/status")
@handle_api_errors
async def get_vpn_status():
    """获取VPN连接状态（多次ping提高可靠性）"""
    config = config_manager.load_config()
    vpn_target = get_primary_vpn_target(config)

    if is_config_host_local(config):
        connected = await asyncio.to_thread(check_local_vpn_connected, vpn_target)
        return JSONResponse(content={
            "success": True,
            "connected": connected,
            "source": "local"
        })

    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return JSONResponse(
            content={"success": False, "error": "SSH连接失败"},
            status_code=500
        )

    max_attempts = 2
    for attempt in range(max_attempts):
        output, error, code = ssh_manager.execute_command(
            ssh,
            f"ping -c 1 -W 1 {vpn_target} 2>&1",  # 减少-W timeout从2到1
            timeout=3  # 减少timeout从5到3
        )

        # 检查ping结果（成功则立即返回）
        if '1 packets transmitted, 1 received' in output or '1 received' in output or 'bytes from' in output:
            ssh_manager.return_connection(ssh)
            logger.info(f"[VPN Status] {vpn_target}: connected (attempt {attempt + 1})")
            return JSONResponse(content={"success": True, "connected": True})

    # 所有尝试都失败，尝试通过nmcli检查VPN连接状态
    try:
        nmcli_output, _, _ = ssh_manager.execute_command(
            ssh,
            "nmcli -t -f NAME,TYPE,STATE connection show --active 2>&1",
            timeout=3  # 减少timeout从5到3
        )

        # 检查是否有VPN类型的活跃连接
        if 'vpn' in nmcli_output.lower() or 'tun' in nmcli_output.lower() or 'tap' in nmcli_output.lower():
            ssh_manager.return_connection(ssh)
            logger.info(f"[VPN Status] VPN detected via nmcli: {nmcli_output.strip()}")
            return JSONResponse(content={"success": True, "connected": True})
    except Exception as e:
        logger.warning(f"[VPN Status] nmcli check failed: {e}")

    # 所有尝试都失败
    ssh_manager.return_connection(ssh)
    logger.info(f"[VPN Status] {vpn_target}: disconnected (0/{max_attempts} successful)")
    return JSONResponse(content={"success": True, "connected": False})

@app.post("/api/vpn/connect")
async def connect_vpn(
    req: Optional[VPNConnectRequest] = Body(default=None)
):
    """连接VPN（使用nmcli）

    请求体完全可选，兼容前端无参数调用
    """
    try:
        config = config_manager.load_config()
        ssh = None
        if not is_config_host_local(config):
            ssh = ssh_manager.get_connection(config)

        if not is_config_host_local(config) and not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            vpn_name = await resolve_vpn_connection_name(config, ssh, active_only=False)
            if not vpn_name:
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": "未配置 VPN 连接名称，且未发现 NetworkManager VPN 连接。请设置 GMS_VPN_CONNECTION_NAME 或 configs/config.json 的 vpn_connection_name。"
                    },
                    status_code=400
                )

            # 使用nmcli连接VPN
            vpn_cmd = f"sudo nmcli connection up {shlex.quote(vpn_name)}"
            output, error, code = await execute_config_host_command(
                config,
                ssh,
                vpn_cmd,
                20
            )

            await asyncio.sleep(2)

            # 检查连接结果
            if code == 0:
                is_connected = True
                message = 'VPN 连接成功'
            elif 'already active' in (error or ''):
                is_connected = True
                message = 'VPN 已连接'
            elif 'unknown connection' in (error or ''):
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": f"VPN 连接 {vpn_name} 不存在，请先在 NetworkManager 中配置"
                    },
                    status_code=404
                )
            else:
                is_connected = False
                message = f'VPN 连接失败: {error or output}'

            if ssh:
                ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": is_connected,
                "connected": is_connected,
                "message": message,
                "vpn_connection_name": vpn_name,
                "output": (output[:500] if output else '')
            })
        except Exception:
            if ssh:
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
        ssh = None
        if not is_config_host_local(config):
            ssh = ssh_manager.get_connection(config)

        if not is_config_host_local(config) and not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            vpn_name = await resolve_vpn_connection_name(config, ssh, active_only=True)
            if not vpn_name:
                if ssh:
                    ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={"success": True, "message": "未发现正在连接的 VPN"},
                    status_code=200
                )

            # 使用nmcli断开VPN
            disconnect_cmd = f"sudo nmcli connection down {shlex.quote(vpn_name)}"
            output, error, code = await execute_config_host_command(
                config,
                ssh,
                disconnect_cmd,
                10
            )

            if ssh:
                ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": code == 0,
                "message": "VPN 已断开" if code == 0 else f"VPN 断开失败: {error or output}",
                "vpn_connection_name": vpn_name
            })
        except Exception:
            if ssh:
                ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error disconnecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== 文件上传 ====================

@app.post("/api/terminal/push")
@app.head("/api/terminal/push")
async def upload_file(
    request: Request,
    file: Optional[UploadFile] = File(None),
    path: str = Form(""),
    chunk_index: Optional[int] = Form(None),
    total_chunks: Optional[int] = Form(None),
    upload_id: Optional[str] = Form(None),
    file_name: Optional[str] = Form(None),
    file_size: Optional[int] = Form(None),
    resume: Optional[str] = Form(None),
    check_chunks: Optional[str] = Form(None)
):
    """
    文件上传 - 支持分块上传和断点续传

    1. 普通上传：接收完整文件并上传到远程服务器
    2. 分块上传：接收文件块，保存到临时目录，所有块上传完成后合并
    3. 断点续传：记录已上传的块，支持从断点继续
    """
    import tempfile
    import os
    import json

    # HEAD 请求：检查已上传的块（断点续传）
    if check_chunks and upload_id:
        session_dir = os.path.join(tempfile.gettempdir(), 'gms_uploads', upload_id)
        chunks_file = os.path.join(session_dir, 'uploaded_chunks.json')

        if os.path.exists(chunks_file):
            with open(chunks_file, 'r') as f:
                uploaded_chunks = json.load(f)
            return JSONResponse(content={
                'success': True,
                'uploaded_chunks': uploaded_chunks
            })
        else:
            return JSONResponse(content={
                'success': True,
                'uploaded_chunks': []
            })

    # 检查文件参数（分块上传时文件是必需的）
    if chunk_index is not None and not file:
        return JSONResponse(
            content={'success': False, 'error': 'No file provided for chunk upload'},
            status_code=400
        )

    # 分块上传模式
    if chunk_index is not None and total_chunks is not None:
        return await upload_file_chunk(
            file, chunk_index, total_chunks, upload_id,
            file_name or file.filename, file_size, resume
        )

    # 普通上传模式
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

        try:
            temp_path = safe_upload_target_path(upload_dir, file.filename, allow_nested=False)
        except ValueError as e:
            return JSONResponse(
                content={'success': False, 'error': str(e)},
                status_code=400
            )

        safe_filename = os.path.basename(temp_path)
        await save_upload_to_path(file, temp_path)

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
            # 如果指定了path参数，使用指定路径；否则使用默认路径
            if path and path.strip():
                # 确保路径存在
                target_dir = path.rstrip('/')
                try:
                    with ssh.open_sftp() as sftp:
                        ssh_manager.optimize_sftp_performance(sftp)
                        try:
                            sftp.stat(target_dir)
                        except IOError:
                            # 目录不存在，创建目录
                            sftp.mkdir(target_dir)
                        remote_path = f"{target_dir}/{safe_filename}"
                        sftp.put(temp_path, remote_path)
                except Exception as e:
                    logger.error(f"Failed to upload to specified path: {e}")
                    # 如果指定路径失败，回退到默认路径
                    remote_path = f"/home/{config['ubuntu_user']}/{safe_filename}"
                    with ssh.open_sftp() as sftp:
                        ssh_manager.optimize_sftp_performance(sftp)
                        sftp.put(temp_path, remote_path)
            else:
                # 使用默认路径
                remote_path = f"/home/{config['ubuntu_user']}/{safe_filename}"
                with ssh.open_sftp() as sftp:
                    ssh_manager.optimize_sftp_performance(sftp)
                    sftp.put(temp_path, remote_path)

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


async def upload_file_chunk(
    file: UploadFile,
    chunk_index: int,
    total_chunks: int,
    upload_id: str,
    file_name: str,
    file_size: Optional[int] = None,
    resume: Optional[str] = None
):
    """
    处理分块上传

    Args:
        file: 上传的文件块
        chunk_index: 当前块的索引
        total_chunks: 总块数
        upload_id: 上传会话ID
        file_name: 原始文件名
        file_size: 文件总大小
        resume: 是否支持断点续传
    """
    import tempfile
    import os
    import json

    try:
        if not upload_id or not file_name:
            return JSONResponse(content={
                'success': False,
                'error': 'upload_id and file_name are required for chunk upload'
            }, status_code=400)

        import time
        start_time = time.time()
        logger.info(f"[ChunkUpload] Received chunk {chunk_index}/{total_chunks} for {upload_id}")

        # 创建上传会话目录
        session_dir = os.path.join(tempfile.gettempdir(), 'gms_uploads', upload_id)
        os.makedirs(session_dir, exist_ok=True)

        # 保存文件块
        chunk_filename = f"chunk_{chunk_index:05d}"
        chunk_path = os.path.join(session_dir, chunk_filename)

        saved_size = await save_upload_to_path(file, chunk_path)

        elapsed = time.time() - start_time
        speed = saved_size / elapsed / (1024 * 1024) if elapsed > 0 else 0
        logger.info(f"[ChunkUpload] Saved chunk {chunk_index} ({saved_size} bytes) in {elapsed:.2f}s ({speed:.2f} MB/s)")

        # 记录已上传的块
        chunks_file = os.path.join(session_dir, 'uploaded_chunks.json')
        uploaded_chunks = set()

        if resume and os.path.exists(chunks_file):
            with open(chunks_file, 'r') as f:
                uploaded_chunks = set(json.load(f))
            logger.info(f"[ChunkUpload] Resuming with {len(uploaded_chunks)} chunks already uploaded")

        uploaded_chunks.add(chunk_index)

        with open(chunks_file, 'w') as f:
            json.dump(list(uploaded_chunks), f)

        # 检查是否所有块都已上传
        if len(uploaded_chunks) == total_chunks:
            merge_start = time.time()
            logger.info(f"[ChunkUpload] All chunks received for {upload_id}, merging...")

            merged_file = safe_upload_target_path(session_dir, file_name, allow_nested=False)
            chunk_paths = [
                os.path.join(session_dir, f"chunk_{i:05d}")
                for i in range(total_chunks)
            ]
            merge_files_to_path(chunk_paths, merged_file)

            merge_time = time.time() - merge_start
            logger.info(f"[ChunkUpload] Merged {total_chunks} chunks in {merge_time:.2f}s")

            # 上传完整文件到远程服务器
            config = config_manager.load_config()
            ssh = ssh_manager.get_connection(config)

            if ssh:
                try:
                    remote_filename = os.path.basename(merged_file)
                    remote_path = f"/home/{config['ubuntu_user']}/{remote_filename}"
                    upload_start = time.time()

                    with ssh.open_sftp() as sftp:
                        # 使用 SSHManager 的 SFTP 性能优化方法
                        ssh_manager.optimize_sftp_performance(sftp)
                        sftp.put(merged_file, remote_path, confirm=True)

                    upload_time = time.time() - upload_start
                    file_size_mb = os.path.getsize(merged_file) / (1024 * 1024)
                    upload_speed = file_size_mb / upload_time if upload_time > 0 else 0
                    logger.info(f"[ChunkUpload] Uploaded {file_size_mb:.2f}MB to remote in {upload_time:.2f}s ({upload_speed:.2f} MB/s)")

                    ssh_manager.return_connection(ssh)

                    # 清理临时文件
                    import shutil
                    shutil.rmtree(session_dir)

                    return JSONResponse(content={
                        'success': True,
                        'upload_complete': True,
                        'remote_path': remote_path,
                        'message': f'文件已上传到 {remote_path}'
                    })
                except Exception as e:
                    ssh_manager.return_connection(ssh)
                    logger.error(f"Error uploading merged file: {e}")
                    return JSONResponse(content={
                        'success': False,
                        'error': f'上传失败: {str(e)}',
                        'chunks_uploaded': len(uploaded_chunks),
                        'total_chunks': total_chunks
                    }, status_code=500)
            else:
                return JSONResponse(content={
                    'success': False,
                    'error': 'SSH connection failed',
                    'chunks_uploaded': len(uploaded_chunks),
                    'total_chunks': total_chunks
                }, status_code=500)

        # 返回当前进度
        return JSONResponse(content={
            'success': True,
            'chunk_index': chunk_index,
            'chunks_uploaded': len(uploaded_chunks),
            'total_chunks': total_chunks,
            'upload_complete': False,
            'progress': round((len(uploaded_chunks) / total_chunks) * 100, 2)
        })

    except Exception as e:
        logger.error(f"Error uploading chunk {chunk_index}: {e}")
        return JSONResponse(content={
            'success': False,
            'error': str(e),
            'chunk_index': chunk_index
        }, status_code=500)


@app.get("/api/files/progress")
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

# ==================== 固件管理 ====================
@app.get("/api/burn/upload-progress")
async def get_firmware_upload_progress(request: Request):
    """
    查询固件上传进度

    返回当前客户端的固件上传状态
    """
    client_id = get_client_id_from_request(request)

    with global_state.firmware_upload_progress_lock:
        # 优化：查询时自动清理过期数据（替代后台线程）
        current_time = time.time()
        expired_clients = [
            cid for cid, data in global_state.firmware_upload_progress.items()
            if current_time - data['timestamp'] > UPLOAD_PROGRESS_EXPIRATION
        ]
        for cid in expired_clients:
            del global_state.firmware_upload_progress[cid]

        if client_id in global_state.firmware_upload_progress:
            progress_data = global_state.firmware_upload_progress[client_id]

            return JSONResponse(content={
                'in_progress': True,
                'progress': progress_data['progress'],
                'filename': progress_data['filename'],
                'uploaded_size': progress_data['uploaded_size'],
                'total_size': progress_data['total_size']
            })
        else:
            return JSONResponse(content={'in_progress': False})


async def upload_firmware_to_test_host(
    ssh,
    client_id: str,
    source,
    remote_path: str,
    filename: str,
    file_size: int,
) -> None:
    """Upload firmware to the test host with shared progress reporting."""
    import scp

    upload_progress_data = {'current_percentage': 0.0, 'last_lock_update': 0.0}
    upload_complete = threading.Event()
    upload_error = [None]

    def update_global_progress(percentage: float, sent: int):
        if percentage - upload_progress_data['last_lock_update'] < 10:
            return

        try:
            with global_state.firmware_upload_progress_lock:
                global_state.firmware_upload_progress[client_id] = {
                    'progress': percentage,
                    'filename': filename,
                    'uploaded_size': sent,
                    'total_size': file_size,
                    'timestamp': time.time(),
                    'stage': 'uploading_to_server'
                }
            upload_progress_data['last_lock_update'] = percentage
        except Exception as e:
            logger.error(f"[Firmware Burn] Failed to update upload progress: {e}")

    def upload_progress(_filename, size, sent):
        percentage = (sent / size) * 100 if size > 0 else 0.0
        upload_progress_data['current_percentage'] = percentage
        logger.info(f"[Firmware Burn] Upload progress: {percentage:.2f}%")
        update_global_progress(percentage, sent)

    def upload_file_worker():
        scp_client = None
        try:
            scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
            if hasattr(source, 'read'):
                try:
                    source.seek(0)
                except Exception:
                    pass
                scp_client.putfo(source, remote_path, size=file_size)
            else:
                scp_client.put(source, remote_path)
            logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_path}")
        except Exception as e:
            logger.error(f"[Firmware Burn] Upload error: {e}")
            upload_error[0] = str(e)
        finally:
            if scp_client:
                try:
                    scp_client.close()
                except Exception:
                    pass
            upload_complete.set()

    try:
        with global_state.firmware_upload_progress_lock:
            global_state.firmware_upload_progress[client_id] = {
                'progress': 0.0,
                'filename': filename,
                'uploaded_size': 0,
                'total_size': file_size,
                'timestamp': time.time(),
                'stage': 'uploading_to_server'
            }

        await safe_websocket_send(client_id, {
            'type': 'file_upload_progress',
            'filename': filename,
            'percentage': 0,
            'total_size': file_size,
            'uploaded_size': 0
        })

        upload_thread = threading.Thread(target=upload_file_worker, daemon=True)
        upload_thread.start()

        last_percentage = 0.0
        last_update_time = time.time()
        while not upload_complete.is_set():
            await asyncio.sleep(1.0)
            current_percentage = upload_progress_data.get('current_percentage', 0.0)
            current_time = time.time()

            if abs(current_percentage - last_percentage) > 1.0 and (current_time - last_update_time) > 2.0:
                sent_size = int((current_percentage / 100) * file_size)
                await safe_websocket_send(client_id, {
                    'type': 'file_upload_progress',
                    'filename': filename,
                    'percentage': round(current_percentage, 2),
                    'total_size': file_size,
                    'uploaded_size': sent_size
                })
                last_percentage = current_percentage
                last_update_time = current_time

        upload_thread.join(timeout=300)
        if upload_thread.is_alive():
            raise RuntimeError("Upload timed out")
        if upload_error[0]:
            raise RuntimeError(f"Upload failed: {upload_error[0]}")

        await safe_websocket_send(client_id, {
            'type': 'file_upload_progress',
            'filename': filename,
            'percentage': 100,
            'total_size': file_size,
            'uploaded_size': file_size
        })
        await safe_websocket_send(client_id, {
            'type': 'log_update',
            'log': '✅ 固件文件上传完成',
            'log_type': 'success'
        })
    finally:
        with global_state.firmware_upload_progress_lock:
            global_state.firmware_upload_progress.pop(client_id, None)

@app.post("/api/burn/firmware")
async def burn_firmware(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """
    固件烧写 - 支持文件上传

    使用 upgrade_tool 烧写固件到选定的设备
    """
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/burn/firmware")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

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
            await release_device_locks(client_id, locked_devices, broadcast=False)

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
        logger.info("[Device Lock] 锁定状态广播完成")

        # 现在才开始等待FormData（此时设备已经锁定并显示）
        form = await request.form()
        firmware_file = form.get('firmware_file')
        firmware_path = form.get('firmware_path', '').strip()

        # 检查固件来源
        if not firmware_file and not firmware_path:
            await release_device_locks(client_id, locked_devices)

            return JSONResponse(
                content={'success': False, 'error': 'Please upload a firmware file or provide a firmware path'}
            )

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            # 1. 上传 upgrade_tool 到测试主机
            logger.info("[Firmware Burn] Uploading upgrade_tool...")
            gms_suite_dir = get_default_suites_path(config)
            local_tool = os.path.join(os.path.dirname(__file__), "tools", "upgrade_tool")
            remote_tool = os.path.join(gms_suite_dir, "upgrade_tool")

            if not os.path.exists(local_tool):
                logger.error(f"[Firmware Burn] upgrade_tool not found: {local_tool}")
                ssh_manager.return_connection(ssh)
                await release_device_locks(client_id, locked_devices)
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
            if firmware_file:
                # UploadFile.file 已由 FastAPI/Starlette 管理，直接复用该流做 SCP，
                # 避免后端再次复制到额外缓冲区。
                logger.info(f"[Firmware Burn] Processing uploaded file: {firmware_file.filename}")
                firmware_name = os.path.basename(firmware_file.filename or '').strip()
                if not firmware_name:
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return JSONResponse(
                        content={'success': False, 'error': 'Invalid firmware filename'}
                    )

                firmware_stream = firmware_file.file

                try:
                    firmware_stream.seek(0, os.SEEK_END)
                    firmware_size = firmware_stream.tell()
                    firmware_stream.seek(0)
                except Exception as e:
                    logger.error(f"[Firmware Burn] Failed to inspect uploaded firmware size: {e}")
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return JSONResponse(
                        content={'success': False, 'error': f'Failed to inspect uploaded firmware size: {e}'}
                    )
                if firmware_size <= 0:
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return JSONResponse(
                        content={'success': False, 'error': 'Uploaded firmware file is empty'}
                    )

                # 直接上传到测试主机
                remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                logger.info(f"[Firmware Burn] Streaming uploaded firmware to test host: {remote_firmware} ({firmware_size} bytes)")

                await upload_firmware_to_test_host(
                    ssh,
                    client_id,
                    firmware_stream,
                    remote_firmware,
                    firmware_name,
                    firmware_size
                )

                logger.info("[Firmware Burn] Firmware uploaded successfully, skipping local file check")

            # 如果没有上传文件，处理其他情况（远程文件或本地文件）
            else:
                # 现在处理固件文件（远程路径或本地文件）
                logger.info(f"[Firmware Burn] Processing firmware: {firmware_path}")
                firmware_name = os.path.basename(firmware_path.rstrip('/'))
                remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                local_firmware_path = None

                # 绝对路径/相对路径优先按测试主机文件处理，避免同机部署时把远程文件再次SCP到自身。
                if firmware_path.startswith('/') or firmware_path.startswith('./'):
                    quoted_firmware_path = shlex.quote(firmware_path)
                    check_cmd = f"test -f {quoted_firmware_path} && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

                    if 'found' in output:
                        logger.info(f"[Firmware Burn] Using remote file: {firmware_path}")
                        remote_firmware = firmware_path
                    elif os.path.exists(firmware_path):
                        local_firmware_path = firmware_path
                    else:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return JSONResponse(
                            content={'success': False, 'error': f'Firmware file not found: {firmware_path}. Please use a valid test-host path or upload the file.'}
                        )
                elif os.path.exists(firmware_path):
                    local_firmware_path = firmware_path
                else:
                    # 可能只是文件名，尝试在 GMS-Suite 目录中查找
                    logger.info(f"[Firmware Burn] Searching for file in GMS-Suite: {firmware_path}")
                    remote_candidate = os.path.join(gms_suite_dir, firmware_path)
                    check_cmd = f"test -f {shlex.quote(remote_candidate)} && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

                    if 'found' in output:
                        remote_firmware = remote_candidate
                        logger.info(f"[Firmware Burn] File found: {remote_firmware}")
                    else:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return JSONResponse(
                            content={'success': False, 'error': f'Firmware file not found: {firmware_path}. Please use a full path or upload the file first.'}
                        )

                if local_firmware_path:
                    file_size = os.path.getsize(local_firmware_path)
                    if file_size <= 0:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return JSONResponse(
                            content={'success': False, 'error': 'Firmware file is empty'}
                        )
                    logger.info(f"[Firmware Burn] Uploading local file: {local_firmware_path} ({file_size} bytes)")
                    await upload_firmware_to_test_host(
                        ssh,
                        client_id,
                        local_firmware_path,
                        remote_firmware,
                        firmware_name,
                        file_size
                    )

                    logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")

            # 3. 让设备进入 Loader 模式
            logger.info("[Firmware Burn] Entering Loader mode...")
            for device in devices:
                cmd = f"adb -s {device} reboot loader"
                ssh_manager.execute_command(ssh, cmd, timeout=5)
                logger.info(f"[Firmware Burn] Device {device} sent to Loader mode")

            logger.info("[Firmware Burn] Waiting for devices to enter Loader mode...")
            await asyncio.sleep(8)

            # 4. 检查 Loader 设备
            check_cmd = f"cd {gms_suite_dir} && ./upgrade_tool ld"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

            # 检查是否有设备进入 loader 模式（0设备=失败）
            if "List of rockusb connected(0)" in output or "List of rockusb connected" not in output:
                ssh_manager.return_connection(ssh)
                await release_device_locks(client_id, locked_devices)
                return JSONResponse(
                    content={'success': False, 'error': f'No Loader devices detected. Output:\n{output}'}
                )

            logger.info(f"[Firmware Burn] Loader devices detected:\n{output}")

            # 5. 烧写固件（upgrade_tool 会自动处理所有设备）
            logger.info("[Firmware Burn] Starting firmware burning...")
            burn_cmd = f"cd {gms_suite_dir} && ./upgrade_tool uf {shlex.quote(remote_firmware)}"

            # 发送开始消息
            if client_id in global_state.websocket_connections:
                try:
                    await safe_websocket_send(client_id, {
                        'type': 'log_update',
                        'log': '🔥 开始烧写固件...',
                        'log_type': 'info'
                    })
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            # 执行烧写并获取实时输出
            stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=300)

            # 实时读取输出并发送到前端
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
                                            await safe_websocket_send(client_id, {
                                                'type': 'log_update',
                                                'log': line,
                                                'log_type': 'error'
                                            })
                                        continue

                                    # 其他正常日志
                                    await safe_websocket_send(client_id, {
                                        'type': 'log_update',
                                        'log': line,
                                        'log_type': 'info'
                                    })
                        except Exception as e:
                            logger.error(f"[Firmware Burn] 发送日志失败: {e}")

                # 如果固件烧写开始，每0.5秒更新一次进度
                if firmware_burn_start and (current_time - last_progress_time > GSI_PROGRESS_POLL_INTERVAL):
                    # 进度条从0%到95%，每0.5秒增加5%
                    current_progress = min(current_progress + GSI_PROGRESS_INCREMENT, GSI_PROGRESS_MAX)
                    last_progress_time = current_time

                    # 发送进度更新到前端（只更新进度条，不显示在日志）
                    if client_id in global_state.websocket_connections:
                        try:
                            await safe_websocket_send(client_id, {
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
                        await safe_websocket_send(client_id, {
                            'type': 'firmware_progress',
                            'percentage': 100
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 发送完成消息
                if client_id in global_state.websocket_connections:
                    try:
                        await safe_websocket_send(client_id, {
                            'type': 'log_update',
                            'log': '✅ 固件烧写完成！',
                            'log_type': 'success'
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                await push_notification(
                    client_id,
                    '固件烧写完成',
                    f"设备：{', '.join(devices)}",
                    'success',
                    'firmware',
                    {'devices': devices, 'firmware': firmware_name}
                )

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                await release_device_locks(client_id, locked_devices)
                logger.info("[Device Lock] 设备解锁完成")

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
                        await safe_websocket_send(client_id, {
                            'type': 'log_update',
                            'log': f'❌ 固件烧写失败 (exit code: {exit_status})',
                            'log_type': 'error'
                        })
                        # 如果有详细错误信息，也发送
                        if error_output and len(error_output) < 500:  # 限制长度
                            await safe_websocket_send(client_id, {
                                'type': 'log_update',
                                'log': f'错误详情: {error_output[:200]}',
                                'log_type': 'error'
                            })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                await push_notification(
                    client_id,
                    '固件烧写失败',
                    (error_output or 'Firmware burn failed')[:300],
                    'error',
                    'firmware',
                    {'devices': devices, 'firmware': firmware_name, 'exit_status': exit_status}
                )

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                await release_device_locks(client_id, locked_devices)
                logger.info("[Device Lock] 设备解锁完成")

                return JSONResponse(
                    content={'success': False, 'error': error_output or 'Firmware burn failed'}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[Firmware Burn] Error: {e}")

            # 释放设备锁
            await release_device_locks(client_id, locked_devices)

            await push_notification(
                client_id,
                '固件烧写异常',
                str(e)[:300],
                'error',
                'firmware',
                {'devices': devices, 'firmware': firmware_name}
            )

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        import traceback
        logger.error(f"Error in burn_firmware: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/burn/gsi")
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
            await release_device_locks(client_id, locked_devices, broadcast=False)

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
        logger.info("[Device Lock] 锁定状态广播完成")

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            import scp

            # 1. 上传必要文件到测试主机
            logger.info("[GSI Burn] Uploading necessary files...")

            gms_suite_dir = get_default_suites_path(config)

            # 上传脚本
            local_script = os.path.join(os.path.dirname(__file__), "scripts", "run_GSI_Burn.sh")
            remote_script = os.path.join(gms_suite_dir, "run_GSI_Burn.sh")

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
            remote_misc = os.path.join(gms_suite_dir, "misc.img")

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
                    remote_vendor = os.path.join(gms_suite_dir, vendor_name)
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
                    await safe_websocket_send(client_id, {
                        'type': 'log_update',
                        'log': f'🔥 开始烧写GSI镜像到 {len(devices)} 台设备...',
                        'log_type': 'info'
                    })
                except (WebSocketDisconnect, ConnectionError, KeyError):
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
                        await safe_websocket_send(client_id, {
                            'type': 'log_update',
                            'log': f'📱 正在烧写设备: {device}',
                            'log_type': 'info'
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 执行命令并实时读取输出
                stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=600)

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
                                        await safe_websocket_send(client_id, {
                                            'type': 'log_update',
                                            'log': line,
                                            'log_type': 'info'
                                        })
                            except (WebSocketDisconnect, ConnectionError, KeyError):
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
                            await safe_websocket_send(client_id, {
                                'type': 'log_update',
                                'log': f'✅ 设备 {device} GSI烧写完成',
                                'log_type': 'success'
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
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

                            await safe_websocket_send(client_id, {
                                'type': 'log_update',
                                'log': f'❌ 设备 {device} GSI烧写失败: {error_msg}',
                                'log_type': 'error'
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass

            ssh_manager.return_connection(ssh)

            # 释放所有设备锁
            await release_device_locks(client_id, locked_devices)

            # 检查是否全部成功
            all_success = all(r['success'] for r in results)
            if all_success:
                await push_notification(
                    client_id,
                    'GSI烧写完成',
                    f"设备：{', '.join(devices)}",
                    'success',
                    'firmware',
                    {'devices': devices, 'results': results}
                )
                return JSONResponse(
                    content={'success': True, 'message': 'GSI burn completed successfully', 'results': results}
                )
            else:
                failed_devices = [r.get('device') for r in results if not r.get('success')]
                await push_notification(
                    client_id,
                    'GSI烧写失败',
                    f"失败设备：{', '.join(failed_devices)}",
                    'error',
                    'firmware',
                    {'devices': devices, 'results': results}
                )
                return JSONResponse(
                    content={'success': False, 'error': 'Some devices failed', 'results': results}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[GSI Burn] Error: {e}")
            await push_notification(
                client_id,
                'GSI烧写异常',
                str(e)[:300],
                'error',
                'firmware',
                {'devices': devices}
            )

            # 释放所有设备锁
            await release_device_locks(client_id, locked_devices)

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/burn/serial")
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

def _create_apk_task(task_id, apk_path, filename):
    """创建APK分析任务并限制总数"""
    os.makedirs(APK_UPLOAD_DIR, exist_ok=True)
    with global_state.apk_analysis_tasks_lock:
        if len(global_state.apk_analysis_tasks) >= APK_MAX_TASKS:
            oldest = min(global_state.apk_analysis_tasks.items(), key=lambda t: t[1].get('timestamp', 0))
            old_dir = os.path.join(APK_UPLOAD_DIR, oldest[0])
            shutil.rmtree(old_dir, ignore_errors=True)
            del global_state.apk_analysis_tasks[oldest[0]]
        global_state.apk_analysis_tasks[task_id] = {
            'status': 'uploaded', 'progress': 0,
            'apk_path': apk_path, 'output_dir': None,
            'filename': filename, 'timestamp': time.time(), 'error': None
        }


def _get_apk_upload_lock(task_id: str) -> asyncio.Lock:
    with global_state.apk_upload_locks_lock:
        return global_state.apk_upload_locks.setdefault(task_id, asyncio.Lock())

ANDROID_NS = 'http://schemas.android.com/apk/res/android'


def _safe_join(base_dir: str, *parts: str) -> str:
    """Join paths and ensure the result stays under base_dir."""
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, *parts))
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        raise ValueError("非法路径")
    return target_abs


def _normalize_apk_filename(filename: Optional[str]) -> str:
    """Normalize upload filenames to a safe APK basename."""
    raw_name = (filename or '').replace('\\', '/')
    basename = os.path.basename(raw_name).strip()
    if not basename or basename in ('.', '..'):
        raise ValueError("文件名无效")
    if not basename.lower().endswith('.apk'):
        raise ValueError("仅支持 .apk 文件")

    stem, ext = os.path.splitext(basename)
    stem = re.sub(r'[^A-Za-z0-9._ -]+', '_', stem).strip(' ._') or 'app'
    return f"{stem}{ext.lower()}"


def _normalize_apk_task_id(upload_id: Optional[str]) -> str:
    """Use UUID task directories only; never trust path-like upload IDs."""
    if not upload_id:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(str(upload_id)))
    except (TypeError, ValueError):
        raise ValueError("非法上传ID")


def _cleanup_files(paths: List[str]):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Failed to remove temporary APK file {path}: {e}")


def _get_apk_task(task_id: str, require_completed: bool = True):
    """获取APK分析任务，返回 (task, error_response)"""
    with global_state.apk_analysis_tasks_lock:
        task = global_state.apk_analysis_tasks.get(task_id)
    if not task:
        return None, ApiResponse.error("任务不存在", status_code=404)
    if require_completed and task['status'] != 'completed':
        return None, ApiResponse.error("分析尚未完成", status_code=400)
    return task, None


def _read_manifest_xml(task):
    """读取并返回 AndroidManifest.xml 的原始内容"""
    manifest_path = os.path.join(task.get('output_dir', ''), 'resources', 'AndroidManifest.xml')
    if not os.path.exists(manifest_path):
        return None, "AndroidManifest.xml 未找到"
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return f.read(), None


JAVA_IDENTIFIER_RE = r'[A-Za-z_$][A-Za-z0-9_$]*'
JAVA_CLASS_DEF_RE = re.compile(rf'\b(?:public|protected|private|static|final|abstract|\s)*(class|interface|enum)\s+({JAVA_IDENTIFIER_RE})\b')
JAVA_METHOD_DEF_RE = re.compile(rf'\b(?:public|protected|private|static|final|synchronized|native|abstract|strictfp|\s)+[\w$<>\[\].?,\s]+\s+({JAVA_IDENTIFIER_RE})\s*\([^;]*\)\s*(?:throws\b[^{{;]*)?(?:\{{|$)')
JAVA_FIELD_DEF_RE = re.compile(rf'\b(?:public|protected|private|static|final|volatile|transient|\s)+[\w$<>\[\].?,\s]+\s+({JAVA_IDENTIFIER_RE})\s*(?:=|;|,)')
JAVA_LOCAL_DEF_RE = re.compile(rf'\b(?:final\s+)?(?:[A-Za-z_$][\w$<>.\[\]?]*)(?:\s*<[^;=()]+>)?(?:\[\])?\s+({JAVA_IDENTIFIER_RE})\s*(?:=|;|,)')
JAVA_CONTROL_WORDS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'throw', 'new', 'case', 'do', 'else', 'try', 'finally'}
APK_SYMBOL_INDEX_MAX_FILE_SIZE = 2 * 1024 * 1024


def _get_apk_sources_dir(task) -> str:
    return _safe_join(task.get('output_dir', ''), 'sources')


def _add_apk_symbol(symbols: Dict[str, List[Dict[str, Any]]], name: str, kind: str, path: str, line: int, column: int):
    if not name or name in JAVA_CONTROL_WORDS:
        return
    symbols.setdefault(name, []).append({
        'name': name,
        'kind': kind,
        'path': path,
        'line': line,
        'column': column,
    })


def _index_java_source_file(sources_dir: str, file_path: str, symbols: Dict[str, List[Dict[str, Any]]]):
    if os.path.getsize(file_path) > APK_SYMBOL_INDEX_MAX_FILE_SIZE:
        return

    rel_path = os.path.relpath(file_path, sources_dir)
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            for line_no, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped or stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*'):
                    continue

                for match in JAVA_CLASS_DEF_RE.finditer(line):
                    _add_apk_symbol(symbols, match.group(2), match.group(1), rel_path, line_no, match.start(2) + 1)

                method_match = JAVA_METHOD_DEF_RE.search(line)
                if method_match:
                    name = method_match.group(1)
                    if name not in JAVA_CONTROL_WORDS:
                        _add_apk_symbol(symbols, name, 'method', rel_path, line_no, method_match.start(1) + 1)
                    continue

                for match in JAVA_FIELD_DEF_RE.finditer(line):
                    _add_apk_symbol(symbols, match.group(1), 'field', rel_path, line_no, match.start(1) + 1)

                for match in JAVA_LOCAL_DEF_RE.finditer(line):
                    name = match.group(1)
                    prefix = line[:match.start(1)].strip()
                    if '(' in prefix and ')' not in prefix:
                        continue
                    _add_apk_symbol(symbols, name, 'local', rel_path, line_no, match.start(1) + 1)
    except UnicodeError:
        logger.warning(f"Failed to decode Java source for APK symbol index: {file_path}")


def _build_apk_symbol_index(task_id: str, task: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    with global_state.apk_analysis_tasks_lock:
        cached = global_state.apk_analysis_tasks.get(task_id, {}).get('symbol_index')
        if cached is not None:
            return cached

    sources_dir = _get_apk_sources_dir(task)
    symbols: Dict[str, List[Dict[str, Any]]] = {}
    if not os.path.isdir(sources_dir):
        return symbols

    for root, _, files in os.walk(sources_dir):
        for filename in files:
            if filename.endswith('.java'):
                _index_java_source_file(sources_dir, os.path.join(root, filename), symbols)

    with global_state.apk_analysis_tasks_lock:
        if task_id in global_state.apk_analysis_tasks:
            global_state.apk_analysis_tasks[task_id]['symbol_index'] = symbols
    return symbols


def _score_apk_symbol_candidate(candidate: Dict[str, Any], current_path: str, current_line: int) -> Tuple[int, int]:
    score = 0
    if candidate.get('path') == current_path:
        score += 100
        if current_line:
            score -= abs(candidate.get('line', 0) - current_line)
    if candidate.get('kind') in ('method', 'field', 'class', 'interface', 'enum'):
        score += 20
    return score, -candidate.get('line', 0)


async def _run_jadx_analysis(task_id: str, apk_path: str, output_dir: str):
    """后台运行 jadx 反编译"""
    try:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'analyzing'
                global_state.apk_analysis_tasks[task_id]['progress'] = 10
                global_state.apk_analysis_tasks[task_id]['error'] = None

        jadx_threads = min(max(os.cpu_count() or 2, 2), 8)
        cmd = [
            JADX_PATH,
            '-d', output_dir,
            '-j', str(jadx_threads),
            '--log-level', 'error',
            '--show-bad-code',
            '-Pdex-input.verify-checksum=no',
            apk_path
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=JADX_TIMEOUT
        )

        if result.returncode != 0:
            with global_state.apk_analysis_tasks_lock:
                if task_id in global_state.apk_analysis_tasks:
                    global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                    global_state.apk_analysis_tasks[task_id]['error'] = result.stderr[-500:] if result.stderr else 'jadx 反编译失败'
            return

        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'completed'
                global_state.apk_analysis_tasks[task_id]['progress'] = 100
                global_state.apk_analysis_tasks[task_id]['output_dir'] = output_dir
    except subprocess.TimeoutExpired:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                global_state.apk_analysis_tasks[task_id]['error'] = 'jadx 反编译超时（超过600秒）'
    except Exception as e:
        with global_state.apk_analysis_tasks_lock:
            if task_id in global_state.apk_analysis_tasks:
                global_state.apk_analysis_tasks[task_id]['status'] = 'error'
                global_state.apk_analysis_tasks[task_id]['error'] = str(e)


@app.post("/api/apk/upload")
@handle_api_errors
async def upload_apk(
    file: Optional[UploadFile] = File(None),
    chunk_index: Optional[int] = Form(None),
    total_chunks: Optional[int] = Form(None),
    upload_id: Optional[str] = Form(None),
    file_name: Optional[str] = Form(None),
):
    """上传APK文件进行分析"""
    if not file:
        return ApiResponse.error("未提供文件", status_code=400)

    try:
        filename = _normalize_apk_filename(file_name or file.filename)
        task_id = _normalize_apk_task_id(upload_id)
        task_dir = _safe_join(APK_UPLOAD_DIR, task_id)
        apk_path = _safe_join(task_dir, filename)
    except ValueError as e:
        return ApiResponse.error(str(e), status_code=400)

    os.makedirs(task_dir, exist_ok=True)

    if (chunk_index is None) != (total_chunks is None):
        return ApiResponse.error("分片参数不完整", status_code=400)

    if chunk_index is not None and total_chunks is not None:
        if total_chunks <= 0 or chunk_index < 0 or chunk_index >= total_chunks:
            return ApiResponse.error("分片参数无效", status_code=400)

        chunk_path = _safe_join(task_dir, f"{filename}.part{chunk_index}")
        try:
            await save_upload_to_path(file, chunk_path, APK_MAX_FILE_SIZE)
        except ValueError as e:
            _cleanup_files([chunk_path])
            return ApiResponse.error(str(e), status_code=400)

        chunk_paths = [
            _safe_join(task_dir, f"{filename}.part{i}")
            for i in range(total_chunks)
        ]
        upload_lock = _get_apk_upload_lock(task_id)
        async with upload_lock:
            if os.path.exists(apk_path):
                file_size = os.path.getsize(apk_path)
                return ApiResponse.success({
                    'task_id': task_id,
                    'filename': filename,
                    'size': file_size,
                    'uploaded': True
                })

            all_chunks_ready = all(
                os.path.exists(path)
                for path in chunk_paths
            )

            if not all_chunks_ready:
                return ApiResponse.success({
                    'task_id': task_id, 'filename': filename,
                    'chunk_received': chunk_index + 1, 'total_chunks': total_chunks, 'uploaded': False
                })

            total_size = sum(os.path.getsize(path) for path in chunk_paths)
            if total_size > APK_MAX_FILE_SIZE:
                _cleanup_files(chunk_paths + [apk_path])
                return ApiResponse.error(f"文件过大，最大支持 {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

            await asyncio.to_thread(merge_files_to_path, chunk_paths, apk_path)
            _cleanup_files(chunk_paths)

            file_size = os.path.getsize(apk_path)
            if file_size > APK_MAX_FILE_SIZE:
                _cleanup_files([apk_path])
                return ApiResponse.error(f"文件过大，最大支持 {APK_MAX_FILE_SIZE // (1024*1024)}MB", status_code=400)

            _create_apk_task(task_id, apk_path, filename)
            return ApiResponse.success({'task_id': task_id, 'filename': filename, 'size': file_size, 'uploaded': True})
    else:
        try:
            file_size = await save_upload_to_path(file, apk_path, APK_MAX_FILE_SIZE)
        except ValueError as e:
            _cleanup_files([apk_path])
            return ApiResponse.error(str(e), status_code=400)

        _create_apk_task(task_id, apk_path, filename)
        return ApiResponse.success({'task_id': task_id, 'filename': filename, 'size': file_size})


@app.post("/api/apk/analyze/{task_id}")
@handle_api_errors
async def analyze_apk(task_id: str):
    """启动 jadx 反编译分析"""
    with global_state.apk_analysis_tasks_lock:
        task = global_state.apk_analysis_tasks.get(task_id)
        task = dict(task) if task else None

    if not task:
        return ApiResponse.error("任务不存在", status_code=404)

    if task['status'] == 'analyzing':
        return ApiResponse.error("分析正在进行中", status_code=400)

    if task['status'] == 'completed':
        return ApiResponse.success({
            'task_id': task_id,
            'status': 'completed',
            'progress': task.get('progress', 100),
            'already_completed': True
        })

    if task['status'] not in ('uploaded', 'error'):
        return ApiResponse.error(f"当前状态 {task['status']} 不允许启动分析", status_code=400)

    apk_path = task['apk_path']
    if not os.path.exists(apk_path):
        return ApiResponse.error("APK文件不存在，请重新上传", status_code=404)

    output_dir = os.path.join(APK_UPLOAD_DIR, task_id, 'jadx_output')

    with global_state.apk_analysis_tasks_lock:
        global_state.apk_analysis_tasks[task_id]['status'] = 'analyzing'
        global_state.apk_analysis_tasks[task_id]['progress'] = 5
        global_state.apk_analysis_tasks[task_id]['output_dir'] = output_dir
        global_state.apk_analysis_tasks[task_id]['error'] = None

    asyncio.create_task(_run_jadx_analysis(task_id, apk_path, output_dir))

    return ApiResponse.success({'task_id': task_id, 'status': 'analyzing'})


@app.get("/api/apk/status/{task_id}")
@handle_api_errors
async def get_apk_status(task_id: str):
    """获取APK分析状态"""
    with global_state.apk_analysis_tasks_lock:
        task = global_state.apk_analysis_tasks.get(task_id)
        task = dict(task) if task else None

    if not task:
        return ApiResponse.error("任务不存在", status_code=404)

    return ApiResponse.success({
        'task_id': task_id,
        'status': task['status'],
        'progress': task['progress'],
        'filename': task.get('filename', ''),
        'error': task.get('error')
    })


@app.get("/api/apk/manifest/{task_id}")
@handle_api_errors
async def get_apk_manifest(task_id: str):
    """获取解析后的 AndroidManifest.xml"""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    raw_xml, err = _read_manifest_xml(task)
    if err:
        return ApiResponse.error(err, status_code=404)

    manifest_info = {}
    try:
        root = ET.fromstring(raw_xml)

        manifest_info['package'] = root.get('package', '')
        manifest_info['versionName'] = root.get(f'{{{ANDROID_NS}}}versionName', '')
        manifest_info['versionCode'] = root.get(f'{{{ANDROID_NS}}}versionCode', '')

        uses_sdk = root.find('uses-sdk')
        if uses_sdk is not None:
            manifest_info['minSdkVersion'] = uses_sdk.get(f'{{{ANDROID_NS}}}minSdkVersion', '')
            manifest_info['targetSdkVersion'] = uses_sdk.get(f'{{{ANDROID_NS}}}targetSdkVersion', '')

        application = root.find('application')
        if application is not None:
            for activity in application.findall('activity'):
                for intent_filter in activity.findall('intent-filter'):
                    for action in intent_filter.findall('action'):
                        if action.get(f'{{{ANDROID_NS}}}name') == 'android.intent.action.MAIN':
                            manifest_info['launchActivity'] = activity.get(f'{{{ANDROID_NS}}}name', '')
                            break
    except ET.ParseError as e:
        logger.warning(f"APK manifest XML parse error for task {task_id}: {e}")

    return ApiResponse.success({'manifest': manifest_info, 'raw_xml': raw_xml})


@app.get("/api/apk/permissions/{task_id}")
@handle_api_errors
async def get_apk_permissions(task_id: str):
    """获取APK权限列表"""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    raw_xml, err = _read_manifest_xml(task)
    if err:
        return ApiResponse.error(err, status_code=404)

    permissions = []
    try:
        root = ET.fromstring(raw_xml)
        for perm in root.findall('uses-permission'):
            perm_name = perm.get(f'{{{ANDROID_NS}}}name', '')
            if perm_name:
                short_name = perm_name.split('.')[-1] if '.' in perm_name else perm_name
                permissions.append({'name': perm_name, 'short_name': short_name})
    except ET.ParseError as e:
        logger.warning(f"APK permissions XML parse error for task {task_id}: {e}")

    return ApiResponse.success({'permissions': permissions, 'total': len(permissions)})


@app.get("/api/apk/source/{task_id}")
@handle_api_errors
async def get_apk_source(task_id: str, path: str = "", view: bool = False):
    """浏览反编译源码树或查看文件内容"""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    sources_dir = _safe_join(task.get('output_dir', ''), 'sources')
    if not os.path.exists(sources_dir):
        return ApiResponse.error("源码目录不存在", status_code=404)

    if view:
        try:
            file_path = _safe_join(sources_dir, path)
        except ValueError:
            return ApiResponse.error("非法路径", status_code=400)
        if not os.path.isfile(file_path):
            return ApiResponse.error("文件不存在", status_code=404)
        file_size = os.path.getsize(file_path)
        if file_size > APK_MAX_SOURCE_FILE_SIZE:
            return ApiResponse.error(f"文件过大({file_size // 1024}KB)，超过{APK_MAX_SOURCE_FILE_SIZE // (1024*1024)}MB限制", status_code=400)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return ApiResponse.success({'path': path, 'content': content, 'size': file_size})
        except Exception as e:
            return ApiResponse.error(f"读取文件失败: {e}", status_code=500)
    else:
        try:
            target_dir = _safe_join(sources_dir, path) if path else sources_dir
        except ValueError:
            return ApiResponse.error("非法路径", status_code=400)
        if not os.path.isdir(target_dir):
            return ApiResponse.error("目录不存在", status_code=404)

        items = []
        try:
            for entry in sorted(os.scandir(target_dir), key=lambda e: (not e.is_dir(), e.name.lower())):
                items.append({
                    'name': entry.name,
                    'type': 'dir' if entry.is_dir() else 'file',
                    'path': os.path.relpath(entry.path, sources_dir),
                    'size': entry.stat().st_size if entry.is_file() else 0
                    })
        except PermissionError:
            return ApiResponse.error("权限不足", status_code=403)

        return ApiResponse.success({'items': items, 'path': path, 'total': len(items)})


@app.get("/api/apk/definition/{task_id}")
@handle_api_errors
async def find_apk_symbol_definition(task_id: str, symbol: str, path: str = "", line: int = 0):
    """Find a best-effort Java symbol definition in decompiled APK sources."""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    symbol = (symbol or '').strip()
    if not re.fullmatch(JAVA_IDENTIFIER_RE, symbol):
        return ApiResponse.error("符号名无效", status_code=400)

    symbols = await asyncio.to_thread(_build_apk_symbol_index, task_id, task)
    candidates = symbols.get(symbol, [])
    if not candidates:
        return ApiResponse.error(f"未找到定义: {symbol}", status_code=404)

    best = sorted(
        candidates,
        key=lambda item: _score_apk_symbol_candidate(item, path, line),
        reverse=True
    )[0]
    return ApiResponse.success({'definition': best, 'candidates': candidates[:20]})


@app.get("/api/apk/download/{task_id}")
@handle_api_errors
async def download_apk_source(task_id: str):
    """下载反编译源码 ZIP"""
    task, err = _get_apk_task(task_id)
    if err:
        return err

    output_dir = task.get('output_dir', '')
    if not os.path.exists(output_dir):
        return ApiResponse.error("输出目录不存在", status_code=404)

    filename = task.get('filename', 'app.apk').replace('.apk', '_decompiled')

    zip_path = shutil.make_archive(
        os.path.join(APK_UPLOAD_DIR, task_id, filename),
        'zip', output_dir
    )

    def iterfile():
        with open(zip_path, 'rb') as f:
            yield from f
        try:
            os.remove(zip_path)
        except Exception:
            pass

    return StreamingResponse(
        iterfile(),
        media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}.zip"'}
    )


@app.get("/api/apk/tasks")
@handle_api_errors
async def list_apk_tasks():
    """列出所有APK分析任务"""
    with global_state.apk_analysis_tasks_lock:
        tasks = []
        for tid, task in global_state.apk_analysis_tasks.items():
            tasks.append({
                'task_id': tid,
                'filename': task.get('filename', ''),
                'status': task['status'],
                'progress': task['progress'],
                'timestamp': task.get('timestamp', 0),
                'error': task.get('error')
            })

    tasks.sort(key=lambda t: t.get('timestamp', 0), reverse=True)
    return ApiResponse.success({'tasks': tasks, 'total': len(tasks)})


@app.delete("/api/apk/task/{task_id}")
@handle_api_errors
async def delete_apk_task(task_id: str):
    """删除APK分析任务及其文件"""
    with global_state.apk_analysis_tasks_lock:
        if task_id not in global_state.apk_analysis_tasks:
            return ApiResponse.error("任务不存在", status_code=404)
        global_state.apk_analysis_tasks.pop(task_id)

    task_dir = os.path.join(APK_UPLOAD_DIR, task_id)
    await asyncio.to_thread(shutil.rmtree, task_dir, ignore_errors=True)
    with global_state.apk_upload_locks_lock:
        global_state.apk_upload_locks.pop(task_id, None)

    return ApiResponse.success(message="任务已删除")


# ==================== 其他功能 ====================

@app.post("/api/files/list")
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
        except Exception:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

# ==================== USB/IP辅助函数 ====================

def is_windows_host(ssh):
    """检查SSH主机是否为Windows"""
    try:
        stdin, stdout, stderr = ssh.exec_command('ver 2>&1', timeout=3)
        output = stdout.read().decode('utf-8', errors='ignore').lower()
        return 'microsoft' in output or 'windows' in output
    except (paramiko.SSHException, AttributeError):
        return False


def find_device_host_password(config, device_host):
    """从 client_ssh_credentials 中查找对应 device_host 的密码"""
    if '@' not in device_host:
        return None

    username, hostname = CommonUtils.parse_host_address(device_host)

    # 从 client_ssh_credentials 中查找匹配的凭据
    for cred in config.get('client_ssh_credentials', []):
        if cred.get('username') == username:
            logger.info(f"[USB/IP] Found SSH credential for username={username}")
            return cred.get('password')

    logger.info(f"[USB/IP] No SSH credential found for {device_host}")
    return None


class DeviceSSHConnection:
    """设备SSH连接上下文管理器，自动处理连接获取和归还（连接池）"""

    def __init__(self, config=None):
        self.config = config or config_manager.load_config()
        self.ssh = None
        self._pool_key = None

    def _get_pool_key(self):
        """生成连接池的键值，基于设备主机地址"""
        device_host = self.config.get('device_host', '')
        if not device_host:
            return None

        if '@' in device_host:
            # 格式: username@hostname
            return device_host
        return device_host

    def __enter__(self):
        self._pool_key = self._get_pool_key()
        if not self._pool_key:
            raise HTTPException(
                status_code=500,
                detail="无效的设备主机配置"
            )

        # 从连接池获取或创建连接
        self.ssh = global_state.device_ssh_pool_get(self._pool_key, self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail=f"无法连接到设备主机: {self._pool_key}"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh and self._pool_key:
            try:
                global_state.device_ssh_pool_return(self._pool_key, self.ssh)
            except Exception as e:
                logger.error(f"Failed to return device SSH connection: {e}")


# ==================== 辅助函数 ====================

def ssh_connection_failed_response():
    """SSH连接失败的标准错误响应"""
    return JSONResponse(
        content={'success': False, 'error': 'SSH connection failed'},
        status_code=500
    )

def find_tradefed_binary(ssh, suite_path: str) -> Optional[str]:
    """在指定目录中查找 tradefed 二进制文件"""
    find_cmd = f"find '{suite_path}' -maxdepth 1 -type f -executable -name '*-tradefed' 2>/dev/null | head -1"
    output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=10)
    result = output.strip()
    return result if result else None


def parse_tradefed_list_results(output: str) -> List[Dict[str, Any]]:
    """解析 tradefed list results 命令输出，支持 STS 和 VTS/CTS 两种格式"""
    # 清理 ANSI 转义序列（使用现有函数）
    cleaned_output = strip_ansi_codes(output)

    results = []
    lines = cleaned_output.strip().split('\n')
    header_found = False

    for line in lines:
        if not header_found:
            if 'Session' in line and 'Pass' in line and 'Fail' in line:
                header_found = True
            continue

        line = line.strip()
        if not line or line.startswith('=====') or line.startswith('------'):
            continue

        if '>' in line and 'Session' not in line:
            continue

        parts = line.split()
        if len(parts) >= 10:
            try:
                has_of_keyword = len(parts) > 4 and parts[4] == 'of'

                if has_of_keyword:
                    result_entry = {
                        'session': parts[0],
                        'pass': int(parts[1]),
                        'fail': int(parts[2]),
                        'modules': parts[3],
                        'modules_total': parts[5],
                        'result_directory': parts[6],
                        'test_plan': parts[7],
                        'device_serial': parts[8],
                        'build_id': parts[9],
                        'product': parts[10] if len(parts) > 10 else ''
                    }
                else:
                    result_entry = {
                        'session': parts[0],
                        'pass': int(parts[1]),
                        'fail': int(parts[2]),
                        'modules': parts[3],
                        'modules_total': parts[4],
                        'result_directory': parts[5],
                        'test_plan': parts[6],
                        'device_serial': parts[7],
                        'build_id': parts[8],
                        'product': parts[9] if len(parts) > 9 else ''
                    }
                results.append(result_entry)
            except (ValueError, IndexError):
                continue

    return results



def execute_tradefed_command(ssh, suite_path: str, tradefed_bin: str, command: str = "list results") -> tuple:
    """
    执行 tradefed 命令（使用登录 shell 加载环境变量）

    使用 invoke_shell 交互式方式执行命令，适用于所有测试套件类型

    性能优化：使用智能等待替代固定延迟，大幅减少查询时间
    """
    import time
    import re

    # 常量定义
    config = config_manager.load_config()
    default_platform_tools = os.path.join(
        "/home",
        config_manager.get_ubuntu_user(config),
        "Software",
        "platform-tools"
    )
    PLATFORM_TOOLS_PATH = os.environ.get("GMS_PLATFORM_TOOLS_PATH", default_platform_tools)
    RECV_BUFFER_SIZE = 8192
    STABLE_OUTPUT_TIMEOUT = 2.0
    POLL_INTERVAL = 0.05

    platform_tools_path = PLATFORM_TOOLS_PATH

    def wait_for_prompt(shell, prompt_patterns, timeout=10, poll_interval=0.05):
        """
        智能等待 shell 提示符出现（优化版）
        :param prompt_patterns: 提示符模式列表，如 ['$', '#', '>', '>']
        :param timeout: 超时时间（秒）
        :param poll_interval: 轮询间隔（秒）- 减少到 0.05 秒以更快响应
        :return: 接收到的所有输出
        """
        output = ""
        start_time = time.time()
        last_output_time = start_time
        last_output_length = 0
        stable_count = 0  # 输出长度稳定的计数器

        while time.time() - start_time < timeout:
            try:
                chunk = shell.recv(RECV_BUFFER_SIZE).decode('utf-8', errors='ignore')  # 增加缓冲区
                if chunk:
                    output += chunk
                    last_output_time = time.time()

                    # 检查是否出现任何提示符模式
                    for pattern in prompt_patterns:
                        current_line = output.split('\n')[-1:][0] if output.split('\n') else ''
                        if re.search(pattern, current_line):
                            return output

                    # 检查输出是否稳定（连续3次长度没有变化）
                    current_length = len(output)
                    if current_length == last_output_length:
                        stable_count += 1
                        if stable_count >= 3:  # 输出稳定了0.15秒（3 × 0.05）
                            return output
                    else:
                        stable_count = 0
                        last_output_length = current_length
            except Exception:
                # 如果超过指定时间没有新输出，认为命令已完成
                if time.time() - last_output_time > STABLE_OUTPUT_TIMEOUT:
                    return output
            time.sleep(POLL_INTERVAL)

        return output

    # 使用 invoke_shell 交互式执行
    try:
        shell = ssh.invoke_shell()
        shell.settimeout(3)  # 减少超时时间

        # 清空欢迎消息
        try:
            shell.recv(1024)
        except Exception:
            pass

        # 发送命令序列，使用智能等待（优化超时）
        shell.send(f"export PATH={platform_tools_path}:$PATH\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        shell.send(f"cd {suite_path}\n")
        wait_for_prompt(shell, [r'\$ ', r'\# ', '> '], timeout=2, poll_interval=0.05)

        # 设置 TERM 为 dumb 以禁用 readline 功能，避免 ANSI 转义序列
        shell.send(f"TERM=dumb {tradefed_bin}\n")
        # 等待 tradefed 启动（查找 tradefed 提示符）
        tradefed_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)'], timeout=6, poll_interval=0.1)

        shell.send(f"{command}\n")
        # 等待命令执行完成（查找命令提示符或结果表格）
        # 对于 list results 命令，需要等待更长时间以确保所有结果都输出完毕
        command_output = wait_for_prompt(shell, ['> ', 'tf> ', r'\(tf\)', 'All done'],
                                        timeout=20, poll_interval=0.1)

        # 额外等待一小段时间，确保所有输出都被接收
        time.sleep(0.5)

        shell.send("exit\n")
        wait_for_prompt(shell, [r'\$ ', r'\# '], timeout=2, poll_interval=0.05)

        # 读取所有剩余输出（使用更大的缓冲区和更多尝试）
        output = tradefed_output + command_output
        max_retries = 10  # 增加重试次数
        for _ in range(max_retries):
            try:
                chunk = shell.recv(16384).decode('utf-8', errors='ignore')  # 增大缓冲区
                if not chunk:
                    break
                output += chunk
                time.sleep(0.1)  # 短暂等待，确保所有数据都被接收
            except Exception:
                break

        try:
            shell.close()
        except Exception:
            pass

        return output, "", 0

    except Exception as e:
        logger.error(f"[TRADEFED] Failed to execute command: {e}")
        return "", str(e), -1


# ==================== WebSocket ====================

@app.websocket("/api/system/websocket/{client_id}")
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

async def handle_tradefed_list_results(client_id: str, websocket: WebSocket, data: dict):
    """处理 tradefed list results 命令 - 通过 SSH 执行*-tradefed list results"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)

        if not ssh:
            await websocket.send_json({
                'type': 'tradefed_list_results_error',
                'error': 'SSH 连接失败'
            })
            return

        # 获取参数
        suite_path = data.get('suite_path', '')
        tradefed_bin = data.get('tradefed_bin', '')

        if not suite_path or not tradefed_bin:
            await websocket.send_json({
                'type': 'tradefed_list_results_error',
                'error': '缺少参数：suite_path 或 tradefed_bin'
            })
            ssh_manager.return_connection(ssh)
            return

        # 执行 tradefed list results 命令（使用共享函数）
        output, error, code = execute_tradefed_command(ssh, suite_path, tradefed_bin)

        ssh_manager.return_connection(ssh)

        if code == 0:
            # 解析结果（使用共享函数）
            results = parse_tradefed_list_results(output)

            await websocket.send_json({
                'type': 'tradefed_list_results',
                'success': True,
                'output': output,
                'results': results,
                'count': len(results),
                'command': f"cd '{suite_path}' && {tradefed_bin} list results"
            })
        else:
            await websocket.send_json({
                'type': 'tradefed_list_results_error',
                'success': False,
                'error': error or f'命令执行失败，退出代码：{code}',
                'command': f"cd '{suite_path}' && {tradefed_bin} list results"
            })

    except Exception as e:
        logger.error(f"[TRADEFED_LIST_RESULTS] Error: {e}")
        await websocket.send_json({
            'type': 'tradefed_list_results_error',
            'success': False,
            'error': str(e)
        })


class LocalPtyChannel:
    """Minimal Paramiko-like PTY channel for local terminal sessions."""

    def __init__(self, command: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None):
        self.command = command
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env or os.environ.copy()
        self.pid, self.fd = pty.fork()
        self.closed = False

        if self.pid == 0:
            try:
                os.chdir(self.cwd)
                os.execvpe(command[0], command, self.env)
            except Exception as e:
                os.write(2, f"Failed to start local terminal: {e}\n".encode("utf-8", errors="ignore"))
                os._exit(127)

        os.set_blocking(self.fd, False)

    def recv_ready(self) -> bool:
        if self.closed:
            return False
        readable, _, _ = select.select([self.fd], [], [], 0)
        return bool(readable)

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            self.closed = True
            return b""

    def send(self, data: Union[str, bytes]) -> int:
        if self.closed:
            return 0
        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        return os.write(self.fd, data)

    def resize_pty(self, width: int = 120, height: int = 30):
        if self.closed:
            return
        packed = struct.pack("HHHH", height, width, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, packed)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            pass


def close_terminal_session_resources(session_info: Dict[str, Any]):
    mode = session_info.get('mode')
    channel = session_info.get('channel')
    ssh = session_info.get('ssh')

    try:
        if channel and mode in {'local', 'local_adb', 'adb'}:
            channel.close()
    except Exception:
        pass

    try:
        if mode == 'adb' and ssh:
            ssh_manager.return_connection(ssh)
        elif ssh:
            ssh.close()
    except Exception:
        pass


def create_local_terminal_channel(command: Optional[List[str]] = None) -> LocalPtyChannel:
    shell = os.environ.get("SHELL") or "/bin/bash"
    terminal_command = command or [shell, "-l"]
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    return LocalPtyChannel(terminal_command, cwd=os.path.expanduser("~"), env=env)


async def handle_adb_shell_connect(client_id: str, websocket: WebSocket, serial_no: str, config: dict):
    """处理ADB Shell连接 - 通过SSH执行adb shell命令"""
    try:
        if is_config_host_local(config):
            ssh = None
            channel = create_local_terminal_channel(['adb', '-s', serial_no, 'shell'])
            backend_mode = 'local_adb'
        else:
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                await websocket.send_json({
                    'type': 'terminal_error',
                    'error': 'SSH连接失败'
                })
                return

            channel = ssh.invoke_shell(term='xterm-256color')
            channel.setblocking(0)
            channel.resize_pty(width=80, height=24)
            channel.send('\n\n\n')
            channel.send('clear\n')
            channel.send(f'adb -s {serial_no} shell\n')
            backend_mode = 'adb'

        # 保存会话
        loop = asyncio.get_event_loop()
        session_id = client_id

        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'host': config.get('ubuntu_host') or get_ubuntu_host(),
                'user': config.get('ubuntu_user') or get_ubuntu_user(),
                'mode': backend_mode,
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
        def read_adb_shell_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] ADB Session {session_id} no longer exists")
                        break

                    try:
                        channel = global_state.terminal_ssh_sessions[session_id]['channel']

                        if channel.recv_ready():
                            data_chunk = channel.recv(4096)
                            if not data_chunk:
                                logger.info("[TERMINAL] No data received, ADB connection closed")
                                break

                            try:
                                text = data_chunk.decode('utf-8')
                            except UnicodeDecodeError:
                                text = data_chunk.decode('utf-8', errors='ignore')

                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({
                                        'type': 'terminal_data',
                                        'data': text
                                    }),
                                    loop
                                )
                                future.result(timeout=5)
                            except Exception as e:
                                logger.error(f"[TERMINAL] Error sending ADB data: {e}")
                                break
                        else:
                            time.sleep(0.01)

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] ADB read error: {e}")
                        break

            except Exception as e:
                logger.error(f"[TERMINAL] ADB read thread error: {e}")
            finally:
                # 清理连接
                logger.info(f"[TERMINAL] ADB read thread exiting for {session_id}")

        # 在后台线程中启动读取
        thread = threading.Thread(target=read_adb_shell_output, daemon=True)
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
        host = data.get('host', config.get('ubuntu_host') or get_ubuntu_host())
        user = data.get('user', config.get('ubuntu_user') or get_ubuntu_user())
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

        if CommonUtils.is_local_host(host):
            channel = create_local_terminal_channel()
            channel.resize_pty(width=80, height=24)
            loop = asyncio.get_event_loop()

            with global_state.terminal_lock:
                if session_id in global_state.terminal_ssh_sessions:
                    close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])

                global_state.terminal_ssh_sessions[session_id] = {
                    'ssh': None,
                    'channel': channel,
                    'host': host,
                    'user': user,
                    'mode': 'local',
                    'connected_at': time.time(),
                    'websocket': websocket,
                    'event_loop': loop
                }

            logger.info(f"[TERMINAL] Local terminal session created for {session_id}")
            await websocket.send_json({
                'type': 'terminal_connected',
                'mode': 'local'
            })

            def read_local_terminal_output():
                try:
                    while True:
                        if session_id not in global_state.terminal_ssh_sessions:
                            break

                        try:
                            current_channel = global_state.terminal_ssh_sessions[session_id]['channel']
                            if current_channel.recv_ready():
                                data_chunk = current_channel.recv(4096)
                                if not data_chunk:
                                    break
                                text = data_chunk.decode('utf-8', errors='ignore')
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({
                                        'type': 'terminal_data',
                                        'data': text
                                    }),
                                    loop
                                )
                                future.result(timeout=5)
                            else:
                                time.sleep(0.01)
                        except OSError:
                            break
                        except Exception as e:
                            logger.error(f"[TERMINAL] Local read error: {e}")
                            break
                finally:
                    with global_state.terminal_lock:
                        if session_id in global_state.terminal_ssh_sessions:
                            close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
                            del global_state.terminal_ssh_sessions[session_id]
                            logger.info(f"[TERMINAL] Cleaned up local session {session_id}")

            thread = threading.Thread(
                target=read_local_terminal_output,
                daemon=True,
                name=f"terminal_local_read_{session_id}"
            )
            thread.start()
            return

        # 使用 ssh_manager 创建SSH连接
        ssh_config = {
            'hostname': host,
            'username': user,
            'password': password,
            'timeout': 5,
            'use_key_auth': config.get('use_key_auth', False),
            'private_key_path': config.get('private_key_path', '~/.ssh/id_rsa')
        }

        ssh = ssh_manager.create_connection(ssh_config)
        if not ssh:
            error_msg = 'SSH连接失败：请检查用户名、密码或密钥配置'
            await websocket.send_json({
                'type': 'terminal_error',
                'error': error_msg
            })
            return

        # 创建shell通道
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        channel.resize_pty(width=80, height=24)

        # 保存SSH会话和当前事件循环
        loop = asyncio.get_event_loop()
        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])

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
        def read_ssh_terminal_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] Session {session_id} no longer exists")
                        break

                    try:
                        channel = global_state.terminal_ssh_sessions[session_id]['channel']

                        if channel.recv_ready():
                            data_chunk = channel.recv(4096)
                            if not data_chunk:
                                logger.info("[TERMINAL] No data received, connection closed")
                                break

                            try:
                                text = data_chunk.decode('utf-8')
                            except UnicodeDecodeError:
                                text = data_chunk.decode('utf-8', errors='ignore')

                            try:
                                future = asyncio.run_coroutine_threadsafe(
                                    websocket.send_json({
                                        'type': 'terminal_data',
                                        'data': text
                                    }),
                                    loop
                                )
                                future.result(timeout=5)
                            except Exception as e:
                                logger.error(f"[TERMINAL] Error sending data: {e}")
                                break
                        else:
                            time.sleep(0.01)

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] Read error: {e}")
                        break

            except Exception as e:
                logger.error(f"[TERMINAL] Read thread error: {e}")
            finally:
                # 清理连接
                with global_state.terminal_lock:
                    if session_id in global_state.terminal_ssh_sessions:
                        close_terminal_session_resources(global_state.terminal_ssh_sessions[session_id])
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
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

        # 启动读取线程
        thread = threading.Thread(target=read_ssh_terminal_output, daemon=True, name=f"terminal_read_{session_id}")
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

# ==================== API文档 ====================

# Skill命令前缀常量
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
    import re
    path_without_params = re.sub(r'\{[^}]+\}', '', path_without_api).strip('/')

    # 将/替换为-
    skill_name = path_without_params.replace("/", "-")

    return f"{SKILL_COMMAND_PREFIX}{skill_name}"

@app.get("/api/system/docs")
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


@app.get("/api/system/help")
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
            'usage': '⭐核心接口'
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
    border_line = "╔════════════════════════════════════════════════════════════════════╗"
    mid_line = "╠════════════════════════════════════════════════════════════════════╣"
    bottom_line = "╚════════════════════════════════════════════════════════════════════╝"

    help_text += f"{border_line}\n"

    # 第一行：方法 + 路径
    method_part = f"  {method}  "
    # 目标：让字符串长度与边框线一致（70个字符）
    # 内容区：70 - 2(左右║) = 68个字符
    content_length = 68
    method_length = len(method_part)
    path_length = len(path)
    needed_padding = content_length - method_length - path_length
    path_part = path + ' ' * needed_padding

    help_text += f"║{method_part}{path_part}║\n"

    help_text += f"{mid_line}\n"

    # 第二行：emoji + 描述
    description = api_details['description']
    desc_prefix = "  📋 "
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

    help_text += f"║{desc_prefix}{desc_part}║\n"

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
        help_text += "📋 API 参数对照表\n\n"

        # 计算列宽（使用显示宽度，但确保最小宽度）
        name_width = max(get_display_width('API 参数'), max((get_display_width(p['name']) for p in params), default=get_display_width('API 参数')))
        desc_width = max(get_display_width('说明'), max(((get_display_width(p['desc'].split('(')[0]) + 6) for p in params), default=get_display_width('说明')))

        # 表格字符定义
        border_char = '─'
        corner_tl = '┌'
        corner_tr = '┐'
        corner_bl = '└'
        corner_br = '┘'
        tee_top = '┬'
        tee_bottom = '┴'
        tee_cross = '┼'  # 用于行分隔线的十字连接符
        bar = '│'

        # 列宽定义（固定）
        col1_width = name_width + 2      # API 参数列（含左右空格）
        col2_width = 6                    # 类型列（固定 6 字符，确保对齐）
        col3_width = desc_width + 10      # 说明列（含标记）
        col4_width = 14                   # 默认值列（固定 14 字符）

        # 构建表格行（使用显示宽度计算表头）
        top_border     = f"{corner_tl}{border_char * col1_width}{tee_top}{border_char * col2_width}{tee_top}{border_char * col3_width}{tee_top}{border_char * col4_width}{corner_tr}\n"
        header_row     = f"{bar}{pad_string('API 参数', col1_width, 'center')}{bar}{pad_string('类型', col2_width, 'center')}{bar}{pad_string('说明', col3_width, 'center')}{bar}{pad_string('默认值', col4_width, 'center')}{bar}\n"
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
                desc_with_mark = f"{desc} ⭐"
            else:
                desc_with_mark = f"{desc} (可选)"

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
    help_text += "📤 响应示例:\n"
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


# ==================== Redmine 回复功能 ====================

@app.post("/api/redmine/reply")
async def redmine_reply(request: Request):
    """
    向 Redmine Issue 发送回复

    参数：
        issue_id: Redmine Issue ID
        reply_text: 回复内容

    返回：
        success: 是否成功
        message: 成功消息
        error: 错误信息（如果失败）
    """
    try:
        body = await request.json()
        issue_id = body.get('issue_id', '').strip()
        reply_text = body.get('reply_text', '').strip()

        if not issue_id:
            return error_response('缺少 issue_id 参数', status_code=400)

        if not reply_text:
            return error_response('缺少 reply_text 参数', status_code=400)

        logger.info(f"[Redmine Reply] 准备发送回复到 Issue #{issue_id}")

        stored_creds = await load_redmine_credentials()
        if not stored_creds:
            return error_response('未配置 Redmine 凭证', status_code=401)

        try:
            redmine_config = config_manager.get_redmine_config()
            base_url = redmine_config['base_url']
        except ValueError as e:
            return error_response(str(e), status_code=404)

        api_url = f"{base_url}/issues/{issue_id}.json"
        username = stored_creds.get('username')
        password = stored_creds.get('password')

        payload = {'issue': {'notes': reply_text}}
        headers = create_basic_auth_header(username, password)
        headers['Content-Type'] = 'application/json'
        headers['User-Agent'] = 'GMS Remote Test/1.0'

        async with aiohttp.ClientSession() as session:
            async with session.put(api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status in (200, 204):
                    logger.info(f"[Redmine Reply] 回复已成功发送到 Issue #{issue_id}")
                    return success_response({
                        'issue_url': f"{base_url}/issues/{issue_id}"
                    }, message=f'回复已发送到 Redmine Issue #{issue_id}')
                else:
                    error_body = await response.text()
                    logger.error(f"[Redmine Reply] 发送失败：HTTP {response.status} - {error_body}")
                    return error_response(f'Redmine API 返回错误：HTTP {response.status}', status_code=500)

    except Exception as e:
        logger.error(f"[Redmine Reply] 发送回复失败：{e}")
        return error_response(f'发送失败：{str(e)}', status_code=500)


# ==================== 主程序 ====================

if __name__ == "__main__":
    logger.info("Starting GMS Auto Test FastAPI Server on port 5001...")
    logger.info("=" * 60)
    logger.info("  GMS Auto Test - FastAPI Server (Port 5001)")
    logger.info("  Framework: FastAPI (Pure)")
    logger.info("  Version: 4.0.0")
    logger.info("  Production Release")
    logger.info("=" * 60)
    logger.info("")

    # 性能优化配置
    logger.info("[Performance] Using uvloop for high performance event loop")
    logger.info("[Performance] Using httptools for fast HTTP parsing")
    logger.info("[Performance] GZip compression enabled (min 500 bytes)")
    logger.info("[Performance] Static file caching: 86400s (1 day)")
    logger.info("[Performance] Connection keep-alive: 120s")
    logger.info(f"[Runtime] GMS_ENV={GMS_ENV}, CORS_ORIGINS={CORS_ORIGINS}, TRUSTED_HOSTS={TRUSTED_HOSTS}")

    # 运行FastAPI应用（单worker模式，因为lifespan事件与多worker不兼容）
    # 如需多worker，请使用 gunicorn + uvicorn.workers.UvicornWorker
    uvicorn.run(
        "app_fastapi_full:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level='info',
        proxy_headers=PROXY_HEADERS_ENABLED,
        forwarded_allow_ips=FORWARDED_ALLOW_IPS if PROXY_HEADERS_ENABLED else "",
        timeout_keep_alive=120,  # 2分钟保持连接（合理平衡资源占用）
        loop='uvloop',  # 高性能事件循环
        http='httptools',  # C扩展HTTP解析
        access_log=(GMS_ENV != 'production'),
        limit_concurrency=500,  # 最大并发连接数
        limit_max_requests=5000,  # 每个worker最大请求数后重启（防内存泄漏）
        backlog=1024,  # TCP连接队列大小
    )
