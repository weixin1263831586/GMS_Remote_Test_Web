"""Registry/builder ZIP contract tests (10.txt §七: one ZIP implementation).

The Controller registry and tools/build_agent_package.py must produce the
SAME single-root layout — the exact bytes gms-agent's extractor expects.
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from features.system import agent_package_builder as builder  # noqa: E402
from features.system.agent_package_registry import _build_archive, _package_version  # noqa: E402


@contextlib.contextmanager
def pkg_dir_without_kimi_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "scripts").mkdir()
        (plugin_dir / "scripts" / "x.sh").write_text("#\n", encoding="utf-8")
        (plugin_dir / "kk.plugin.json").write_text("{}\n", encoding="utf-8")
        # kimi.plugin.json deliberately absent
        yield plugin_dir


class BuilderContractTests(unittest.TestCase):
    def test_single_root_layout(self):
        plugin_dir = REPO_ROOT / "plugins" / "gms-remote-test"
        data = builder.build_package_bytes(plugin_dir, "0.0.0-test", client="universal")
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
        roots = {name.split("/")[0] for name in names}
        self.assertEqual(roots, {"gms-remote-test"})
        # No double wrapping like gms-remote-test-<ver>/gms-remote-test/...
        self.assertFalse(any(name.startswith("gms-remote-test-") for name in names))
        for required in (
            "gms-remote-test/scripts/gms-remote-test.sh",
            "gms-remote-test/scripts/mcp_server.py",
            "gms-remote-test/scripts/gms-agent",
            "gms-remote-test/scripts/gms_agent/client.py",
            "gms-remote-test/skills/gms-remote-test/SKILL.md",
            "gms-remote-test/kk.plugin.json",
            "gms-remote-test/kimi.plugin.json",
            "gms-remote-test/.codex-plugin/plugin.json",
        ):
            self.assertIn(required, names, f"missing {required} in universal package")
        # No __pycache__ leaks into the archive.
        self.assertFalse(any("__pycache__" in name for name in names))

    def test_client_variants_carry_only_their_manifests(self):
        plugin_dir = REPO_ROOT / "plugins" / "gms-remote-test"
        for client, expected in builder.CLIENT_MANIFESTS.items():
            data = builder.build_package_bytes(plugin_dir, "0.0.0-test", client=client)
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
            # Manifest entries live directly at the package root (or in
            # .codex-plugin/), unlike payload files which always sit under
            # scripts/ skills/ tests/.
            payload_prefixes = ("gms-remote-test/scripts/", "gms-remote-test/skills/", "gms-remote-test/tests/")
            manifests = sorted(
                name for name in names if not name.startswith(payload_prefixes)
            )
            self.assertEqual(
                manifests,
                sorted(f"gms-remote-test/{m}" for m in expected),
                f"{client} variant manifest set mismatch",
            )

    def test_executable_bits_preserved(self):
        plugin_dir = REPO_ROOT / "plugins" / "gms-remote-test"
        data = builder.build_package_bytes(plugin_dir, "0.0.0-test", client="universal")
        with zipfile.ZipFile(BytesIO(data)) as archive:
            modes = {
                info.filename: (info.external_attr >> 16) & 0o777
                for info in archive.infolist()
            }
        self.assertEqual(modes["gms-remote-test/scripts/gms-remote-test.sh"], 0o755)
        self.assertEqual(modes["gms-remote-test/scripts/gms-agent"], 0o755)
        self.assertEqual(modes["gms-remote-test/skills/gms-remote-test/SKILL.md"], 0o644)

    def test_missing_manifest_raises(self):
        with pkg_dir_without_kimi_manifest() as plugin_dir:
            with self.assertRaises(FileNotFoundError):
                builder.check_client_manifests(plugin_dir, "universal")
            # The kk-only variant is satisfiable (kk.plugin.json present).
            builder.check_client_manifests(plugin_dir, "kkagent")


class RegistryTests(unittest.TestCase):
    def test_registry_builds_same_layout_as_builder(self):
        version = _package_version()
        if not version:
            self.skipTest("package.yaml unavailable")
        plugin_dir = REPO_ROOT / "plugins" / "gms-remote-test"
        self.assertEqual(
            _build_archive(version),
            builder.build_package_bytes(plugin_dir, version, client="universal"),
        )


if __name__ == "__main__":
    unittest.main()
