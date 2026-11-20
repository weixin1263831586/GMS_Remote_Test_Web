"""Compiled release hardening: never regress back to shipping Python sources."""
from __future__ import annotations

import ast
import io
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from foundation.runtime_settings import RuntimeSettings
from scripts.release.verify_binary_tree import inspect


ROOT = Path(__file__).resolve().parents[1]


class CompiledReleaseContracts(unittest.TestCase):
    def test_compiled_sources_are_syntactically_valid(self):
        for name in (
            "scripts/release/compiled_entry.py",
            "scripts/release/build_binary_release.py",
            "scripts/release/verify_binary_tree.py",
        ):
            with self.subTest(source=name):
                ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)

    def test_installer_shell_syntax(self):
        subprocess.run(["bash", "-n", str(ROOT / "scripts/release/install_compiled.sh")],
                       check=True, capture_output=True)

    def test_release_rejects_python_and_key_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            (root / "runtime/app.py").write_text("secret = True\n")
            (root / "runtime/secret.pem").write_text(
                "-----BEGIN PRIVATE KEY-----\nexample\n-----END PRIVATE KEY-----\n")
            problems = inspect(root)
            self.assertTrue(any("app.py" in issue for issue in problems))
            self.assertTrue(any("secret.pem" in issue for issue in problems))

    def test_release_rejects_python_inside_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(root / "tools.zip", "w") as archive:
                archive.writestr("private/core.py", "implementation")
            with tarfile.open(root / "native.tar.gz", "w:gz") as archive:
                payload = b"implementation"
                info = tarfile.TarInfo("src/config.pyc")
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            problems = inspect(root)
            self.assertTrue(any("tools.zip" in item for item in problems))
            self.assertTrue(any("native.tar.gz" in item for item in problems))

    def test_empty_compiled_tree_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runtime").mkdir()
            (root / "runtime/gms-runtime").write_bytes(b"ELF stub")
            (root / "runtime/index.html").write_text("<p>Web</p>")
            self.assertEqual(inspect(root), [])

    def test_explicit_binary_resource_root_does_not_change_source_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RuntimeSettings.from_environment(
                environ={"GMS_PROJECT_ROOT": str(root), "GMS_ENV": "production"})
            self.assertEqual(runtime.project_root, root.resolve())
            self.assertEqual(runtime.data_root, (root / "data").resolve())
            source = RuntimeSettings.from_environment(environ={"GMS_ENV": "production"})
            self.assertNotEqual(source.project_root, root.resolve())

    def test_installer_fails_closed_and_never_installs_python_venv(self):
        source = (ROOT / "scripts/release/install_compiled.sh").read_text(encoding="utf-8")
        self.assertIn("sha256sum --quiet -c manifest.sha256", source)
        self.assertIn("systemctl", source)
        self.assertIn("read -r -p", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("git clone", source)
        self.assertIn("--no-admin-prompt", source)

    def test_installer_obtains_consent_before_os_changes(self):
        source = (ROOT / "scripts/release/install_compiled.sh").read_text(encoding="utf-8")
        consent = source.index('read -r -p "Install compiled GMS')
        self.assertLess(consent, source.index("apt-get update"))
        self.assertLess(consent, source.index("useradd --system"))

    def test_builder_has_real_nuitka_requirement_and_source_gate(self):
        source = (ROOT / "scripts/release/build_binary_release.py").read_text(encoding="utf-8")
        self.assertIn('"-m", "nuitka"', source)
        self.assertIn("findings = inspect(stage)", source)
        self.assertIn("GMS_RELEASE_SIGNING_KEY", source)
        self.assertNotIn("--include-package=features", source)


if __name__ == "__main__":
    unittest.main()
