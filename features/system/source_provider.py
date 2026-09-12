"""只读 SDK 源码 provider。

统一 ``SourceProvider`` 接口；首期实现 ``local_git``：

- provider 与仓库根目录只来自管理员部署配置（config ``sdk_sources.providers``），
  客户端不能提供 base URL 或本地路径；
- revision 必须通过 ``git rev-parse --verify <rev>^{commit}`` 解析成 commit；
- 文件读取使用 ``git show <commit>:<path>``（blob 内容），保证可复现且不读
  工作树未提交内容；
- 结果 ID 为服务端签名的 opaque token（source/commit/path 的 HMAC），后续
  read 不接受自由路径；
- 拒绝 ``.git`` 私有数据与绝对路径/``..``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 30
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_SEARCH_FILES = 20000
MAX_SEARCH_SCAN_BYTES = 512 * 1024 * 1024
SEARCH_DEFAULT_LIMIT = 50
SEARCH_MAX_LIMIT = 200
READ_MAX_LINES = 4000

# result_id 是自包含的 opaque token：``src1_<base64url(payload)>_<hmac16>``。
# payload 内编码 source_id/commit/path；HMAC 防篡改，后续 read 只接受
# result_id，不再接受自由 path/commit。
RESULT_ID_PREFIX = "src1_"
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


class SourceProviderError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _git(repo_root: str, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> str:
    """以参数数组调用 git；任何输出异常都转成 SourceProviderError。"""
    try:
        completed = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise SourceProviderError(
            f"git 调用失败: {type(exc).__name__}", status_code=502
        ) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip().splitlines()
        detail = stderr[0][:200] if stderr else f"git exit {completed.returncode}"
        raise SourceProviderError(f"git: {detail}", status_code=422)
    return completed.stdout


def _safe_repo_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not normalized or normalized.startswith("..") or "/../" in normalized or "\x00" in normalized:
        raise SourceProviderError("非法路径", status_code=422)
    parts = [part for part in normalized.split("/") if part not in (".", "")]
    normalized = "/".join(parts)
    if normalized.startswith(".git") or "/.git/" in normalized:
        raise SourceProviderError("拒绝读取 .git 私有数据", status_code=422)
    return normalized


@dataclass(frozen=True)
class ProviderConfig:
    source_id: str
    provider: str
    repo_root: str
    default_revision: str = ""


def load_provider_configs(config: dict[str, Any]) -> list[ProviderConfig]:
    section = (config or {}).get("sdk_sources") or {}
    providers: list[ProviderConfig] = []
    seen: set[str] = set()
    for item in section.get("providers") or []:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id") or "").strip()
        provider = str(item.get("provider") or "").strip()
        repo_root = str(item.get("repo_root") or "").strip()
        if not source_id or source_id in seen:
            continue
        if provider != "local_git":
            logger.warning("未知 SDK provider 类型已忽略: %s", provider)
            continue
        if not repo_root:
            continue
        seen.add(source_id)
        providers.append(
            ProviderConfig(
                source_id=source_id,
                provider=provider,
                repo_root=repo_root,
                default_revision=str(item.get("default_revision") or "").strip(),
            )
        )
    return providers


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
            "total": len(matches),
            "scanned_files": scanned,
            "limited": bool(limited),
            "matches": matches,
        }

    def _make_result_id(self, path: str, commit: str) -> str:
        payload = json.dumps(
            {
                "source_id": self.config.source_id,
                "commit": commit,
                "path": path,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()[:16]
        return f"{RESULT_ID_PREFIX}{encoded}_{signature}"

    def decode_result_id(self, result_id: str) -> dict[str, str]:
        """校验并解码自包含 result_id；任何失败都是 422，绝不返回载荷。"""
        value = str(result_id or "").strip()
        if not value.startswith(RESULT_ID_PREFIX):
            raise SourceProviderError("result_id 格式非法", status_code=422)
        encoded, sep, signature = value[len(RESULT_ID_PREFIX):].rpartition("_")
        if not sep or not encoded or not signature:
            raise SourceProviderError("result_id 格式非法", status_code=422)
        expected = hmac.new(
            self._secret, encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()[:16]
        if not hmac.compare_digest(expected, signature):
            raise SourceProviderError("result_id 校验失败", status_code=422)
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(
                    encoded + "=" * (-len(encoded) % 4)
                ).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceProviderError("result_id 载荷非法", status_code=422) from exc
        if not isinstance(payload, dict):
            raise SourceProviderError("result_id 载荷非法", status_code=422)
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

    def __init__(self, configs: list[ProviderConfig], secret: bytes):
        self._providers: dict[str, LocalGitProvider] = {
            item.source_id: LocalGitProvider(item, secret) for item in configs
        }

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

    def get(self, source_id: str) -> LocalGitProvider:
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
        decoded = any_provider.decode_result_id(result_id)
        provider = self.get(decoded["source_id"])
        return provider.read_signed(result_id, offset=offset, limit=limit)


_REGISTRY: SourceRegistry | None = None


def configure_source_registry(configs: list[ProviderConfig], secret: bytes) -> None:
    global _REGISTRY
    _REGISTRY = SourceRegistry(configs, secret)


def source_registry() -> SourceRegistry:
    if _REGISTRY is None:
        configure_source_registry(load_provider_configs({}), b"")
    return _REGISTRY
