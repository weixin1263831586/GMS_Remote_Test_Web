"""本地 Git 仓库只读 provider（local_git）。

从 :mod:`source_provider` 拆出独立模块，供同包内的 ``ssh_git`` provider
（:mod:`source_ssh_git`）复用 result_id 签名与分段读取语义而不形成循环
import；``source_provider`` 保持原有的公共导出面（re-export）。
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import Any

from .source_git_search import grep_matches
from .source_provider_contract import (
    _COMMIT_RE,
    GIT_TIMEOUT_SECONDS,
    MAX_BLOB_BYTES,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    ProviderConfig,
    SourceProviderError,
    _git,
    _make_signed_id,
    _safe_repo_path,
    decode_signed_id,
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
        filter_value = str(path_filter or "").strip().lower()
        # 单进程 git grep 全树匹配只需数秒；逐文件 `git show` 扫描在
        # 真实 SDK 树（约 2 万文件）要分钟级，HTTP 30s 客户端必超时。
        # --fixed-strings/--ignore-case 与旧 Python 匹配的字面量、大小写
        # 不敏感语义一致；-I 跳过二进制。
        matches, scanned, limited = grep_matches(
            self.config.repo_root, commit, str(query),
            path_filter=filter_value, limit=limit, timeout=GIT_TIMEOUT_SECONDS,
        )
        for match in matches:
            match.update(source_id=self.config.source_id, commit=commit)
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
