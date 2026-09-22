"""kkagent 会话启动前 gms MCP 健康前置检查（#653167 nightly 复盘）。"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from features.redmine.kkagent import mcp_health
from features.redmine.kkagent.mcp_health import (
    McpHealthProbe,
    parse_doctor_payload,
    probe_kkagent_mcp_health,
)


HEALTHY = {
    "ok": True,
    "clients": [{
        "client": "kkagent",
        "skill_present": True,
        "profile": {"count": 1, "names": ["p1"], "selected": "p1", "valid": True},
        "token": {"configured": True, "present": True, "mode_ok": True, "owner_ok": True},
        "mcp": {"registered": True, "config_path": "/home/u/.kkagent/config.toml"},
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
                "mcp": {"registered": True},
            }],
        }
        ok, reason = parse_doctor_payload(json.dumps(payload).encode())
        self.assertFalse(ok)
        self.assertIn("token 文件权限/属主异常", reason)


class ProbeKkAgentMcpHealthTests(unittest.TestCase):
    def test_unbound_profile_skips_probe_without_spawning(self):
        async def scenario():
            with patch.object(
                mcp_health.asyncio, "create_subprocess_exec", AsyncMock()
            ) as spawn:
                return await probe_kkagent_mcp_health({}), spawn

        probe, spawn = asyncio.run(scenario())
        self.assertTrue(probe.ok)
        self.assertTrue(probe.skipped)
        spawn.assert_not_awaited()

    def test_missing_cli_fails_fast_with_install_hint(self):
        async def scenario():
            with patch.object(
                mcp_health, "doctor_command", return_value=None
            ):
                return await probe_kkagent_mcp_health({"GMS_RT_PROFILE": "p"})

        probe = asyncio.run(scenario())
        self.assertFalse(probe.ok)
        self.assertIn("gms-agent CLI 不存在", probe.reason)

    def test_healthy_doctor_run_passes_with_trace(self):
        async def scenario():
            with patch.object(
                mcp_health, "doctor_command",
                return_value=["gms-agent", "doctor", "--client", "kkagent", "--json"],
            ), patch.object(
                mcp_health.asyncio, "create_subprocess_exec", AsyncMock(
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
                mcp_health, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                mcp_health.asyncio, "create_subprocess_exec", AsyncMock(
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
                mcp_health, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                mcp_health.asyncio, "create_subprocess_exec", AsyncMock(
                    return_value=process
                )
            ), patch.object(
                mcp_health.asyncio, "wait_for", side_effect=instant_timeout
            ), patch.object(
                mcp_health, "terminate_process_tree", AsyncMock()
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
                mcp_health, "doctor_command", return_value=["gms-agent"]
            ), patch.object(
                mcp_health.asyncio, "create_subprocess_exec",
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
