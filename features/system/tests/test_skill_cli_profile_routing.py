"""Direct gms-rt profile selection and TLS recovery contracts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from features.system.tests.skill_cli_mock_server import ApiHandler


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "agent" / "gms-remote-test" / "runtime" / "gms-remote-test.sh"


class SkillCliProfileRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def _write_profile(
        home: Path,
        name: str,
        server: str,
        token: bool = True,
        token_value: str = "test-token",
    ) -> None:
        profile_root = home / ".config" / "gms-agent" / "profiles"
        state_root = home / ".local" / "state" / "gms-remote-test"
        profile_root.mkdir(parents=True, exist_ok=True)
        state_root.mkdir(parents=True, exist_ok=True)
        token_file = state_root / f"{name}.token"
        if token:
            token_file.write_text(f"{token_value}\n", encoding="utf-8")
            token_file.chmod(0o600)
        (profile_root / f"{name}.toml").write_text(
            f'profile = "{name}"\n'
            'client = "codex"\n\n'
            "[controller]\n"
            f'url = "{server}"\n'
            'ca_cert = ""\n\n'
            "[auth]\n"
            'mode = "service-token"\n'
            f'token_file = "{token_file}"\n',
            encoding="utf-8",
        )

    @staticmethod
    def _run(home: Path, *arguments: str, extra_env: dict[str, str] | None = None):
        env = os.environ.copy()
        for key in (
            "GMS_REMOTE_TEST_SERVER",
            "GMS_RT_PROFILE",
            "GMS_CURL_CA_CERT",
            "GMS_AUTH_TOKEN_FILE",
        ):
            env.pop(key, None)
        env.update(
            {
                "HOME": str(home),
                "GMS_AUTH_COOKIE_JAR": str(home / "session.cookies"),
                "NO_COLOR": "1",
            }
        )
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(HELPER), *arguments],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_single_profile_is_selected_for_direct_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "only", self.server_url)
            result = self._run(home, "gms-rt-auth-credential-mode", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["mode"], "agent_token")
        self.assertTrue(payload["data"]["token_file"].endswith("/only.token"))

    def test_equivalent_client_profiles_support_bare_direct_cli(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "one", self.server_url)
            self._write_profile(home, "two", self.server_url)
            result = self._run(home, "gms-rt-auth-credential-mode", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["mode"], "agent_token")

    def test_different_client_tokens_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "one", self.server_url, token_value="one")
            self._write_profile(home, "two", self.server_url, token_value="two")
            result = self._run(home, "gms-rt-devices-list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("多个不同或不可用的 Agent 凭据", result.stderr)
        self.assertIn("GMS_RT_HUMAN_SESSION=1", result.stderr)

    def test_human_session_does_not_load_equivalent_agent_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "one", self.server_url)
            self._write_profile(home, "two", self.server_url)
            result = self._run(
                home,
                "gms-rt-auth-credential-mode",
                "--json",
                extra_env={"GMS_RT_HUMAN_SESSION": "1"},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["data"]["mode"], "session_cookie")

    def test_different_controllers_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "one", "https://one.example:5001")
            self._write_profile(home, "two", "https://two.example:5001")
            result = self._run(home, "gms-rt-devices-list")
        self.assertEqual(result.returncode, 2)
        self.assertIn("多个不同的 Controller/TLS profile", result.stderr)

    def test_missing_explicit_profile_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run(
                Path(temporary),
                "gms-rt-devices-list",
                extra_env={"GMS_RT_PROFILE": "missing"},
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("不存在、不可读或缺少 Controller: missing", result.stderr)

    def test_profile_server_mismatch_fails_before_sending_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self._write_profile(home, "chosen", "https://controller-a.example:5001")
            result = self._run(
                home,
                "gms-rt-devices-list",
                extra_env={
                    "GMS_RT_PROFILE": "chosen",
                    "GMS_REMOTE_TEST_SERVER": "https://controller-b.example:5001",
                },
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("拒绝向 https://controller-b.example:5001 发送其凭据", result.stderr)

    def test_tls_error_prefers_profile_ca_and_does_not_recommend_insecure(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            bin_dir = home / "bin"
            bin_dir.mkdir()
            curl = bin_dir / "curl"
            curl.write_text("#!/bin/sh\nexit 60\n", encoding="utf-8")
            curl.chmod(0o755)
            result = self._run(
                home,
                "gms-rt-devices-list",
                extra_env={
                    "PATH": f"{bin_dir}:/usr/bin:/bin",
                    "GMS_REMOTE_TEST_SERVER": "https://controller.example:5001",
                },
            )
        self.assertEqual(result.returncode, 6, result.stderr)
        self.assertIn("export GMS_RT_PROFILE=<PROFILE>", result.stderr)
        self.assertIn("GMS_CURL_CA_CERT=/path/to/controller-ca.pem", result.stderr)
        self.assertNotIn("GMS_CURL_INSECURE=1", result.stderr)


if __name__ == "__main__":
    unittest.main()
