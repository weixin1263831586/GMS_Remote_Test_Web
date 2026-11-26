"""GMS authentication and MCP health preflight tests."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from features.redmine.analysis.kkagent import preflight
from features.redmine.analysis.kkagent.preflight import (
    McpHealthProbe,
    parse_doctor_payload,
    probe_kkagent_mcp_health,
)


class PreflightGmsAuthTests(unittest.TestCase):
    """无 profile / selfcheck 不可用 / 超时 / 明确未认证 → 全部拦截。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write_selfcheck(self, payload: str) -> Path:
        script = self.dir / "selfcheck.sh"
        script.write_text(f"#!/bin/sh\necho '{payload}'\n", encoding="utf-8")
        return script

    def _payload(self, authenticated: bool) -> str:
        return json.dumps({
            "ok": authenticated,
            "exit_code": 0 if authenticated else 3,
            "data": {
                "profile": "kkagent-host-x",
                "credential": {"token_file": "/tmp/tok"},
                "auth": {"ok": True,
                         "status": {"authenticated": authenticated}},
            },
        })

    def _preflight(self, script: Path | None, **kw):
        import asyncio

        env = {"GMS_RT_PROFILE": "kkagent-host-x"}
        with patch.object(preflight, "GMS_SELFCHECK_SCRIPT",
                          str(script or self.dir / "nope.sh")), \
                patch("shutil.which", return_value=None):
            return asyncio.run(preflight.preflight_gms_auth(env, **kw))

    def test_no_profile_is_blocked_fail_closed(self):
        """Daily Brief 强制 Redmine 只读取证：无 profile 即 analysis_unavailable。"""
        import asyncio

        ok, reason = asyncio.run(preflight.preflight_gms_auth({}))
        self.assertFalse(ok)
        self.assertIn("analysis_unavailable", reason)
        self.assertIn("agent_profile", reason)

    def test_revoked_token_blocks_with_reenroll_hint(self):
        ok, reason = self._preflight(self._write_selfcheck(self._payload(False)))
        self.assertFalse(ok)
        self.assertIn("kkagent-host-x", reason)
        self.assertIn("/tmp/tok", reason)
        self.assertIn("gms-rt-agent-enroll", reason)

    def test_authenticated_token_passes(self):
        ok, reason = self._preflight(self._write_selfcheck(self._payload(True)))
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_selfcheck_missing_or_garbage_fails_closed(self):
        # 脚本不存在且 PATH 上没有：不能放行"凭上下文猜"的晨报。
        ok, reason = self._preflight(None)
        self.assertFalse(ok)
        self.assertIn("gms-rt-agent-enroll", reason)
        # 输出非法 JSON：同样拦截。
        ok, reason = self._preflight(self._write_selfcheck("not json"))
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_selfcheck_timeout_fails_closed(self):
        script = self.dir / "slow.sh"
        script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
        ok, reason = self._preflight(script, timeout_seconds=0.2)
        self.assertFalse(ok)
        self.assertIn("analysis_unavailable", reason)

    def test_parse_payload_flat_authenticated_fallback(self):
        payload = json.dumps({"data": {"authenticated": True}})
        ok, reason = preflight.parse_selfcheck_payload(payload.encode(), {"GMS_RT_PROFILE": "p"})
        self.assertTrue(ok)
        self.assertEqual(reason, "")


HEALTHY = {
    "ok": True,
    "clients": [{
        "client": "kkagent",
        "skill_present": True,
        "profile": {"count": 1, "names": ["p1"], "selected": "p1", "valid": True},
        "token": {"configured": True, "present": True, "mode_ok": True, "owner_ok": True},
        "mcp": {
            "registered": True,
            "launchable": True,
            "config_path": "/home/u/.kkagent/config.toml",
        },
    }],
}


def _fake_process(stdout: bytes = b"", returncode: int = 0) -> MagicMock:
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(stdout, b""))
    process.returncode = returncode
    return process


class ParseDoctorPayloadTests(unittest.TestCase):
    def test_healthy_payload_passes(self):
        ok, reason = parse_doctor_payload(json.dumps(HEALTHY).encode())
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_invalid_json_fails_with_tail(self):
        ok, reason = parse_doctor_payload(b"<html>boom</html>")
        self.assertFalse(ok)
        self.assertIn("不是合法 JSON", reason)
        self.assertIn("boom", reason)

    def test_doctor_not_ok_surfaces_actions(self):
        payload = {"ok": False, "actions": ["run gms-agent install"]}
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("部署异常", reason)
        self.assertIn("run gms-agent install", reason)
        self.assertIn("sync_package", reason)

    def test_missing_kkagent_client_names_the_gap(self):
        payload = {"ok": True, "clients": [{"client": "codex", "skill_present": True}]}
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("未包含 kkagent", reason)

    # #654649 nightly 复盘：老版 doctor 只查注册文本，payload 被清空也报
    # healthy。新版 doctor 的 launchable=False 必须拦下并给出恢复路径；
    # 旧版 payload（无 launchable 字段）无法证明 launcher 存在，必须失败。
    def test_registered_but_missing_launcher_fails_probe(self):
        payload = json.loads(json.dumps(HEALTHY))
        payload["clients"][0]["mcp"] = {
            "registered": True,
            "launchable": False,
            "launcher_path": "/home/u/.kkagent/plugins/local/gms-remote-test/scripts/mcp_launcher.py",
            "config_path": "/home/u/.kkagent/config.toml",
        }
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("启动脚本缺失", reason)
        self.assertIn("mcp_launcher.py", reason)
        self.assertIn("sync_package", reason)

    def test_missing_launchable_field_fails_closed(self):
        payload = json.loads(json.dumps(HEALTHY))
        payload["clients"][0]["mcp"].pop("launchable")
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("无法确认可启动性", reason)

    def test_each_unhealthy_bit_is_reported(self):
        payload = {
            "ok": True,
            "clients": [{
                "client": "kkagent",
                "skill_present": False,
                "profile": {"count": 0, "valid": False},
                "token": {"present": False},
                "mcp": {"registered": False},
            }],
        }
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("gms skill 未安装", reason)
        self.assertIn("profile 无效", reason)
        self.assertIn("agent token 缺失", reason)
        self.assertIn("MCP server 未注册", reason)
        self.assertIn("sync_package", reason)

    def test_bad_token_permissions_are_reported(self):
        payload = {
            "ok": True,
            "clients": [{
                "client": "kkagent",
                "skill_present": True,
                "profile": {"count": 1, "valid": True},
                "token": {"present": True, "mode_ok": False, "owner_ok": True},
                "mcp": {"registered": True, "launchable": True},
            }],
        }
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("token 文件权限/属主异常", reason)


class ProbeKkAgentMcpHealthTests(unittest.TestCase):
    def test_unbound_profile_skips_probe_without_spawning(self):
        async def scenario():
            with patch.object(
                preflight.asyncio, "create_subprocess_exec", AsyncMock()
            ) as spawn:
                return await probe_kkagent_mcp_health({}), spawn

        probe, spawn = asyncio.run(scenario())
        self.assertTrue(probe.ok)
        self.assertTrue(probe.skipped)
        spawn.assert_not_awaited()

    def test_missing_cli_fails_fast_with_install_hint(self):
        async def scenario():
            with patch.object(
                preflight, "doctor_command", return_value=None
            ):
                return await probe_kkagent_mcp_health({"GMS_RT_PROFILE": "p"})

        probe = asyncio.run(scenario())
        self.assertFalse(probe.ok)
        self.assertIn("gms-agent CLI 不存在", probe.reason)

    def test_healthy_doctor_run_passes_with_trace(self):
        async def scenario():
            with patch.object(
                preflight, "doctor_command",
                return_value=["gms-agent", "doctor", "--client", "kkagent", "--json"],
            ), patch.object(
                preflight.asyncio, "create_subprocess_exec", AsyncMock(
                    return_value=_fake_process(json.dumps(HEALTHY).encode())
                )
            ) as spawn:
                probe = await probe_kkagent_mcp_health({"GMS_RT_PROFILE": "p"})
                return probe, spawn

        probe, spawn = asyncio.run(scenario())
        self.assertTrue(probe.ok)
        self.assertFalse(probe.skipped)
        self.assertEqual(spawn.await_count, 1)
        self.assertGreater(probe.output_bytes, 0)
        self.assertTrue(probe.output_sha256)
        trace = probe.tool_trace()
        self.assertEqual(trace.status, "succeeded")
        self.assertTrue(trace.tool_call_id.startswith("preflight:mcp_doctor:"))

    def test_unhealthy_doctor_run_blocks_with_reason(self):
        async def scenario():
            payload = {**HEALTHY, "clients": [{
                **HEALTHY["clients"][0], "mcp": {"registered": False},
            }]}
            with patch.object(
                preflight, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                preflight.asyncio, "create_subprocess_exec", AsyncMock(
                    return_value=_fake_process(json.dumps(payload).encode())
                )
            ):
                return await probe_kkagent_mcp_health({"GMS_RT_PROFILE": "p"})

        probe = asyncio.run(scenario())
        self.assertFalse(probe.ok)
        self.assertIn("MCP server 未注册", probe.reason)
        self.assertIn("sync_package", probe.reason)
        trace = probe.tool_trace()
        self.assertEqual(trace.status, "failed")
        self.assertEqual(trace.failure_kind, "mcp_unavailable")

    def test_doctor_timeout_kills_process_and_blocks(self):
        process = _fake_process()

        async def instant_timeout(stream, timeout=None):
            raise asyncio.TimeoutError()

        async def scenario():
            with patch.object(
                preflight, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                preflight.asyncio, "create_subprocess_exec", AsyncMock(
                    return_value=process
                )
            ), patch.object(
                preflight.asyncio, "wait_for", side_effect=instant_timeout
            ), patch.object(
                preflight, "terminate_process_tree", AsyncMock()
            ) as terminate:
                return await probe_kkagent_mcp_health(
                    {"GMS_RT_PROFILE": "p"}, timeout_seconds=0.01
                ), terminate

        probe, terminate = asyncio.run(scenario())
        self.assertFalse(probe.ok)
        self.assertIn("超时", probe.reason)
        terminate.assert_awaited_once()

    def test_doctor_spawn_oserror_blocks(self):
        async def scenario():
            with patch.object(
                preflight, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                preflight.asyncio, "create_subprocess_exec",
                AsyncMock(side_effect=OSError("no exec")),
            ):
                return await probe_kkagent_mcp_health({"GMS_RT_PROFILE": "p"})

        probe = asyncio.run(scenario())
        self.assertFalse(probe.ok)
        self.assertIn("启动失败", probe.reason)

    def test_probe_is_dataclass_default_ok_false(self):
        probe = McpHealthProbe(ok=False, reason="x")
        self.assertFalse(probe.skipped)

if __name__ == "__main__":
    unittest.main()
