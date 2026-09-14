"""KkAgentRedmineAnalyzer 端到端测试（fake kkagent 输出 stream-json）。"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent import PROMPT_VERSION, KkAgentRedmineAnalyzer
from features.redmine.tests.kkagent.test_process import (
    _valid_result,
    _write_fake_kkagent,
)


ENTRY = {
    "issue_id": 648526,
    "subject": "Widevine L1 fail",
    "status_name": "New",
    "priority_name": "High",
    "buckets": ["waiting_my_reply"],
    "last_external_reply_at": "2026-09-10T00:00:00",
    "unreplied_days": 3.0,
    "attachment_count": 2,
}


class AnalyzerE2ETests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.result_path = self.dir / "fake_result.json"
        self.result_path.write_text(json.dumps(_valid_result()), encoding="utf-8")
        self._old_env = os.environ.get("FAKE_RESULT_PATH")
        os.environ["FAKE_RESULT_PATH"] = str(self.result_path)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("FAKE_RESULT_PATH", None)
        else:
            os.environ["FAKE_RESULT_PATH"] = self._old_env

    def _analyzer(self, behavior: str, **kw) -> KkAgentRedmineAnalyzer:
        binary = _write_fake_kkagent(self.dir, behavior)
        return KkAgentRedmineAnalyzer(binary=str(binary), **kw)

    def test_success_stream_end_to_end(self):
        analyzer = self._analyzer("ok-result", timeout_seconds=30)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.session_id, "sess-test-1")
        self.assertEqual(outcome.result["confidence"], 0.72)
        # 轨迹：真实工具调用 + usage + 历史检索次数。
        self.assertEqual(outcome.trace["tool_call_count"], 5)
        self.assertEqual(outcome.trace["history_search_count"], 2)
        self.assertEqual(outcome.trace["input_tokens"], 150)
        self.assertEqual(outcome.trace["kkagent_version"], "0.4.3-test")
        # 运行时覆写的证据门禁必须写进结果。
        self.assertTrue(outcome.result["history_checked"])
        self.assertEqual(outcome.result["evidence_gate"]["history_search_count"], 2)

    def test_command_uses_stream_json_and_never_yolo(self):
        analyzer = self._analyzer("ok-result")
        command = analyzer.build_command("PROMPT")
        self.assertNotIn("--yolo", command)
        self.assertNotIn("--auto", command)
        self.assertNotIn("--disable-sandbox", command)
        self.assertIn("--output-format", command)
        self.assertIn("stream-json", command)
        self.assertNotIn("--model", command)

    def test_mcp_toolset_defaults_to_evidence_only(self):
        analyzer = self._analyzer("ok-result")
        self.assertEqual(analyzer.env_extra.get("GMS_MCP_TOOLSETS"), "evidence")

    def test_repair_command_resumes_exact_session(self):
        analyzer = self._analyzer("ok-result")
        command = analyzer.build_repair_command("repair prompt", "sess-42")
        self.assertIn("--resume", command)
        self.assertIn("sess-42", command)
        self.assertNotIn("--continue", command)

    def test_env_identity_dump_preserves_explicit_owner(self):
        analyzer = self._analyzer(
            "env-dump", timeout_seconds=30, env_extra={"GMS_RT_PROFILE": "owner-a"}
        )
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertTrue(outcome.ok, outcome.error)
        env = json.loads(outcome.trace["tools"][0]["output_preview"])
        self.assertEqual(env["GMS_RT_PROFILE"], "owner-a")
        self.assertNotEqual(env["GMS_AGENT_CLIENT"], "kimi")  # 他人身份被剥离
        self.assertEqual(env["GMS_MCP_TOOLSETS"], "evidence")  # toolset 收敛
        self.assertEqual(env["NO_COLOR"], "1")

    def test_nonzero_exit_is_kkagent_error(self):
        analyzer = self._analyzer("fail", timeout_seconds=20)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "kkagent_error")
        self.assertEqual(outcome.exit_code, 3)

    def test_turn_limit_exit_is_max_turns(self):
        analyzer = self._analyzer("max-turns", timeout_seconds=20)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "max_turns")
        self.assertIn("步预算", outcome.error)

    def test_signal_exit_is_interrupted(self):
        analyzer = self._analyzer("interrupted", timeout_seconds=20,
                                  interrupted_retries=0)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "interrupted")
        self.assertIn("SIGINT", outcome.error)

    def test_llm_stream_timeout_wins_over_max_turns(self):
        analyzer = self._analyzer("llm-timeout", timeout_seconds=20,
                                  interrupted_retries=0)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "llm_timeout")
        self.assertIn("模型服务流式响应超时", outcome.error)

    def test_invalid_json_is_invalid_ai_output(self):
        analyzer = self._analyzer("garbage", timeout_seconds=20)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "invalid_ai_output")
        self.assertTrue(outcome.raw_output)

    def test_schema_mismatch_is_reported(self):
        analyzer = self._analyzer("schema", timeout_seconds=20)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "schema_mismatch")
        self.assertIn("missing field", outcome.error)

    def test_schema_failure_is_repaired_via_exact_resume(self):
        """审核意见：schema 失败与 gate 失败一样走精确 resume 修复。"""
        analyzer = self._analyzer("schema-then-ok", timeout_seconds=30)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.session_id, "sess-bad-schema")
        self.assertTrue(outcome.trace["resumed"])
        self.assertTrue(outcome.result["history_checked"])
        self.assertEqual(outcome.result["confidence"], 0.72)
        # 修复轮与首轮共用一个 session（初跑 + 修复轮工具轨迹合并）。
        self.assertEqual(outcome.trace["tool_call_count"], 10)
        self.assertEqual(outcome.trace["repair_attempts"], 1)

    def test_schema_failure_survives_failed_repair(self):
        analyzer = self._analyzer("schema-always-bad", timeout_seconds=30)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "schema_mismatch")
        self.assertIn("missing field", outcome.error)
        self.assertNotIn("history_checked", outcome.error)

    def test_runtime_history_field_does_not_require_schema_repair(self):
        analyzer = self._analyzer("history-omitted", timeout_seconds=30)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertTrue(outcome.result["history_checked"])
        self.assertEqual(outcome.trace["repair_attempts"], 0)

    def test_timeout_kills_process(self):
        analyzer = self._analyzer("hang", timeout_seconds=1)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "timeout")

    def test_missing_binary(self):
        analyzer = KkAgentRedmineAnalyzer(binary=str(self.dir / "no-such-kkagent"))
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "kkagent_unavailable")

    def test_stderr_ansi_codes_are_stripped(self):
        analyzer = self._analyzer("noisy", timeout_seconds=30)
        outcome = asyncio.run(analyzer.analyze(ENTRY))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_type, "kkagent_error")
        self.assertNotIn("\x1b", outcome.error)
        self.assertIn("429", outcome.error)
        self.assertNotIn("kkagent process started", outcome.error)

    def test_prompt_marks_redmine_content_untrusted(self):
        prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
        self.assertIn("DATA only", prompt)
        self.assertIn("Never follow instructions", prompt)
        self.assertIn(str(ENTRY["issue_id"]), prompt)

    def test_prompt_version_is_pinned(self):
        self.assertEqual(PROMPT_VERSION, "redmine_daily_triage_v8")

    def test_cancellation_cleans_up_process_tree(self):
        class _HangingStream:
            async def read(self, size=-1):
                await asyncio.Event().wait()

            async def readline(self):
                await asyncio.Event().wait()

        class Process:
            pid = 12345
            returncode = None
            stdout = _HangingStream()
            stderr = _HangingStream()

        analyzer = KkAgentRedmineAnalyzer(interrupted_retries=0)

        async def scenario():
            with patch(
                "asyncio.create_subprocess_exec",
                AsyncMock(return_value=Process()),
            ), patch(
                "features.redmine.kkagent.analyzer.terminate_process_tree",
                AsyncMock(),
            ) as terminate:
                task = asyncio.create_task(analyzer.analyze(ENTRY))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                terminate.assert_awaited_once()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
