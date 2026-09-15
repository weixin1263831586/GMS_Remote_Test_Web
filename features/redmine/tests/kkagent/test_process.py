"""kkagent 子进程基础设施测试：截断读取 / 环境清洗 / 进程组隔离。"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.kkagent.process import (
    CAPTURE_HEAD_BYTES,
    CAPTURE_TAIL_BYTES,
    CappedCapture,
    child_env,
    read_stream_capped,
)


class StreamCaptureTests(unittest.TestCase):
    def _feed_and_collect(self, payload: bytes) -> bytes:
        async def scenario():
            reader = asyncio.StreamReader()
            reader.feed_data(payload)
            reader.feed_eof()
            return await read_stream_capped(reader)

        return asyncio.run(scenario())

    def test_stream_capture_is_memory_capped(self):
        payload = (b"x" * 65536) * 40  # 2.5MB，远超 head+tail 上限
        captured = self._feed_and_collect(payload)
        self.assertLessEqual(len(captured), CAPTURE_HEAD_BYTES + CAPTURE_TAIL_BYTES + 64)
        self.assertIn(b"...[truncated]...", captured)
        self.assertTrue(captured.startswith(b"x"))
        self.assertTrue(captured.endswith(b"x"))

    def test_stream_capture_small_output_untouched(self):
        captured = self._feed_and_collect(b'{"result": {"ok": true}}')
        self.assertEqual(captured, b'{"result": {"ok": true}}')

    def test_capped_capture_accepts_text_chunks(self):
        capture = CappedCapture(head=8, tail=8)
        capture.feed("中文ab")
        capture.feed("cd")
        self.assertTrue(capture.text())


class ChildEnvTests(unittest.TestCase):
    def test_child_env_strips_inherited_mcp_identity(self):
        leaked = {
            "GMS_RT_PROFILE": "other-owner",
            "GMS_AGENT_CLIENT": "kimi",
            "GMS_AGENT_AUTH_MODE": "service-token",
            "GMS_AUTH_TOKEN_FILE": "/tmp/other-owner.token",
            "GMS_REMOTE_TEST_SERVER": "https://controller.example",
            "GMS_MCP_TOOLSETS": "core,admin",
        }
        old_env = {k: os.environ.get(k) for k in leaked}
        try:
            os.environ.update(leaked)
            env = child_env({"GMS_RT_PROFILE": "owner-a"})
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(env["GMS_RT_PROFILE"], "owner-a")
        # 泄漏的宿主身份要么不存在、要么为空，绝不允许出现他人值。
        self.assertNotEqual(env.get("GMS_AGENT_CLIENT"), "kimi")
        self.assertNotIn("other-owner.token", env.get("GMS_AUTH_TOKEN_FILE", ""))
        self.assertEqual(env.get("GMS_REMOTE_TEST_SERVER", ""), "")
        # 继承的 toolset 必须被剥离（由晨报侧显式注入 evidence）。
        self.assertNotEqual(env.get("GMS_MCP_TOOLSETS"), "core,admin")
        self.assertEqual(env["NO_COLOR"], "1")


class SubprocessIsolationTests(unittest.TestCase):
    """kkagent 必须运行在独立 POSIX 进程组（取消时可整组清理）。"""

    ENTRY = {"issue_id": 1}

    def test_subprocess_starts_in_an_independent_posix_session(self):
        from features.redmine.kkagent import KkAgentRedmineAnalyzer

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        binary = _write_fake_kkagent(Path(directory.name), "ok-result")
        analyzer = KkAgentRedmineAnalyzer(binary=str(binary), timeout_seconds=30)
        seen = {}
        original = asyncio.create_subprocess_exec

        async def capture(*args, **kwargs):
            seen.update(kwargs)
            return await original(*args, **kwargs)

        with patch("asyncio.create_subprocess_exec", capture):
            outcome = asyncio.run(analyzer.analyze(self.ENTRY))
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(seen["start_new_session"], os.name == "posix")


def _write_fake_kkagent(directory: Path, behavior: str) -> Path:
    """生成行为可控的假 kkagent：输出 stream-json NDJSON 事件流。"""
    script = directory / f"kkagent-{behavior}"
    result_literal = json.dumps(_valid_result(), ensure_ascii=False)
    bodies = {
        # 完整事件流：system → session → tool_call/result → usage → result。
        "ok-result": f"import json, sys\n_emit(sys.stdout, json.loads({result_literal!r}), 'sess-test-1')\n",
        "fail": "import sys; sys.stderr.write('model unreachable'); sys.exit(3)",
        "garbage": "print('not json at all')",
        "schema": (
            "import json, sys\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', "
            "'exit_code': 0, 'message': json.dumps({'problem_summary': 'x'})}))\n"
        ),
        "hang": "import time; time.sleep(30)",
        "interrupted": (
            "import sys\n"
            "sys.stderr.write('Traceback (most recent call last):\\nKeyboardInterrupt\\n')\n"
            "sys.exit(130)\n"
        ),
        "max-turns": (
            "import json, sys\n"
            "sys.stderr.write('Agent turn limit reached for session x\\n')\n"
            "print(json.dumps({'type': 'result', 'subtype': 'max_turns', "
            "'exit_code': 3, 'message': 'partial work only'}))\n"
            "sys.exit(3)\n"
        ),
        "llm-timeout": (
            "import json, sys\n"
            "sys.stderr.write('ERROR kkagent_core::agent_loop: LLM stream error: "
            "error sending request for url (...) [kind=request, kind=timeout]: "
            "operation timed out\\n')\n"
            "print(json.dumps({'type': 'result', 'subtype': 'max_turns', "
            "'exit_code': 3, 'message': 'partial'}))\n"
            "sys.exit(3)\n"
        ),
        # stderr ANSI 噪音 + 429，非零退出。
        "noisy": (
            "import sys\n"
            "sys.stderr.write('\\x1b[2m2026-09-12T06:47:45Z\\x1b[0m INFO kkagent process started\\n')\n"
            "sys.stderr.write('\\x1b[31mERROR\\x1b[0m LLM stream error: API returned HTTP 429"
            " Too Many Requests\\n')\n"
            "sys.exit(3)\n"
        ),
        # 环境回显（身份清洗断言）：完整工具轨迹 + env 回显为 issue 输出。
        "env-dump": (
            "import json, os, sys\n"
            "env = {k: os.environ.get(k, '') for k in "
            "['GMS_RT_PROFILE', 'GMS_AGENT_CLIENT', 'GMS_AGENT_AUTH_MODE', "
            "'GMS_AUTH_TOKEN_FILE', 'GMS_REMOTE_TEST_SERVER', 'GMS_MCP_TOOLSETS', 'NO_COLOR']}\n"
            "_emit_env(sys.stdout, env, "
            f"json.loads({result_literal!r}))\n"
        ),
        # 首轮缺字段（schema 失败）；--resume 修复轮返回完整合法 JSON
        # （resume 延续同一 session，session_id 保持不变）。
        "schema-then-ok": (
            "import json, sys\n"
            "if '--resume' in sys.argv:\n"
            f"    _emit(sys.stdout, json.loads({result_literal!r}), 'sess-bad-schema')\n"
            "else:\n"
            f"    _emit_bad(sys.stdout, json.loads({result_literal!r}))\n"
        ),
        # 首轮和第一次修复都缺字段，第二次同 session 修复才返回合法 JSON。
        "schema-twice-then-ok": (
            "import json, os, pathlib, sys\n"
            "counter = pathlib.Path(os.environ['FAKE_RESULT_PATH']).with_name('repair-count')\n"
            "count = int(counter.read_text() if counter.exists() else '0')\n"
            "counter.write_text(str(count + 1))\n"
            "if '--resume' in sys.argv and count >= 2:\n"
            f"    _emit(sys.stdout, json.loads({result_literal!r}), 'sess-bad-schema')\n"
            "else:\n"
            f"    _emit_bad(sys.stdout, json.loads({result_literal!r}))\n"
        ),
        # 首轮与修复轮都缺字段：schema 修复失败，保持 schema_mismatch。
        "schema-always-bad": (
            "import json, sys\n"
            f"_emit_bad(sys.stdout, json.loads({result_literal!r}))\n"
        ),
        # 模型在完整结果里漏掉 confidence，但根因置信枚举仍在：Controller
        # 可作受控确定性映射，无需重跑取证或消耗 resume 预算。
        "missing-confidence-with-root-type": (
            "import json, sys\n"
            f"result = json.loads({result_literal!r})\n"
            "result.pop('confidence', None)\n"
            "_emit(sys.stdout, result, 'sess-missing-confidence')\n"
        ),
        "history-omitted": (
            "import json, sys\n"
            f"result = json.loads({result_literal!r})\n"
            "result.pop('history_checked', None)\n"
            "_emit(sys.stdout, result, 'sess-no-history-field')\n"
        ),
    }
    helper = (
        "import json\n"
        "def _tool(stream, call_id, name, output, tool_input=None):\n"
        "    stream.write(json.dumps({'type': 'tool_call', 'tool_call_id': call_id, "
        "'tool_name': name, 'input': tool_input or {}}) + '\\n')\n"
        "    stream.write(json.dumps({'type': 'tool_result', 'tool_call_id': call_id, "
        "'is_error': False, 'output': output}) + '\\n')\n"
        "def emit_result(stream, result, session_id):\n"
        "    stream.write(json.dumps({'type': 'result', 'subtype': 'success', "
        "'exit_code': 0, 'session_id': session_id, 'duration_ms': 1234, "
        "'rounds': 4, 'turns': 1, "
        "'usage': {'input_tokens': 150, 'output_tokens': 20, "
        "'cache_read_input_tokens': 5}, "
        "'message': json.dumps(result, ensure_ascii=False)}) + '\\n')\n"
        "def _emit(stream, result, session_id, prefix='c'):\n"
        "    stream.write(json.dumps({'type': 'system', 'version': '0.4.3-test'}) + '\\n')\n"
        "    stream.write(json.dumps({'type': 'session', 'session_id': session_id}) + '\\n')\n"
        "    _tool(stream, prefix + '1', 'gms_rt_redmine_issue_fetch', 'issue body')\n"
        "    _tool(stream, prefix + '2', 'gms_rt_redmine_journals', 'journals')\n"
        "    _tool(stream, prefix + '3', 'gms_rt_redmine_attachments', "
        "json.dumps({'data': {'artifacts': [{'artifact_id': 'a1', 'kind': 'image', 'status': 'ready'}, {'artifact_id': 'a2', 'kind': 'image', 'status': 'ready'}]}}))\n"
        "    _tool(stream, prefix + '4', 'gms_rt_redmine_history_search', "
        "json.dumps({'items': [{'issue_id': 646504}]}), {'q': 'Widevine L1'})\n"
        "    _tool(stream, prefix + '5', 'gms_rt_redmine_history_search', "
        "json.dumps({'items': [{'issue_id': 646504}]}), {'q': 'CTS DRM'})\n"
        "    stream.write(json.dumps({'type': 'usage', 'usage': "
        "{'input_tokens': 100, 'output_tokens': 10}}) + '\\n')\n"
        "    emit_result(stream, result, session_id)\n"
        "def _emit_env(stream, env, result):\n"
        "    stream.write(json.dumps({'type': 'system', 'version': '0.4.3-test'}) + '\\n')\n"
        "    stream.write(json.dumps({'type': 'session', 'session_id': 'env-session'}) + '\\n')\n"
        "    _tool(stream, 'c1', 'gms_rt_redmine_issue_fetch', json.dumps(env))\n"
        "    _tool(stream, 'c2', 'gms_rt_redmine_journals', 'journals')\n"
        "    _tool(stream, 'c3', 'gms_rt_redmine_attachments', "
        "json.dumps({'data': {'artifacts': [{'artifact_id': 'a1', 'kind': 'image', 'status': 'ready'}, {'artifact_id': 'a2', 'kind': 'image', 'status': 'ready'}]}}))\n"
        "    _tool(stream, 'c4', 'gms_rt_redmine_history_search', "
        "json.dumps({'items': [{'issue_id': 646504}]}), {'q': 'Widevine L1'})\n"
        "    _tool(stream, 'c5', 'gms_rt_redmine_history_search', "
        "json.dumps({'items': [{'issue_id': 646504}]}), {'q': 'CTS DRM'})\n"
        "    emit_result(stream, result, 'env-session')\n"
        "def _emit_bad(stream, result):\n"
        "    result.pop('history_checked', None)\n"
        "    result.pop('suggested_solution', None)\n"
        "    _emit(stream, result, 'sess-bad-schema', 'b')\n"
    )
    script.write_text(
        "#!/usr/bin/env python3\n" + helper + bodies[behavior], encoding="utf-8"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _valid_result() -> dict:
    return {
        "problem_summary": "Widevine L1 初始化失败",
        "customer_request": "请定位 CTS DRM 失败原因",
        "current_blocker": "缺少 bugreport",
        "root_cause": "liboemcrypto 版本与 TA 不匹配",
        "root_cause_type": "likely",
        "evidence": [{"source": "journal", "reference": "#12", "fact": "客户提供了失败截图"}],
        "recommended_actions": [{"step": 1, "action": "复现并抓取 bugreport", "reason": "确认 TA 版本"}],
        "suggested_solution": "升级 liboemcrypto 后重跑 CtsMediaTestCases",
        "similar_issues": [
            {"issue_id": 646504, "subject": "RK3576 Widevine L1", "similarity": "similar",
             "reusable_fix": "同版本升级方案可参考", "reference_fact": "结案说明 #20"},
        ],
        "history_checked": True,
        "missing_information": ["bugreport"],
        "suggested_reply_en": "Could you please provide a bugreport?",
        "suggested_reply_zh": "请提供一份 bugreport 以便定位。",
        "detailed_report": "## 一、问题概况\n\n| 项目 | 内容 |",
        "risk": "medium",
        "confidence": 0.72,
    }


if __name__ == "__main__":
    unittest.main()
