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
import unittest
import unittest.mock

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
    """多仓模式：HEAD 直查分片、命中仓后置解析、按仓读取、自动发现。"""

    SDK_ROOT = "/remote/sdk"
    REPO_FB = "/remote/sdk/frameworks/base"
    REPO_KERN = "/remote/sdk/kernel-6.1"
    COMMIT_FB = "b" * 40
    COMMIT_KERN = "c" * 40

    def setUp(self):
        self.executed: list[str] = []
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
        provider._new_shard_manager = lambda: self._FakeShardManager(
            handler, self.executed)
        provider._ssh = provider._new_shard_manager()
        return provider

    def _heads_stdout(self, with_subject: bool = False) -> str:
        out = (f"{_REPO_SEP}frameworks/base\t{self.COMMIT_FB}\n"
               f"{_REPO_SEP}kernel-6.1\t{self.COMMIT_KERN}\n")
        if with_subject:
            out += f"{_SUBJ_SEP}Fix fb\n{_SUBJ_SEP}Fix kernel\n"
        return out

    def _grep_stdout(self) -> str:
        return (
            f"{_REPO_SEP}frameworks/base\t{_HEAD_LABEL}\n"
            + _grep_record(_HEAD_LABEL, "core/java/A.java", 7, "void run()")
            + f"{_REPO_SEP}kernel-6.1\t{_HEAD_LABEL}\n"
            + _grep_record(_HEAD_LABEL, "drivers/gpio.c", 3, "gpio_set()")
        )

    # ---- tests -------------------------------------------------------

    def test_metadata_lists_per_repo_heads(self):
        self.provider = self._provider(self._handler(
            heads_stdout=self._heads_stdout(True)))
        meta = self.provider.revision_metadata("")
        self.assertTrue(meta["multi_repo"])
        self.assertEqual(meta["repo_count"], 2)
        self.assertEqual(meta["commit"], self.COMMIT_FB)
        self.assertEqual(meta["repos"]["kernel-6.1"]["subject"], "Fix kernel")

    def test_search_resolves_hit_commits_and_reads(self):
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
            show_stdout="gpio text\n",
        ))
        result = self.provider.search("", "run")
        self.assertTrue(result["multi_repo"])
        self.assertEqual(result["total"], 2)
        first, second = result["matches"]
        self.assertEqual(first["path"], "frameworks/base/core/java/A.java")
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

    def test_search_grep_runs_at_head_without_per_repo_revparse(self):
        self.provider = self._provider(self._handler(
            grep_stdout=self._grep_stdout(),
            heads_stdout=self._heads_stdout(),
        ))
        self.provider.search("", "run")
        grep_cmds = [c for c in self.executed if " grep " in c]
        self.assertTrue(grep_cmds)
        for cmd in grep_cmds:
            # HEAD 直查：分片命令不包含逐仓 rev-parse。
            self.assertNotIn("rev-parse", cmd)
            self.assertIn(" HEAD -- ", cmd)

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

    def test_discover_enumerates_and_caches_in_process(self):
        def handler(command: str, timeout: int):
            from foundation.command_result import CommandResult

            if " find " in command:
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
        find_count = sum(1 for c in self.executed if " find " in c)
        provider.revision_metadata("")
        self.assertEqual(
            sum(1 for c in self.executed if " find " in c), find_count)

    def test_discover_zero_repos_maps_502(self):
        from foundation.command_result import CommandResult

        def handler(command: str, timeout: int):
            if " find " in command:
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
                    stdout=f"{_REPO_SEP}frameworks/base\t{_HEAD_LABEL}\n"
                           + _grep_record(_HEAD_LABEL, "A.java", 1, "x"),
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
