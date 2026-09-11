"""GMS Agent package lifecycle integration tests.

Covers the exact gaps the static packaging tests missed (code review
2026-08): fresh-HOME install, update into the correct versions/<ver>/
directory, version-directory immutability, whole-package re-activation
(skill/plugin/MCP follow the runtime), rollback as package activation,
bootstrap-from-registry for a standalone gms-agent, and malicious-archive
rejection. Everything runs inside TemporaryDirectory sandboxes via
GMS_AGENT_RUNTIME_ROOT/HOME overrides — no real machine state is touched.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
GMS_AGENT = REPO_ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-agent"
PLUGIN_DIR = REPO_ROOT / "plugins" / "gms-remote-test"
PACKAGE_VERSION = "0.13.1"


def load_gms_agent_module():
    spec = importlib.util.spec_from_loader(
        "gms_agent_cli", importlib.machinery.SourceFileLoader("gms_agent_cli", str(GMS_AGENT))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The thin shell imports gms_agent.package_manager; keep a handle so the
    # sandbox can patch the module the functions actually read.
    module.pm = sys.modules["gms_agent.package_manager"]
    return module


def build_registry_tree(version: str, dest: Path, mutate_marker: str = "0") -> Path:
    """Create a fake installed registry package tree (builder layout)."""
    root = dest / "gms-remote-test"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "gms-remote-test.sh").write_text(
        f'#!/usr/bin/env bash\nGMS_RT_VERSION="{version}"\nMARKER={mutate_marker}\n', encoding="utf-8"
    )
    (scripts / "mcp_server.py").write_text("# mcp stub\n", encoding="utf-8")
    (root / "kk.plugin.json").write_text('{"name": "gms-remote-test"}\n', encoding="utf-8")
    skills = root / "skills" / "gms-remote-test"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(f"---\nname: gms-remote-test\n---\nmarker {mutate_marker}\n", encoding="utf-8")
    return root


def zipsafe_package(root: Path, version: str, mutate_marker: str = "0") -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                arcname = f"gms-remote-test/{path.relative_to(root)}"
                info = zipfile.ZipInfo(arcname)
                info.external_attr = (0o755 if not path.suffix else 0o644) << 16
                archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.runtime_root = self.home / ".local" / "share" / "gms-remote-test"
        self.addCleanup(self._tmp.cleanup)
        self.agent = load_gms_agent_module()
        # 拆分后,生命周期函数住在 gms_agent.package_manager;
        # 沙箱化 = 同时替换薄壳与真模块的全局(函数体读的是后者)。
        # profile 存储收口在 gms_agent.profile_store（ADR 0003）——
        # PROFILE_ROOT
        # 只能 patch 那里（package_manager 与 mcp_launcher 都经它读取）；
        # MCP_ENV_DIR / legacy .env 契约已删除。
        sandbox = {
            "RUNTIME_ROOT": self.runtime_root,
            "VERSIONS_DIR": self.runtime_root / "versions",
            "CURRENT_LINK": self.runtime_root / "current",
        }
        for target in (self.agent, self.agent.pm):
            for key, value in sandbox.items():
                setattr(target, key, value)
        from gms_agent import profile_store

        original_profile_root = profile_store.PROFILE_ROOT
        profile_store.PROFILE_ROOT = self.home / ".config" / "gms-agent" / "profiles"
        self.addCleanup(
            setattr, profile_store, "PROFILE_ROOT", original_profile_root
        )
        self.profile_store = profile_store

    # --- 1. version comes from the PACKAGE, not the running script ------
    def test_install_uses_package_version_not_running_version(self):
        package = build_registry_tree("9.9.9", self.home / "pkg")
        target = self.agent.install_runtime(package, "9.9.9", "a" * 64)
        self.assertEqual(target.name, "9.9.9")
        self.assertTrue((target / "scripts" / "gms-remote-test.sh").is_file())
        self.assertEqual(self.agent.installed_version(), "9.9.9")

    def test_update_installs_new_version_into_new_directory(self):
        # The running checkout is 0.13.1; an update package is 9.9.9 — the
        # install must land in versions/9.9.9, NOT the running version's dir.
        old = build_registry_tree(PACKAGE_VERSION, self.home / "old")
        self.agent.install_runtime(old, PACKAGE_VERSION, "b" * 64)
        new = build_registry_tree("9.9.9", self.home / "new", mutate_marker="1")
        target = self.agent.install_runtime(new, "9.9.9", "c" * 64)
        self.assertEqual(target.name, "9.9.9")
        self.assertTrue((self.agent.VERSIONS_DIR / PACKAGE_VERSION / "scripts").is_dir())
        self.assertEqual(self.agent.installed_version(), "9.9.9")

    # --- 2. version directories are immutable ---------------------------
    def test_same_version_different_content_is_rejected(self):
        first = build_registry_tree("1.0.0", self.home / "p1", mutate_marker="A")
        self.agent.install_runtime(first, "1.0.0", "d" * 64)
        second = build_registry_tree("1.0.0", self.home / "p2", mutate_marker="B")
        with self.assertRaises(RuntimeError):
            self.agent.install_runtime(second, "1.0.0", "e" * 64)

    def test_same_version_same_content_is_reused(self):
        package = build_registry_tree("1.0.0", self.home / "p1")
        self.agent.install_runtime(package, "1.0.0", "f" * 64)
        package_again = build_registry_tree("1.0.0", self.home / "p2")
        target = self.agent.install_runtime(package_again, "1.0.0", "f" * 64)
        self.assertEqual(target.name, "1.0.0")

    # --- 3. kkagent plugin payload located at package root --------------
    def test_kkagent_plugin_found_at_package_root(self):
        # Canonical builder layout: manifests at the package ROOT.
        runtime = self.home / "runtime"
        (runtime / "scripts").mkdir(parents=True)
        (runtime / "kk.plugin.json").write_text("{}\n", encoding="utf-8")
        captured = {}

        def fake_copytree(source, target):
            captured["source"] = Path(source)
            captured["target"] = Path(target)

        with unittest.mock.patch.object(self.agent.pm.shutil, "copytree", side_effect=fake_copytree):
            self.agent.install_plugin_for_kkagent(runtime)
        self.assertEqual(captured["source"], runtime)
        self.assertEqual(captured["target"].name, "gms-remote-test")

    # --- 4. profile is data-only TOML with an escaped server URL --------
    def test_write_profile_toml_escapes_server_url(self):
        dangerous_url = 'https://"quoted"/host'
        name = self.agent.write_profile("codex", dangerous_url, "")
        flat = self.agent.pm.load_profile(name)
        # The raw URL round-trips through the TOML reader (data-only store).
        self.assertEqual(flat["url"], dangerous_url)
        self.assertEqual(flat["mode"], "service-token")
        # TOML-only contract: write_profile must not create any
        # legacy <client>.env anywhere in the sandbox home.
        self.assertEqual([], list(self.home.rglob("*.env")))
        # And sourcing the profile in bash executes nothing.
        profile_path = self.profile_store.profile_path(name)
        result = subprocess.run(
            ["bash", "-c", f"source {profile_path} 2>/dev/null; printf %s \"$url\""],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "")  # TOML has no $url shell variable

    # --- 5. configured_clients / profile_server round trip --------------
    def test_configured_clients_round_trip(self):
        self.assertEqual(self.agent.configured_clients(), [])
        self.agent.write_profile("kimi", "https://ctrl:5001", "")
        self.assertEqual(self.agent.configured_clients(), ["kimi"])
        server, ca = self.agent.profile_server_and_ca("kimi")
        self.assertEqual(server, "https://ctrl:5001")
        self.assertEqual(ca, "")

    # --- 6. safe_extract rejects traversal / symlinks / bombs -----------
    def test_safe_extract_rejects_path_traversal(self):
        archive = self.home / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "pwned")
        with self.assertRaises(ValueError):
            self.agent.safe_extract(archive, self.home / "out")

    def test_safe_extract_rejects_symlink_entry(self):
        archive = self.home / "link.zip"
        info = zipfile.ZipInfo("gms-remote-test/link")
        info.external_attr = 0o120777 << 16  # S_IFLNK
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(info, "/etc/passwd")
        with self.assertRaises(ValueError):
            self.agent.safe_extract(archive, self.home / "out")

    def test_safe_extract_rejects_absolute_path(self):
        archive = self.home / "abs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("/etc/evil.conf", "x")
        with self.assertRaises(ValueError):
            self.agent.safe_extract(archive, self.home / "out")

    def test_safe_extract_rejects_file_bomb(self):
        archive = self.home / "bomb.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for i in range(self.agent.MAX_ARCHIVE_FILES + 1):
                zf.writestr(f"gms-remote-test/f{i}", "x")
        with self.assertRaises(ValueError):
            self.agent.safe_extract(archive, self.home / "out")

    def test_safe_extract_accepts_canonical_package(self):
        package = build_registry_tree(PACKAGE_VERSION, self.home / "src")
        data = zipsafe_package(package, PACKAGE_VERSION)
        archive = self.home / "good.zip"
        archive.write_bytes(data)
        out = self.home / "out"
        self.agent.safe_extract(archive, out)
        self.assertTrue((out / "gms-remote-test" / "scripts" / "gms-remote-test.sh").is_file())

    # --- 7. artifact URL must be same-origin with the Controller --------
    def test_artifact_url_must_be_same_origin(self):
        self.assertTrue(self.agent.artifact_url_ok("https://ctrl:5001/api/x", "https://ctrl:5001"))
        self.assertFalse(self.agent.artifact_url_ok("https://evil.example/api/x", "https://ctrl:5001"))
        self.assertFalse(self.agent.artifact_url_ok("ftp://ctrl:5001/x", "https://ctrl:5001"))
        self.assertFalse(self.agent.artifact_url_ok(None, "https://ctrl:5001"))

    # --- 8. bootstrap: standalone script has no local package ----------
    def test_standalone_script_has_no_local_package(self):
        # Simulate the standalone download: copy gms-agent alone into a
        # temp dir — local_package_root must return None (registry path).
        standalone = self.home / "download" / "gms-agent"
        standalone.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(GMS_AGENT, standalone)
        spec = importlib.util.spec_from_loader(
            "gms_agent_standalone",
            importlib.machinery.SourceFileLoader("gms_agent_standalone", str(standalone)),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The standalone copy sits alone: no package root around it.
        self.assertIsNone(module.local_package_root("", script_dir=standalone.parent))
        # The shipped checkout (agent/gms-remote-test) IS a package root.
        self.assertIsNotNone(module.local_package_root(str(GMS_AGENT.parent.parent)))

    # --- 9. update/rollback re-activate every configured client ---------
    def test_update_reactivates_configured_clients(self):
        # Install 1.0.0, configure kimi, then simulate an update loop to
        # 2.0.0: the skill copy under the (sandboxed) kimi root must be
        # refreshed from the new version's tree.
        old = build_registry_tree("1.0.0", self.home / "p1", mutate_marker="A")
        self.agent.install_runtime(old, "1.0.0", "1" * 64)
        kimi_root = self.home / "kimi-home" / "skills"
        kimi_root.mkdir(parents=True)
        self.agent.write_profile("kimi", "https://ctrl:5001", "")
        # Re-point skill roots into the sandbox before activation.
        self.agent.pm.client_skill_root = lambda client: kimi_root  # type: ignore[method-assign]
        self.agent.install_skill("kimi", self.agent.CURRENT_LINK)
        marker_file = kimi_root / "gms-remote-test" / "SKILL.md"
        self.assertIn("marker A", marker_file.read_text(encoding="utf-8"))

        new = build_registry_tree("2.0.0", self.home / "p2", mutate_marker="B")
        self.agent.install_runtime(new, "2.0.0", "2" * 64)
        self.agent.install_skill("kimi", self.agent.CURRENT_LINK)
        self.assertIn("marker B", marker_file.read_text(encoding="utf-8"))
        # Rollback flips the whole package back (MCP reconcile stubbed —
        # the fake tree carries no real agent_mcp_config.py).
        with unittest.mock.patch.object(self.agent.pm, "reconcile_mcp"):
            self.agent.cmd_rollback(type("Args", (), {"version": "1.0.0"})())
        self.assertIn("marker A", marker_file.read_text(encoding="utf-8"))

    # --- 10. update refuses to downgrade -------------------
    def test_update_rejects_downgrade(self):
        self.agent.install_runtime(
            build_registry_tree("9.9.9", self.home / "cur"), "9.9.9", "3" * 64
        )
        stale_manifest = {
            "name": "gms-remote-test",
            "version": "1.0.0",
            "artifacts": {"universal": {"url": "https://ctrl:5001/x", "sha256": "a" * 64}},
        }
        with unittest.mock.patch.object(
            self.agent.pm, "http_get", return_value=(json.dumps(stale_manifest).encode(), {})
        ):
            code = self.agent.cmd_update(
                type("Args", (), {"server": "https://ctrl:5001", "force": False})()
            )
        self.assertEqual(code, 5)
        self.assertEqual(self.agent.installed_version(), "9.9.9")

    # --- 11. artifact URL must match scheme as well -------
    def test_artifact_url_scheme_must_match_controller(self):
        self.assertFalse(
            self.agent.artifact_url_ok("http://ctrl:5001/api/x", "https://ctrl:5001")
        )
        self.assertTrue(
            self.agent.artifact_url_ok("https://ctrl:5001/api/x", "https://ctrl:5001")
        )

    # --- 12. dispatcher install links every CLI command ----
    def test_install_cli_dispatcher_creates_all_command_links(self):
        cli_dir = self.agent.CURRENT_LINK / "scripts"
        cli_dir.mkdir(parents=True, exist_ok=True)
        (cli_dir / "gms-remote-test.sh").write_text(
            '#!/usr/bin/env bash\nGMS_RT_VERSION="9.9.9"\n'
            "gms-rt-devices-list() { :; }\n"
            "gms-rt-system-health() { :; }\n"
            "gms-rt-agent-enroll() { :; }\n",
            encoding="utf-8",
        )
        (cli_dir / "gms-agent").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        bin_dir = self.home / "bin"
        with unittest.mock.patch.dict(
            os.environ, {"GMS_BIN_DIR": str(bin_dir)}, clear=False
        ):
            created = self.agent.install_cli_dispatcher()
        names = {path.name for path in created}
        self.assertIn("gms-rt", names)
        self.assertIn("gms-agent", names)
        self.assertIn("gms-rt-devices-list", names)
        self.assertIn("gms-rt-system-health", names)
        self.assertIn("gms-rt-agent-enroll", names)
        # argv0 invocation: each command link is a symlink to the dispatcher;
        # the dispatcher resolves the command from its own invoked name.
        self.assertTrue(
            os.path.islink(bin_dir / "gms-rt-devices-list")
        )
        self.assertEqual(
            (os.stat(bin_dir / "gms-rt").st_mode & 0o777), 0o755
        )

    # --- 13. kkagent install registers the local plugin ----
    def test_kkagent_plugin_registered_in_installed_json(self):
        kkagent_home = self.home / "kkagent-home"
        runtime = self.home / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "kk.plugin.json").write_text(
            '{"name": "gms-remote-test", "version": "9.9.9"}\n', encoding="utf-8"
        )
        with unittest.mock.patch.dict(
            os.environ, {"KKAGENT_HOME": str(kkagent_home)}, clear=False
        ):
            self.agent.install_plugin_for_kkagent(runtime)
        target = kkagent_home / "plugins" / "local" / "gms-remote-test"
        self.assertTrue(target.is_dir())
        registry = json.loads(
            (kkagent_home / "plugins" / "installed.json").read_text(encoding="utf-8")
        )
        entry = next(
            p for p in registry["plugins"] if p["id"] == "gms-remote-test"
        )
        self.assertEqual(entry["root"], str(target))
        self.assertEqual(entry["version"], "9.9.9")
        self.assertTrue(entry["enabled"])

    # --- 14. reconcile_mcp gets the RESOLVED server --------
    def test_reactivate_clients_resolves_profile_server_for_mcp(self):
        # The rollback bug: write_profile received server_i or env-fallback
        # but reconcile_mcp received the raw (possibly empty) profile value.
        # reactivate_clients() must resolve ONCE for both.
        old = build_registry_tree("1.0.0", self.home / "p1", mutate_marker="A")
        self.agent.install_runtime(old, "1.0.0", "1" * 64)
        self.agent.write_profile("kimi", "https://ctrl-from-profile:5001", "")
        kimi_root = self.home / "kimi-home" / "skills"
        kimi_root.mkdir(parents=True)
        self.agent.pm.client_skill_root = lambda client: kimi_root  # type: ignore[method-assign]
        captured = {}

        def fake_reconcile(client, server, name, ca_cert):
            captured["server"] = server
            captured["ca"] = ca_cert

        with unittest.mock.patch.object(
            self.agent.pm, "reconcile_mcp", side_effect=fake_reconcile
        ), unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GMS_REMOTE_TEST_SERVER", None)
            reactivated = self.agent.pm.reactivate_clients("", "")
        self.assertEqual(reactivated, ["kimi"])
        self.assertEqual(captured["server"], "https://ctrl-from-profile:5001")

    # --- 15. activation failure triggers compensation ------
    def test_reactivate_clients_compensates_back_to_previous_version(self):
        old = build_registry_tree("1.0.0", self.home / "p1", mutate_marker="A")
        self.agent.install_runtime(old, "1.0.0", "1" * 64)
        self.agent.write_profile("kimi", "https://ctrl:5001", "")
        kimi_root = self.home / "kimi-home" / "skills"
        kimi_root.mkdir(parents=True)
        self.agent.pm.client_skill_root = lambda client: kimi_root  # type: ignore[method-assign]
        new = build_registry_tree("2.0.0", self.home / "p2", mutate_marker="B")
        self.agent.install_runtime(new, "2.0.0", "2" * 64)
        self.assertEqual(self.agent.installed_version(), "2.0.0")

        calls = {"n": 0}

        def failing_install_skill(client, runtime_dir):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("activation boom")
            return kimi_root / "gms-remote-test"

        with (
            unittest.mock.patch.object(
                self.agent.pm, "install_skill", side_effect=failing_install_skill
            ),
            unittest.mock.patch.object(self.agent.pm, "reconcile_mcp"),self.assertRaises(RuntimeError)
        ):
            self.agent.pm.reactivate_clients(
                "https://ctrl:5001",
                "",
                previous_target=self.agent.VERSIONS_DIR / "1.0.0",
            )
        # current flipped back to the previous version and the client was
        # re-activated FROM it — no mixed runtime/skill state.
        # (install_skill is mocked here, so verify the compensation ran by
        # call count; the content-level check lives in the SystemExit test
        # where install_skill stays real.)
        self.assertEqual(self.agent.installed_version(), "1.0.0")
        self.assertEqual(calls["n"], 2)

    def test_reactivate_clients_compensates_on_reconcile_systemexit(self):
        # reconcile_mcp signals failure via SystemExit(1) — the REAL failure
        # type in production. Compensation must catch it, not let the host
        # end up with a fresh runtime and stale client wiring.
        old = build_registry_tree("1.0.0", self.home / "p1", mutate_marker="A")
        self.agent.install_runtime(old, "1.0.0", "1" * 64)
        self.agent.write_profile("kimi", "https://ctrl:5001", "")
        kimi_root = self.home / "kimi-home" / "skills"
        kimi_root.mkdir(parents=True)
        self.agent.pm.client_skill_root = lambda client: kimi_root  # type: ignore[method-assign]
        new = build_registry_tree("2.0.0", self.home / "p2", mutate_marker="B")
        self.agent.install_runtime(new, "2.0.0", "2" * 64)

        calls = {"n": 0}

        def failing_reconcile(client, server, name, ca_cert):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit(1)

        with (
            unittest.mock.patch.object(
                self.agent.pm, "reconcile_mcp", side_effect=failing_reconcile
            ),self.assertRaises(SystemExit)
        ):
            self.agent.pm.reactivate_clients(
                "https://ctrl:5001",
                "",
                previous_target=self.agent.VERSIONS_DIR / "1.0.0",
            )
        self.assertEqual(self.agent.installed_version(), "1.0.0")
        marker_file = kimi_root / "gms-remote-test" / "SKILL.md"
        self.assertIn("marker A", marker_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
