"""Command-level installer lifecycle tests.

Covers the post-refactor installer surface with real function calls and
failure injection:

* P0-1  `install_cli_dispatcher` must link `gms-agent` to the real CLI
        entry point (scripts/gms-agent), never to the gms-rt dispatcher,
        while gms-rt-* links still go through the dispatcher (argv0 → $1).
* P0-3  `write_enrollment_token` must resolve TOML-only profiles — no
        FileNotFoundError when the legacy <client>.env store is absent.
* P1-5  mcp_launcher resolves the client from a pinned GMS_RT_PROFILE's
        `client =` field instead of the kimi→codex→kkagent probe.
* P1-7  `fetch_registry_package(expected_version=…)` rejects a manifest
        that changed between the version decision and the download.
* P1-9  `register_kkagent_plugin` fails closed on a corrupt registry and
        never overwrites other plugins' registrations; writes are atomic.
* 其他  sync_one fixes a lost executable bit even when content matches.
* 其他  gms_agent.client._load_token rejects a token file owned by
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


SYNC_SCRIPT = REPO_ROOT / "tools" / "sync_agent_package.py"


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
    """P0-1: gms-agent → scripts/gms-agent; gms-rt-* → dispatcher."""

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


class TestEnrollTomlOnly(EnvSandbox):
    """P0-3: enrollment token must persist for TOML-only profiles."""

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


class TestLauncherProfilePinning(EnvSandbox):
    """P1-5: a pinned GMS_RT_PROFILE selects its own client, not the probe."""

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
    def _args(client: str, server: str = ""):
        return type(
            "Args",
            (),
            {
                "client": client,
                "server": server,
                "package": str(REPO_ROOT / "agent" / "gms-remote-test"),
                "enroll_code": "",
            },
        )()

    def test_client_none_installs_console_link_without_profile(self):
        self.assertEqual(pm._cmd_install_locked(self._args("none")), 0)
        self.assertTrue((self.root / "bin" / "gms-rt-devices-console").is_symlink())
        self.assertFalse(pm.profile_store.PROFILE_ROOT.exists())

    def test_client_install_rejects_invalid_controller_before_changes(self):
        self.assertEqual(
            pm._cmd_install_locked(self._args("codex", "https://$(unsafe)/host")),
            2,
        )
        self.assertFalse(pm.CURRENT_LINK.exists())


class TestUpdateManifestPin(EnvSandbox):
    """P1-7: manifest swap between decision and download must be rejected."""

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
    """P1-9: corrupt registry → fail closed, other plugins survive."""

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
    """其他: identical content must still fix a lost executable bit."""

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
    """其他: SDK parity — token file owned by another user is rejected."""

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
