"""
配置管理器 - 核心业务逻辑
"""
import getpass
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from foundation.config_paths import runtime_config_path, user_tools_path
from foundation.config_persistence import ConfigPersistenceMixin
from foundation.networking import is_local_host
from foundation.runtime_settings import RuntimeSettings


logger = logging.getLogger(__name__)


settings = RuntimeSettings.from_environment()

PROJECT_ROOT = str(settings.project_root)
SERVER_PORT = settings.server_port
GMS_ENV = settings.environment
SERVER_HOST = settings.server_host
DEFAULT_SERVER_URL = os.getenv('GMS_SERVER_URL', f'http://server:{SERVER_PORT}')
PROXY_HEADERS_ENABLED = settings.proxy_headers_enabled
FORWARDED_ALLOW_IPS = settings.forwarded_allow_ips


def _parse_csv_env(env_name: str, default: str) -> list[str]:
    items = [item.strip() for item in os.getenv(env_name, default).split(',') if item.strip()]
    return items or [default]


CORS_ORIGINS = _parse_csv_env('CORS_ORIGINS', '*')
TRUSTED_HOSTS = _parse_csv_env('TRUSTED_HOSTS', '*')

UPLOAD_PROGRESS_QUERY_TIMEOUT = 5
UPLOAD_PROGRESS_EXPIRATION = 10
UPLOAD_PROGRESS_CLEANUP_INTERVAL = 60
GSI_PROGRESS_POLL_INTERVAL = 0.5
DEFAULT_FAVICON_TIMEOUT = 10
MAX_BATCH_SIZE = 20
GSI_PROGRESS_INCREMENT = 5
GSI_PROGRESS_MAX = 95
MAX_LOG_ENTRIES = 1000
CLEANUP_INTERVAL_SECONDS = 3600
USER_STATE_MAX_AGE_HOURS = 24
UPLOAD_PROGRESS_MAX_AGE_SECONDS = 600
USBIP_STATE_MAX_AGE_SECONDS = 86400
APK_TASK_MAX_AGE_SECONDS = 86400
TERMINAL_SESSION_MAX_AGE_SECONDS = 3600
FIRMWARE_UPLOAD_PROGRESS_MAX_ITEMS_PER_CLIENT = 1
DEVICE_CACHE_TTL = 3
DEVICE_SSH_POOLS_MAX = 10
VALID_NOTIFICATION_LEVELS = {'info', 'success', 'warning', 'error'}
MAX_NOTIFICATIONS_PER_CLIENT = 200
JADX_PATH = os.path.join(PROJECT_ROOT, 'tools', 'jadx', 'bin', 'jadx')
APK_UPLOAD_DIR = os.path.join(PROJECT_ROOT, 'data', 'apk_uploads')
APK_MAX_FILE_SIZE = 500 * 1024 * 1024
APK_MAX_SOURCE_FILE_SIZE = 2 * 1024 * 1024
APK_MAX_TASKS = 50
JADX_TIMEOUT = 600
REDMINE_ISSUE_ID_CACHE_MAX_SIZE = 100
TOOLS_DATA_FILE = str(user_tools_path(PROJECT_ROOT))

# 测试 Wi-Fi 默认值统一从 config.json 的 wifi 节点读取。
DEFAULT_WIFI_SSID = ""
DEFAULT_WIFI_PASSWORD = ""

# 预编译配置占位符正则。
PLACEHOLDER_PATTERN = re.compile(r'\$\{([^}]+)\}')


class ConfigManager(ConfigPersistenceMixin):
    """Reads/writes static + runtime config with a short TTL cache to cut disk I/O."""

    def __init__(
        self,
        base_dir: str = None,
        cache_ttl: int = 5,
        project_root: str | Path | None = None,
    ):
        """Configure the config root (default: this module's dir) and cache TTL (default 5s)."""
        if project_root is not None:
            root = Path(project_root).resolve()
            base_dir = str(root / 'foundation')
        if base_dir is None:
            base_dir = os.path.dirname(os.path.abspath(__file__))

        self.base_dir = base_dir
        self.project_root = Path(base_dir).resolve().parent
        # 配置文件位于 configs 目录。真实 config.json 属于本机部署数据
        # （不入库）；缺失时回退到随源码携带的 config.example.json，
        # 保证全新 checkout / CI 可直接启动。
        self.config_path = os.path.join(base_dir, '..', 'configs', 'config.json')
        self.config_fallback_path = os.path.join(
            base_dir, '..', 'configs', 'config.example.json'
        )
        self.runtime_config_path = str(runtime_config_path(self.project_root))

        # 缓存相关
        self._cache: dict[str, Any] | None = None
        self._cache_timestamp: float = 0
        self._cache_ttl: int = cache_ttl
        self._cache_lock: threading.Lock = threading.Lock()
        self._runtime_write_lock = threading.RLock()

        # 文件修改时间追踪
        self._static_mtime: float = 0
        self._runtime_mtime: float = 0
        self._section_normalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._section_denormalizers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def load_config(self, force_reload: bool = False) -> dict[str, Any]:
        """Return the merged static+runtime config, bypassing the cache when force_reload."""
        with self._cache_lock:
            current_time = time.time()
            if not force_reload and self._is_cache_valid(current_time):
                return self._cache.copy() if self._cache else {}

            config = self._load_and_merge_config()

            self._cache = config
            self._cache_timestamp = current_time

            return config.copy() if config else {}

    def _is_cache_valid(self, current_time: float) -> bool:
        """True iff the cached snapshot is within its TTL and no config file changed on disk."""
        if self._cache is None:
            return False

        if current_time - self._cache_timestamp > self._cache_ttl:
            return False

        try:
            static_path = (
                self.config_path
                if os.path.isfile(self.config_path)
                else getattr(self, 'config_fallback_path', self.config_path)
            )
            static_mtime = os.path.getmtime(static_path)
            try:
                runtime_mtime = os.path.getmtime(self.runtime_config_path)
            except OSError:
                runtime_mtime = 0

            if (
                static_mtime != self._static_mtime
                or runtime_mtime != self._runtime_mtime
            ):
                return False

        except Exception as e:
            logger.warning(f"Error checking file mtime: {e}")
            return False

        return True

    def _load_and_merge_config(self) -> dict[str, Any]:
        """Load static config and overlay runtime overrides, returning the merged dict."""
        try:
            self._static_mtime = os.path.getmtime(
                self.config_path
                if os.path.isfile(self.config_path)
                else getattr(self, 'config_fallback_path', self.config_path)
            )
            try:
                self._runtime_mtime = os.path.getmtime(self.runtime_config_path)
            except OSError:
                self._runtime_mtime = 0
        except Exception as e:
            logger.warning(f"Error updating file mtime: {e}")

        config = self._load_static_config()

        # 运行配置覆盖静态默认值，但保留静态 ai_models 配置。
        runtime_config = self._load_runtime_config()
        if runtime_config:
            ai_config = config.get('ai_models', {})
            config.update(runtime_config)
            if ai_config:
                config['ai_models'] = ai_config

        return config

    def invalidate_cache(self):
        """Drop the cache so the next load_config() reloads from disk."""
        with self._cache_lock:
            self._cache = None
            self._cache_timestamp = 0

    def get_ai_config(self) -> dict[str, Any]:
        """Return the AI config dict, or {} when AI is unconfigured or disabled."""
        config = self.load_config()
        ai_models = config.get('ai_models', {})

        if not ai_models.get('enabled', False):
            return {}

        return ai_models

    def get_redmine_config(self) -> dict[str, Any]:
        """Return the Redmine config (domain + base_url); raise ValueError if incomplete."""
        config = self.load_config()
        redmine_config = config.get('redmine', {})

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

    def get_redmine_base_url(self, config: dict[str, Any] = None) -> str:
        """Return the configured Redmine base_url, falling back to DEFAULT_REDMINE_BASE_URL (no exception)."""
        try:
            redmine_config = (config if config is not None else self.load_config()).get("redmine") or {}
        except Exception:
            redmine_config = {}
        return str(redmine_config.get("base_url") or "").strip().rstrip("/")

    def get_ai_provider_config(self, provider_name: str) -> dict[str, Any] | None:
        """Return the named provider's config (e.g. 'qwen', 'zhipu'), or None if absent."""
        ai_config = self.get_ai_config()
        if not ai_config:
            return None

        providers = ai_config.get('providers', {})
        return providers.get(provider_name)

    def is_ai_enabled(self) -> bool:
        """True iff the AI feature is enabled and usable."""
        ai_config = self.get_ai_config()
        return ai_config.get('enabled', False)

    def _load_static_config(self) -> dict[str, Any]:
        """Load and validate the static config from disk (placeholders expanded).

        Falls back to configs/config.example.json when the deployment-local
        configs/config.json does not exist (fresh checkout / CI)."""
        path = self.config_path
        if not os.path.isfile(path):
            path = getattr(self, 'config_fallback_path', '')
            if not path or not os.path.isfile(path):
                logger.warning(f"Config file not found: {self.config_path}")
                return {}
        try:
            with open(path, encoding='utf-8') as f:
                config = json.load(f)

                config_copy = self._replace_placeholders(config)

                self._validate_ai_config(config_copy)

                return config_copy

        except FileNotFoundError:
            logger.warning(f"Config file not found: {path}")
            return {}
        except Exception as e:
            logger.error(f"Error loading static config: {e}")
            return {}

    def _validate_ai_config(self, config: dict[str, Any]) -> None:
        """Raise ValueError if the enabled AI config references a missing primary_provider."""
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

    def _replace_placeholders(self, value: Any, config: dict = None) -> Any:
        """Recursively expand ${name} placeholders: config keys, ${ENV_VAR}, and ${VAR:default}."""
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

    def _replace_single_placeholder(self, value: str, config: dict = None) -> str:
        """Replace a single ${...} placeholder in *value* (env var, config key, or ${VAR:default})."""
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

    def _load_runtime_config(self) -> dict[str, Any] | None:
        """加载运行时配置。

        configs/config_runtime.json 保存安装脚本写入的部署身份和用户操作产生的数据，
        覆盖随源码携带的静态默认值（config.json）。
        """
        try:
            with open(self.runtime_config_path, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            logger.warning(f"Runtime config {self.runtime_config_path} is not a dict: {type(data).__name__}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Error loading runtime config {self.runtime_config_path}: {e}")
        return None

    def get_runtime_config(self) -> dict[str, Any]:
        """Public read-only access to the runtime configuration."""
        return self._load_runtime_config() or {}

    def configure_section_normalizer(
        self,
        key: str,
        *,
        normalizer: Callable[[dict[str, Any]], dict[str, Any]],
        denormalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._section_normalizers[key] = normalizer
        if denormalizer is not None:
            self._section_denormalizers[key] = denormalizer

    def _normalize_section(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        normalizer = self._section_normalizers.get(key)
        return normalizer(raw) if normalizer else dict(raw)

    def _denormalize_section(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        denormalizer = self._section_denormalizers.get(key)
        return denormalizer(raw) if denormalizer else dict(raw)

    def _get_section(self, key: str) -> dict[str, Any]:
        """读取并规范化配置中的某个区段（如 redmine_dashboard / gerrit_dashboard）。"""
        return self._normalize_section(key, self.load_config().get(key) or {})

    def _save_section(self, key: str, payload: dict[str, Any], *, merge_from_runtime: bool = False) -> bool:
        """合并并持久化某个配置区段到运行时配置文件。

        merge_from_runtime=True 时优先从已加载的 runtime 取 current（用于 stats 这类
        只在 runtime 维护、静态配置不持有的区段），否则从合并后的 load_config 取。
        """
        try:
            with self._runtime_write_lock:
                runtime = self._load_runtime_config() or {}
                current = (
                    (runtime.get(key) if merge_from_runtime else None)
                    or self.load_config().get(key)
                    or {}
                )
                runtime[key] = self._denormalize_section(
                    key,
                    {**current, **(payload or {})},
                )
                return self._write_runtime_config_file(
                    runtime,
                    preserve_redmine_auth=False,
                )
        except Exception as e:
            logger.error(f"Error saving {key} config: {e}")
            return False

    def get_redmine_stats_config(self) -> dict[str, int]:
        """Return normalized Redmine stats settings after static/runtime merge."""
        return self._get_section('redmine_stats')

    def save_redmine_stats_config(self, stats_config: dict[str, Any]) -> bool:
        """Save Redmine stats settings to runtime config so UI changes take effect immediately."""
        # redmine_stats 仅由运行配置维护。
        return self._save_section('redmine_stats', stats_config, merge_from_runtime=True)

    def get_redmine_dashboard_config(self) -> dict[str, Any]:
        """Return normalized Redmine dashboard profile configuration."""
        return self._get_section('redmine_dashboard')

    def save_redmine_dashboard_config(self, dashboard_config: dict[str, Any]) -> bool:
        """Save Redmine dashboard profiles to runtime config."""
        return self._save_section('redmine_dashboard', dashboard_config)

    def get_gerrit_dashboard_config(self) -> dict[str, Any]:
        """Return normalized Gerrit dashboard configuration."""
        return self._get_section('gerrit_dashboard')

    def save_gerrit_dashboard_config(self, dashboard_config: dict[str, Any]) -> bool:
        """Save Gerrit dashboard settings to runtime config."""
        return self._save_section('gerrit_dashboard', dashboard_config)

    def save_client_ssh_credentials(self, credentials: list) -> bool:
        """Persist host-scoped SSH credentials encrypted at rest."""
        try:
            if credentials is None:
                credentials = []
            if not isinstance(credentials, list):
                raise ValueError("client_ssh_credentials must be a list")

            return self.update_runtime_config(
                {'client_ssh_credentials': self._encrypt_ssh_credentials(credentials)},
            )
        except Exception as e:
            logger.error(f"Error saving client SSH credentials: {e}")
            return False

    @staticmethod
    def _encrypt_ssh_credentials(credentials: list) -> list[dict[str, Any]]:
        from foundation.secrets import encrypt_secret

        protected: list[dict[str, Any]] = []
        for raw in credentials or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            plaintext = str(item.pop("password", "") or "")
            if plaintext:
                item["encrypted_password"] = encrypt_secret(plaintext)
            protected.append(item)
        return protected

    def prepare_client_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Merge *updates* into the existing client_hosts/client_ssh_credentials and return the full client config."""
        existing = self._load_runtime_config() or {}
        existing_credentials = existing.get('client_ssh_credentials', [])

        runtime_config = existing.copy()
        runtime_config['client_hosts'] = updates.get('client_hosts', existing.get('client_hosts', {}))
        runtime_config['client_ssh_credentials'] = self._encrypt_ssh_credentials(
            updates.get('client_ssh_credentials', existing_credentials)
        )

        # 只有在明确提供local_server时才保存（避免空值覆盖）
        if updates.get('local_server'):
            runtime_config['local_server'] = updates['local_server']
        elif 'local_server' in existing:
            runtime_config['local_server'] = existing['local_server']

        return runtime_config

    def get_device_hosts(self, config: dict[str, Any] = None) -> list:
        """Return the device-host list, reloading config when *config* is omitted."""
        if config is None:
            config = self.load_config()
        return config.get('device_hosts', [])

    def get_device_host_config(self, host: str, config: dict[str, Any] = None) -> dict[str, Any] | None:
        """Return the config block for *host* (or None), reloading config when *config* is omitted."""
        device_hosts = self.get_device_hosts(config)
        for device_host in device_hosts:
            if device_host.get('host') == host:
                return device_host
        return None

    def get_ubuntu_user(self, config: dict[str, Any] = None) -> str:
        """Return the Ubuntu username, reloading config when *config* is omitted."""
        if config is None:
            config = self.load_config()
        return config.get('ubuntu_user') or get_ubuntu_user()

    def get_wifi_defaults(self, config: dict[str, Any] = None) -> dict[str, str]:
        """Read Wi-Fi settings from the plaintext config, with an env override."""
        if config is None:
            config = self.load_config()
        wifi_cfg = config.get("wifi") or {}
        password = os.getenv("GMS_WIFI_PASSWORD", "")
        if not password:
            # 优先读取明文 password，缺失时读取 encrypted_password。
            password = str(wifi_cfg.get("password") or "")
        encrypted_password = str(wifi_cfg.get("encrypted_password") or "")
        if not password and encrypted_password:
            from foundation.secrets import decrypt_secret

            try:
                password = decrypt_secret(encrypted_password)
            except RuntimeError:
                logger.warning("Stored Wi-Fi credential cannot be decrypted; rotate it")
        return {
            "ssid": str(wifi_cfg.get("ssid") or DEFAULT_WIFI_SSID),
            "password": password or DEFAULT_WIFI_PASSWORD,
        }

    def get_ubuntu_host(self, config: dict[str, Any] = None) -> str:
        """Return the Ubuntu host address, reloading config when *config* is omitted."""
        if config is None:
            config = self.load_config()
        return config.get('ubuntu_host') or get_ubuntu_host()

    def is_config_host_local(self, config: dict[str, Any] = None) -> bool:
        """Return whether the configured Ubuntu host resolves to this machine."""
        if config is None:
            config = self.load_config()
        return is_local_host(self.get_ubuntu_host(config))

    def _split_device_host(self, device_host: str) -> tuple[str, str]:
        if not device_host or '@' not in device_host:
            return "", ""
        username, hostname = device_host.split('@', 1)
        return username.strip(), hostname.strip()

    def find_device_host_password(self, device_host: str, config: dict[str, Any] = None) -> str | None:
        """Return an exact host-scoped SSH secret decrypted at use time."""
        if config is None:
            config = self.load_config()

        if '@' not in device_host:
            return None

        username, hostname = self._split_device_host(device_host)

        from foundation.secrets import decrypt_secret

        for cred in config.get('client_ssh_credentials', []):
            cred_device_host = str(cred.get('device_host') or '').strip()
            cred_host = str(cred.get('host') or cred.get('hostname') or '').strip()
            cred_username = str(cred.get('username') or '').strip()
            if cred_device_host and cred_device_host == device_host:
                matched = True
            else:
                matched = cred_username == username and cred_host == hostname
            if not matched:
                continue
            encrypted = str(cred.get("encrypted_password") or "")
            if encrypted:
                try:
                    return decrypt_secret(encrypted)
                except RuntimeError:
                    logger.warning("SSH credential for %s cannot be decrypted; rotate it", device_host)
                    return None
            if cred.get("password"):
                logger.warning("Ignoring plaintext SSH credential for %s; rotate it", device_host)
            return None

        logger.debug(f"[Config] No SSH credential found for {device_host}")
        return None

    def upsert_device_host_password(self, device_host: str, password: str) -> bool:
        """Insert or update one Windows client SSH password in runtime config."""
        from foundation.secrets import encrypt_secret

        device_host = str(device_host or '').strip()
        password = str(password or '')
        username, hostname = self._split_device_host(device_host)
        if not username or not hostname or not password:
            return False

        with self._runtime_write_lock:
            runtime = self._load_runtime_config() or {}
            credentials = runtime.get('client_ssh_credentials') or []
            if not isinstance(credentials, list):
                credentials = []

            updated = False
            next_credentials = []
            for cred in credentials:
                if not isinstance(cred, dict):
                    continue
                cred_device_host = str(cred.get('device_host') or '').strip()
                cred_host = str(cred.get('host') or cred.get('hostname') or '').strip()
                cred_username = str(cred.get('username') or '').strip()
                is_same_host = cred_device_host == device_host or (
                    cred_username == username and cred_host == hostname
                )
                if is_same_host:
                    cred = {
                        **cred,
                        "device_host": device_host,
                        "username": username,
                        "host": hostname,
                        "encrypted_password": encrypt_secret(password),
                    }
                    updated = True
                next_credentials.append(cred)

            if not updated:
                next_credentials.append({
                    "device_host": device_host,
                    "username": username,
                    "host": hostname,
                    "encrypted_password": encrypt_secret(password),
                })

            runtime['client_ssh_credentials'] = next_credentials
            return self._write_runtime_config_file(
                runtime,
                preserve_redmine_auth=False,
            )

    def save_redmine_credentials(self, username: str, password: str) -> bool:
        """Encrypt Redmine credentials with the deployment-managed key."""
        try:
            from foundation.secrets import encrypt_secret

            encrypted_password = encrypt_secret(password)

            runtime = self._load_runtime_config() or {}
            runtime['redmine_auth'] = {
                'username': username,
                'encrypted_password': encrypted_password,
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S')
            }
            if self._write_runtime_config_file(runtime, preserve_redmine_auth=False):
                logger.info(f"[Redmine Auth] Saved credentials for {username}")
                return True
            return False
        except Exception as e:
            logger.error(f"[Redmine Auth] Failed to save credentials: {e}")
            return False

    def load_redmine_credentials(self) -> dict[str, str] | None:
        """从 configs/config_runtime.json 加载并解密 Redmine 凭证。"""
        try:
            runtime = self._load_runtime_config()
            if not runtime:
                return None
            data = runtime.get('redmine_auth')
            if not data or 'encrypted_password' not in data:
                return None

            from foundation.secrets import decrypt_secret

            decrypted_password = decrypt_secret(data['encrypted_password'])
            return {
                'username': data['username'],
                'password': decrypted_password
            }
        except Exception as e:
            logger.warning(f"[Redmine Auth] Failed to load credentials: {e}")
            return None


config_manager = ConfigManager()


# ==================== 本地主机信息自动获取 ====================

# 缓存本地主机信息。
_cached_ubuntu_user: str | None = None
_cached_ubuntu_host: str | None = None

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
            except Exception as exc:
                logger.debug("Could not detect local ubuntu host via UDP probe: %s", exc)
                _cached_ubuntu_host = ''
    return _cached_ubuntu_host
