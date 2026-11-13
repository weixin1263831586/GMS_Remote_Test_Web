"""远端只读 git SDK 源码 provider（ssh_git）。

SDK 仓库位于远端编译服务器时的只读检索 provider：经 ADR 0004 唯一 SSH
执行层（``SSHManager`` 连接池 + ``SSHExecutor``）在远端执行只读 git 四件套
（``rev-parse --verify`` / ``log -1`` / ``grep`` / ``show``），继承
:class:`~features.system.source_local_git.LocalGitProvider`
的 result_id 签名与分段读取语义；SSH 目标与凭据只来自管理员部署配置
（``sdk_sources.providers``，环境变量优先）。

只读保证（三层结构性强制）：

- 子命令白名单 ``_READONLY_GIT_SUBCOMMANDS``：``_git_command`` 之外直接抛
  编程错误（500），不存在任何写入/删除/网络子命令通道；
- 全参数 ``shlex.quote``：revision / query / path 永远不会离开引号边界，
  无法与远端 shell 元字符拼接；
- 统一 ``--no-optional-locks``：远端仓库不产生/更新 ``index.lock`` 等副作用。

与 local_git 的语义差异（如实声明，evidence gate 依赖此契约）：

- ``reproducible`` 仍为 True：revision 解析为 commit、read 走
  ``git show <commit>:<path>``，与 local_git 相同的 blob 语义；
- ``blob_sha256`` 基于 SSH 传输解码后的 UTF-8 文本再编码，而非原始字节：
  非 UTF-8 文本会被 ``errors="replace"`` 改写（``grep -I`` 已跳过二进制，
  受影响面仅极少数非 UTF-8 文本文件）；
- 全树 grep 输出超过执行器 8MB 捕获上限时按截断处理（``limited=True``），
  不报错——与 local_git 的 deadline 截断语义一致。
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from foundation.command_result import CommandResult

# 与执行器共用同一截断标记（单一来源）；ssh_git 输出内出现该标记意味着
# 远端输出超过执行器捕获上限，搜索结果必须如实上报 limited=True。
from foundation.ssh_executor import _TRUNCATION_MARKER

from .source_local_git import LocalGitProvider
from .source_provider_contract import (
    _COMMIT_RE,
    MAX_BLOB_BYTES,
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    ProviderConfig,
    SourceProviderError,
    _safe_repo_path,
)


# 远端 git grep 全树耗时与本地同量级；上限放宽到 120s 覆盖跨机房链路。
SSH_GIT_DEFAULT_TIMEOUT_SECONDS = 60
SSH_GIT_GREP_TIMEOUT_SECONDS = 120
# repo_manifest 多仓模式：服务端超时必须低于 MCP 快路径 120s HTTP 上限；
# 全仓串行超时返回 limited 结果 + hint（agent 会用 path_filter 收窄重试）。
SSH_GIT_MULTI_GREP_TIMEOUT_SECONDS = 100
# 全量发现模式：并行 SSH 分片参数（每分片一条独立连接、一条串行循环命令）。
# - 每片 32 仓按实测吞吐校准（远端 0.7-1.5s/仓 → 每片 25-50s，100s 内）；
# - 每波最多 8 条并发连接：远端 sshd MaxStartups 默认 10:30:100（同时建连
#   超过 10 个随机丢弃连接，banner 报错实测复现），8 条留安全余量；
# - 分片数超过一波时自动分波执行（波内并行、波间串行）；
# - 仓级 timeout 隔离巨型仓（prebuilts/clang 单仓 90s+ 实测，跳过保其余）。
SSH_GIT_SHARD_REPOS = 32              # 每片仓数（ceil 分片）
SSH_GIT_SHARD_WAVE_SIZE = 8           # 每波并发连接数
SSH_GIT_PER_REPO_TIMEOUT_SECONDS = 15  # 单仓 grep 远端超时（跳过慢仓）
SSH_GIT_DISCOVER_TIMEOUT_SECONDS = 180
SSH_GIT_DISCOVER_MAX_REPOS = 8192     # 发现子仓数的防御上限
# metadata 逐仓收集 HEAD 的仓数上限：超过则只返回 repo_count（上千仓的
# 逐仓 rev-parse 是分钟级操作，metadata 不承担这个成本）。
SSH_GIT_HEADS_MAX_REPOS = 64

# 唯一放行的远端 git 子命令（只读）；其他子命令一律编程错误。
_READONLY_GIT_SUBCOMMANDS = frozenset({"rev-parse", "log", "grep", "show"})

# 多仓输出协议（固定标记，不来自用户输入）：
#   @@GMS_SDK_REPO@@<repo>\t<commit>   —— 仓库切换头（commit 可为 HEAD 占位）
#   @@GMS_SDK_SUBJ@@<subject>          —— 仓库提交主题（metadata 专用）
#   <commit>:<rel path>\0<line>\0<text> —— git grep -z -n 记录
_REPO_SEP = "@@GMS_SDK_REPO@@"
_SUBJ_SEP = "@@GMS_SDK_SUBJ@@"
_HEAD_LABEL = "HEAD"


def _stderr_tail(result: CommandResult) -> str:
    lines = (result.stderr or "").strip().splitlines()
    return (lines[-1][:200] if lines else "") or f"git exit {result.code}"


def _parse_remote_grep_output(
    stdout: str, commit: str, path_filter: str, limit: int,
) -> tuple[list[dict], int, bool]:
    """解析远端 ``git grep -z -n`` 记录流，返回 (matches, scanned, limited)。

    记录协议与 :mod:`source_git_search` 相同：``<commit>:<path>\\0<line>\\0
    <text>\\n``。与本地解析的差异：SSHExecutor 按 8MB 上限截断整流并追加
    截断标记，残留的半条记录只说明截断，不能当作协议错误，因此统一降级为
    ``limited=True``（与 local_git 的 deadline 截断语义一致）。
    """
    matches: list[dict] = []
    scanned = 0
    limited = False
    prefix = commit + ":"
    fields: list[str] = []
    start = 0
    length = len(stdout)
    while start < length and not limited:
        delimiter = "\0" if len(fields) < 2 else "\n"
        end = stdout.find(delimiter, start)
        if end < 0:
            break
        fields.append(stdout[start:end])
        start = end + 1
        if len(fields) != 3:
            continue
        label, line_number, snippet = fields
        fields = []
        scanned += 1
        if not label.startswith(prefix) or not line_number.isdigit():
            raise SourceProviderError("git grep 远端输出格式无效", status_code=502)
        path = label[len(prefix):]
        if path_filter and path_filter not in path.lower():
            continue
        matches.append({
            "path": path, "line": int(line_number), "snippet": snippet[:400],
        })
        if len(matches) > limit:
            limited = True
    # 截断标记、半条记录（start < length）或未闭合字段都按截断上报。
    incomplete = bool(fields) or start < length
    return matches[:limit], scanned, limited or _TRUNCATION_MARKER in stdout or incomplete


def _parse_multi_output(
    stdout: str, path_filter: str, limit: int, strip_root: str = "",
) -> tuple[list[dict], int, bool]:
    """解析多仓聚合的 grep 记录流，返回 (matches, scanned, limited)。

    仓库切换头 ``@@REPO@@<repo>\\t<commit>`` 决定后续记录的归属；头里的
    仓库路径先经 ``strip_root`` 归一化为 SDK 相对路径，文件路径转成
    ``<repo rel>/<记录 path>``。截断标记或半条记录如实上报
    ``limited=True``（与单仓模式一致）。
    """
    matches: list[dict] = []
    scanned = 0
    limited = False
    current: tuple[str, str] | None = None  # (repo_rel, commit)
    start = 0
    length = len(stdout)
    while start < length and not limited:
        end = stdout.find("\n", start)
        if end < 0:
            # 尾部残行：流被截断在行中间。
            limited = True
            break
        unit = stdout[start:end]
        start = end + 1
        if unit.startswith(_REPO_SEP):
            header = unit[len(_REPO_SEP):]
            repo_abs, _, commit = header.partition("\t")
            commit = commit.strip()
            # 全量分片模式在 grep 阶段只带 ``HEAD`` 占位，真实 commit 由
            # 命中仓回查（_resolve_hit_commits）在签名前补齐。
            if commit != _HEAD_LABEL and not _COMMIT_RE.match(commit):
                raise SourceProviderError("多仓输出头格式无效", status_code=502)
            repo_rel = repo_abs.rstrip("/")
            root = strip_root.rstrip("/")
            if root and repo_rel.startswith(root + "/"):
                repo_rel = repo_rel[len(root) + 1:]
            current = (repo_rel, commit)
            continue
        if unit.startswith(_SUBJ_SEP):
            # heads 输出里的主题行（search 流不会出现，忽略即可）。
            continue
        if not unit:
            continue
        if current is None:
            raise SourceProviderError("多仓 grep 输出缺少仓库头", status_code=502)
        fields = unit.split("\0")
        if len(fields) != 3:
            # 截断在记录中间：残记录按协议丢弃，如实上报截断。
            limited = True
            break
        label, line_number, snippet = fields
        scanned += 1
        prefix = current[1] + ":"
        if not label.startswith(prefix) or not line_number.isdigit():
            raise SourceProviderError("git grep 远端输出格式无效", status_code=502)
        full_path = current[0] + "/" + label[len(prefix):]
        if path_filter and path_filter not in full_path.lower():
            continue
        matches.append({
            "path": full_path,
            "line": int(line_number),
            "snippet": snippet[:400],
            "repo": current[0],
            "commit": current[1],
        })
        if len(matches) > limit:
            limited = True
    return matches[:limit], scanned, limited or _TRUNCATION_MARKER in stdout


class SSHGitProvider(LocalGitProvider):
    """在远端编译服务器上对 SDK Git 仓库做只读检索的 provider。"""

    kind = "ssh_git"

    def __init__(self, config: ProviderConfig, secret: bytes):
        super().__init__(config, secret)
        self._multi = bool(config.repos) or config.discover_repos
        self._repos = config.repos
        self._repo_abs = {self._repo_rel(repo): repo for repo in config.repos}
        # 全量发现模式的状态：repos 清单缓存（进程内 + 磁盘）与加载锁；
        # 首次检索时枚举远端子仓，TTL 过期后由下次检索刷新。
        self._discovered_repos: tuple[str, ...] | None = None
        self._discovered_at: float = 0.0
        self._discover_lock = threading.Lock()
        # 延迟 import：source.system.ssh 同包模块；连接池容量小、按需复用。
        from .ssh import SSHManager

        self._ssh = SSHManager(pool_size=4)

    # ------------------------------------------------------------- plumbing

    def _repo_rel(self, repo: str) -> str:
        """远端子仓绝对路径 → SDK 根相对路径（root 缺失时原样返回）。"""
        root = self.config.repo_root.rstrip("/")
        if root and repo.startswith(root + "/"):
            return repo[len(root) + 1:]
        return repo.rstrip("/")

    def _owning_repo_rel(self, path: str) -> str | None:
        """全 SDK 相对路径 → 所属子仓相对路径（最长前缀匹配）。"""
        best: str | None = None
        for repo in self._repos_all():
            rel = self._repo_rel(repo)
            if path == rel or path.startswith(rel + "/"):
                if best is None or len(rel) > len(best):
                    best = rel
        return best

    # ---------------------------------------------------- repo discovery

    def _discover_command(self) -> str:
        """全量子仓枚举的固定模板（只读，无任何来自用户输入的插值）。

        目录型 ``.git``（普通仓）免校验直接采纳——其父目录必是 git 仓；
        仅 gitfile/submodule 型（``.git`` 为文件）才执行 ``rev-parse``
        校验（AOSP 树里极少）。``-maxdepth 6`` 覆盖 AOSP 最深层子仓且
        剪除深目录遍历；``out/``（构建产物，百万级文件）与 ``.repo``
        内部结构显式剪除——不加 maxdepth 时全树 find 在真实 SDK 上是
        分钟级（实测 180s 超时）。
        """
        return (
            "find " + shlex.quote(self.config.repo_root)
            + " -maxdepth 6 "
            + r"\( -name out -o -path '*/out/*' -o -path '*/.repo/*' \) -prune -o "
            + "-name .git \\( -type d -o -type f \\) -prune -print 2>/dev/null "
            + "| while read -r g; do "
            + "if [ -d \"$g\" ]; then printf '%s\\n' \"${g%/.git}\"; "
            + "else d=$(dirname \"$g\"); "
            + "git --no-optional-locks -C \"$d\" rev-parse --git-dir >/dev/null 2>&1 "
            + "&& printf '%s\\n' \"$d\"; fi; done | sort"
        )

    def _cache_path(self):
        """发现结果磁盘缓存路径（data_root/sdk_source_discovery/<hash>.json）。"""
        import hashlib

        from foundation.config import settings

        key = json.dumps([
            self.config.ssh_host, self.config.ssh_port, self.config.ssh_username,
            self.config.repo_root,
        ], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return settings.data_root / "sdk_source_discovery" / f"{digest}.json"

    def _load_discovered(self) -> tuple[str, ...] | None:
        """读取未过期的磁盘缓存（进程内缓存未命中时）。"""
        try:
            path = self._cache_path()
            if not path.is_file():
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            generated = float(payload.get("generated_at") or 0)
            if time.time() - generated > self.config.discover_ttl_hours * 3600:
                return None
            repos = payload.get("repos")
            if not isinstance(repos, list) or not repos:
                return None
            return tuple(str(r) for r in repos)
        except Exception:
            return None

    def _store_discovered(self, repos: tuple[str, ...]) -> None:
        try:
            from foundation.private_config import write_private_json

            write_private_json(self._cache_path(), {
                "generated_at": time.time(),
                "source_id": self.config.source_id,
                "repo_root": self.config.repo_root,
                "repo_count": len(repos),
                "repos": list(repos),
            })
        except Exception:
            # 缓存写失败不影响检索（只是下次要重新发现）。
            pass

    def _repos_all(self) -> tuple[str, ...]:
        """当前生效的子仓清单（发现模式下惰性加载/刷新）。"""
        if not self.config.discover_repos:
            return self._repos
        now = time.time()
        if (
            self._discovered_repos is not None
            and now - self._discovered_at <= self.config.discover_ttl_hours * 3600
        ):
            return self._discovered_repos
        with self._discover_lock:
            if (
                self._discovered_repos is not None
                and time.time() - self._discovered_at
                <= self.config.discover_ttl_hours * 3600
            ):
                return self._discovered_repos
            cached = self._load_discovered()
            if cached is not None:
                self._discovered_repos = cached
                self._discovered_at = time.time()
                return cached
            repos = self._discover_repos_remote()
            self._discovered_repos = repos
            self._discovered_at = time.time()
            self._store_discovered(repos)
            return repos

    def _discover_repos_remote(self) -> tuple[str, ...]:
        stdout = self._run_command_checked(
            self._discover_command(), "git 仓库发现",
            SSH_GIT_DISCOVER_TIMEOUT_SECONDS,
        )
        repos = tuple(
            line.strip() for line in stdout.splitlines() if line.strip()
        )
        if not repos:
            raise SourceProviderError(
                "SDK 源自动发现未找到任何 git 子仓；"
                "请确认 repo_root 在远端存在且包含 git 仓库",
                status_code=502,
            )
        if len(repos) > SSH_GIT_DISCOVER_MAX_REPOS:
            repos = repos[:SSH_GIT_DISCOVER_MAX_REPOS]
        return repos

    def _shards(self, repos: tuple[str, ...]) -> list[tuple[str, ...]]:
        """把子仓清单切成分片：每片 SSH_GIT_SHARD_REPOS 个仓（按实测吞吐
        校准，100s 超时内余量充足）；分波执行由调用方按
        SSH_GIT_SHARD_WAVE_SIZE 切。"""
        if len(repos) <= SSH_GIT_SHARD_REPOS:
            return [repos]
        size = SSH_GIT_SHARD_REPOS
        return [repos[i:i + size] for i in range(0, len(repos), size)]

    def _connection_config(self) -> dict:
        """SSHManager 连接参数（host/port/username 键名与 features/system/ssh.py 对齐）。"""
        return {
            "host": self.config.ssh_host,
            "port": self.config.ssh_port,
            "username": self.config.ssh_username,
            "use_key_auth": self.config.ssh_use_key_auth,
            "private_key_path": self.config.ssh_key_path or "~/.ssh/id_rsa",
            "password": self.config.ssh_password,
        }

    def _run_remote(self, command: str, timeout: int) -> CommandResult:
        """唯一远端执行入口：SSHManager 连接池 + SSHExecutor（ADR 0004）。"""
        try:
            with self._ssh.connection(self._connection_config()) as ssh:
                return self._ssh.execute_command(ssh, command, timeout=timeout)
        except RuntimeError as exc:
            raise SourceProviderError(
                "无法连接 SDK 源服务器 "
                f"{self.config.ssh_host}:{self.config.ssh_port}（SSH 连接失败）；"
                "请检查 sdk_sources.ssh_git 配置、网络与凭据",
                status_code=502,
            ) from exc

    def _git_command(self, *args: str, repo: str = "") -> str:
        """构造远端只读 git 命令行：子命令白名单 + 全参数 shlex.quote。"""
        subcommand = args[0] if args else ""
        if subcommand not in _READONLY_GIT_SUBCOMMANDS:
            raise RuntimeError(
                f"ssh_git provider 拒绝非只读 git 子命令: {subcommand}"
            )
        quoted = [shlex.quote(arg) for arg in args]
        target = repo or self.config.repo_root
        return " ".join(
            ["git", "--no-optional-locks", "-C", shlex.quote(target), *quoted]
        )

    # ------------------------------------------------- multi-repo builders
    #
    # 多仓命令是**固定模板**：查询词/路径全部经 shlex.quote 进入 ``in_q`` /
    # ``in_f`` 赋值边界，循环变量 ``$r`` 只来自内置仓库清单，远端 shell 永远
    # 不会执行任何拼接出的额外语句。git 子命令仍限于白名单四件套。

    def _shard_grep_command(
        self, query: str, path_grep: str, repos: tuple[str, ...],
        per_repo_timeout: int = 0,
    ) -> str:
        """单分片固定模板：逐仓在 HEAD 上 grep，仓库头带 ``HEAD`` 占位。

        全量模式不再逐仓 rev-parse（千仓 ×0.5s 是分钟级）；命中仓的真实
        commit 在结果聚合后仅对命中仓批量回查（_resolve_hit_commits）。

        ``per_repo_timeout > 0`` 时远端对每仓 grep 套 ``timeout <n>s``：
        prebuilts/clang 这类巨型仓单仓 grep 可达 90s+，没有仓级隔离时
        一颗老鼠屎坏一锅粥（整分片超时、全部仓的命中丢失）；仓级超时把
        慢仓的损失限制在该仓自身（跳过该仓、其余仓照常出结果）。
        """
        repos_sql = " ".join(shlex.quote(repo) for repo in repos)
        per_repo_guard = (
            f"timeout {int(per_repo_timeout)} " if per_repo_timeout > 0 else ""
        )
        return (
            "in_q=" + shlex.quote(query) + "; "
            "in_f=" + (shlex.quote(path_grep) if path_grep else "''") + "; "
            "in=$(printf '%s\\n' " + repos_sql + "); "
            "for r in $in; do "
            "printf '@@GMS_SDK_REPO@@%s\\tHEAD\\n' \"$r\"; "
            f"{per_repo_guard}"
            "git --no-optional-locks -C \"$r\" grep --fixed-strings --ignore-case "
            "--no-color --no-heading --no-break --no-column --full-name "
            "-z -n -I -e \"$in_q\" $in_f HEAD -- 2>/dev/null; "
            "done"
        )

    def _multi_heads_command(
        self, with_subject: bool, repos: tuple[str, ...] = (),
    ) -> str:
        """逐仓收集 HEAD commit（可选主题），metadata/命中仓回查专用。"""
        repos_sql = " ".join(shlex.quote(repo) for repo in (repos or self._repos_all()))
        body = (
            "for r in $in; do "
            "c=$(git --no-optional-locks -C \"$r\" rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || continue; "
            "printf '@@GMS_SDK_REPO@@%s\\t%s\\n' \"$r\" \"$c\"; "
        )
        if with_subject:
            body += (
                "s=$(git --no-optional-locks -C \"$r\" log -1 --format=%s 2>/dev/null); "
                "printf '@@GMS_SDK_SUBJ@@%s\\n' \"$s\"; "
            )
        body += "done"
        return "in=$(printf '%s\\n' " + repos_sql + "); " + body

    def _parse_heads_output(self, stdout: str) -> dict[str, dict[str, str]]:
        """解析 heads/命中仓回查输出；{repo_rel: {commit, subject}}。"""
        heads: dict[str, dict[str, str]] = {}
        current: str | None = None
        root = self.config.repo_root.rstrip("/")
        for unit in stdout.splitlines():
            if unit.startswith(_REPO_SEP):
                repo_abs, _, commit = unit[len(_REPO_SEP):].partition("\t")
                commit = commit.strip()
                if not _COMMIT_RE.match(commit):
                    raise SourceProviderError("多仓输出头格式无效", status_code=502)
                repo_rel = repo_abs.rstrip("/")
                if root and repo_rel.startswith(root + "/"):
                    repo_rel = repo_rel[len(root) + 1:]
                current = repo_rel
                heads[current] = {"commit": commit, "subject": ""}
            elif unit.startswith(_SUBJ_SEP):
                if current is not None:
                    heads[current]["subject"] = unit[len(_SUBJ_SEP):][:200]
        return heads

    def _run_command_checked(
        self, command: str, label: str, timeout: int, error_status: int = 502,
    ) -> str:
        """执行并校验：超时→504；执行器异常/远端非零退出→error_status。"""
        result = self._run_remote(command, timeout)
        if result.timed_out:
            raise SourceProviderError(
                f"{label} 在远端执行超时（{timeout}s）", status_code=504
            )
        if result.code == -1:
            raise SourceProviderError(
                f"{label} 执行失败: {_stderr_tail(result)}", status_code=error_status
            )
        if result.code != 0:
            raise SourceProviderError(
                f"{label}: {_stderr_tail(result)}", status_code=error_status
            )
        return result.stdout

    def _run_remote_checked(self, *args: str, timeout: int, error_status: int = 502) -> str:
        """单仓只读 git 调用（白名单校验在 _git_command 内）。"""
        return self._run_command_checked(
            self._git_command(*args), f"git {args[0]}", timeout, error_status
        )

    def _multi_heads(
        self, with_subject: bool = False, repos: tuple[str, ...] = (),
        timeout: int = SSH_GIT_MULTI_GREP_TIMEOUT_SECONDS,
    ) -> dict[str, dict[str, str]]:
        """逐仓收集 HEAD commit（可选主题）；返回 {repo_rel: {commit, subject}}。

        单仓 rev-parse 约 0.5s；仓数上限 SSH_GIT_HEADS_MAX_REPOS 由调用方
        （metadata）把控，命中仓回查通常只有个位数仓。
        """
        stdout = self._run_command_checked(
            self._multi_heads_command(with_subject, repos),
            "git rev-parse/log（多仓）", timeout,
        )
        heads = self._parse_heads_output(stdout)
        if not heads:
            raise SourceProviderError(
                "配置的远端子仓均不可用（rev-parse 全部失败）；"
                "请联系管理员检查 sdk_sources.ssh_git 配置",
                status_code=502,
            )
        return heads

    def _resolve_hit_commits(
        self, hit_repo_rels: set[str],
    ) -> dict[str, str]:
        """仅对有命中的子仓批量回查 HEAD commit（通常个位数仓）。"""
        root = self.config.repo_root.rstrip("/")
        abs_repos = tuple(
            rel if rel.startswith("/") else (root + "/" + rel)
            for rel in sorted(hit_repo_rels)
        )
        if not abs_repos:
            return {}
        try:
            heads = self._multi_heads(with_subject=False, repos=abs_repos, timeout=60)
        except SourceProviderError:
            # 回查失败不炸整次搜索：调用方对无 commit 的命中降级处理。
            return {}
        return {rel: info["commit"] for rel, info in heads.items()}

    # ------------------------------------------------------ revision helpers

    def _verify_repo(self) -> None:
        if self._multi:
            # 多仓校验延迟到实际调用（发现/heads 空结果会显式报错），
            # 避免每次查询多打一轮 SSH。
            return
        try:
            self._run_remote_checked(
                "rev-parse", "--is-inside-work-tree",
                timeout=SSH_GIT_DEFAULT_TIMEOUT_SECONDS,
            )
        except SourceProviderError:
            raise SourceProviderError(
                "配置的 repo_root 不是远端有效 Git 仓库；"
                "请联系管理员检查 sdk_sources.ssh_git 配置",
                status_code=502,
            ) from None

    def resolve_commit(self, revision: str) -> str:
        revision = str(revision or "").strip()
        if not revision:
            revision = self.config.default_revision
        if not revision:
            raise SourceProviderError("revision 不能为空", status_code=422)
        if revision.startswith("-") or ".." in revision:
            raise SourceProviderError("revision 非法", status_code=422)
        if self._multi:
            # repo_manifest 多仓模式没有单一 revision：revision 只作请求
            # 标签，实际 commit 逐仓解析（见 search/_multi_heads）。
            return ""
        stdout = self._run_remote_checked(
            "rev-parse", "--verify", f"{revision}^{{commit}}",
            timeout=SSH_GIT_DEFAULT_TIMEOUT_SECONDS, error_status=422,
        )
        commit = stdout.strip()
        if not _COMMIT_RE.match(commit):
            raise SourceProviderError("revision 无法解析为 commit", status_code=422)
        return commit

    def revision_metadata(self, revision: str = "") -> dict[str, Any]:
        self._verify_repo()
        if self._multi:
            label = str(revision or "").strip() or self.config.default_revision
            repos = self._repos_all()
            # 逐仓 rev-parse 是 0.5s/仓：大仓清单（全量发现模式可达千仓）
            # 只返回 repo_count + 相对路径清单，不做逐仓 HEAD 收集。
            if len(repos) > SSH_GIT_HEADS_MAX_REPOS:
                return {
                    "source_id": self.config.source_id,
                    "revision": label,
                    "commit": "",
                    "reproducible": True,
                    "multi_repo": True,
                    "discovered": self.config.discover_repos,
                    "repo_count": len(repos),
                    "repos": {self._repo_rel(r): {} for r in repos},
                }
            heads = self._multi_heads(with_subject=True, repos=repos)
            first = next(iter(heads))
            return {
                "source_id": self.config.source_id,
                "revision": label,
                "commit": heads[first]["commit"],
                "commit_subject": heads[first]["subject"],
                "reproducible": True,
                "multi_repo": True,
                "discovered": self.config.discover_repos,
                "repo_count": len(heads),
                "repos": heads,
            }
        commit = self.resolve_commit(revision)
        subject = self._run_remote_checked(
            "log", "-1", "--format=%s", commit,
            timeout=SSH_GIT_DEFAULT_TIMEOUT_SECONDS,
        ).strip()
        date = self._run_remote_checked(
            "log", "-1", "--format=%cI", commit,
            timeout=SSH_GIT_DEFAULT_TIMEOUT_SECONDS,
        ).strip()
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
        filter_value = str(path_filter or "").strip().lower()
        if self._multi:
            return self._search_multi(revision, str(query), filter_value, limit)
        self._verify_repo()
        commit = self.resolve_commit(revision)
        # 与 local_git 相同的 -z -n -I 记录流；-I 在远端跳过二进制。
        command = self._git_command(
            "grep", "--fixed-strings", "--ignore-case", "--no-color",
            "--no-heading", "--no-break", "--no-column", "--full-name",
            "-z", "-n", "-I", "-e", str(query), commit, "--",
        )
        result = self._run_remote(command, SSH_GIT_GREP_TIMEOUT_SECONDS)
        if result.timed_out:
            matches, scanned, limited = [], 0, True
        elif result.code == -1:
            raise SourceProviderError(
                f"git grep 执行失败: {_stderr_tail(result)}", status_code=502
            )
        elif result.code not in (0, 1):
            raise SourceProviderError(
                f"git grep 失败: {_stderr_tail(result)}", status_code=502
            )
        else:
            matches, scanned, limited = _parse_remote_grep_output(
                result.stdout, commit, filter_value, limit
            )
        for match in matches:
            match.update(source_id=self.config.source_id, commit=commit)
        return self._search_payload(commit, matches, scanned, limited)

    # ------------------------------------------------------------- multi search

    def _new_shard_manager(self):
        """分片连接工厂（每分片一条独立连接；测试可覆写拦截）。"""
        from .ssh import SSHManager

        return SSHManager(pool_size=1)

    def _run_shard(
        self, shard: tuple[str, ...], query: str, path_grep: str,
    ) -> CommandResult:
        """一个分片 = 一条独立 SSH 连接上的一条串行循环命令（ADR 0004 层）。

        分片连接不与主连接池共享（SSHManager 池按 destination 复用，
        分片之间并行、各自开一条连接）；命令仍是固定模板。
        """
        manager = self._new_shard_manager()
        try:
            with manager.connection(self._connection_config()) as ssh:
                return manager.execute_command(
                    ssh,
                    self._shard_grep_command(
                        query, path_grep, shard,
                        per_repo_timeout=SSH_GIT_PER_REPO_TIMEOUT_SECONDS,
                    ),
                    timeout=SSH_GIT_MULTI_GREP_TIMEOUT_SECONDS,
                )
        except RuntimeError as exc:
            raise SourceProviderError(
                "无法连接 SDK 源服务器 "
                f"{self.config.ssh_host}:{self.config.ssh_port}（SSH 连接失败）；"
                "请检查 sdk_sources.ssh_git 配置、网络与凭据",
                status_code=502,
            ) from exc

    def _search_multi(
        self, revision: str, query: str, filter_value: str, limit: int,
    ) -> dict[str, Any]:
        """全量/多仓聚合检索：分片并行 HEAD grep，命中仓后置解析 commit。

        - path_filter 下推到仓库选择（rel 包含该子串的仓才执行）；
        - 全量发现模式的子仓清单经 ``_repos_all()`` 自动枚举（TTL 缓存）；
        - 分片并行把千仓全库 grep 的墙钟压进 100s（低于 MCP 快路径 120s
          HTTP 上限）；超时分片返回已聚合的部分结果 + limited=True + hint。
        """
        label = str(revision or "").strip() or self.config.default_revision
        if label and (label.startswith("-") or ".." in label):
            raise SourceProviderError("revision 非法", status_code=422)
        repos = self._repos_all()
        ranking: list[tuple[str, float, str]] = []
        if filter_value:
            repos = tuple(
                repo for repo in repos
                if filter_value in self._repo_rel(repo).lower()
            )
            if not repos:
                return self._multi_payload(
                    label, [], 0, False,
                    hint=f"path_filter '{filter_value}' 未命中任何子仓；"
                         "可用子仓见 revision 元数据",
                )
        else:
            # 无 path_filter：按强相关排序（codesearch 符号落点 / knowledge
            # 锚点 / 子系统映射 / 路径词法），强相关仓集中在首批分片，
            # 预算内优先完成。
            from .source_repo_rank import (
                codesearch_hints_for_query,
                knowledge_hints_for_query,
                rank_repos,
            )

            ranking = rank_repos(
                query, [self._repo_rel(r) for r in repos],
                knowledge_hints=knowledge_hints_for_query(query),
                codesearch_hints=codesearch_hints_for_query(query),
            )
            rel_to_abs = {self._repo_rel(r): r for r in repos}
            repos = tuple(rel_to_abs[rel] for rel, _, _ in ranking)
        shards = self._shards(repos)
        matches: list[dict[str, Any]] = []
        scanned = 0
        limited = False
        first_error: SourceProviderError | None = None
        results: list[CommandResult] = []
        executor = ThreadPoolExecutor(max_workers=SSH_GIT_SHARD_WAVE_SIZE)
        deadline = time.monotonic() + SSH_GIT_MULTI_GREP_TIMEOUT_SECONDS
        try:
            from concurrent.futures import FIRST_COMPLETED, wait

            # 分波执行：每波最多 SSH_GIT_SHARD_WAVE_SIZE 条并发连接
            # （远端 sshd MaxStartups 约束）；强相关仓经排序已集中在前
            # 几片，通常第一波就产出全部命中。
            for wave_start in range(0, len(shards), SSH_GIT_SHARD_WAVE_SIZE):
                wave = shards[wave_start:wave_start + SSH_GIT_SHARD_WAVE_SIZE]
                futures = {executor.submit(self._run_shard, s, query, ""): s
                           for s in wave}
                pending = set(futures)
                while pending:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 and not results:
                        limited = True
                        break
                    done, pending = wait(pending, timeout=max(0.0, remaining),
                                         return_when=FIRST_COMPLETED)
                    for future in done:
                        try:
                            result = future.result(timeout=0)
                        except TimeoutError:
                            limited = True
                            continue
                        except SourceProviderError as exc:
                            # 分片失败（连接/执行错误）＝覆盖不完整，
                            # 与超时同语义。
                            first_error = first_error or exc
                            limited = True
                            continue
                        if result.timed_out:
                            limited = True
                            continue
                        if result.code not in (0, 1):
                            first_error = first_error or SourceProviderError(
                                f"git grep 失败: {_stderr_tail(result)}",
                                status_code=502,
                            )
                            continue
                        results.append(result)
                        shard_matches, shard_scanned, shard_limited = _parse_multi_output(
                            result.stdout, filter_value, limit,
                            strip_root=self.config.repo_root,
                        )
                        matches.extend(shard_matches)
                        scanned += shard_scanned
                        limited = limited or shard_limited
                    if len(matches) >= limit:
                        limited = len(matches) > limit
                        break
                if limited and len(matches) >= limit:
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not results and first_error is not None and not matches:
            raise first_error
        matches = matches[:limit]
        # 命中仓批量回查真实 commit，逐条签名 result_id；回查失败的命中
        # 降级丢弃（没有 commit 的 result_id 无法复现读取）。
        commits = self._resolve_hit_commits({m["repo"] for m in matches})
        kept: list[dict[str, Any]] = []
        for match in matches:
            commit = commits.get(match["repo"])
            if not commit:
                continue
            match["source_id"] = self.config.source_id
            match["commit"] = commit
            match["result_id"] = self._make_result_id(match["path"], commit)
            kept.append(match)
        if kept and len(kept) < len(matches):
            limited = True
        payload = self._multi_payload(label, kept, scanned, limited)
        if ranking:
            payload["ranking"] = [
                {"repo": rel, "score": score, "reason": reason}
                for rel, score, reason in ranking[:8] if score > 0
            ]
        if limited and not filter_value:
            payload["hint"] = (
                "全库检索超时/截断：请用 path_filter 缩小子仓范围"
                "（如 frameworks/base、kernel-6.1）后重试"
            )
        return payload

    def _multi_payload(
        self, label: str, matches: list[dict[str, Any]], scanned: int, limited: bool,
        hint: str = "",
    ) -> dict[str, Any]:
        payload = {
            "source_id": self.config.source_id,
            "commit": "",
            "reproducible": True,
            "multi_repo": True,
            "discovered": self.config.discover_repos,
            "revision": label,
            "total": len(matches),
            "scanned_files": scanned,
            "limited": bool(limited),
            "matches": matches,
        }
        if hint:
            payload["hint"] = hint
        return payload

    # ---------------------------------------------------------------- read

    def _blob(self, commit: str, path: str) -> bytes | None:
        """远端 ``git show <commit>:<path>``，返回 UTF-8 再编码的 blob 字节。"""
        try:
            safe_path = _safe_repo_path(path)
        except SourceProviderError:
            return None
        if self._multi:
            repo_rel = self._owning_repo_rel(safe_path)
            if repo_rel is None:
                return None
            rel_path = safe_path[len(repo_rel) + 1:] if safe_path != repo_rel else ""
            if not rel_path:
                return None
            root = self.config.repo_root.rstrip("/")
            repo_abs = repo_rel if repo_rel.startswith("/") else (
                root + "/" + repo_rel if root else repo_rel
            )
            try:
                stdout = self._run_command_checked(
                    self._git_command("show", f"{commit}:{rel_path}", repo=repo_abs),
                    "git show", SSH_GIT_DEFAULT_TIMEOUT_SECONDS, error_status=404,
                )
            except SourceProviderError as exc:
                if exc.status_code == 404:
                    return None
                raise
        else:
            try:
                stdout = self._run_remote_checked(
                    "show", f"{commit}:{safe_path}",
                    timeout=SSH_GIT_DEFAULT_TIMEOUT_SECONDS, error_status=404,
                )
            except SourceProviderError as exc:
                if exc.status_code == 404:
                    return None
                raise
        if _TRUNCATION_MARKER in stdout:
            return None
        blob = stdout.encode("utf-8")
        if len(blob) > MAX_BLOB_BYTES:
            return None
        return blob
