"""Command-level installer lifecycle tests.

Covers the post-refactor installer surface with real function calls and
failure injection:

* `install_cli_dispatcher` must link `gms-agent` to the real CLI
        entry point (scripts/gms-agent), never to the gms-rt dispatcher,
        while gms-rt-* links still go through the dispatcher (argv0 → $1).
* `write_enrollment_token` must resolve TOML-only profiles — no
        FileNotFoundError when the legacy <client>.env store is absent.
* mcp_launcher resolves the client from a pinned GMS_RT_PROFILE's
        `client =` field instead of the kimi→codex→kkagent probe.
* `fetch_registry_package(expected_version=…)` rejects a manifest
        that changed between the version decision and the download.
* `register_kkagent_plugin` fails closed on a corrupt registry and
        never overwrites other plugins' registrations; writes are atomic.
* `sync_one` fixes a lost executable bit even when content matches.
* `gms_agent.client._load_token` rejects a token file owned by
        another user (owner check, CLI parity).
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "agent" / "gms-remote-test" / "runtime"))

import gms_agent.package_manager as pm  # noqa: E402
import mcp_launcher  # noqa: E402
from gms_agent import client as gms_client  # noqa: E402


SYNC_SCRIPT = REPO_ROOT / "tools" / "scripts" / "agent" / "sync_package.py"


class EnvSandbox(unittest.TestCase):
    """Base fixture: redirect every package_manager state dir to a tmp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._saved: dict[str, object] = {}
        patches = {
            "RUNTIME_ROOT": self.root / "runtime",
            "VERSIONS_DIR": self.root / "runtime" / "versions",
            "CURRENT_LINK": self.root / "runtime" / "current",
        }
        for name, path in patches.items():
            self._saved[name] = getattr(pm, name)
            setattr(pm, name, path)

        def _restore(name: str, value: object) -> None:
            setattr(pm, name, value)

        for name, value in self._saved.items():
            self.addCleanup(_restore, name, value)
        # profile/token 存储唯一实现在 gms_agent.profile_store（ADR 0003）——
        # patch 一处即同时作用于 package_manager 与 mcp_launcher（二者都经
        # profile_store 读写）。legacy .env / MCP_ENV_DIR 已删除。
        self._store_profile_root = pm.profile_store.PROFILE_ROOT
        self._store_state_dir = pm.profile_store.STATE_DIR
        pm.profile_store.PROFILE_ROOT = self.root / "profiles"
        pm.profile_store.STATE_DIR = self.root / "state"
        self.addCleanup(
            setattr, pm.profile_store, "PROFILE_ROOT", self._store_profile_root
        )
        self.addCleanup(
            setattr, pm.profile_store, "STATE_DIR", self._store_state_dir
        )
        # kkagent_plugin_registry() reads KKAGENT_HOME at call time.
        env_patch = mock.patch.dict(
            os.environ,
            {
                "KKAGENT_HOME": str(self.root / "kkagent-home"),
                "GMS_BIN_DIR": str(self.root / "bin"),
                "GMS_CODEX_SKILLS_DIR": str(self.root / "codex-skills"),
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def make_installed_runtime(self) -> Path:
        """Create a minimal versions/<v>/ tree with current flipped to it."""
        scripts = self.root / "runtime" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / "gms-agent").write_text(
            "#!/usr/bin/env python3\nprint('gms-agent')\n", encoding="utf-8"
        )
        (scripts / "gms-agent").chmod(0o755)
        cli = scripts / "gms-remote-test.sh"
        cli.write_text(
            "GMS_RT_VERSION=9.9.9\n"
            "gms-rt-system-health() { echo ok; }\n",
            encoding="utf-8",
        )
        version_dir = pm.VERSIONS_DIR / "9.9.9"
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil_copytree(scripts, version_dir / "scripts")
        if pm.CURRENT_LINK.is_symlink() or pm.CURRENT_LINK.exists():
            pm.CURRENT_LINK.unlink()
        pm.CURRENT_LINK.symlink_to(version_dir)
        return version_dir


def shutil_copytree(src: Path, dst: Path) -> None:
    import shutil

    shutil.copytree(src, dst)


class TestCliDispatcherLinks(EnvSandbox):
    """gms-agent → scripts/gms-agent; gms-rt-* → dispatcher."""

    def test_gms_agent_link_points_to_cli_entry_point(self):
        self.make_installed_runtime()
        bin_dir = self.root / "bin"
        with mock.patch.dict(os.environ, {"GMS_BIN_DIR": str(bin_dir)}):
            created = pm.install_cli_dispatcher()
        by_name = {p.name: p for p in created}
        self.assertIn("gms-agent", by_name)
        self.assertEqual(
            Path(os.readlink(by_name["gms-agent"])),
            pm.CURRENT_LINK / "scripts" / "gms-agent",
        )
        # gms-rt-* must resolve to the dispatcher (argv0 → $1 translation)
        self.assertIn("gms-rt-system-health", by_name)
        self.assertEqual(
            Path(os.readlink(by_name["gms-rt-system-health"])),
            bin_dir / "gms-rt",
        )

    def test_gms_agent_help_runs_after_install(self):
        """Command-level smoke: the linked entry point is the real argparse CLI
        (uses the actual source-tree entry script, not a stub)."""
        real_cli = REPO_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-agent"
        result = subprocess.run(
            [str(real_cli), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("enroll", result.stdout)

    def test_reconcile_adds_new_commands_and_removes_only_managed_stale_links(self):
        runtime = self.make_installed_runtime()
        bin_dir = self.root / "bin"
        with mock.patch.dict(os.environ, {"GMS_BIN_DIR": str(bin_dir)}):
            pm.install_cli_dispatcher()
            dispatcher = bin_dir / "gms-rt"
            stale = bin_dir / "gms-rt-removed-command"
            stale.symlink_to(dispatcher)
            user_managed = bin_dir / "gms-rt-user-command"
            user_managed.write_text("#!/bin/sh\n", encoding="utf-8")

            cli = runtime / "scripts" / "gms-remote-test.sh"
            cli.write_text(
                cli.read_text(encoding="utf-8")
                + "gms-rt-devices-console() { echo console; }\n",
                encoding="utf-8",
            )
            created = pm.install_cli_dispatcher()

        by_name = {path.name: path for path in created}
        self.assertIn("gms-rt-devices-console", by_name)
        self.assertFalse(stale.exists())
        self.assertTrue(user_managed.is_file())


class TestSandboxHomeGuard(EnvSandbox):
    """安装期沙箱 HOME 拦截 + dispatcher 运行时路径解析。

    线上事故：AI 客户端沙箱把 HOME 重定向到 /tmp/tmpXXXX，装出来的
    gms-rt/gms-agent 链接与 dispatcher 全部写死临时绝对路径，/tmp 清理后
    全部死链（"gms-rt 补全只剩 4 个"）。这里锁定三层防线。
    """

    def test_sandbox_home_reason_detects_tmp_home(self):
        with mock.patch.object(pm.Path, "home", return_value=Path("/tmp/tmpABC123")):
            reason = pm.sandbox_home_reason()
        self.assertIn("/tmp/tmpABC123", reason)
        self.assertIn("ephemeral", reason)
        # 真实 HOME（/home/xxx）不命中
        with mock.patch.object(pm.Path, "home", return_value=Path("/home/hcq")):
            self.assertEqual(pm.sandbox_home_reason(), "")

    def test_install_refuses_sandbox_home_unless_overridden(self):
        self.make_installed_runtime()
        with mock.patch.object(pm.Path, "home", return_value=Path("/tmp/tmpDEAD")):
            with self.assertRaises(SystemExit):
                pm.install_cli_dispatcher()
            # 显式 override 后放行
            with mock.patch.dict(
                os.environ, {"GMS_INSTALL_ALLOW_SANDBOX_HOME": "1"}
            ):
                created = pm.install_cli_dispatcher()
        self.assertTrue(created)

    def test_dispatcher_resolves_runtime_at_invocation_time(self):
        """dispatcher 脚本不得内嵌安装期绝对路径（否则换 HOME 即死）。"""
        self.make_installed_runtime()
        with mock.patch.dict(os.environ, {"GMS_BIN_DIR": str(self.root / "bin")}):
            pm.install_cli_dispatcher()
        dispatcher = self.root / "bin" / "gms-rt"
        text = dispatcher.read_text(encoding="utf-8")
        self.assertNotIn(str(pm.CURRENT_LINK), text)
        self.assertNotIn(str(Path.home()), text)
        self.assertIn('runtime_root="${GMS_AGENT_RUNTIME_ROOT:-', text)
        self.assertIn("current/scripts/gms-remote-test.sh", text)

    def test_broken_stale_links_are_removed(self):
        """指向已消失 runtime 的悬空链接一律摘除，不留假命令补全。"""
        self.make_installed_runtime()
        bin_dir = self.root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        gone_target = self.root / "gone-runtime" / "gms-rt"
        dangling = bin_dir / "gms-rt-dead-command"
        dangling.symlink_to(gone_target)
        with mock.patch.dict(os.environ, {"GMS_BIN_DIR": str(bin_dir)}):
            pm.install_cli_dispatcher()
        self.assertFalse(dangling.exists())

    def test_refuses_to_link_when_runtime_itself_is_ephemeral(self):
        """插件更新器在沙箱里装 runtime 但把 bin 指向真实目录的场景：
        runtime 落在 /tmp 而 bin 持久时拒绝写用户 bin 链接。

        bin 路径用测试沙箱内的「模拟持久目录」（sandbox-root/persistent-bin），
        由 _is_ephemeral_path 的 mock 声明其持久性——绝不用真实
        ~/.local/bin，防线逻辑一旦失效也不会污染用户环境。
        """
        self.make_installed_runtime()
        fake_runtime = Path("/tmp/tmpHIJACK/.local/share/gms-remote-test")
        persistent_bin = self.root / "persistent-bin"
        with mock.patch.object(pm, "CURRENT_LINK", fake_runtime / "current"), \
             mock.patch.object(pm, "_is_ephemeral_path",
                               lambda p: Path(p) != persistent_bin), \
             mock.patch.dict(os.environ, {"GMS_BIN_DIR": str(persistent_bin)}), self.assertRaises(SystemExit) as ctx:
            pm.install_cli_dispatcher()
        self.assertIn("ephemeral", str(ctx.exception))
        # 防线必须在写任何链接之前触发
        self.assertFalse(persistent_bin.exists())


class TestMultiControllerFailClosed(EnvSandbox):
    """多 Controller 主机的写路径与 Controller 解析必须 fail closed。

    锁定 ADR 0003 的完整闭环：默认 profile 名包含 Controller 身份
    （两个 Controller 各占一个文件，绝不互相覆盖）；resolve_controller
    只在「全部 profile 恰好指向唯一 Controller」时自动选择；enroll 在
    歧义主机上拒绝落盘，one-shot 配对码不允许被存到另一个 Controller 的
    profile 里。
    """

    def test_default_profile_name_includes_controller_identity(self):
        a = pm.profile_store.default_profile_name("codex", "https://ctrl-a:5001")
        b = pm.profile_store.default_profile_name("codex", "https://ctrl-b:5001")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("codex-"))
        # Same controller → same name (idempotent re-install overwrites
        # itself, which is fine; a DIFFERENT controller never does).
        self.assertEqual(
            a, pm.profile_store.default_profile_name("codex", "https://ctrl-a:5001/")
        )

    def test_install_default_write_profile_never_overwrites_other_controller(self):
        name_a = pm.write_profile("codex", "https://ctrl-a:5001", "")
        name_b = pm.write_profile("codex", "https://ctrl-b:5001", "")
        self.assertNotEqual(name_a, name_b)
        self.assertEqual(
            pm.profile_store.controller_url(pm.load_profile(name_a)),
            "https://ctrl-a:5001",
        )
        self.assertEqual(
            pm.profile_store.controller_url(pm.load_profile(name_b)),
            "https://ctrl-b:5001",
        )

    def test_resolve_controller_explicit_server_wins(self):
        pm.write_profile_toml(
            "codex-a", "codex", "https://ctrl-a:5001", ""
        )
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}):
            server, _ca = pm.resolve_controller("https://explicit:5001", "")
        self.assertEqual(server, "https://explicit:5001")

    def test_resolve_controller_explicit_profile(self):
        pm.write_profile_toml("codex-a", "codex", "https://ctrl-a:5001", "")
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "")
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}):
            server, _ca = pm.resolve_controller("", "codex-b")
        self.assertEqual(server, "https://ctrl-b:5001")

    def test_explicit_profile_overrides_unrelated_environment_controller(self):
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "/ca-b.pem")
        with mock.patch.dict(
            os.environ, {"GMS_REMOTE_TEST_SERVER": "https://ctrl-a:5001"}
        ):
            server, ca = pm.resolve_controller("", "codex-b")
        self.assertEqual((server, ca), ("https://ctrl-b:5001", "/ca-b.pem"))

    def test_explicit_server_cannot_conflict_with_profile(self):
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "")
        with self.assertRaises(SystemExit) as ctx:
            pm.resolve_controller("https://ctrl-a:5001", "codex-b")
        self.assertEqual(ctx.exception.code, 2)

    def test_resolve_controller_unique_profile_set_is_automatic(self):
        pm.write_profile_toml("codex-x", "codex", "https://only:5001", "")
        pm.write_profile_toml("kimi-y", "kimi", "https://only:5001", "")
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}):
            server, _ca = pm.resolve_controller("", "")
        self.assertEqual(server, "https://only:5001")

    def test_resolve_controller_without_profiles_fails_closed(self):
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}), self.assertRaises(SystemExit) as ctx:
            pm.resolve_controller("", "")
        self.assertEqual(ctx.exception.code, 2)

    def test_resolve_controller_ambiguous_host_fails_closed(self):
        pm.write_profile_toml("codex-a", "codex", "https://ctrl-a:5001", "")
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "")
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}), self.assertRaises(SystemExit):
            pm.resolve_controller("", "")
        # And crucially: ambiguity does NOT fall through to another client's
        # sole controller.
        pm.write_profile_toml("kimi-c", "kimi", "https://ctrl-c:5001", "")
        with mock.patch.dict(os.environ, {"GMS_REMOTE_TEST_SERVER": ""}), self.assertRaises(SystemExit):
            pm.resolve_controller("", "")

    def test_write_enrollment_token_ambiguous_host_writes_nothing(self):
        pm.write_profile_toml("codex-a", "codex", "https://ctrl-a:5001", "")
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "")
        self.assertEqual(pm.write_enrollment_token("tok-1"), [])
        self.assertEqual(list(pm.profile_store.STATE_DIR.glob("*.token")), [])

    def test_write_enrollment_token_explicit_profile(self):
        pm.write_profile_toml("codex-a", "codex", "https://ctrl-a:5001", "")
        pm.write_profile_toml("codex-b", "codex", "https://ctrl-b:5001", "")
        written = pm.write_enrollment_token("tok-1", profile="codex-a")
        self.assertEqual(written, [str(pm.profile_store.token_file("codex-a"))])
        self.assertEqual(
            pm.profile_store.token_file("codex-b").exists(), False
        )

    def test_write_enrollment_token_honors_custom_profile_token_file(self):
        pm.write_profile_toml("production", "codex", "https://ctrl-a:5001", "")
        custom = self.root / "custom" / "service.token"
        path = pm.profile_store.profile_path("production")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                str(pm.profile_store.token_file("production")), str(custom)
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            pm.write_enrollment_token("tok-custom", profile="production"),
            [str(custom)],
        )
        self.assertEqual(custom.read_text(encoding="utf-8"), "tok-custom\n")

    def test_custom_profile_name_is_discovered_by_declared_client(self):
        pm.write_profile_toml("production", "codex", "https://ctrl-a:5001", "")
        self.assertEqual(
            [item.stem for item in pm.profile_store.profile_candidates("codex")],
            ["production"],
        )

    def test_write_enrollment_token_unique_controller_covers_all_profiles(self):
        pm.write_profile_toml("hand-named", "codex", "https://only:5001", "")
        pm.write_profile_toml("kimi-default", "kimi", "https://only:5001", "")
        written = pm.write_enrollment_token("tok-9")
        self.assertEqual(len(written), 2)


class TestEnrollTomlOnly(EnvSandbox):
    """Enrollment token must persist for TOML-only profiles."""

    def test_write_enrollment_token_resolves_toml_profile(self):
        profile = pm.profile_name("codex")
        pm.write_profile_toml(profile, "codex", "https://ctrl.example:5001", "")
        # TOML-only contract: no legacy <client>.env may appear
        # anywhere in the sandbox.
        self.assertEqual([], list(self.root.rglob("*.env")))
        written = pm.write_enrollment_token("tok-123")
        self.assertEqual(
            written, [str(pm.profile_store.token_file(profile))]
        )
        token_file = pm.profile_store.token_file(profile)
        self.assertEqual(token_file.read_text(encoding="utf-8").strip(), "tok-123")
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_write_enrollment_token_without_any_profile_is_empty(self):
        self.assertEqual(pm.write_enrollment_token("tok-123"), [])

    def test_shell_enroll_rejects_environment_profile_controller_mismatch(self):
        config_root = self.root / "config"
        profile_root = config_root / "gms-agent" / "profiles"
        profile_root.mkdir(parents=True)
        (profile_root / "production.toml").write_text(
            'profile = "production"\nclient = "codex"\n\n'
            '[controller]\nurl = "https://ctrl-b:5001"\n\n'
            '[auth]\ntoken_file = "/tmp/production.token"\n',
            encoding="utf-8",
        )
        script = REPO_ROOT / "agent/gms-remote-test/runtime/gms-remote-test.sh"
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; gms-rt-agent-enroll CODE --profile production',
                "bash",
                str(script),
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "XDG_CONFIG_HOME": str(config_root),
                "GMS_REMOTE_TEST_SERVER": "https://ctrl-a:5001",
            },
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("different Controllers", result.stderr)

    def test_shell_enrollment_code_json_success_returns_zero(self):
        script = REPO_ROOT / "agent/gms-remote-test/runtime/gms-remote-test.sh"
        command = """
source "$1"
curl() {
  printf '%s\\n' '{"success":true,"enrollment":{"code":"once","expires_at":"","ttl_minutes":5}}' 'HTTP_STATUS:200'
}
gms-rt-agent-enroll-code --name fixture
"""
        result = subprocess.run(
            ["bash", "-c", command, "bash", str(script)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "GMS_RT_OUTPUT": "json",
                # The helper fail-fasts when no Controller URL is configured
                # and localhost:5001 is not listening (CI runners). Point the
                # preflight at a fixture URL — curl is stubbed above, so no
                # real network call happens; the stubbed enrollment is what
                # the test exercises.
                "GMS_REMOTE_TEST_SERVER": "https://fixture.invalid:5001",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TestLauncherProfilePinning(EnvSandbox):
    """A pinned GMS_RT_PROFILE selects its own client, not the probe."""

    def test_profile_client_field_wins_over_probe_order(self):
        # Kimi configured first (probe would pick kimi); codex profile pinned.
        kimi_profile = pm.profile_name("kimi")
        pm.write_profile_toml(kimi_profile, "kimi", "https://kimi-ctrl:5001", "")
        codex_profile = pm.profile_name("codex")
        pm.write_profile_toml(codex_profile, "codex", "https://codex-ctrl:5001", "")
        self.assertEqual(mcp_launcher._client_from_profile(codex_profile), "codex")

    def test_explicit_profile_wins_with_multiple_profiles_for_client(self):
        pm.write_profile_toml(
            "codex-controller-a", "codex", "https://controller-a:5001", ""
        )
        pm.write_profile_toml(
            "codex-controller-b", "codex", "https://controller-b:5001", ""
        )
        with mock.patch.dict(
            os.environ,
            {
                "GMS_RT_PROFILE": "codex-controller-b",
                "GMS_AGENT_CLIENT": "codex",
                "GMS_REMOTE_TEST_SERVER": "",
            },
            clear=False,
        ):
            self.assertTrue(
                mcp_launcher.load_named_profile("codex-controller-b", "codex")
            )
            self.assertEqual(
                os.environ["GMS_REMOTE_TEST_SERVER"], "https://controller-b:5001"
            )

    def test_implicit_profile_selection_fails_when_ambiguous(self):
        pm.write_profile_toml(
            "codex-controller-a", "codex", "https://controller-a:5001", ""
        )
        pm.write_profile_toml(
            "codex-controller-b", "codex", "https://controller-b:5001", ""
        )
        self.assertFalse(mcp_launcher.load_profile("codex"))

    def test_profile_name_cannot_escape_profile_root(self):
        self.assertEqual(mcp_launcher._client_from_profile("../codex-secret"), "")


class TestDoctorAndProfiles(EnvSandbox):
    def test_doctor_reports_token_metadata_without_token_contents(self):
        profile = pm.profile_name("codex")
        pm.write_profile_toml(profile, "codex", "https://ctrl.example:5001", "")
        token = pm.profile_store.token_file(profile)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("top-secret-token\n", encoding="utf-8")
        token.chmod(0o600)

        report = pm.doctor_report("codex", profile)

        encoded = json.dumps(report)
        self.assertNotIn("top-secret-token", encoded)
        self.assertTrue(report["clients"][0]["token"]["present"])
        self.assertTrue(report["clients"][0]["token"]["mode_ok"])
        self.assertEqual(report["clients"][0]["profile"]["selected"], profile)

    def test_profile_list_marks_invalid_controller_url(self):
        profile = pm.profile_name("codex")
        pm.write_profile_toml(profile, "codex", "https://$(unsafe)/host", "")
        args = type(
            "Args",
            (),
            {
                "profile_action": "list",
                "client": "codex",
                "name": "",
                "json": True,
            },
        )()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(pm.cmd_profile(args), 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["profiles"][0]["controller_url_valid"])

    def test_doctor_exact_profile_limits_auto_client_scope(self):
        codex_profile = pm.profile_name("codex")
        pm.write_profile_toml(
            codex_profile, "codex", "https://codex-ctrl.example:5001", ""
        )
        pm.write_profile_toml(
            pm.profile_name("kimi"), "kimi", "https://kimi-ctrl.example:5001", ""
        )
        report = pm.doctor_report("auto", codex_profile)
        self.assertEqual(
            [item["client"] for item in report["clients"]], ["codex"]
        )

    def test_doctor_recognizes_enabled_codex_native_plugin(self):
        config = pm.client_skill_root("codex").parent / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            '[plugins."gms-remote-test@personal"]\nenabled = true\n',
            encoding="utf-8",
        )
        self.assertTrue(pm._mcp_registration_status("codex")["registered"])

    def test_doctor_action_explains_invalid_controller_url(self):
        profile = pm.profile_name("codex")
        pm.write_profile_toml(profile, "codex", "https://$(unsafe)/host", "")
        report = pm.doctor_report("codex", profile)
        self.assertTrue(
            any("invalid Controller URL" in action for action in report["actions"])
        )


class TestLocalRuntimeOnlyInstall(EnvSandbox):
    @staticmethod
    def _args(client: str, server: str = "", profile: str = ""):
        return type(
            "Args",
            (),
            {
                "client": client,
                "server": server,
                "package": str(REPO_ROOT / "agent" / "gms-remote-test"),
                "enroll_code": "",
                "profile": profile,
            },
        )()

    def test_client_none_installs_console_link_without_profile(self):
        self.assertEqual(pm._cmd_install_locked(self._args("none")), 0)
        self.assertTrue((self.root / "bin" / "gms-rt-devices-console").is_symlink())
        self.assertFalse(pm.profile_store.PROFILE_ROOT.exists())

    def test_auto_with_paircode_and_no_detected_client_fails_before_install(self):
        args = self._args("auto", "https://ctrl.example:5001")
        with mock.patch.object(pm, "detect_clients", return_value=[]), mock.patch.dict(
            os.environ, {"GMS_AGENT_ENROLL_CODE": "ONE-SHOT"}
        ):
            self.assertEqual(pm._cmd_install_locked(args), 2)
        self.assertFalse(pm.CURRENT_LINK.exists())
        self.assertFalse(pm.profile_store.PROFILE_ROOT.exists())

    def test_enrolled_install_prints_each_explicit_cli_profile(self):
        args = self._args("codex", "https://ctrl.example:5001")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"GMS_AGENT_ENROLL_CODE": "ONE-SHOT"}), \
                mock.patch.object(pm, "install_runtime", return_value=self.root / "current"), \
                mock.patch.object(pm, "install_cli_dispatcher", return_value=[self.root / "gms-rt"]), \
                mock.patch.object(pm, "activate_clients", return_value=["codex-prod"]), \
                mock.patch.object(pm, "cmd_enroll", return_value=0), \
                mock.patch("sys.stdout", output):
            self.assertEqual(pm._cmd_install_locked(args), 0)
        self.assertIn(
            "GMS_RT_PROFILE=codex-prod gms-rt-system-selfcheck --json",
            output.getvalue(),
        )

    def test_client_install_rejects_invalid_controller_before_changes(self):
        self.assertEqual(
            pm._cmd_install_locked(self._args("codex", "https://$(unsafe)/host")),
            2,
        )
        self.assertFalse(pm.CURRENT_LINK.exists())

    def test_auto_install_rejects_one_profile_for_multiple_clients_before_changes(self):
        with mock.patch.object(pm, "detect_clients", return_value=["codex", "kimi"]):
            result = pm._cmd_install_locked(
                self._args("auto", "https://ctrl.example:5001", "production")
            )
        self.assertEqual(result, 2)
        self.assertFalse(pm.CURRENT_LINK.exists())

    def test_install_rejects_retargeting_profile_with_existing_token(self):
        pm.write_profile_toml(
            "production", "codex", "https://ctrl-a.example:5001", ""
        )
        self.assertEqual(
            pm._cmd_install_locked(
                self._args("codex", "https://ctrl-b.example:5001", "production")
            ),
            2,
        )
        self.assertFalse(pm.CURRENT_LINK.exists())


class TestProfilePreservingReactivation(EnvSandbox):
    def test_reactivation_keeps_custom_profile_identity(self):
        self.make_installed_runtime()
        pm.write_profile_toml(
            "production", "codex", "https://ctrl.example:5001", "/ca.pem"
        )
        with mock.patch.object(pm, "install_skill"), mock.patch.object(
            pm, "reconcile_mcp"
        ) as reconcile:
            self.assertEqual(
                pm.reactivate_clients("", "", profile="production"), ["codex"]
            )
        self.assertEqual(pm.profile_store.list_profiles(), ["production"])
        self.assertEqual(reconcile.call_args.args[2], "production")


class TestUpdateManifestPin(EnvSandbox):
    """A manifest swap between decision and download must be rejected."""

    def test_http_controller_download_does_not_require_tls_module_side_effect(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"ok"
        response.headers = {"Content-Type": "application/json"}
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch.object(pm.urllib.request, "build_opener", return_value=opener):
            body, headers = pm.http_get("http://ctrl.example/api/health")
        self.assertEqual(body, b"ok")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_fetch_rejects_version_mismatch(self):
        manifest = {
            "version": "9.9.9",
            "artifacts": {"universal": {"url": "https://ctrl.example/p.zip", "sha256": "x"}},
        }
        with (
            mock.patch.object(
                pm, "http_get", return_value=(json.dumps(manifest).encode(), {})
            ),
            self.assertRaisesRegex(RuntimeError, "发生了变化"),
        ):
            pm.fetch_registry_package(
                "https://ctrl.example", "", expected_version="1.0.0"
            )


class TestKkagentRegistryFailClosed(EnvSandbox):
    """Corrupt registry → fail closed, other plugins survive."""

    def test_corrupt_registry_aborts_with_backup(self):
        target = self.root / "plugin-payload"
        target.mkdir()
        (target / "kk.plugin.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
        registry = pm.kkagent_plugin_registry()
        registry.parent.mkdir(parents=True)
        registry.write_text("{corrupt json", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "拒绝覆盖"):
            pm.register_kkagent_plugin(target)
        # original (corrupt) content preserved (backup), registry NOT overwritten
        self.assertIn("{corrupt json", registry.read_text(encoding="utf-8"))
        backups = list(registry.parent.glob("installed.json.corrupt.*"))
        self.assertEqual(len(backups), 1)
        self.assertIn("{corrupt json", backups[0].read_text(encoding="utf-8"))

    def test_valid_registry_updated_atomically(self):
        target = self.root / "plugin-payload"
        target.mkdir()
        (target / "kk.plugin.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
        registry = pm.kkagent_plugin_registry()
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps({"plugins": [{"id": "other-plugin", "enabled": True}]}),
            encoding="utf-8",
        )
        pm.register_kkagent_plugin(target)
        data = json.loads(registry.read_text(encoding="utf-8"))
        ids = {p["id"] for p in data["plugins"]}
        self.assertEqual(ids, {"other-plugin", "gms-remote-test"})


class TestSyncOneExecBit(unittest.TestCase):
    """Identical content must still fix a lost executable bit."""

    def test_identical_content_fixes_exec_bit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "launcher.py"
            src.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            dst = tmp_path / "generated" / "launcher.py"
            dst.parent.mkdir()
            dst.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            dst.chmod(0o644)
            sync = _load_sync_module()
            with mock.patch.object(sync, "print"):
                copied = sync.sync_one(src, dst)
            self.assertTrue(copied)
            self.assertTrue(dst.stat().st_mode & stat.S_IXUSR)


def _load_sync_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("sync_agent_package", SYNC_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTokenOwnerCheck(unittest.TestCase):
    """SDK parity — token file owned by another user is rejected."""

    def test_token_file_owner_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "tok"
            token_file.write_text("secret\n", encoding="utf-8")
            token_file.chmod(0o600)
            real_euid = os.geteuid() if hasattr(os, "geteuid") else None
            if real_euid is None:
                self.skipTest("no geteuid on this platform")
            with mock.patch.object(os, "geteuid", return_value=real_euid + 1):
                result = gms_client._load_token(token_file)
            self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
