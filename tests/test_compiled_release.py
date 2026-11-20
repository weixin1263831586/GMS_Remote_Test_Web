"""Compiled release hardening: never regress back to shipping Python sources."""
from __future__ import annotations

import ast
import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.release.verify_binary_tree import inspect
from foundation.runtime_settings import RuntimeSettings

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

    def test_native_builder_can_run_directly_from_checkout(self):
        # GitHub Actions invokes the path, rather than python -m scripts.release...
        completed = subprocess.run(
            [os.environ.get("PYTHON", "python3"),
             str(ROOT / "scripts/release/build_binary_release.py"), "--help"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--dist-dir", completed.stdout)

    def test_installer_accepts_standard_service_user_names(self):
        source = (ROOT / "scripts/release/install_compiled.sh").read_text(encoding="utf-8")
        self.assertIn(
            '[[ "$RUN_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "invalid user"',
            source,
        )
        expression = r'^[a-z_][a-z0-9_-]*        with tempfile.TemporaryDirectory() as tmp:
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

    def test_builder_has_real_nuitka_requirement_and_source_gate(self):
        source = (ROOT / "scripts/release/build_binary_release.py").read_text(encoding="utf-8")
        self.assertIn('"-m", "nuitka"', source)
        self.assertIn("findings = inspect(stage)", source)
        self.assertIn("GMS_RELEASE_SIGNING_KEY", source)
        self.assertNotIn("--include-package=features", source)


if __name__ == "__main__":
    unittest.main()

        for valid in ("gms-web", "hcq", "service_01"):
            with self.subTest(user=valid):
                completed = subprocess.run(
                    ["bash", "-c", '[[ "$1" =~ $2 ]]', "bash", valid, expression],
                    capture_output=True,
                )
                self.assertEqual(completed.returncode, 0)
        for bad in ("root/../../etc", "0bad", "space name"):
            with self.subTest(user=bad):
                completed = subprocess.run(
                    ["bash", "-c", '[[ "$1" =~ $2 ]]', "bash", bad, expression],
                    capture_output=True,
                )
                self.assertNotEqual(completed.returncode, 0)

    def test_compiled_doctor_initializes_release_root(self):
        source = (ROOT / "scripts/release/compiled_entry.py").read_text(encoding="utf-8")
        segment = source.split("def doctor() -> int:", 1)[1].split("\ndef main()", 1)[0]
        self.assertIn("root = release_root()", segment)

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

    def test_builder_has_real_nuitka_requirement_and_source_gate(self):
        source = (ROOT / "scripts/release/build_binary_release.py").read_text(encoding="utf-8")
        self.assertIn('"-m", "nuitka"', source)
        self.assertIn("findings = inspect(stage)", source)
        self.assertIn("GMS_RELEASE_SIGNING_KEY", source)
        self.assertNotIn("--include-package=features", source)


if __name__ == "__main__":
    unittest.main()
