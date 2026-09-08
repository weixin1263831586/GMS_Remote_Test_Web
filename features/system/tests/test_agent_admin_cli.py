"""Admin CLI commands for Agent Service Token management (15.txt §三).

`gms-rt-agent-tokens` / `gms-rt-agent-enroll-code` / `gms-rt-agent-token-revoke`
close the tooling gap: an admin can fully drive the Agent enrollment
lifecycle from a terminal without hand-crafted curl.
"""

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
HELPER = ROOT / "skills" / "gms-remote-test" / "scripts" / "gms-remote-test.sh"


class AgentAdminCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ApiHandler.requests.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env.update(
                {
                    "HOME": temporary,
                    "GMS_REMOTE_TEST_SERVER": (
                        f"http://127.0.0.1:{self.server.server_port}"
                    ),
                    "GMS_AUTH_COOKIE_JAR": str(Path(temporary) / "session.cookies"),
                    "NO_COLOR": "1",
                }
            )
            return subprocess.run(
                ["bash", str(HELPER), *arguments],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )

    def test_agent_tokens_lists_metadata_only(self):
        result = self._run("gms-rt-agent-tokens", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        tokens = payload["data"]
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["id"], "agt_deadbeef")
        self.assertEqual(tokens[0]["name"], "codex-build01")
        self.assertEqual(tokens[0]["scopes"], ["devices.read", "tests.execute"])
        # 永远不出现原始 token 字段（服务端本就不存）。
        self.assertNotIn("token", tokens[0])

    def test_agent_enroll_code_mints_one_shot_code(self):
        ApiHandler.requests.clear()
        result = self._run(
            "gms-rt-agent-enroll-code",
            "--name", "kimi-build03",
            "--scopes", "devices.read,tests.execute,jobs.read",
            "--workers", "ats-041055-64g",
            "--expires-days", "30",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        enrollment = payload["data"]
        self.assertEqual(enrollment["code"], "7K3M-FG9A-WX21")
        self.assertEqual(enrollment["name"], "kimi-build03")
        # 服务端收到的 scopes 是数组形式。
        sent = [
            body
            for path, body in ApiHandler.requests
            if path == "/api/auth/agent-enrollment-codes"
        ]
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sent[0]["scopes"],
            ["devices.read", "tests.execute", "jobs.read"],
        )
        self.assertEqual(sent[0]["allowed_workers"], "ats-041055-64g")
        self.assertEqual(sent[0]["expires_days"], 30)

    def test_agent_enroll_code_requires_name(self):
        result = self._run("gms-rt-agent-enroll-code", "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])

    def test_agent_token_revoke_deletes_by_id(self):
        ApiHandler.requests.clear()
        result = self._run("gms-rt-agent-token-revoke", "agt_deadbeef", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["revoked"], "agt_deadbeef")
        # DELETE 请求命中正确的 token 路径。
        self.assertTrue(any(
            path == "/api/auth/agent-tokens/agt_deadbeef"
            for path, _body in ApiHandler.requests
        ))

    def test_agent_token_revoke_requires_id(self):
        result = self._run("gms-rt-agent-token-revoke", "--json")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
