"""SDK source providers and registry public surface."""

from __future__ import annotations

import hashlib
import subprocess
from typing import Any

from .source_codesearch import CodesearchProvider
from .source_provider_contract import (
    _COMMIT_RE,
    GIT_TIMEOUT_SECONDS,
    MAX_BLOB_BYTES,
    MAX_SEARCH_FILES,
    MAX_SEARCH_SCAN_BYTES,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    _git,
    _make_signed_id,
    _safe_repo_path,
    decode_signed_id,
)
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
    load_provider_configs as load_provider_configs,
)


class LocalGitProvider:
    """读取指定 commit blob 的本地 Git provider。"""

    kind = "local_git"

    def __init__(self, config: ProviderConfig, secret: bytes):
        self.config = config
        self._secret = secret

    # -------------------------------------------------------------- helpers

    def resolve_commit(self, revision: str) -> str:
        revision = str(revision or "").strip()
        if not revision:
            if self.config.default_revision:
                revision = self.config.default_revision
            else:
                raise SourceProviderError("revision 不能为空", status_code=422)
        if revision.startswith("-") or ".." in revision:
            raise SourceProviderError("revision 非法", status_code=422)
        commit = _git(self.config.repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
        if not commit:
            raise SourceProviderError("revision 无法解析为 commit", status_code=422)
        return commit

    def _verify_repo(self) -> None:
        try:
            _git(self.config.repo_root, "rev-parse", "--show-toplevel")
        except SourceProviderError as exc:
            raise SourceProviderError(
                "配置的 repo_root 不是有效 Git 仓库；请联系管理员检查 sdk_sources 配置",
                status_code=502,
            ) from exc

    def revision_metadata(self, revision: str = "") -> dict[str, Any]:
        self._verify_repo()
        commit = self.resolve_commit(revision)
        subject = _git(self.config.repo_root, "log", "-1", "--format=%s", commit).strip()
        date = _git(self.config.repo_root, "log", "-1", "--format=%cI", commit).strip()
        return {
            "source_id": self.config.source_id,
            "revision": revision or self.config.default_revision,
            "commit": commit,
            "commit_subject": subject[:200],
            "commit_date": date,
            "reproducible": True,
        }

    # -------------------------------------------------------------- search

    def search(
        self,
        revision: str,
        query: str,
        *,
        path_filter: str = "",
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        if not str(query or "").strip():
            raise SourceProviderError("query 不能为空", status_code=422)
        limit = max(1, min(int(limit or SEARCH_DEFAULT_LIMIT), SEARCH_MAX_LIMIT))
        self._verify_repo()
        commit = self.resolve_commit(revision)
        listing = _git(self.config.repo_root, "ls-tree", "-r", "--name-only", "-z", commit)
        names = [name for name in listing.split("\0") if name]
        if len(names) > MAX_SEARCH_FILES:
            names = names[:MAX_SEARCH_FILES]
        filter_value = str(path_filter or "").strip().lower()
        needle = str(query)
        lowered = needle.lower()
        matches: list[dict[str, Any]] = []
        scanned = 0
        scanned_bytes = 0
        limited = False
        for name in names:
            if filter_value and filter_value not in name.lower():
                continue
            if scanned >= MAX_SEARCH_FILES or scanned_bytes >= MAX_SEARCH_SCAN_BYTES:
                limited = True
                break
            scanned += 1
            blob = self._blob(commit, name)
            if blob is None:
                continue
            scanned_bytes += len(blob)
            lines = blob.decode("utf-8", errors="replace").splitlines()
            for line_no, line in enumerate(lines, start=1):
                if needle in line or lowered in line.lower():
                    matches.append({
                        "source_id": self.config.source_id,
                        "commit": commit,
                        "path": name,
                        "line": line_no,
                        "snippet": line[:400],
                    })
                    if len(matches) >= limit:
                        return self._search_payload(commit, matches, scanned, limited or True)
        return self._search_payload(commit, matches, scanned, limited)

    def _search_payload(
        self, commit: str, matches: list[dict[str, Any]], scanned: int, limited: bool
    ) -> dict[str, Any]:
        for match in matches:
            match["result_id"] = self._make_result_id(match["path"], commit)
        return {
            "source_id": self.config.source_id,
            "commit": commit,
            "reproducible": True,
            "total": len(matches),
            "scanned_files": scanned,
            "limited": bool(limited),
            "matches": matches,
        }

    def _make_result_id(self, path: str, commit: str) -> str:
        return _make_signed_id(
            {
                "source_id": self.config.source_id,
                "commit": commit,
                "path": path,
            },
            self._secret,
        )

    def decode_result_id(self, result_id: str) -> dict[str, str]:
        """校验并解码自包含 result_id；任何失败都是 422，绝不返回载荷。"""
        payload = decode_signed_id(result_id, self._secret)
        if str(payload.get("source_id") or "") != self.config.source_id:
            raise SourceProviderError(
                f"result_id 不属于 source '{self.config.source_id}'", status_code=422
            )
        commit = str(payload.get("commit") or "")
        if not _COMMIT_RE.match(commit):
            raise SourceProviderError("result_id 载荷 commit 非法", status_code=422)
        return {"source_id": self.config.source_id, "commit": commit.lower(),
                "path": str(payload.get("path") or "")}

    def _blob(self, commit: str, path: str) -> bytes | None:
        try:
            completed = subprocess.run(
                ["git", "-C", self.config.repo_root, "show", f"{commit}:{path}"],
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if completed.returncode != 0:
            return None
        blob = completed.stdout
        if len(blob) > MAX_BLOB_BYTES:
            return None
        return blob

    # ---------------------------------------------------------------- read

    def read_signed(
        self, result_id: str, *, offset: int = 0, limit: int = 400
    ) -> dict[str, Any]:
        """解码自包含 result_id 后按 commit blob 分段读取（唯一 read 入口）。"""
        decoded = self.decode_result_id(result_id)
        safe_path = _safe_repo_path(decoded["path"])
        commit = decoded["commit"]
        blob = self._blob(commit, safe_path)
        if blob is None:
            raise SourceProviderError("文件不存在或超过读取上限", status_code=404)
        blob_hash = hashlib.sha256(blob).hexdigest()
        text = blob.decode("utf-8", errors="replace")
        lines = text.splitlines()
        window = lines[offset:offset + limit]
        return {
            "source_id": self.config.source_id,
            "commit": commit,
            "reproducible": True,
            "path": safe_path,
            "blob_sha256": blob_hash,
            "total_lines": len(lines),
            "offset": offset,
            "returned_lines": len(window),
            "next_offset": offset + limit if offset + limit < len(lines) else None,
            "truncated": offset + limit < len(lines),
            "lines": [
                {"line": offset + index + 1, "text": line}
                for index, line in enumerate(window)
            ],
        }


class SourceRegistry:
    """管理所有已配置 provider；secret 用于 result_id 签名。"""

    _PROVIDER_CLASSES = {
        "local_git": LocalGitProvider,
        "codesearch": CodesearchProvider,
    }

    def __init__(self, configs: list[ProviderConfig], secret: bytes):
        self._providers: dict[str, LocalGitProvider | CodesearchProvider] = {}
        for item in configs:
            provider_class = self._PROVIDER_CLASSES.get(item.provider)
            if provider_class is None:  # load_provider_configs 已过滤；双保险
                continue
            self._providers[item.source_id] = provider_class(item, secret)

    def list_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "source_id": provider.config.source_id,
                "provider": provider.kind,
                "default_revision": provider.config.default_revision,
                "repo_root_exposed": False,
            }
            for provider in self._providers.values()
        ]

    def get(self, source_id: str) -> LocalGitProvider | CodesearchProvider:
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


def registry_state() -> str:
    """注册表状态：UNINITIALIZED / AVAILABLE / UNAVAILABLE。

    审核意见（P1）：进程内未初始化 ≠ 部署确认无 source。独立 Worker /
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
    """
    if config is None:
        # 与 Web bootstrap 相同的配置来源；features → foundation 是合法
        # 依赖方向（注意实际模块是 foundation.config）。
        from foundation.config import config_manager

        config = config_manager.load_config()
    from foundation.secrets import derive_application_key

    secret = derive_application_key("gms-sdk-source-v1:")
    configure_source_registry(load_provider_configs(config or {}), secret)
    return registry_state()


def configure_source_registry(configs: list[ProviderConfig], secret: bytes) -> None:
    global _REGISTRY
    _REGISTRY = SourceRegistry(configs, secret)


def source_registry() -> SourceRegistry:
    if _REGISTRY is None:
        configure_source_registry(load_provider_configs({}), b"")
    return _REGISTRY


__all__ = [
    "CODESEARCH_TIMEOUT_SECONDS",
    "CodesearchProvider",
    "LocalGitProvider",
    "ProviderConfig",
    "SourceProviderError",
    "SourceRegistry",
    "configure_source_registry",
    "initialize_source_runtime",
    "load_provider_configs",
    "registry_state",
    "source_registry",
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
    return bool(source_registry().list_sources())
