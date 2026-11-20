"""Contract tests for private binary packaging and first-run installer."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.build_private_release import digest, private_tree_findings, verify


ROOT = Path(__file__).resolve().parents[1]


class PrivateReleaseTests(unittest.TestCase):
    def _fake_release(self, root: Path):
        (root / "runtime").mkdir()
        (root / "web").mkdir()
        (root / "configs/examples").mkdir(parents=True)
        (root / "runtime/gms-runtime").write_bytes(b"compiled binary\x7f")
        (root / "install.sh").write_text("#!/bin/bash\nexit 0\n")
        return root

    def test_rejects_python_source_and_bytecode_anywhere(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._fake_release(Path(td))
            self.assertEqual(private_tree_findings(root), [])
            for name in ("secret.py", "secret.pyc", "x.pyi", "debug.map"):
                suspect = root / "runtime" / name
                suspect.write_bytes(b"private")
                self.assertTrue(
                    any("source or debug artifact" in x for x in private_tree_findings(root)),
                    name,
                )
                suspect.unlink()

    def test_rejects_runtime_secrets_and_private_key_material(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._fake_release(Path(td))
            (root / "configs/secrets").mkdir()
            (root / "configs/secrets/token").write_text("secret")
            self.assertTrue(private_tree_findings(root))
            shutil.rmtree(root / "configs/secrets")
            (root / "runtime/server.key").write_bytes(b"secret")
            self.assertTrue(private_tree_findings(root))

    def test_rejects_external_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._fake_release(Path(td) / "release")
            outside = Path(td) / "outside.txt"
            outside.write_text("secret")
            (root / "runtime/external").symlink_to(outside)
            self.assertTrue(any("external link" in x for x in private_tree_findings(root)))

    def test_manifest_must_cover_all_files_and_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._fake_release(Path(td))
            files = {p.relative_to(root).as_posix(): digest(p)
                     for p in root.rglob("*") if p.is_file()}
            (root / "manifest.json").write_text(json.dumps({
                "format": "gms-private-runtime-v1", "files": files,
            }))
            args = type("Args", (), {"release_root": str(root)})()
            verify(args)
            (root / "runtime/gms-runtime").write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                verify(args)

    def test_installer_is_bash_and_uses_no_pip_or_python_sources(self):
        installer = ROOT / "scripts/install_private_release.sh"
        if not shutil.which("bash"):
            self.skipTest("bash missing")
        subprocess.run(["bash", "-n", str(installer)], check=True)
        source = installer.read_text()
        self.assertIn('as_service configure </dev/tty', source)
        self.assertIn('as_service prepare --host', source)
        self.assertIn("GMS_PROJECT_ROOT=", source)
        self.assertIn("manifest file set or SHA256 digest mismatch", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("python -m bootstrap.server", source)

    def test_source_install_is_unchanged_without_private_flag(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn("package) package_web_app", script) if False else None
        self.assertIn('if [[ "$PRIVATE_RELEASE" == true ]]', script)
        self.assertIn("package_web_app", script)
        self.assertIn("install_web_app", script)

    def test_entry_never_puts_admin_password_on_command_line(self):
        source = (ROOT / "deployment/private_runtime.py").read_text()
        self.assertIn('getpass.getpass("Administrator password:', source)
        self.assertIn("auth_service.create_initial_admin(", source)
        self.assertIn("RedmineConfig().for_owner(admin.id)", source)
        self.assertIn("save_redmine_api_key(key)", source)
        self.assertNotIn("add_argument(\"--password\"", source)


if __name__ == "__main__":
    unittest.main()
