"""
配置管理器 - 核心业务逻辑
"""
import base64
import hashlib
import json
import os
import logging
import re
import time
import threading
import socket
import getpass
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Precompile regex pattern for placeholder replacement (efficiency)
PLACEHOLDER_PATTERN = re.compile(r'\$\{([^}]+)\}')


class ConfigManager:
    """
    配置管理器

    管理配置文件的读取和保存，支持静态配置和动态配置
    支持TTL缓存机制，减少磁盘I/O操作
    """

    def __init__(self, base_dir: str = None, cache_ttl: int = 5):
        """
        初始化配置管理器

        Args:
            base_dir: 基础目录（默认为当前文件所在目录）
            cache_ttl: 缓存生存时间（秒），默认5秒
        """
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        self.base_dir = base_dir
        # 配置文件已移动到 configs/ 目录
        self.config_path = os.path.join(base_dir, '..', 'configs', 'config.json')
        self.runtime_config_path = os.path.join(base_dir, '..', 'configs', 'config_runtime.json')

        # 缓存相关
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = cache_ttl
        self._cache_lock: threading.Lock = threading.Lock()

        # 文件修改时间追踪
        self._static_mtime: float = 0
        self._runtime_mtime: float = 0

    def load_config(self, force_reload: bool = False) -> Dict[str, Any]:
        """
        加载配置（静态 + 动态）

        Args:
            force_reload: 强制重新加载，忽略缓存

        Returns:
            配置字典
        """
        with self._cache_lock:
            current_time = time.time()

            # 检查是否需要重新加载
            if not force_reload and self._is_cache_valid(current_time):
                return self._cache.copy() if self._cache else {}

            # 加载配置
            config = self._load_and_merge_config()

            # 更新缓存
            self._cache = config
            self._cache_timestamp = current_time

            return config.copy() if config else {}

    def _is_cache_valid(self, current_time: float) -> bool:
        """
        检查缓存是否有效

        Args:
            current_time: 当前时间戳

        Returns:
            缓存是否有效
        """
        # 检查缓存是否存在
        if self._cache is None:
            return False

        # 检查TTL是否过期
        if current_time - self._cache_timestamp > self._cache_ttl:
            return False

        # 检查文件是否被修改
        try:
            static_mtime = os.path.getmtime(self.config_path)
            try:
                runtime_mtime = os.path.getmtime(self.runtime_config_path)
            except FileNotFoundError:
                runtime_mtime = 0

            # 如果文件修改时间变化，缓存失效
            if (
                static_mtime != self._static_mtime
                or runtime_mtime != self._runtime_mtime
            ):
                return False

        except Exception as e:
            logger.warning(f"Error checking file mtime: {e}")
            return False

        return True

    def _load_and_merge_config(self) -> Dict[str, Any]:
        """
        加载并合并配置

        Returns:
            合并后的配置字典
        """
        # 更新文件修改时间
        try:
            self._static_mtime = os.path.getmtime(self.config_path)
            try:
                self._runtime_mtime = os.path.getmtime(self.runtime_config_path)
            except FileNotFoundError:
                self._runtime_mtime = 0
        except Exception as e:
            logger.warning(f"Error updating file mtime: {e}")

        # 加载静态配置
        config = self._load_static_config()

        # 加载运行时配置并合并
        runtime_config = self._load_runtime_config()
        if runtime_config:
            ai_config = config.get('ai_models', {})
            config.update(runtime_config)
            if ai_config:
                config['ai_models'] = ai_config

        return config

    def invalidate_cache(self):
        """使缓存失效，下次调用load_config时将重新加载"""
        with self._cache_lock:
            self._cache = None
            self._cache_timestamp = 0

    def get_ai_config(self) -> Dict[str, Any]:
        """
        获取 AI 配置（统一的配置访问接口）

        Returns:
            AI 配置字典，如果未配置或未启用则返回空字典
        """
        config = self.load_config()
        ai_models = config.get('ai_models', {})

        # 如果 AI 未启用，返回空配置
        if not ai_models.get('enabled', False):
            return {}

        return ai_models

    def get_redmine_config(self) -> Dict[str, Any]:
        """
        获取 Redmine 配置（统一的配置访问接口）

        Returns:
            Redmine 配置字典，包含 domain 和 base_url

        Raises:
            ValueError: 如果 Redmine 未配置或配置不完整
        """
        config = self.load_config()
        redmine_config = config.get('redmine', {})

        # 如果配置为空或不完整，抛出异常
        if not redmine_config or 'base_url' not in redmine_config:
            raise ValueError(
                'Redmine 未配置或配置不完整，请在 configs/config.json 中配置 redmine 段，'
                '包含 domain 和 base_url 字段'
            )

        # 验证必需字段
        if 'domain' not in redmine_config:
            # 如果domain字段缺失，从base_url中提取
            from urllib.parse import urlparse
            parsed = urlparse(redmine_config['base_url'])
            redmine_config['domain'] = parsed.netloc

        return redmine_config

    def get_ai_provider_config(self, provider_name: str) -> Optional[Dict[str, Any]]:
        """
        获取指定 AI provider 的配置

        Args:
            provider_name: provider 名称（如 'qwen', 'zhipu'）

        Returns:
            provider 配置字典，如果不存在则返回 None
        """
        ai_config = self.get_ai_config()
        if not ai_config:
            return None

        providers = ai_config.get('providers', {})
        return providers.get(provider_name)

    def is_ai_enabled(self) -> bool:
        """
        检查 AI 功能是否已启用

        Returns:
            AI 是否已启用
        """
        ai_config = self.get_ai_config()
        return ai_config.get('enabled', False)

    def _load_static_config(self) -> Dict[str, Any]:
        """加载静态配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

                # 递归替换所有占位符（支持 ${ubuntu_user} 和环境变量 ${VAR_NAME}）
                config_copy = self._replace_placeholders(config)

                # 验证替换后的 AI 配置
                self._validate_ai_config(config_copy)

                return config_copy

        except FileNotFoundError:
            logger.warning(f"Config file not found: {self.config_path}")
            return {}
        except Exception as e:
            logger.error(f"Error loading static config: {e}")
            return {}

    def _validate_ai_config(self, config: Dict[str, Any]) -> None:
        """
        验证 AI 配置的有效性

        Args:
            config: 配置字典

        Raises:
            ValueError: 如果配置无效
        """
        ai_models = config.get('ai_models', {})
        if not ai_models or not ai_models.get('enabled', False):
            return

        primary_provider = ai_models.get('primary_provider')
        providers = ai_models.get('providers', {})

        if primary_provider and primary_provider not in providers:
            available = list(providers.keys())
            raise ValueError(
                f"AI 配置错误: primary_provider '{primary_provider}' 不存在。"
                f"可用的 providers: {available if available else '(无)'}"
            )

    def _replace_placeholders(self, value: Any, config: Dict = None) -> Any:
        """
        递归替换配置中的占位符

        支持的占位符格式：
        - ${ubuntu_user} -> 配置中的 ubuntu_user 值
        - ${ENV_VAR} -> 环境变量值
        - ${VAR:default} -> 环境变量值，如果不存在则使用默认值

        Args:
            value: 配置值（可以是 dict, list, str 等）
            config: 原始配置对象（用于获取配置值）

        Returns:
            替换后的值
        """
        if isinstance(value, dict):
            # 第一次遍历时保存配置引用
            if config is None:
                config = value
            return {k: self._replace_placeholders(v, config) for k, v in value.items()}
        elif isinstance(value, list):
            return [self._replace_placeholders(item, config) for item in value]
        elif isinstance(value, str):
            # 递归处理嵌套占位符，最多 3 层嵌套
            for _ in range(3):
                if '${' not in value:
                    break
                new_value = self._replace_single_placeholder(value, config)
                if new_value == value:
                    break
                value = new_value
            return value
        else:
            return value

    def _replace_single_placeholder(self, value: str, config: Dict = None) -> str:
        """替换字符串中的单个占位符"""
        full_placeholder_match = PLACEHOLDER_PATTERN.fullmatch(value)

        def replace_var(match):
            var_expr = match.group(1)
            # 检查是否有默认值
            if ':' in var_expr:
                var_name, default_val = var_expr.split(':', 1)
                # 优先使用环境变量
                if var_name in os.environ:
                    return os.environ[var_name]
                # 其次使用配置中的值
                elif config and var_name in config:
                    return str(config[var_name])
                else:
                    # 默认值可能也是占位符（如 ${USER}），需要进一步处理
                    if '${' in default_val:
                        return self._replace_single_placeholder(default_val, config)
                    return default_val
            else:
                var_name = var_expr
                placeholder = match.group(0)

                # 优先使用环境变量
                if var_name in os.environ:
                    return os.environ[var_name]

                # 常见部署占位符允许在环境变量缺失时兜底，避免 UI 显示 ${...}
                if var_name == 'UBUNTU_HOST':
                    return ''
                if var_name == 'UBUNTU_USER':
                    detected_user = get_ubuntu_user()
                    if detected_user:
                        return detected_user

                # 其次使用配置中的值，但不要把同一个占位符原样递归替回去
                if config and var_name in config:
                    config_value = str(config[var_name])
                    if config_value != placeholder:
                        return config_value

                # 保留原样（未找到替换值）
                logger.warning(f"Placeholder ${{{var_name}}} not found in config or environment")
                return placeholder

        # 使用预编译的 regex pattern 替换所有 ${...} 格式的占位符
        replaced = PLACEHOLDER_PATTERN.sub(replace_var, value)
        if full_placeholder_match:
            normalized = replaced.strip().lower()
            if normalized in ('true', 'false'):
                return normalized == 'true'
        return replaced

    def _load_runtime_config(self) -> Optional[Dict[str, Any]]:
        """加载运行时配置（合并了原 dynamic + credentials）"""
        try:
            with open(self.runtime_config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error loading runtime config: {e}")
            return None

    def save_client_ssh_credentials(self, credentials: list) -> bool:
        """保存客户端 SSH 凭据到运行时配置文件。"""
        try:
            if credentials is None:
                credentials = []
            if not isinstance(credentials, list):
                raise ValueError("client_ssh_credentials must be a list")

            runtime = self._load_runtime_config() or {}
            runtime['client_ssh_credentials'] = credentials
            return self._save_runtime_config(runtime, preserve_redmine_auth=False)
        except Exception as e:
            logger.error(f"Error saving client SSH credentials: {e}")
            return False

    def save_config(self, config: Dict[str, Any]) -> bool:
        """
        保存静态配置

        Args:
            config: 配置字典

        Returns:
            是否保存成功
        """
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            logger.info(f"Saved config to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False

    def save_dynamic_config(self, dynamic_config: Dict[str, Any]) -> bool:
        """
        保存运行时配置（包含动态配置和 SSH 凭据）

        Args:
            dynamic_config: 运行时配置字典

        Returns:
            是否保存成功
        """
        try:
            dynamic_config = dict(dynamic_config or {})
            return self._save_runtime_config(dynamic_config)
        except Exception as e:
            logger.error(f"Error saving runtime config: {e}")
            return False

    def _save_runtime_config(self, runtime_config: Dict[str, Any], preserve_redmine_auth: bool = True) -> bool:
        """保存运行时配置到文件

        Args:
            runtime_config: 完整的运行时配置字典
            preserve_redmine_auth: 是否自动保留已有的 redmine_auth（调用方已加载时可传 False 避免重复读文件）
        """
        try:
            # 仅在调用方未包含 redmine_auth 且未明确跳过时，从文件读取保留
            if preserve_redmine_auth and 'redmine_auth' not in runtime_config:
                existing = self._load_runtime_config()
                if existing and 'redmine_auth' in existing:
                    runtime_config['redmine_auth'] = existing['redmine_auth']

            os.makedirs(os.path.dirname(self.runtime_config_path), exist_ok=True)
            with open(self.runtime_config_path, 'w', encoding='utf-8') as f:
                json.dump(runtime_config, f, indent=4, ensure_ascii=False)
            logger.info(f"Saved runtime config to {self.runtime_config_path}")
            self.invalidate_cache()
            return True
        except Exception as e:
            logger.error(f"Error writing runtime config: {e}")
            return False

    def prepare_client_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备客户端相关配置，保留现有client_hosts和client_ssh_credentials

        Args:
            updates: 要更新的字段字典

        Returns:
            完整的客户端配置字典
        """
        existing = self._load_runtime_config() or {}
        existing_credentials = existing.get('client_ssh_credentials', [])

        dynamic_config = existing.copy()
        dynamic_config['client_hosts'] = updates.get('client_hosts', existing.get('client_hosts', {}))
        dynamic_config['client_ssh_credentials'] = updates.get(
            'client_ssh_credentials',
            existing_credentials
        )

        # 只有在明确提供local_server时才保存（避免空值覆盖）
        if 'local_server' in updates and updates['local_server']:
            dynamic_config['local_server'] = updates['local_server']
        elif 'local_server' in existing:
            dynamic_config['local_server'] = existing['local_server']

        return dynamic_config

    def get_device_hosts(self, config: Dict[str, Any] = None) -> list:
        """
        获取设备主机列表

        Args:
            config: 配置字典（如果不提供则重新加载）

        Returns:
            设备主机配置列表
        """
        if config is None:
            config = self.load_config()
        return config.get('device_hosts', [])

    def get_device_host_config(self, host: str, config: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """
        获取指定主机的配置

        Args:
            host: 主机地址
            config: 配置字典（如果不提供则重新加载）

        Returns:
            主机配置字典
        """
        device_hosts = self.get_device_hosts(config)
        for device_host in device_hosts:
            if device_host.get('host') == host:
                return device_host
        return None

    def get_ubuntu_user(self, config: Dict[str, Any] = None) -> str:
        """
        获取 Ubuntu 用户名

        Args:
            config: 配置字典（如果不提供则重新加载）

        Returns:
            Ubuntu 用户名
        """
        if config is None:
            config = self.load_config()
        return config.get('ubuntu_user') or get_ubuntu_user()

    def get_ubuntu_host(self, config: Dict[str, Any] = None) -> str:
        """
        获取 Ubuntu 主机地址

        Args:
            config: 配置字典（如果不提供则重新加载）

        Returns:
            Ubuntu 主机地址
        """
        if config is None:
            config = self.load_config()
        return config.get('ubuntu_host') or get_ubuntu_host()

    def find_device_host_password(self, device_host: str, config: Dict[str, Any] = None) -> Optional[str]:
        """
        从 client_ssh_credentials 中查找对应 device_host 的密码

        Args:
            device_host: 设备主机地址（格式: username@ip）
            config: 配置字典（如果不提供则重新加载）

        Returns:
            密码字符串，如果找不到则返回 None
        """
        if config is None:
            config = self.load_config()

        if '@' not in device_host:
            return None

        username, hostname = device_host.split('@', 1)

        # 从 client_ssh_credentials 中查找匹配的凭据
        for cred in config.get('client_ssh_credentials', []):
            if cred.get('username') == username:
                logger.debug(f"[Config] Found SSH credential for username={username}")
                return cred.get('password')

        logger.debug(f"[Config] No SSH credential found for {device_host}")
        return None

    def _get_redmine_cipher_suite(self):
        """获取 Redmine 凭证加密用的 Fernet 实例"""
        from cryptography.fernet import Fernet
        encryption_key = base64.urlsafe_b64encode(hashlib.sha256(b'gms_remote_test_redmine_2024').digest())
        return Fernet(encryption_key)

    def save_redmine_credentials(self, username: str, password: str) -> bool:
        """加密保存 Redmine 凭证到 config_runtime.json"""
        try:
            cipher_suite = self._get_redmine_cipher_suite()
            encrypted_password = cipher_suite.encrypt(password.encode()).decode()

            runtime = self._load_runtime_config() or {}
            runtime['redmine_auth'] = {
                'username': username,
                'encrypted_password': encrypted_password,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S')
            }
            if self._save_runtime_config(runtime, preserve_redmine_auth=False):
                logger.info(f"[Redmine Auth] Saved credentials for {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"[Redmine Auth] Failed to save credentials: {e}")
            return False

    def load_redmine_credentials(self) -> Optional[Dict[str, str]]:
        """从 config_runtime.json 加载并解密 Redmine 凭证"""
        try:
            runtime = self._load_runtime_config()
            if not runtime:
                return None
            data = runtime.get('redmine_auth')
            if not data or 'encrypted_password' not in data:
                return None

            cipher_suite = self._get_redmine_cipher_suite()
            decrypted_password = cipher_suite.decrypt(data['encrypted_password'].encode()).decode()
            return {
                'username': data['username'],
                'password': decrypted_password
            }
        except Exception as e:
            logger.warning(f"[Redmine Auth] Failed to load credentials: {e}")
            return None


# 全局配置管理器实例
config_manager = ConfigManager()


# ==================== 本地主机信息自动获取 ====================

# Cache for local host info (avoid repeated system calls)
_cached_ubuntu_user: Optional[str] = None
_cached_ubuntu_host: Optional[str] = None

def get_ubuntu_user() -> str:
    """自动获取 Ubuntu 用户名（带缓存）"""
    global _cached_ubuntu_user
    if _cached_ubuntu_user is None:
        _cached_ubuntu_user = os.environ.get('UBUNTU_USER') or os.environ.get('USER') or getpass.getuser() or 'gms'
    return _cached_ubuntu_user


def get_ubuntu_host() -> str:
    """自动获取 Ubuntu 主机 IP 地址（带缓存）"""
    global _cached_ubuntu_host
    if _cached_ubuntu_host is None:
        # 优先使用环境变量
        env_host = os.environ.get('UBUNTU_HOST')
        if env_host:
            _cached_ubuntu_host = env_host
        else:
            # 自动检测本地 IP
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(2)
                    s.connect(('8.8.8.8', 53))
                    _cached_ubuntu_host = s.getsockname()[0]
            except Exception:
                _cached_ubuntu_host = ''
    return _cached_ubuntu_host
