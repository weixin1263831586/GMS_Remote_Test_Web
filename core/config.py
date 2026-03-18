"""
配置管理器 - 核心业务逻辑
"""
import json
import os
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    配置管理器

    管理配置文件的读取和保存，支持静态配置和动态配置
    """

    def __init__(self, base_dir: str = None):
        """
        初始化配置管理器

        Args:
            base_dir: 基础目录（默认为当前文件所在目录）
        """
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        self.base_dir = base_dir
        self.config_path = os.path.join(base_dir, '..', 'config.json')
        self.dynamic_config_path = os.path.join(base_dir, '..', 'config_dynamic.json')

    def load_config(self) -> Dict[str, Any]:
        """
        加载配置（静态 + 动态）

        Returns:
            配置字典
        """
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

    def _load_static_config(self) -> Dict[str, Any]:
        """加载静态配置"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

                # 替换 ${ubuntu_user} 占位符
                ubuntu_user = config.get('ubuntu_user', 'hcq')
                for key, value in config.items():
                    if isinstance(value, str) and '${ubuntu_user}' in value:
                        config[key] = value.replace('${ubuntu_user}', ubuntu_user)

                logger.debug(f"Loaded static config from {self.config_path}")
                return config

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
