"""SDK source providers and registry public surface.

provider 实现按模块拆分：``local_git`` 见 :mod:`source_local_git`、
``codesearch`` 见 :mod:`source_codesearch`、``ssh_git``（远端编译服务器
只读检索）见 :mod:`source_ssh_git`；本模块只保留注册表与进程级初始化，
并 re-export 各 provider 以维持既有公共导出面。
"""

from __future__ import annotations

import logging
from typing import Any

from .source_codesearch import CodesearchProvider
from .source_local_git import LocalGitProvider
from .source_provider_contract import (
    CODESEARCH_TIMEOUT_SECONDS as CODESEARCH_TIMEOUT_SECONDS,
)
from .source_provider_contract import (
    ProviderConfig as ProviderConfig,
)
from .source_provider_contract import (
    SourceProviderError as SourceProviderError,
)
from .source_provider_contract import (
    decode_signed_id as decode_signed_id,
)
from .source_provider_contract import (
    load_provider_configs as load_provider_configs,
)
from .source_ssh_git import SSHGitProvider
from .source_user_store import UserSourceStore as UserSourceStore
from .source_user_store import UserSourceStoreError as UserSourceStoreError
from .source_user_store import (
    user_source_to_provider_config as user_source_to_provider_config,
)


class SourceRegistry:
    """管理所有已配置 provider；secret 用于 result_id 签名。"""

    _PROVIDER_CLASSES = {
        "local_git": LocalGitProvider,
        "codesearch": CodesearchProvider,
        "ssh_git": SSHGitProvider,
    }

    def __init__(self, configs: list[ProviderConfig], secret: bytes):
        self._providers: dict[str, LocalGitProvider | CodesearchProvider | SSHGitProvider] = {}
        for item in configs:
            provider_class = self._PROVIDER_CLASSES.get(item.provider)
            if provider_class is None:  # load_provider_configs 已过滤；双保险
                continue
            self._providers[item.source_id] = provider_class(item, secret)

    def list_sources(self) -> list[dict[str, Any]]:
        # repo_root 永不暴露；ssh_git 额外给出连接目标（host:port）与
        # 检索模式，供管理页只读展示——不含任何凭据。
        listing = []
        for provider in self._providers.values():
            entry = {
                "source_id": provider.config.source_id,
                "provider": provider.kind,
                "default_revision": provider.config.default_revision,
                "android_version": provider.config.android_version,
                "repo_root_exposed": False,
            }
            if isinstance(provider, SSHGitProvider):
                entry["ssh_host"] = provider.config.ssh_host
                entry["ssh_port"] = provider.config.ssh_port
                entry["ssh_username"] = provider.config.ssh_username
                entry["multi_repo"] = provider._multi
                entry["discover_repos"] = provider.config.discover_repos
            listing.append(entry)
        return listing

    def get(self, source_id: str) -> LocalGitProvider | CodesearchProvider | SSHGitProvider:
        provider = self._providers.get(str(source_id or "").strip())
        if provider is None:
            raise SourceProviderError(
                f"SDK source '{source_id}' 未配置；可用: {sorted(self._providers)}",
                status_code=404,
            )
        return provider

    def read_signed(
        self, result_id: str, *, offset: int = 0, limit: int = 400
    ) -> dict[str, Any]:
        """用自包含 result_id 读取；provider 由 result_id 载荷决定。"""
        # 先用一个临时 provider 解码载荷以拿到 source_id（所有 provider 共享
        # registry secret，解码结果一致）。
        any_provider = next(iter(self._providers.values()), None)
        if any_provider is None:
            raise SourceProviderError("SDK source 未配置", status_code=404)
        decoded = decode_signed_id(result_id, any_provider._secret)
        provider = self.get(str(decoded.get("source_id") or ""))
        return provider.read_signed(result_id, offset=offset, limit=limit)


_REGISTRY: SourceRegistry | None = None
# reload 复用的密钥与静态配置快照（initialize 时缓存；未初始化时为 None）。
_REGISTRY_SECRET: bytes | None = None
_LAST_STATIC_CONFIG: dict[str, Any] | None = None
_USER_STORE: UserSourceStore | None = None


def registry_state() -> str:
    """注册表状态：UNINITIALIZED / AVAILABLE / UNAVAILABLE。

    进程内未初始化 ≠ 部署确认无 source。独立 Worker /
    CLI 必须先调用 :func:`initialize_source_runtime`；否则
    ``sdk_sources_available()`` 不能当作"部署没有 SDK 源"的证据。
    """
    if _REGISTRY is None:
        return "UNINITIALIZED"
    return "AVAILABLE" if _REGISTRY.list_sources() else "UNAVAILABLE"


def initialize_source_runtime(config: dict[str, Any] | None = None) -> str:
    """进程无关的 SDK source 运行时初始化（FastAPI / Worker / CLI 共用）。

    之前只有 Web bootstrap 调 configure_source_registry，导致独立
    daily-brief Worker 的 ``sdk_sources_available()`` 恒为 False，evidence
    gate 对测试类失败降级放行（与 Web 进程行为不一致）。返回 registry
    状态字符串。

    配置读取 / 密钥派生失败时**显式抛错**，绝不吞异常后用空配置兜底：
    registry 保持 UNINITIALIZED（``sdk_sources_available()`` → None），
    evidence gate 按 fail-safe 强制取证，而不是把初始化失败误标成
    UNAVAILABLE（部署明明配置了 source 却被降级）。

    配置来源 = 静态 ``configs/local/config.json`` 的
    ``sdk_sources.providers`` **加上**用户经「添加SDK」面板注册的动态源
    （:mod:`features.system.source_user_store`）；同 source_id 冲突时
    静态配置优先，动态条目跳过并告警。
    """
    global _REGISTRY_SECRET, _LAST_STATIC_CONFIG
    if config is None:
        # 与 Web bootstrap 相同的配置来源；features → foundation 是合法
        # 依赖方向（注意实际模块是 foundation.config）。
        from foundation.config import config_manager

        config = config_manager.load_config()
    from foundation.secrets import derive_application_key

    secret = derive_application_key("gms-sdk-source-v1:")
    _REGISTRY_SECRET = secret
    _LAST_STATIC_CONFIG = dict(config or {})
    _rebuild_source_registry(_LAST_STATIC_CONFIG, secret)
    return registry_state()


def user_source_store() -> UserSourceStore:
    """用户注册 SDK 源的单例存储（进程内共享）。"""
    global _USER_STORE
    if _USER_STORE is None:
        from foundation.config import settings
        from foundation.config_paths import sdk_user_sources_path

        _USER_STORE = UserSourceStore(
            sdk_user_sources_path(settings.project_root)
        )
    return _USER_STORE


def _build_registry_configs(config: dict[str, Any]) -> list[ProviderConfig]:
    """静态 providers + 用户注册源合并。

    单条记录解密/解析失败只跳过该条（不拖垮其他源与静态源）；但存储
    **文件损坏**（JSON 解析失败）必须向上抛——吞掉它会让热重载/初始化
    用"没有用户源"的空视图重建 registry，表现为已注册源静默消失。
    """
    providers = load_provider_configs(config)
    static_ids = {item.source_id for item in providers}
    try:
        records = user_source_store().list_sources()
    except UserSourceStoreError:
        raise  # 存储文件损坏/不可读：调用方决定保留旧 registry 或显式失败
    except Exception as exc:
        logger.warning("用户 SDK 源存储不可读，仅使用静态配置：%s", exc)
        return providers
    for record in records:
        sid = str(record.get("source_id") or "")
        if not sid or sid in static_ids:
            if sid in static_ids:
                logger.warning(
                    "用户 SDK 源 '%s' 与静态配置冲突，静态优先，已跳过", sid
                )
            continue
        try:
            password = user_source_store().get_password(sid)
            item = user_source_to_provider_config(record, password=password)
            provider_config = load_provider_configs(
                {"sdk_sources": {"providers": [item]}}
            )
            if provider_config:
                providers.append(provider_config[0])
        except UserSourceStoreError as exc:
            logger.warning("用户 SDK 源 '%s' 加载失败，已跳过：%s", sid, exc)
        except Exception as exc:
            logger.warning("用户 SDK 源 '%s' 构建异常，已跳过：%s", sid, exc)
    return providers


def reload_source_runtime() -> str:
    """「添加SDK / 删除」后的进程内热重载（不重新派生密钥）。

    复用 initialize 时的静态配置快照（:data:`_LAST_STATIC_CONFIG`），否则
    空 config 会把静态 providers 全部清掉。进程尚未初始化时回退到完整
    :func:`initialize_source_runtime`（独立 Worker 场景没有管理端点，
    不会走到这里；Web 进程正常时一定已初始化）。
    """
    if _REGISTRY_SECRET is None:
        return initialize_source_runtime()
    _rebuild_source_registry(_LAST_STATIC_CONFIG or {}, _REGISTRY_SECRET)
    return registry_state()


def _rebuild_source_registry(config: dict[str, Any], secret: bytes) -> None:
    global _REGISTRY, _REGISTRY_BUILT_AT
    # 在读取记录前取指纹：构建期间的并发写入必须由下一次读检测到。
    fingerprint = _user_store_fingerprint()
    registry = SourceRegistry(_build_registry_configs(config), secret)
    _REGISTRY = registry
    _REGISTRY_BUILT_AT = fingerprint


def configure_source_registry(configs: list[ProviderConfig], secret: bytes) -> None:
    global _REGISTRY, _REGISTRY_BUILT_AT
    _REGISTRY = SourceRegistry(configs, secret)
    _REGISTRY_BUILT_AT = _user_store_fingerprint()


# 跨进程热更新：记录 registry 构建时用户存储的 (mtime_ns, size)。其他进程
# （Web 双 worker / daily-brief Worker）添加或删除 SDK 源后，本进程在下次
# ``source_registry()`` 读路径上检测到文件变化即自动重建，无需重启；
# ``reload_source_runtime()`` 保留为显式触发入口。经 UserSourceStore 的
# fcntl 文件锁保护下原子替换（os.replace），mtime+size 双指标不会读到半写状态。
_REGISTRY_BUILT_AT: tuple[int, int] | None = None


_FINGERPRINT_MISSING: tuple[str] = ("missing",)


def _user_store_fingerprint() -> tuple[int, int] | tuple[str] | None:
    """用户存储文件指纹（三态，2026-11-14 审核意见）。

    - ``(mtime_ns, size)``：文件存在；
    - ``_FINGERPRINT_MISSING``：文件确认不存在（无用户源的基线，与
      "存在但内容没变"是不同状态——文件被删除后旧 registry 必须重建，
      不能继续用已消失的用户源）；
    - ``None``：stat 失败（权限/I/O 错误）——**歧义态**，调用方必须按
      "可能已变化"处理（触发重建），不能当作无变化静默沿用旧 registry。
    """
    try:
        stat = user_source_store().path.stat()
    except FileNotFoundError:
        return _FINGERPRINT_MISSING
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _registry_is_stale() -> bool:
    """构建后用户存储被其他进程改动（或文件消失/stat 失败）→ 需要重建。"""
    if _REGISTRY is None or _REGISTRY_SECRET is None:
        return False  # 未初始化进程走显式 503，不做隐式初始化
    current = _user_store_fingerprint()
    if current is None:
        # stat 歧义态：保守判定为 stale。重建读失败时保留旧 registry 并
        # 记录错误（显式可见），绝不把"读不出状态"当成"没有变化"。
        return True
    return current != _REGISTRY_BUILT_AT


def source_registry() -> SourceRegistry:
    """当前进程的 SDK source registry。

    旧的惰性兜底（``configure_source_registry([], b"")``）
    会在未初始化进程里用**空密钥**建空 registry 并写回全局——第一条
    ``/api/sdk/*`` 读请求就把状态从 UNINITIALIZED 静默转成 UNAVAILABLE，
    ``sdk_sources_available()`` 由 None（fail-safe 强制取证）变 False，
    evidence gate 随之降级放水。未初始化必须显式失败（503 依赖不可用），
    初始化入口只有 :func:`initialize_source_runtime`。

    已初始化进程在用户存储被其他进程修改后自动重建（mtime+size 变化检测），
    保证多进程部署下「添加SDK / 删除」最终一致；重建失败保留旧 registry
    并记录错误（不吞成空配置）。
    """
    if _REGISTRY is None:
        raise SourceProviderError(
            "SDK source 运行时未初始化；请先调用 initialize_source_runtime()",
            status_code=503,
        )
    if _registry_is_stale():
        try:
            reload_source_runtime()
        except Exception:
            logger.exception("SDK source registry 热更新失败；沿用旧 registry")
    return _REGISTRY


logger = logging.getLogger(__name__)

__all__ = [
    "CODESEARCH_TIMEOUT_SECONDS",
    "CodesearchProvider",
    "LocalGitProvider",
    "ProviderConfig",
    "SSHGitProvider",
    "SourceProviderError",
    "SourceRegistry",
    "UserSourceStore",
    "UserSourceStoreError",
    "configure_source_registry",
    "initialize_source_runtime",
    "load_provider_configs",
    "registry_state",
    "reload_source_runtime",
    "source_registry",
    "user_source_store",
    "user_source_to_provider_config",
]


def sdk_sources_available() -> bool | None:
    """Whether any SDK source provider is configured in this process.

    Public surface for other features (e.g. the daily brief's evidence gate):
    an empty *initialized* registry means source-level verification tools
    have nothing to query, so the gate degrades instead of dead-locking
    test-failure issues. ``None`` = 进程未初始化（fail-safe：调用方不得
    由此降级，evidence gate 按强制处理）。
    """
    if registry_state() == "UNINITIALIZED":
        return None
    # registry 已初始化（AVAILABLE/UNAVAILABLE）：空 registry = 部署确认
    # 无源（降级依据），有源 = 要求取证。读路径同样检查跨进程更新，
    # 避免初始为空的 Worker 永久绕过新注册源的取证要求。
    return bool(source_registry().list_sources())
