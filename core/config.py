"""
配置管理器 - 核心业务逻辑
"""
import json
import os
import logging
import time
import threading
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


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

        # 缓存相关
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = cache_ttl
        self._cache_lock: threading.Lock = threading.Lock()

        # 文件修改时间追踪
        self._static_mtime: float = 0
        self._dynamic_mtime: float = 0

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
                logger.debug("Using cached config")
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
            dynamic_mtime = 0
            if os.path.exists(self.dynamic_config_path):
                dynamic_mtime = os.path.getmtime(self.dynamic_config_path)

            # 如果文件修改时间变化，缓存失效
            if static_mtime != self._static_mtime or dynamic_mtime != self._dynamic_mtime:
                logger.debug("Config files modified, cache invalidated")
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
            if os.path.exists(self.dynamic_config_path):
                self._dynamic_mtime = os.path.getmtime(self.dynamic_config_path)
        except Exception as e:
            logger.warning(f"Error updating file mtime: {e}")

        # 加载静态配置
        config = self._load_static_config()

        # 加载动态配置并合并
        dynamic_config = self._load_dynamic_config()
        if dynamic_config:
            # AI配置优先使用静态配置（config.json），防止被动态配置覆盖
            ai_config = config.get('ai_models', {})
            config.update(dynamic_config)
            if ai_config:
                config['ai_models'] = ai_config

        return config

    def invalidate_cache(self):
        """使缓存失效，下次调用load_config时将重新加载"""
        with self._cache_lock:
            self._cache = None
            self._cache_timestamp = 0
            logger.debug("Config cache invalidated")

    def _load_static_config(self) -> Dict[str, Any]:
        """加载静态配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

                # 替换 ${ubuntu_user} 占位符（不修改原始配置，返回副本）
                ubuntu_user = config.get('ubuntu_user', 'hcq')
                config_copy = {}
                for key, value in config.items():
                    if isinstance(value, str) and '${ubuntu_user}' in value:
                        config_copy[key] = value.replace('${ubuntu_user}', ubuntu_user)
                    else:
                        config_copy[key] = value

                logger.debug(f"Loaded static config from {self.config_path}")
                return config_copy

        except FileNotFoundError:
            logger.warning(f"Config file not found: {self.config_path}")
            return {}
        except Exception as e:
            logger.error(f"Error loading static config: {e}")
            return {}

    def _load_dynamic_config(self) -> Optional[Dict[str, Any]]:
        """加载动态配置"""
        try:
            with open(self.dynamic_config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.debug(f"Loaded dynamic config from {self.dynamic_config_path}")
                return config

        except FileNotFoundError:
            logger.debug(f"Dynamic config file not found: {self.dynamic_config_path}")
            return None
        except Exception as e:
            logger.error(f"Error loading dynamic config: {e}")
            return None

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
            with open(self.dynamic_config_path, 'w', encoding='utf-8') as f:
                json.dump(dynamic_config, f, indent=4, ensure_ascii=False)
            logger.info(f"Saved dynamic config to {self.dynamic_config_path}")

            # 保存后使缓存失效
            self.invalidate_cache()

            return True
        except Exception as e:
            logger.error(f"Error saving dynamic config: {e}")
            return False

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
        return config.get('ubuntu_user', 'hcq')

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
