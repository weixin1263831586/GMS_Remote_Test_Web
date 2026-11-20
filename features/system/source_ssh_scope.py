"""Strict one-repository SDK source search for SSHGitProvider.

Separated from source_ssh_git to keep the provider below its architecture
size limit. All remote execution remains in the provider's read-only executor.
"""
from __future__ import annotations

import time
from typing import Any

from . import source_ssh_git as src
from .source_local_git import LocalGitProvider
from .source_provider_contract import (
    _COMMIT_RE,
    SourceProviderError,
    _make_signed_id,
    _safe_repo_path,
    decode_signed_id,
)


def sign_result_with_repo(provider, path: str, commit: str) -> str:
    """Store authenticated repo provenance with a pinned source result."""
    if not provider._multi:
        return LocalGitProvider._make_result_id(provider, path, commit)
    owner = provider._owning_repo_rel(path)
    if not owner:
        return LocalGitProvider._make_result_id(provider, path, commit)
    return _make_signed_id({
        "source_id": provider.config.source_id, "commit": commit,
        "path": path, "repo_scope": owner,
    }, provider._secret)


def read_signed_with_repo(
    provider, result_id: str, *, offset: int = 0, limit: int = 400,
) -> dict[str, Any]:
    """Skip cold full-tree discovery when signed result contains its repo.

    The repo hint is authenticated by the result HMAC, checked against the
    file path and current registered repo list, never supplied as free input.
    Legacy tokens remain readable via the original discovery path.
    """
    if provider._multi:
        decoded = provider.decode_result_id(result_id)
        signed = decode_signed_id(result_id, provider._secret)
        claimed = signed.get("repo_scope")
        if claimed is not None:
            scope = _safe_repo_path(str(claimed))
            path = _safe_repo_path(decoded["path"])
            if not path.startswith(scope + "/"):
                raise SourceProviderError(
                    "result_id 子仓与文件路径不匹配", status_code=422
                )
            if provider.config.discover_repos:
                absolute = provider.config.repo_root.rstrip("/") + "/" + scope
            else:
                allowed = {provider._repo_rel(repo): repo for repo in provider._repos}
                if scope not in allowed:
                    raise SourceProviderError(
                        "result_id 子仓未在当前 SDK 源中配置", status_code=422
                    )
                absolute = allowed[scope]
            with provider._search_lock:
                provider._scoped_repos[scope] = absolute
    return LocalGitProvider.read_signed(
        provider, result_id, offset=offset, limit=limit
    )


def search_scoped(
    provider, revision: str, query: str, path_filter: str, limit: int, scope: str,
) -> dict[str, Any]:
    """Search exactly one verified child repo; never imply global absence."""
    root = provider.config.repo_root.rstrip("/")
    candidate = root + "/" + scope
    if not provider.config.discover_repos:
        allowed = {provider._repo_rel(r): r for r in provider._repos}
        if scope not in allowed:
            raise SourceProviderError(
                "repo_scope 未配置为有效子仓，请从已注册子仓中选择完整路径",
                status_code=422,
            )
        candidate = allowed[scope]
    else:
        # Direct validation avoids a cold 180-second full SDK discovery scan.
        stdout = provider._run_command_checked(
            provider._git_command("rev-parse", "--show-toplevel", repo=candidate),
            "repo_scope 子仓校验", src.SSH_GIT_DEFAULT_TIMEOUT_SECONDS,
            error_status=422,
        ).strip()
        if stdout.rstrip("/") != candidate.rstrip("/"):
            raise SourceProviderError(
                "repo_scope 必须精确指向 Git 子仓根目录", status_code=422
            )
    with provider._search_lock:
        provider._scoped_repos[scope] = candidate
    pinned = bool(_COMMIT_RE.fullmatch(revision))
    if not pinned and not src._is_head_label(revision, provider.config.default_revision):
        raise SourceProviderError(
            "多仓子仓范围仅支持 HEAD 或固定 40 位 commit", status_code=422
        )
    deadline = src._SEARCH_DEADLINE.get() or (
        time.monotonic() + src.SSH_GIT_MULTI_GREP_TIMEOUT_SECONDS
    )
    result = provider._run_shard(
        (candidate,), query, "", deadline=deadline,
        revision=revision if pinned else "HEAD",
    )
    if result.timed_out:
        matches, scanned, limited = [], 0, True
    elif result.code not in (0, 1):
        raise SourceProviderError(
            f"指定子仓 git grep 失败: {src._stderr_tail(result)}", status_code=502
        )
    else:
        matches, scanned, limited = src._parse_multi_output(
            result.stdout, path_filter, limit, strip_root=provider.config.repo_root
        )
    # Even zero-hit searches must report the real SHA pinned by the SSH shard.
    headers = [
        line[len(src._REPO_SEP):].partition("\t")[2].strip()
        for line in result.stdout.splitlines() if line.startswith(src._REPO_SEP)
    ]
    commit = next((value for value in headers if _COMMIT_RE.fullmatch(value)), "")
    if not commit and not limited:
        limited = True
    kept = []
    for match in matches:
        if match.get("repo") != scope or match.get("commit") != commit:
            limited = True
            continue
        match.update(
            source_id=provider.config.source_id,
            result_id=provider._make_result_id(match["path"], commit),
        )
        kept.append(match)
    payload = provider._multi_payload(
        revision, kept, scanned, limited,
        repos_total=1, repos_pending=0 if commit else 1,
    )
    payload.update(
        scope="repository", repo_scope=scope, searched_scope=scope,
        searched_repos=[scope], commit=commit, revision_is_head=not pinned,
        global_coverage_complete=False, coverage_scope="selected_repository",
    )
    if not kept:
        payload["hint"] = (
            "仅在指定子仓检索，零命中不代表全 SDK 不存在；"
            "如需全库结论，请取消 repo_scope 并确认 coverage_complete"
        )
    return payload


