"""
常量定义 - 集中管理项目中使用的所有常量

包括服务器配置、上传进度、缓存清理、设备管理、通知、APK分析、Redmine等常量
"""

import os
from typing import List

# ==================== 项目路径 ====================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==================== 服务器配置 ====================


def _parse_int_env(env_name: str, default: int) -> int:
    """Parse integer environment variable with a safe default."""
    raw = os.getenv(env_name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


SERVER_PORT = _parse_int_env('GMS_PORT', 5001)
GMS_ENV = os.getenv('GMS_ENV', 'development').strip().lower()
SERVER_HOST = os.getenv('GMS_SERVER_HOST', '127.0.0.1' if GMS_ENV == 'production' else '0.0.0.0')
# 用于文档和示例的默认URL（使用占位符而非硬编码IP）
DEFAULT_SERVER_URL = os.getenv('GMS_SERVER_URL', f'http://server:{SERVER_PORT}')
PROXY_HEADERS_ENABLED = os.getenv(
    'GMS_PROXY_HEADERS',
    'true' if GMS_ENV == 'production' else 'false'
).strip().lower() == 'true'
FORWARDED_ALLOW_IPS = os.getenv('GMS_FORWARDED_ALLOW_IPS', '127.0.0.1')


def _parse_csv_env(env_name: str, default: str) -> List[str]:
    """Parse CSV environment variable into a normalized non-empty list."""
    items = [item.strip() for item in os.getenv(env_name, default).split(',') if item.strip()]
    return items or [default]


# 解析后的 CORS 和 TrustedHosts 配置
CORS_ORIGINS = _parse_csv_env('CORS_ORIGINS', '*')
TRUSTED_HOSTS = _parse_csv_env('TRUSTED_HOSTS', '*')


# ==================== 上传进度相关常量 ====================

UPLOAD_PROGRESS_QUERY_TIMEOUT = 5  # 查询超时（秒）
UPLOAD_PROGRESS_EXPIRATION = 10  # 进度过期时间（秒）
UPLOAD_PROGRESS_CLEANUP_INTERVAL = 60  # 清理间隔（秒）

# GSI 固件烧写进度轮询配置
GSI_PROGRESS_POLL_INTERVAL = 0.5  # 服务器端进度更新间隔（秒）

# Favicon 获取配置
DEFAULT_FAVICON_TIMEOUT = 10  # 默认超时时间（秒）
MAX_BATCH_SIZE = 20  # 批量请求最大数量

GSI_PROGRESS_INCREMENT = 5  # 每次增加的百分比
GSI_PROGRESS_MAX = 95  # 最大进度百分比（等待完成前）

# ==================== 缓存与清理常量 ====================

MAX_LOG_ENTRIES = 1000  # 最大日志条目数
CLEANUP_INTERVAL_SECONDS = 3600  # 定期清理间隔（1小时）
USER_STATE_MAX_AGE_HOURS = 24  # 用户状态最大存活时间
UPLOAD_PROGRESS_MAX_AGE_SECONDS = 600  # 上传进度过期时间（10分钟）
USBIP_STATE_MAX_AGE_SECONDS = 86400  # USB/IP状态过期时间（24小时）
APK_TASK_MAX_AGE_SECONDS = 86400  # APK 分析任务过期时间（24小时）
TERMINAL_SESSION_MAX_AGE_SECONDS = 3600  # 终端会话过期时间（1小时）
FIRMWARE_UPLOAD_PROGRESS_MAX_ITEMS_PER_CLIENT = 1  # 每客户端最多保存的固件上传进度项数

# ==================== 设备相关常量 ====================

DEVICE_CACHE_TTL = 3  # 设备缓存TTL（秒）
DEVICE_SSH_POOLS_MAX = 10  # 最大设备SSH连接池数量

# ==================== 通知相关常量 ====================

VALID_NOTIFICATION_LEVELS = {'info', 'success', 'warning', 'error'}
MAX_NOTIFICATIONS_PER_CLIENT = 200

# ==================== APK分析配置常量 ====================

JADX_PATH = os.path.join(PROJECT_ROOT, 'tools', 'jadx', 'bin', 'jadx')
APK_UPLOAD_DIR = os.path.join(PROJECT_ROOT, 'data', 'apk_uploads')
APK_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
APK_MAX_SOURCE_FILE_SIZE = 2 * 1024 * 1024  # 2MB - 单个源码文件查看上限
APK_MAX_TASKS = 50  # 最大保存任务数
JADX_TIMEOUT = 600  # jadx 反编译超时(秒)

# ==================== Redmine 相关常量 ====================

REDMINE_ISSUE_ID_CACHE_MAX_SIZE = 100  # 最多缓存100个附件ID到问题ID的映射

# ==================== 工具数据文件 ====================

TOOLS_DATA_FILE = os.path.join(PROJECT_ROOT, 'data', 'user_tools_data.json')
