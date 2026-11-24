"""ssh_git 远端只读 SDK 源码 provider 测试。

用假 SSH 网关（记录命令、按子命令回放结果）验证：
- 单仓模式：revision 解析 / metadata / search / read 全链路语义不变；
- 多仓模式：HEAD 直查分片（无逐仓 rev-parse）、命中仓后置解析 commit、
  read 绑定子仓 commit、自动发现（TTL 缓存）、分片失败降级 partial；
- 子命令白名单拒绝任何非只读 git 子命令（编程错误）；
- 查询参数（含 shell 元字符）全部进入 shlex.quote 边界；
- SSH 连接失败 → 502；远端超时 → 504；grep 无命中（exit 1）→ 空结果；
- 截断（执行器截断标记 / 半条记录）如实上报 limited=True；
- 配置解析：缺 ssh_host / 无凭据的口令模式被忽略，密钥模式放行。
"""

from __future__ import annotations

import contextlib
import shlex
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from features.system.source_provider import (
    SourceProviderError,
    SSHGitProvider,
    load_provider_configs,
)
from features.system.source_provider_contract import ProviderConfig
from features.system.source_ssh_git import _HEAD_LABEL, _REPO_SEP, _SUBJ_SEP


COMMIT = "a" * 40
FILE_PATH = "framework/base/Service.java"
FILE_TEXT = "class Service {\n  void run() { /* ok */ }\n}\n"


def _grep_record(commit: str, path: str, line: int, text: str) -> str:
    return f"{commit}:{path}\0{line}\0{text}\n"


class _FakeSSHGateway:
    """记录远端命令并按子命令回放结果的假 SSHManager。"""

    def __init__(self, handler):
        self.handler = handler
        self.commands: list[tuple[str, int]] = []
        self.last_config: dict | None = None

    def connection(self, config: dict):
        self.last_config = config
        return contextlib.nullcontext(object())

    def execute_command(self, ssh, command: str, timeout: int = 30,
                        get_pty: bool = False):
        self.commands.append((command, timeout))
        return self.handler(command, timeout)


def _remote_git_handler(grep_stdout: str = "", grep_code: int = 0,
                        show_stdout: str = "", show_code: int = 0):
    def handler(command: str, timeout: int):
        from foundation.command_result import CommandResult

        tokens = shlex.split(command)
        assert tokens[:4] == ["git", "--no-optional-locks", "-C", "/remote/sdk"], command
        subcommand = tokens[4]
        if subcommand == "rev-parse":
            if "--is-inside-work-tree" in tokens:
                return CommandResult(stdout="true\n", code=0)
            return CommandResult(stdout=COMMIT + "\n", code=0)
        if subcommand == "log":
            if "--format=%s" in tokens:
                return CommandResult(stdout="Fix service boot\n", code=0)
            return CommandResult(stdout="2026-11-13T10:00:00+08:00\n", code=0)
        if subcommand == "grep":
            return CommandResult(stdout=grep_stdout, code=grep_code)
        if subcommand == "show":
            return CommandResult(stdout=show_stdout, code=show_code)
        return CommandResult(stdout="", stderr=f"unexpected: {subcommand}", code=3)

    return handler


def _make_provider(gateway: _FakeSSHGateway) -> SSHGitProvider:
    provider = SSHGitProvider(
        ProviderConfig(
            source_id="android-206",
            provider="ssh_git",
            repo_root="/remote/sdk",
            default_revision="main",
            ssh_host="10.10.10.206",
            ssh_port=22,
            ssh_username="hcq",
            ssh_password="secret",
        ),
        b"test-secret",
    )
    provider._ssh = gateway
    return provider


class SSHGitProviderTests(unittest.TestCase):
    def test_revision_metadata_via_remote(self):
        gateway = _FakeSSHGateway(_remote_git_handler())
        provider = _make_provider(gateway)
        metadata = provider.revision_metadata("")
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(metadata["revision"], "main")
        self.assertEqual(metadata["commit_subject"], "Fix service boot")
        self.assertTrue(metadata["reproducible"])
        # 全部命令都经假网关执行，且只在白名单子命令内。
        executed = {shlex.split(command)[4] for command, _ in gateway.commands}
        self.assertEqual(executed, {"rev-parse", "log"})

    def test_search_and_read_round_trip(self):
        grep_stdout = _grep_record(COMMIT, FILE_PATH, 2, "void run() { /* ok */ }")
        gateway = _FakeSSHGateway(_remote_git_handler(
            grep_stdout=grep_stdout, show_stdout=FILE_TEXT,
        ))
        provider = _make_provider(gateway)
        result = provider.search("main", "run()")
        self.assertEqual(result["commit"], COMMIT)
        self.assertTrue(result["reproducible"])
        self.assertFalse(result["limited"])
        self.assertEqual(len(result["matches"]), 1)
        match = result["matches"][0]
        self.assertEqual(match["path"], FILE_PATH)
        self.assertEqual(match["line"], 2)
        self.assertEqual(match["source_id"], "android-206")

        read = provider.read_signed(match["result_id"], offset=0, limit=10)
        self.assertEqual(read["path"], FILE_PATH)
        self.assertEqual(read["total_lines"], 3)
        self.assertIn("/* ok */", read["lines"][1]["text"])
        # blob_sha256 基于传输文本再编码（与原始字节语义的差异已在契约声明）。
        self.assertTrue(read["blob_sha256"])

    def test_readonly_guard_rejects_write_subcommands(self):
        gateway = _FakeSSHGateway(_remote_git_handler())
        provider = _make_provider(gateway)
        for subcommand in ("push", "reset", "checkout", "clean", "config"):
            with self.assertRaises(RuntimeError):
                provider._git_command(subcommand)
        self.assertEqual(gateway.commands, [])

    def test_query_metacharacters_stay_quoted(self):
        gateway = _FakeSSHGateway(_remote_git_handler())
        provider = _make_provider(gateway)
        command = provider._git_command("grep", "-e", "run(); rm -rf /", COMMIT)
        self.assertIn("'run(); rm -rf /'", command)
        # 整条命令可以被安全地 shlex 还原，不会出现裸元字符。
        tokens = shlex.split(command)
        self.assertIn("run(); rm -rf /", tokens)

    def test_ssh_connection_failure_maps_502(self):
        class _DeadGateway:
            def connection(self, config: dict):
                raise RuntimeError("Failed to get SSH connection")

        provider = _make_provider(unittest.mock.Mock())
        provider._ssh = _DeadGateway()
        with self.assertRaises(SourceProviderError) as ctx:
            provider.revision_metadata("main")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_executor_failure_is_502_not_caller_status(self):
        from foundation.command_result import CommandResult

        gateway = _FakeSSHGateway(
            lambda command, timeout: CommandResult(code=-1, stderr="secret channel", timed_out=False)
        )
        provider = _make_provider(gateway)
        with self.assertRaises(SourceProviderError) as ctx:
            provider.resolve_commit("main")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertNotIn("secret", str(ctx.exception))

    def test_remote_timeout_maps_504(self):
        from foundation.command_result import CommandResult

        gateway = _FakeSSHGateway(
            lambda command, timeout: CommandResult(code=-1, timed_out=True)
        )
        provider = _make_provider(gateway)
        with self.assertRaises(SourceProviderError) as ctx:
            provider.resolve_commit("main")
        self.assertEqual(ctx.exception.status_code, 504)

    def test_grep_no_matches_exit_1(self):
        gateway = _FakeSSHGateway(_remote_git_handler(grep_code=1))
        provider = _make_provider(gateway)
        result = provider.search("main", "missing-symbol")
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["limited"])

    def test_grep_truncated_output_reports_limited(self):
        from foundation.ssh_executor import _TRUNCATION_MARKER

        # 截断标记紧跟完整记录：记录保留，limited=True。
        gateway = _FakeSSHGateway(_remote_git_handler(
            grep_stdout=_grep_record(COMMIT, FILE_PATH, 2, "x") + _TRUNCATION_MARKER,
        ))
        provider = _make_provider(gateway)
        result = provider.search("main", "run")
        self.assertTrue(result["limited"])
        self.assertEqual(len(result["matches"]), 1)

        # 半条记录（第三字段中间截断）：残记录按协议丢弃，仍如实上报截断。
        gateway = _FakeSSHGateway(_remote_git_handler(
            grep_stdout=_grep_record(COMMIT, FILE_PATH, 2, "void run()")[:-1],
        ))
        provider = _make_provider(gateway)
        result = provider.search("main", "run")
        self.assertTrue(result["limited"])
        self.assertEqual(result["matches"], [])

    def test_show_truncated_blob_maps_404(self):
        from foundation.ssh_executor import _TRUNCATION_MARKER

        gateway = _FakeSSHGateway(_remote_git_handler(
            grep_stdout=_grep_record(COMMIT, FILE_PATH, 2, "x"),
            show_stdout=FILE_TEXT[:100] + _TRUNCATION_MARKER,
        ))
        provider = _make_provider(gateway)
        match = provider.search("main", "run")["matches"][0]
        with self.assertRaises(SourceProviderError) as ctx:
            provider.read_signed(match["result_id"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_show_remote_failure_maps_404(self):
        gateway = _FakeSSHGateway(_remote_git_handler(
            grep_stdout=_grep_record(COMMIT, FILE_PATH, 2, "x"),
            show_stdout="", show_code=128,
        ))
        provider = _make_provider(gateway)
        match = provider.search("main", "run")["matches"][0]
        with self.assertRaises(SourceProviderError) as ctx:
            provider.read_signed(match["result_id"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_path_filter_lowercased_substring(self):
        grep_stdout = (
            _grep_record(COMMIT, "framework/base/Service.java", 2, "A")
            + _grep_record(COMMIT, "base/core/Other.java", 9, "B")
        )
        gateway = _FakeSSHGateway(_remote_git_handler(grep_stdout=grep_stdout))
        provider = _make_provider(gateway)
        result = provider.search("main", "x", path_filter="Framework/Base")
        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["framework/base/Service.java"],
        )


class SSHGitMultiRepoTests(unittest.TestCase):
    """多仓模式：解析后 commit 检索、按仓读取、自动发现、分片降级。"""

    SDK_ROOT = "/remote/sdk"
    REPO_FB = "/remote/sdk/frameworks/base"
    REPO_KERN = "/remote/sdk/kernel-6.1"
    COMMIT_FB = "b" * 40
    COMMIT_KERN = "c" * 40
    COMMIT_R0 = "a" * 40

    def setUp(self):
        self.executed: list[str] = []
        # Discovery cache must not leak between fake SDK fixtures or CI runs.
        self._cache_temp = tempfile.TemporaryDirectory(prefix="ssh-git-test-")
        self.addCleanup(self._cache_temp.cleanup)
        self.provider = self._provider(self._handler())
        self.provider._ssh = self.provider._new_shard_manager()

    # ---- fakes -------------------------------------------------------

    class _FakeShardManager:
        def __init__(self, handler, recorder):
            self._handler = handler
            self._recorder = recorder

        def connection(self, config: dict):
            return contextlib.nullcontext(object())

        def execute_command(self, ssh, command: str, timeout: int = 30,
                            get_pty: bool = False):
            return self._handler(command, timeout)

    def _handler(self, grep_stdout: str = "", heads_stdout: str = "",
                 show_stdout: str = "", show_code: int = 0):
        from foundation.command_result import CommandResult

        def handler(command: str, timeout: int):
            self.executed.append(command)
            if _SUBJ_SEP in command:      # heads（含主题，metadata 专用）
                return CommandResult(stdout=heads_stdout, code=0)
            if " grep " in command:       # 分片 HEAD 直查
                return CommandResult(stdout=grep_stdout, code=0)
            if "rev-parse" in command:    # 命中仓 commit 回查
                return CommandResult(stdout=heads_stdout, code=0)
            if " show " in command:
                return CommandResult(stdout=show_stdout, code=show_code)
            return CommandResult(stdout="", stderr="unexpected", code=3)

        return handler

    def _provider(self, handler, repos=(REPO_FB, REPO_KERN),
                  discover=False, ttl=24):
        provider = SSHGitProvider(
            ProviderConfig(
                source_id="android206-x",
                provider="ssh_git",
                repo_root=self.SDK_ROOT,
                default_revision="HEAD",
                ssh_host="remote.example",
                ssh_username="op",
                ssh_password="secret",
                repos=repos,
                discover_repos=discover,
                discover_ttl_hours=ttl,
            ),
            b"test-secret",
        )
        provider._cache_path = lambda: Path(self._cache_temp.name) / "discovered.json"
        provider._new_shard_manager = lambda: self._FakeShardManager(
            handler, self.executed)
        provider._ssh = provider._new_shard_manager()
        return provider

    def _heads_stdout(self, with_subject: bool = False) -> str:
        # _multi_heads_command writes each subject immediately after its repo.
        out = f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
        if with_subject:
            out += f"{_SUBJ_SEP}Fix fb\n"
        out += f"{_REPO_SEP}kernel-6.1\t{self.COMMIT_KERN}\n"
        if with_subject:
            out += f"{_SUBJ_SEP}Fix kernel\n"
        return out

    def _grep_stdout(self) -> str:
        # 分片命令在 grep 前解析真实 SHA：结果头/记录都绑定该 commit。
        return (
            f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
            + _grep_record(self.COMMIT_FB, "core/java/A.java", 7, "void run()")
            + f"{_REPO_SEP}kernel-6.1\t{self.COMMIT_KERN}\n"
            + _grep_record(self.COMMIT_KERN, "drivers/gpio.c", 3, "gpio_set()")
        )

    def _scoped_grep_stdout(self) -> str:
        # repo_scope 检索只 grep 目标子仓：远端不会出现其他仓的记录，
        # 混入即触发实现的防御性 limited（覆盖不完整）。
        return (
            f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
            + _grep_record(self.COMMIT_FB, "core/java/A.java", 7, "void run()")
        )

    # ---- tests -------------------------------------------------------

    def test_metadata_lists_per_repo_heads(self):
        self.provider = self._provider(self._handler(
            heads_stdout=self._heads_stdout(True)))
        meta = self.provider.revision_metadata("")
        self.assertTrue(meta["multi_repo"])
        self.assertEqual(meta["repo_count"], 2)
        self.assertEqual(meta["repos_resolved"], 2)
        self.assertEqual(meta["commit"], "")
        self.assertTrue(meta["coverage_complete"])
        self.assertTrue(meta["reproducible"])
        self.assertEqual(meta["repos"]["frameworks/base"]["commit"], self.COMMIT_FB)
        self.assertEqual(meta["repos"]["frameworks/base"]["subject"], "Fix fb")
        self.assertEqual(meta["repos"]["kernel-6.1"]["subject"], "Fix kernel")

    def test_multi_metadata_is_not_reproducible_when_repo_heads_missing(self):
        partial = f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
        self.provider = self._provider(self._handler(heads_stdout=partial))
        meta = self.provider.revision_metadata("")
        self.assertEqual(meta["repo_count"], 2)
        self.assertEqual(meta["repos_resolved"], 1)
        self.assertFalse(meta["coverage_complete"])
        self.assertFalse(meta["reproducible"])
        self.assertEqual(meta["commit"], "")

    def test_multi_discovery_metadata_without_sha_cannot_claim_reproducibility(self):
        from features.system import source_ssh_git as mod

        self.provider = self._provider(self._handler(heads_stdout=""))
        with unittest.mock.patch.object(mod, "SSH_GIT_HEADS_MAX_REPOS", 1):
            meta = self.provider.revision_metadata("")
        self.assertEqual(meta["repo_count"], 2)
        self.assertEqual(meta["repos_resolved"], 0)
        self.assertFalse(meta["coverage_complete"])
        self.assertFalse(meta["reproducible"])
        self.assertEqual(meta["commit"], "")
        self.assertTrue(all(not value for value in meta["repos"].values()))

    def test_multi_search_rejects_non_head_revision(self):
        """多仓模式显式 revision（分支/tag/commit）必须 422 拒绝。

        回归：旧实现接受任意 revision 标签却仍在
        各子仓 HEAD 上检索，跨版本分析会把 HEAD 结果当作目标版本证据。
        """
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
        ))
        for revision in ("android-16-release", "a" * 40):
            with self.assertRaises(SourceProviderError) as ctx:
                self.provider.search(revision, "run")
            self.assertEqual(ctx.exception.status_code, 422)
        # 检索从未发生（无 grep 命令发出）。
        self.assertFalse([c for c in self.executed if " grep " in c])
        # 元数据端点同样拒绝。
        with self.assertRaises(SourceProviderError) as ctx:
            self.provider.revision_metadata("android-16-release")
        self.assertEqual(ctx.exception.status_code, 422)
        # HEAD 与源默认 revision 仍放行。
        self.assertTrue(self.provider.search("", "run")["multi_repo"])
        self.assertTrue(self.provider.search("HEAD", "run")["multi_repo"])

    def test_multi_metadata_marks_revision_is_head(self):
        self.provider = self._provider(self._handler(
            heads_stdout=self._heads_stdout(True)))
        meta = self.provider.revision_metadata("")
        self.assertTrue(meta["revision_is_head"])

    def test_search_resolves_hit_commits_and_reads(self):
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
            show_stdout="gpio text\n",
        ))
        result = self.provider.search("", "run")
        self.assertTrue(result["multi_repo"])
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["repos_total"], 2)
        self.assertEqual(result["repos_pending"], 0)
        first, second = result["matches"]
        self.assertEqual(first["path"], "frameworks/base/core/java/A.java")
        # commit 来自 grep 前解析并随结果头携带的 SHA（与检索绑定）。
        self.assertEqual(first["commit"], self.COMMIT_FB)
        self.assertEqual(first["repo"], "frameworks/base")
        self.assertEqual(second["path"], "kernel-6.1/drivers/gpio.c")
        # read 绑定 kernel 子仓 commit（git show -C <abs repo>）。
        read = self.provider.read_signed(second["result_id"], offset=0, limit=5)
        self.assertEqual(read["path"], "kernel-6.1/drivers/gpio.c")
        self.assertEqual(read["commit"], self.COMMIT_KERN)
        show_cmd = [c for c in self.executed if " show " in c][-1]
        self.assertIn("-C /remote/sdk/kernel-6.1", show_cmd)
        self.assertIn(f"{self.COMMIT_KERN}:drivers/gpio.c", show_cmd)

    def test_explicit_repo_scope_searches_only_selected_repo(self):
        """Scope is exact; legacy path_filter remains only a match filter."""
        self.provider = self._provider(self._handler(
            grep_stdout=self._scoped_grep_stdout(),
        ))
        result = self.provider.search("", "run", repo_scope="frameworks/base")
        grep = [command for command in self.executed if " grep " in command]
        self.assertEqual(len(grep), 1)
        self.assertIn(self.REPO_FB, grep[0])
        self.assertNotIn(self.REPO_KERN, grep[0])
        self.assertEqual([m["repo"] for m in result["matches"]], ["frameworks/base"])
        self.assertEqual(result["scope"], "repository")
        self.assertEqual(result["repos_total"], 1)
        self.assertFalse(result["global_coverage_complete"])
        self.assertTrue(result["coverage_complete"])

    def test_explicit_repo_scope_rejects_unknown_or_unsafe_repo(self):
        for scope in ("../../etc", "frameworks/unknown"):
            with self.subTest(scope=scope), self.assertRaises(SourceProviderError) as caught:
                self.provider.search("", "needle", repo_scope=scope)
            self.assertEqual(caught.exception.status_code, 422)

    def test_exact_search_uses_literal_blob_path_and_rejects_other_matches(self):
        provider = self._provider(self._handler(grep_stdout=self._scoped_grep_stdout()))
        target = "frameworks/base/core/java/A.java"
        result = provider.search("HEAD", "run", repo_scope="frameworks/base", path_filter=target, exact_path=True)
        self.assertEqual(result["searched_scope"], target)
        self.assertEqual(result["coverage_scope"], "selected_file")
        self.assertEqual([match["path"] for match in result["matches"]], [target])
        self.assertFalse(result["global_coverage_complete"])
        with self.assertRaises(SourceProviderError) as wrong_path:
            provider.search("HEAD", "run", repo_scope="frameworks/base", path_filter=target + ".bak", exact_path=True)
        self.assertEqual(wrong_path.exception.status_code, 502)
        with self.assertRaises(SourceProviderError) as wrong_repo:
            provider.search("HEAD", "run", repo_scope="kernel-6.1", path_filter=target, exact_path=True)
        self.assertEqual(wrong_repo.exception.status_code, 422)

    def test_exact_grep_in_a_real_git_tree_excludes_nearby_paths_and_directories(self):
        import subprocess

        with tempfile.TemporaryDirectory() as repo:
            root = Path(repo)
            for name in ("dir/A[1].java", "dir/A[1].java.bak", "dir/A1.java", "dir/only/Other.java"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("testSymbol\n", encoding="utf-8")
            git = ["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid"]
            for args in (["init", "-q"], ["add", "."], ["-c", "commit.gpgsign=false", "commit", "-qm", "fixture"]):
                subprocess.run([*git, *args], check=True, timeout=10)

            for target, expected in (("dir/A[1].java", True), ("dir/only", False), ("dir/missing.java", False)):
                with self.subTest(target=target):
                    command = self.provider._shard_grep_command("testSymbol", target, (repo,))
                    output = subprocess.run(
                        ["sh", "-c", command], capture_output=True, text=True, check=True, timeout=10,
                    ).stdout
                    records = [line for line in output.splitlines() if "\0" in line]
                    self.assertEqual(len(records), int(expected))
                    if expected:
                        self.assertIn(f":{target}\0", records[0])

    def test_scoped_commit_is_pinned_in_grep_command(self):
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
        ))
        self.provider.search(self.COMMIT_FB, "run", repo_scope="frameworks/base")
        grep = next(command for command in self.executed if " grep " in command)
        self.assertIn(self.COMMIT_FB + "^{commit}", grep)
        self.assertNotIn("'HEAD^{commit}'", grep)

    def test_reused_pinned_query_skips_duplicate_ssh(self):
        self.provider = self._provider(self._handler(grep_stdout=self._scoped_grep_stdout()))
        first = self.provider.search(
            self.COMMIT_FB, "run", repo_scope="frameworks/base", permission_scope="user1"
        )
        initial = len(self.executed)
        second = self.provider.search(
            self.COMMIT_FB, "run", repo_scope="frameworks/base", permission_scope="user1"
        )
        self.assertEqual(second, first)
        self.assertEqual(len(self.executed), initial)
        self.provider.search(
            self.COMMIT_FB, "run", repo_scope="frameworks/base", permission_scope="user2"
        )
        self.assertGreater(len(self.executed), initial)

    def test_filename_path_filter_searches_all_repos_not_empty(self):
        """文件名 path_filter 不命中子仓名：全量搜索 + 完整路径过滤。

        回归：旧实现对未命中子仓名且不含 ``/`` 的
        filter 直接返回空结果且 coverage_complete=true（没搜索却声明完整
        覆盖）。正确语义：按完整文件路径过滤，零命中才是诚实空结果。
        """
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
        ))
        result = self.provider.search("", "run", path_filter="A.java")
        self.assertEqual(
            [match["path"] for match in result["matches"]],
            ["frameworks/base/core/java/A.java"],
        )
        # 两个子仓都被检索（分片命令覆盖全部仓），覆盖元数据如实完整。
        self.assertEqual(result["repos_total"], 2)
        self.assertEqual(result["repos_pending"], 0)
        self.assertTrue(result["coverage_complete"])
        self.assertFalse(result["limited"])

    def test_search_greps_the_pre_resolved_commit(self):
        """分片命令先 rev-parse 固定 SHA，再在该 commit 上 grep。

        审核 P1：旧实现 HEAD 直查、聚合后回查 HEAD 生成证据 ID——同步
        期间 HEAD 移动会让搜索结果与 sdk_read 内容不一致。新协议在同一
        循环体先解析 ``$c`` 再 ``grep "$c"``，结果绑定的就是检索时的
        commit。
        """
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
        ))
        self.provider.search("", "run")
        grep_cmds = [c for c in self.executed if " grep " in c]
        self.assertTrue(grep_cmds)
        for cmd in grep_cmds:
            self.assertIn("rev-parse --verify 'HEAD^{commit}'", cmd)
            self.assertIn('"$c"', cmd)
            self.assertNotIn(" HEAD -- ", cmd)

    def test_head_placeholder_header_is_protocol_error(self):
        """仓库头带 HEAD 占位＝旧协议输出，直接拒绝（防回退）。"""

        stale = (
            f"{_REPO_SEP}frameworks/base\t{_HEAD_LABEL}\n"
            + _grep_record(_HEAD_LABEL, "A.java", 1, "x")
        )
        provider = self._provider(self._handler(grep_stdout=stale))
        with self.assertRaises(SourceProviderError) as ctx:
            provider.search("", "x")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_multi_read_rejects_path_outside_repos(self):
        result_id = self.provider._make_result_id(
            "vendor/other/x.c", self.COMMIT_FB)
        with self.assertRaises(SourceProviderError) as ctx:
            self.provider.read_signed(result_id)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_search_command_quotes_query(self):
        command = self.provider._shard_grep_command(
            "run(); rm -rf /", "", (self.REPO_FB,))
        # 查询词整体进入单引号边界（shlex 赋值），远端 shell 无法拆出执行。
        self.assertIn("'run(); rm -rf /'", command)
        tokens = shlex.split(command)
        self.assertTrue(any("run(); rm -rf /" in token for token in tokens))

    def test_shard_command_is_space_safe(self):
        """含空格的仓库路径必须原样到达 ``git -C``：read -r 不做单词拆分。"""
        import subprocess

        spaced = "/remote/sdk/my repo"
        command = self.provider._shard_grep_command("needle", "", (spaced,))
        self.assertIn("IFS= read -r r", command)
        # 执行实际生成的命令，并检查 git 收到的完整参数。
        probe = "git() { printf '[%s]\\n' \"$@\"; }; " + command
        out = subprocess.run(
            ["bash", "-c", probe], capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        self.assertIn("[/remote/sdk/my repo]", out.splitlines())

    def test_limit_counts_pending_repos_instead_of_shards(self):
        from features.system import source_ssh_git as mod

        repos = tuple(f"/remote/sdk/r{i}" for i in range(5))
        stdout = (f"{_REPO_SEP}/remote/sdk/r0\t{self.COMMIT_R0}\n"
                  + _grep_record(self.COMMIT_R0, "A.java", 1, "x"))
        provider = self._provider(self._handler(grep_stdout=stdout), repos=repos)
        with unittest.mock.patch.object(mod, "SSH_GIT_SHARD_REPOS", 2), \
             unittest.mock.patch.object(mod, "SSH_GIT_SHARD_WAVE_SIZE", 1):
            result = provider.search("", "x", limit=1)
        self.assertEqual(result["repos_total"], 5)
        self.assertEqual(result["repos_pending"], 3)
        self.assertTrue(result["limited"])
        self.assertFalse(result["coverage_complete"])

    def test_limit_waits_for_other_shards_and_marks_truncated_results(self):
        import threading

        from features.system import source_ssh_git as mod
        from foundation.command_result import CommandResult

        first_finished = threading.Event()

        def run_shard(shard, _query, _path, **_kwargs):
            if shard[0] == self.REPO_FB:
                first_finished.set()
            else:
                self.assertTrue(first_finished.wait(timeout=5))
            rel = self.provider._repo_rel(shard[0])
            return CommandResult(
                stdout=f"{_REPO_SEP}{shard[0]}\t{self.COMMIT_R0}\n"
                       + _grep_record(self.COMMIT_R0, "A.java", 1, rel),
                code=0,
            )

        with unittest.mock.patch.object(mod, "SSH_GIT_SHARD_REPOS", 1), \
             unittest.mock.patch.object(self.provider, "_run_shard", side_effect=run_shard):
            result = self.provider.search("", "x", limit=1)
        self.assertEqual(result["repos_pending"], 0)
        self.assertTrue(result["limited"])
        self.assertFalse(result["coverage_complete"])

    def test_incomplete_shard_marks_empty_result_incomplete(self):
        """rev-parse 失败的仓输出 INCOMPLETE 标记：空结果 + limited。"""
        from features.system.source_ssh_git import _TRUNCATION_MARKER

        provider = self._provider(self._handler(
            grep_stdout=_TRUNCATION_MARKER))
        result = provider.search("", "run")
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["limited"])
        self.assertFalse(result["coverage_complete"])

    def test_repo_failure_keeps_later_hits_and_marks_coverage_incomplete(self):
        import subprocess

        from foundation.command_result import CommandResult

        command = self.provider._shard_grep_command("needle", "", (self.REPO_FB, self.REPO_KERN))
        # 首仓 rev-parse 模拟 timeout 的 124（INCOMPLETE + continue），
        # 末仓正常检索；shell 总退出码仍然是 0。
        # git() 参数位序：$1=--no-optional-locks $2=-C $3=<repo> $4=<子命令>。
        script = (
            'git() { if [ "$3" = /remote/sdk/frameworks/base ]; then return 124; fi; '
            "if [ \"$4\" = rev-parse ]; then printf '%s\\n' "
            f"'{self.COMMIT_KERN}'; return 0; fi; "
            "printf '%s\\0%s\\0%s\\n' '" + self.COMMIT_KERN + ":gpio.c' 1 needle; }; "
            + command
        )
        completed = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=10, check=True,
        )
        self.assertIn("@@GMS_SDK_INCOMPLETE@@", completed.stdout)
        provider = self._provider(self._handler(
            grep_stdout=completed.stdout, heads_stdout=self._heads_stdout(),
        ))
        with unittest.mock.patch.object(provider, "_run_shard", return_value=CommandResult(
            stdout=completed.stdout, code=0,
        )):
            result = provider.search("", "needle")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["repo"], "kernel-6.1")
        self.assertEqual(result["matches"][0]["commit"], self.COMMIT_KERN)
        self.assertTrue(result["limited"])
        self.assertFalse(result["coverage_complete"])

    def test_discover_enumerates_and_caches_in_process(self):
        def handler(command: str, timeout: int):
            from foundation.command_result import CommandResult

            if command.lstrip().startswith("find "):
                # 模拟远端输出（发现模板已剪除 .repo，这里只返回干净清单）。
                return CommandResult(
                    stdout="/remote/sdk/frameworks/base\n"
                           "/remote/sdk/kernel-6.1\n", code=0)
            if _SUBJ_SEP in command or "rev-parse" in command:
                return CommandResult(stdout=self._heads_stdout(True), code=0)
            return CommandResult(stdout="", stderr="unexpected", code=3)

        provider = self._provider(handler, repos=(), discover=True)
        meta = provider.revision_metadata("")
        self.assertTrue(meta["discovered"])
        self.assertEqual(meta["repo_count"], 2)
        self.assertIn("frameworks/base", meta["repos"])
        # 二次调用走进程内缓存：不再触发 find。
        find_count = sum(1 for c in self.executed if c.lstrip().startswith("find "))
        provider.revision_metadata("")
        self.assertEqual(
            sum(1 for c in self.executed if c.lstrip().startswith("find ")), find_count)

    def test_discover_zero_repos_maps_502(self):
        from foundation.command_result import CommandResult

        def handler(command: str, timeout: int):
            if command.lstrip().startswith("find "):
                return CommandResult(stdout="", code=0)
            return CommandResult(stdout="", stderr="unexpected", code=3)

        provider = self._provider(handler, repos=(), discover=True)
        with self.assertRaises(SourceProviderError) as ctx:
            provider.revision_metadata("")
        self.assertEqual(ctx.exception.status_code, 502)

    def test_shard_failure_partial_results_limited(self):
        from foundation.command_result import CommandResult

        # 两个分片：kernel 分片 SSH 失败 → frameworks 部分结果 + limited。
        def handler(command: str, timeout: int):
            if " grep " in command:
                if "kernel-6.1" in command:
                    raise RuntimeError("Failed to get SSH connection")
                return CommandResult(
                    stdout=f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
                           + _grep_record(self.COMMIT_FB, "A.java", 1, "x"),
                    code=0)
            if "rev-parse" in command:
                return CommandResult(
                    stdout=f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n",
                    code=0)
            return CommandResult(stdout="", stderr="unexpected", code=3)

        provider = self._provider(handler)
        with unittest.mock.patch.object(
                SSHGitProvider, "_shards",
                lambda self, repos: [repos[:1], repos[1:]]):
            result = provider.search("", "run")
        self.assertTrue(result["limited"])
        self.assertEqual(result["total"], 1)

    def test_sharding_splits_large_repo_lists(self):
        repos = tuple(f"/remote/sdk/r{i:03d}" for i in range(100))
        shards = self.provider._shards(repos)
        # ceil(100/32)=4 分片、8 片上限、总量守恒。
        self.assertEqual(len(shards), 4)
        self.assertEqual(sum(len(s) for s in shards), 100)
        # 小清单不分片
        small = tuple(f"/remote/sdk/r{i}" for i in range(10))
        self.assertEqual(len(self.provider._shards(small)), 1)

    def test_deadline_preserves_finished_results_without_waiting_for_shards(self):
        from concurrent.futures import Future

        from features.system import source_ssh_git as mod
        from foundation.command_result import CommandResult

        repos = tuple(f"/remote/sdk/r{i:02d}" for i in range(3))
        finished, pending = Future(), Future()
        finished.set_result(CommandResult(
            stdout=(f"{_REPO_SEP}{repos[0]}\t{self.COMMIT_R0}\n"
                    + _grep_record(self.COMMIT_R0, "A.java", 1, "x")),
            code=0,
        ))
        provider = self._provider(self._handler(), repos=repos)
        with (
            unittest.mock.patch.object(mod, "SSH_GIT_SHARD_REPOS", 1),
            unittest.mock.patch.object(mod, "SSH_GIT_SHARD_WAVE_SIZE", 2),
            unittest.mock.patch.object(mod, "ThreadPoolExecutor") as pool,
            unittest.mock.patch("concurrent.futures.wait", side_effect=[
                ({finished}, {pending}), (set(), {pending}),
            ]) as wait,
            unittest.mock.patch("features.system.source_repo_rank.codesearch_hints_for_query", return_value=[]),
        ):
            pool.return_value.submit.side_effect = [finished, pending]
            result = provider.search("", "x")
        self.assertEqual(pool.return_value.submit.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        self.assertGreater(wait.call_args.kwargs["timeout"], 0)
        pool.return_value.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.assertEqual([match["path"] for match in result["matches"]], ["r00/A.java"])
        self.assertEqual(result["repos_pending"], 2)
        self.assertTrue(result["limited"])
        self.assertFalse(result["coverage_complete"])

    def test_expired_discovery_budget_does_not_submit_first_shards(self):
        from features.system import source_ssh_git as mod

        token = mod._SEARCH_DEADLINE.set(0.01)
        try:
            with (
                unittest.mock.patch.object(mod, "ThreadPoolExecutor") as pool,
                unittest.mock.patch("features.system.source_repo_rank.codesearch_hints_for_query", return_value=[]),
            ):
                result = self.provider._search_multi("HEAD", "x", "", 50)
        finally:
            mod._SEARCH_DEADLINE.reset(token)
        pool.return_value.submit.assert_not_called()
        self.assertEqual(result["repos_pending"], result["repos_total"])
        self.assertFalse(result["coverage_complete"])


class SSHGitConfigTests(unittest.TestCase):
    def _item(self, **overrides):
        item = {
            "source_id": "android-206",
            "provider": "ssh_git",
            "repo_root": "/remote/sdk",
            "ssh_host": "10.10.10.206",
            "ssh_username": "hcq",
            "ssh_use_key_auth": True,
        }
        item.update(overrides)
        return item

    def test_key_auth_without_password(self):
        configs = load_provider_configs({"sdk_sources": {"providers": [self._item()]}})
        self.assertEqual(len(configs), 1)
        self.assertTrue(configs[0].ssh_use_key_auth)
        self.assertEqual(configs[0].ssh_host, "10.10.10.206")
        self.assertEqual(configs[0].ssh_port, 22)

    def test_password_env(self):
        with unittest.mock.patch.dict("os.environ", {"TEST_SDK_SSH_PWD": "p"}):
            configs = load_provider_configs({"sdk_sources": {"providers": [
                self._item(ssh_use_key_auth=False, ssh_password_env="TEST_SDK_SSH_PWD"),
            ]}})
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].ssh_password, "p")

    def test_password_mode_without_password_ignored(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            configs = load_provider_configs({"sdk_sources": {"providers": [
                self._item(ssh_use_key_auth=False),
            ]}})
        self.assertEqual(configs, [])

    def test_invalid_password_env_ignored(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            configs = load_provider_configs({"sdk_sources": {"providers": [
                self._item(ssh_use_key_auth=False, ssh_password_env="9BAD-NAME"),
            ]}})
        self.assertEqual(configs, [])

    def test_missing_host_or_repo_root_ignored(self):
        configs = load_provider_configs({"sdk_sources": {"providers": [
            self._item(ssh_host=""),
            self._item(repo_root=""),
        ]}})
        self.assertEqual(configs, [])

    def test_discover_repos_flag_and_ttl(self):
        configs = load_provider_configs({"sdk_sources": {"providers": [
            self._item(repos=[], discover_repos=True, discover_ttl_hours=6),
        ]}})
        self.assertEqual(len(configs), 1)
        self.assertTrue(configs[0].discover_repos)
        self.assertEqual(configs[0].discover_ttl_hours, 6)
        self.assertEqual(configs[0].repos, ())

    def test_discover_defaults_ttl(self):
        configs = load_provider_configs({"sdk_sources": {"providers": [
            self._item(discover_repos=True),
        ]}})
        self.assertEqual(configs[0].discover_ttl_hours, 24)


if __name__ == "__main__":
    unittest.main()
