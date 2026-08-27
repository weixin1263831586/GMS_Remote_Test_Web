from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from features.system.api import _skill_directory


ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "skills" / "gms-remote-test"
INSTALLER = SKILL_DIR / "scripts" / "install.sh"


class SkillInstallerTests(unittest.TestCase):
    def test_skill_directory_rejects_path_traversal(self):
        self.assertIsNone(_skill_directory("../configs"))
        self.assertIsNone(_skill_directory("/tmp"))
        self.assertIsNone(_skill_directory("gms_remote_test"))

    def _create_skill_archive(self, archive_path: Path) -> None:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(SKILL_DIR.rglob("*")):
                if source.is_file():
                    relative = source.relative_to(SKILL_DIR)
                    archive.write(source, Path("gms-remote-test") / relative)

    def _run(self, command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_install_cli_and_repeatable_update_in_isolated_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            skills_dir = base / "codex" / "skills"
            bin_dir = base / "bin"
            runtime_bin_dir = base / "runtime-bin"
            profile = home / ".profile"
            archive_path = base / "gms-remote-test.zip"
            home.mkdir()
            self._create_skill_archive(archive_path)

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "GMS_CODEX_SKILLS_DIR": str(skills_dir),
                    "GMS_BIN_DIR": str(bin_dir),
                    "GMS_RUNTIME_BIN_DIR": str(runtime_bin_dir),
                    "GMS_PROFILE_FILE": str(profile),
                    "GMS_REMOTE_TEST_SERVER": "https://controller.example:5001",
                    "GMS_SKILL_DOWNLOAD_URL": archive_path.as_uri(),
                    "GMS_INSTALL_INSECURE": "0",
                    # file:// 下载没有响应头，跳过 SHA-256 校验；
                    # 完整性校验由下方专用的 HTTP 用例覆盖。
                    "GMS_INSTALL_SKIP_SHA256": "1",
                }
            )

            first = self._run(["bash", str(INSTALLER)], env)
            target = skills_dir / "gms-remote-test"
            dispatcher = runtime_bin_dir / "gms-rt-dispatcher"

            self.assertIn("Installed Skill:", first.stdout)
            self.assertTrue((target / "SKILL.md").is_file())
            self.assertTrue(dispatcher.is_file())
            self.assertFalse((bin_dir / "gms-rt").exists())
            self.assertFalse((bin_dir / "gms-remote-test").exists())
            self.assertTrue(dispatcher.stat().st_mode & stat.S_IXUSR)
            self.assertTrue((target / "scripts" / "install.sh").stat().st_mode & stat.S_IXUSR)
            self.assertIn(
                "export GMS_CURL_INSECURE=0",
                dispatcher.read_text(encoding="utf-8"),
            )

            helper_text = (target / "scripts" / "gms-remote-test.sh").read_text(
                encoding="utf-8"
            )
            command_names = set(
                re.findall(r"^(gms-rt-[a-z0-9-]+)\(\)", helper_text, re.MULTILINE)
            )
            for command_name in command_names:
                command_link = bin_dir / command_name
                self.assertTrue(command_link.is_symlink(), command_name)
                self.assertEqual(command_link.resolve(), dispatcher.resolve())

            removed_commands = (
                "gms-rt-capabilities",
                "gms-rt-command-describe",
                "gms-rt-commands",
                "gms-rt-update",
                "gms-rt-version",
            )
            for command_name in removed_commands:
                self.assertFalse((bin_dir / command_name).exists(), command_name)

            help_result = self._run([str(bin_dir / "gms-rt-system-help")], env)
            self.assertIn("GMS Remote Test API Helper", help_result.stdout)
            self.assertIn("https://controller.example:5001", help_result.stdout)

            stale_link = bin_dir / "gms-rt-obsolete"
            stale_link.symlink_to(dispatcher)
            for command_name in removed_commands:
                (bin_dir / command_name).symlink_to(dispatcher)
            legacy_wrapper = bin_dir / "gms-rt"
            legacy_wrapper.write_text(
                "#!/usr/bin/env bash\n"
                "HELPER=/tmp/gms-remote-test/scripts/gms-remote-test.sh\n"
                "export GMS_SKILL_DOWNLOAD_URL=https://controller.example/skill\n",
                encoding="utf-8",
            )
            legacy_wrapper.chmod(0o755)
            legacy_alias = bin_dir / "gms-remote-test"
            legacy_alias.symlink_to(legacy_wrapper)
            obsolete = target / "obsolete-after-update"
            obsolete.write_text("remove me", encoding="utf-8")
            update_result = self._run(
                [str(bin_dir / "gms-rt-system-update"), "--json"],
                env,
            )

            update_envelope = json.loads(update_result.stdout)
            self.assertTrue(update_envelope["ok"])
            self.assertEqual(
                update_envelope["command"],
                "gms-rt-system-update",
            )
            self.assertIn("Installed CLI Runtime:", update_envelope["output"])
            self.assertFalse(obsolete.exists())
            self.assertFalse(stale_link.exists())
            for command_name in removed_commands:
                self.assertFalse((bin_dir / command_name).exists(), command_name)
            self.assertFalse(legacy_wrapper.exists())
            self.assertFalse(legacy_alias.exists())
            self.assertEqual(
                profile.read_text(encoding="utf-8").count("# GMS Remote Test CLI"),
                1,
            )


class _SkillZipHandler(BaseHTTPRequestHandler):
    """Mimic the controller's /api/system/skills download response."""

    zip_bytes = b""
    sha256_header: str | None = None
    signature_header: str | None = None

    def do_GET(self) -> None:
        body = self.zip_bytes
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        if self.sha256_header is not None:
            self.send_header("X-GMS-SHA256", self.sha256_header)
        if self.signature_header is not None:
            self.send_header("X-GMS-Signature", self.signature_header)
            self.send_header("X-GMS-Signature-Algorithm", "ed25519")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence test output
        return


class SkillArchiveIntegrityTests(unittest.TestCase):
    """X-GMS-SHA256 integrity checks in install.sh."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        self.archive = base / "gms-remote-test.zip"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            for source in sorted(SKILL_DIR.rglob("*")):
                if source.is_file():
                    relative = source.relative_to(SKILL_DIR)
                    archive.write(source, Path("gms-remote-test") / relative)
        _SkillZipHandler.zip_bytes = self.archive.read_bytes()
        _SkillZipHandler.sha256_header = None
        _SkillZipHandler.signature_header = None
        self.signing_key = Ed25519PrivateKey.generate()
        public_pem = self.signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.verify_key_b64 = base64.b64encode(public_pem).decode("ascii")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SkillZipHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.home),
                "GMS_CODEX_SKILLS_DIR": str(self.base / "skills"),
                "GMS_BIN_DIR": str(self.base / "bin"),
                "GMS_RUNTIME_BIN_DIR": str(self.base / "runtime-bin"),
                "GMS_PROFILE_FILE": str(self.home / ".profile"),
                "GMS_REMOTE_TEST_SERVER": f"http://127.0.0.1:{self.server.server_port}",
                "GMS_SKILL_DOWNLOAD_URL": (
                    f"http://127.0.0.1:{self.server.server_port}/api/system/skills"
                ),
                "GMS_INSTALL_INSECURE": "0",
            }
        )
        return env

    def test_matching_sha256_header_installs(self):
        _SkillZipHandler.sha256_header = hashlib.sha256(
            _SkillZipHandler.zip_bytes
        ).hexdigest()
        result = self._run_ok(self._env())
        self.assertIn("技能包 SHA-256 校验通过", result.stdout)
        self.assertTrue((self.base / "skills" / "gms-remote-test" / "SKILL.md").is_file())

    def test_matching_ed25519_signature_installs(self):
        _SkillZipHandler.sha256_header = hashlib.sha256(
            _SkillZipHandler.zip_bytes
        ).hexdigest()
        _SkillZipHandler.signature_header = base64.b64encode(
            self.signing_key.sign(_SkillZipHandler.zip_bytes)
        ).decode("ascii")
        env = self._env()
        env["GMS_INSTALL_VERIFY_KEY_B64"] = self.verify_key_b64

        result = self._run_ok(env)

        self.assertIn("Ed25519 签名校验通过", result.stdout)

    def test_missing_or_tampered_ed25519_signature_is_rejected(self):
        _SkillZipHandler.sha256_header = hashlib.sha256(
            _SkillZipHandler.zip_bytes
        ).hexdigest()
        env = self._env()
        env["GMS_INSTALL_VERIFY_KEY_B64"] = self.verify_key_b64

        missing = self._run_fail(env)
        self.assertIn("缺少 X-GMS-Signature", missing.stderr)

        _SkillZipHandler.signature_header = base64.b64encode(b"x" * 64).decode("ascii")
        tampered = self._run_fail(env)
        self.assertIn("Ed25519 签名校验失败", tampered.stderr)

    def test_tampered_archive_is_rejected(self):
        _SkillZipHandler.sha256_header = "0" * 64
        result = self._run_fail(self._env())
        self.assertIn("SHA-256 校验失败", result.stderr)

    def test_missing_header_is_rejected_unless_explicitly_skipped(self):
        _SkillZipHandler.sha256_header = None
        result = self._run_fail(self._env())
        self.assertIn("缺少 X-GMS-SHA256", result.stderr)

        env = self._env()
        env["GMS_INSTALL_SKIP_SHA256"] = "1"
        skipped = self._run_ok(env)
        self.assertIn("跳过完整性校验", skipped.stdout)

    def _run_ok(self, env: dict[str, str]):
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def _run_fail(self, env: dict[str, str]):
        result = subprocess.run(
            ["bash", str(INSTALLER)],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result


class InstallerEndpointHostTests(unittest.TestCase):
    """Controller 渲染 install.sh 时必须拒绝可疑 Host（3d1e089 后新增防护）。"""

    def _installer(self, host: str):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from features.system.api import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, base_url="http://placeholder")
        return client.get(
            "/api/system/skills/install.sh", headers={"Host": host}
        )

    def test_normal_hosts_render_installer(self):
        for host in ("172.16.14.233:5001", "gms.example.local", "[::1]:5001"):
            with self.subTest(host=host):
                response = self._installer(host)
                self.assertEqual(response.status_code, 200)
                self.assertIn("GMS_REMOTE_TEST_SERVER", response.text)

    def test_shell_metacharacter_host_is_rejected(self):
        for host in ("evil'; id; '", "a b", "h/../../../etc", "h?x=1", "h#f"):
            with self.subTest(host=host):
                response = self._installer(host)
                self.assertEqual(response.status_code, 400)
                self.assertIn("服务地址", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
