"""
配置管理器 - 核心业务逻辑
"""
import json
import os
import logging
import re
import time
import threading
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
        self.dynamic_config_path = os.path.join(base_dir, '..', 'configs', 'config_dynamic.json')
        self.client_ssh_credentials_path = os.path.join(
            base_dir,
            '..',
            'configs',
            'client_ssh_credentials.local.json'
        )

        # 缓存相关
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = cache_ttl
        self._cache_lock: threading.Lock = threading.Lock()

        # 文件修改时间追踪
        self._static_mtime: float = 0
        self._dynamic_mtime: float = 0
        self._credentials_mtime: float = 0

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
            # 直接使用 try/except 处理文件不存在，避免 TOCTOU 竞争条件
            try:
                dynamic_mtime = os.path.getmtime(self.dynamic_config_path)
            except FileNotFoundError:
                dynamic_mtime = 0
            try:
                credentials_mtime = os.path.getmtime(self.client_ssh_credentials_path)
            except FileNotFoundError:
                credentials_mtime = 0

            # 如果文件修改时间变化，缓存失效
            if (
                static_mtime != self._static_mtime
                or dynamic_mtime != self._dynamic_mtime
                or credentials_mtime != self._credentials_mtime
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
            # 直接使用 try/except 处理文件不存在，避免 TOCTOU 竞争条件
            try:
                self._dynamic_mtime = os.path.getmtime(self.dynamic_config_path)
            except FileNotFoundError:
                self._dynamic_mtime = 0
            try:
                self._credentials_mtime = os.path.getmtime(self.client_ssh_credentials_path)
            except FileNotFoundError:
                self._credentials_mtime = 0
        except Exception as e:
            logger.warning(f"Error updating file mtime: {e}")

        # 加载静态配置
        config = self._load_static_config()

        # 加载动态配置并合并
        dynamic_config = self._load_dynamic_config()
        if dynamic_config:
            ai_config = config.get('ai_models', {})
            config.update(dynamic_config)
            if ai_config:
                config['ai_models'] = ai_config

        local_credentials = self._load_client_ssh_credentials()
        if local_credentials is not None:
            config['client_ssh_credentials'] = local_credentials

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
                        return default_val
                else:
                    var_name = var_expr
                    # 优先使用环境变量
                    if var_name in os.environ:
                        return os.environ[var_name]
                    # 其次使用配置中的值
                    elif config and var_name in config:
                        return str(config[var_name])
                    else:
                        # 保留原样（未找到替换值）
                        logger.warning(f"Placeholder ${{{var_name}}} not found in config or environment")
                        return match.group(0)

            # 使用预编译的 regex pattern 替换所有 ${...} 格式的占位符
            replaced = PLACEHOLDER_PATTERN.sub(replace_var, value)
            if full_placeholder_match:
                normalized = replaced.strip().lower()
                if normalized in ('true', 'false'):
                    return normalized == 'true'
            return replaced
        else:
            return value

    def _load_dynamic_config(self) -> Optional[Dict[str, Any]]:
        """加载动态配置"""
        try:
            with open(self.dynamic_config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config

        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error loading dynamic config: {e}")
            return None

    def _load_client_ssh_credentials(self) -> Optional[list]:
        """从本地忽略文件加载客户端 SSH 凭据。"""
        try:
            with open(self.client_ssh_credentials_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            logger.error(f"Error loading client SSH credentials: {e}")
            return None

        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            credentials = data.get('client_ssh_credentials')
            return credentials if isinstance(credentials, list) else []
        logger.warning("Invalid client SSH credentials format")
        return []

    def save_client_ssh_credentials(self, credentials: list) -> bool:
        """保存客户端 SSH 凭据到本地忽略文件，避免写入受跟踪配置。"""
        try:
            if credentials is None:
                credentials = []
            if not isinstance(credentials, list):
                raise ValueError("client_ssh_credentials must be a list")

            os.makedirs(os.path.dirname(self.client_ssh_credentials_path), exist_ok=True)
            with open(self.client_ssh_credentials_path, 'w', encoding='utf-8') as f:
                json.dump(credentials, f, indent=4, ensure_ascii=False)
            logger.info(f"Saved client SSH credentials to {self.client_ssh_credentials_path}")
            self.invalidate_cache()
            return True
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
        保存动态配置

        Args:
            dynamic_config: 动态配置字典

        Returns:
            是否保存成功
        """
        try:
            dynamic_config = dict(dynamic_config or {})
            credentials_marker = object()
            credentials = dynamic_config.pop('client_ssh_credentials', credentials_marker)

            os.makedirs(os.path.dirname(self.dynamic_config_path), exist_ok=True)
            with open(self.dynamic_config_path, 'w', encoding='utf-8') as f:
                json.dump(dynamic_config, f, indent=4, ensure_ascii=False)
            logger.info(f"Saved dynamic config to {self.dynamic_config_path}")

            if credentials is not credentials_marker:
                if not self.save_client_ssh_credentials(credentials):
                    return False

            # 保存后使缓存失效
            self.invalidate_cache()

            return True
        except Exception as e:
            logger.error(f"Error saving dynamic config: {e}")
            return False

    def prepare_client_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备客户端相关配置，保留现有client_hosts和client_ssh_credentials

        Args:
            updates: 要更新的字段字典

        Returns:
            完整的客户端配置字典
        """
        existing = self._load_dynamic_config() or {}
        local_credentials = self._load_client_ssh_credentials()
        existing_credentials = (
            local_credentials
            if local_credentials is not None
            else existing.get('client_ssh_credentials', [])
        )

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
        return config.get('ubuntu_user') or os.environ.get('USER') or 'gms'

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
        return config.get('ubuntu_host', '')

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


# 全局配置管理器实例
config_manager = ConfigManager()
