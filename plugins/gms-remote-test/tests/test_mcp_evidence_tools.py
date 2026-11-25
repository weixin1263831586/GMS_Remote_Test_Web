"""MCP typed evidence tools: Redmine, APK, SDK and image contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


# In the source tree this test lives under agent/gms-remote-test/
# tests/ and imports the runtime from agent/gms-remote-test/runtime; the
# synced copy under plugins/gms-remote-test/tests/ uses scripts/.
_TEST_DIR = Path(__file__).resolve().parent
_AGENT_PKG = _TEST_DIR.parent
if (_AGENT_PKG / "runtime" / "mcp_server.py").is_file():
    RUNTIME_DIR = _AGENT_PKG / "runtime"
else:  # generated-plugin copy
    RUNTIME_DIR = _TEST_DIR.parent / "scripts"
sys.path.insert(0, str(RUNTIME_DIR))

import mcp_server  # noqa: E402


class RedmineEvidenceToolTests(unittest.TestCase):
    """Redmine/apk/sdk typed tools wiring."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._original_cli = mcp_server.cli_script
        self.cli_path = Path(self._tmp.name) / "gms-remote-test.sh"
        self.cli_path.write_text('#!/bin/bash\necho \'{"ok":true,"exit_code":0,"data":{}}\'\nexit 0\n')
        self.cli_path.chmod(0o755)
        mcp_server.cli_script = lambda: self.cli_path

    def tearDown(self):
        mcp_server.cli_script = self._original_cli

    def _capture_run(self) -> dict:
        captured = {}

        def fake_run(command, args=None, **_kwargs):
            captured["command"] = command
            captured["args"] = args
            return "{}", False

        original = mcp_server.run_cli
        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        return captured

    def test_fetch_requires_issue(self):
        text, is_error = mcp_server.redmine_issue_fetch_tool({})
        self.assertTrue(is_error)
        self.assertIn("issue", text)

    def test_fetch_maps_arguments(self):
        captured = self._capture_run()
        mcp_server.redmine_issue_fetch_tool({
            "issue": "648526", "download": "all", "wait": True, "max_wait": 60,
        })
        self.assertEqual(captured["command"], "gms-rt-redmine-issue-fetch")
        self.assertEqual(
            captured["args"],
            ["648526", "--download", "all", "--wait", "--max-wait", "60"],
        )

    def test_fetch_maps_dry_run(self):
        captured = self._capture_run()
        mcp_server.redmine_issue_fetch_tool({"issue": "648526", "dry_run": True})
        self.assertEqual(
            captured["args"], ["648526", "--dry-run"]
        )

    def test_fetch_rejects_bad_download(self):
        _, is_error = mcp_server.redmine_issue_fetch_tool({
            "issue": "1", "download": "everything",
        })
        self.assertTrue(is_error)

    def test_journals_pagination_args(self):
        captured = self._capture_run()
        mcp_server.redmine_journals_tool({
            "snapshot_id": "SNAP", "limit": 100, "cursor": "100",
        })
        self.assertEqual(
            captured["args"], ["SNAP", "--limit", "100", "--cursor", "100"],
        )

    def test_artifact_search_maps_filters_context_collapse(self):
        captured = self._capture_run()
        _, is_error = mcp_server.redmine_artifact_search_tool({
            "snapshot_id": "ev_1", "query": "LoadSdkException",
            "artifact": "logcat.zip", "member": "main",
            "context": 400, "collapse": True,
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"],
            ["ev_1", "LoadSdkException", "--artifact", "logcat.zip",
             "--member", "main", "--context", "400", "--collapse"],
        )

    def test_artifact_search_batch_maps_filters(self):
        captured = self._capture_run()
        _, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1", "queries": ["a", "b"],
            "artifact": "776657", "context": 0, "collapse": True,
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"],
            ["ev_1", "a", "b", "--artifact", "776657",
             "--context", "0", "--collapse"],
        )

    def test_artifact_read_allows_negative_offset(self):
        captured = self._capture_run()
        _, is_error = mcp_server.redmine_artifact_read_tool({
            "artifact_id": "art_x", "offset": -65536, "limit": 4096,
        })
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["art_x", "--offset", "-65536", "--limit", "4096"])

        # 非负约束只对 search 侧 offset 生效，read 放行负值。
        _, is_error = mcp_server.redmine_artifact_read_tool({
            "artifact_id": "art_x", "offset": -1,
        })
        self.assertFalse(is_error)

    def test_attachment_download_passes_output_path(self):
        captured = self._capture_run()
        text, is_error = mcp_server.redmine_attachment_download_tool({"artifact_id": "art_x"})
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["art_x"])

        mcp_server.redmine_attachment_download_tool({
            "artifact_id": "art_x", "output_path": "/tmp/a.bin",
        })
        self.assertEqual(captured["args"], ["art_x", "/tmp/a.bin"])

        text, is_error = mcp_server.redmine_attachment_download_tool({})
        self.assertTrue(is_error)
        self.assertIn("artifact_id", text)

    def test_artifact_failures_requires_and_maps_id(self):
        captured = self._capture_run()
        text, is_error = mcp_server.redmine_artifact_failures_tool({})
        self.assertTrue(is_error)
        self.assertIn("artifact_id", text)

        mcp_server.redmine_artifact_failures_tool({"artifact_id": "art_r"})
        self.assertEqual(captured["command"], "gms-rt-redmine-artifact-failures")
        self.assertEqual(captured["args"], ["art_r"])

    def test_sdk_search_revision_optional(self):
        captured = self._capture_run()
        _, is_error = mcp_server.sdk_search_tool({
            "source": "aosp-main", "query": "BinderProxy",
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"],
            ["--source", "aosp-main", "--query", "BinderProxy"],
        )

        mcp_server.sdk_search_tool({
            "source": "aosp-main", "revision": "android-16", "query": "BinderProxy",
        })
        self.assertEqual(
            captured["args"],
            ["--source", "aosp-main", "--query", "BinderProxy",
             "--revision", "android-16"],
        )

        _, is_error = mcp_server.sdk_search_tool({"source": "s"})
        self.assertTrue(is_error)

    def test_artifact_search_batch_args_and_validation(self):
        # 多关键词单遍搜索：一次 CLI 往返完成全部关键词匹配。
        captured = self._capture_run()
        text, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1",
            "queries": ["process died", "am_kill", " OOM ", ""],
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"],
            ["ev_1", "process died", "am_kill", "OOM"],
        )

        text, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1", "queries": [],
        })
        self.assertTrue(is_error)
        self.assertIn("queries", text)

        text, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1",
            "queries": [f"k{i}" for i in range(17)],
        })
        self.assertTrue(is_error)
        self.assertIn("16", text)

        text, is_error = mcp_server.redmine_artifact_search_batch_tool({})
        self.assertTrue(is_error)
        self.assertIn("snapshot_id", text)

        captured = self._capture_run()
        text, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1", "queries": ["oom"], "per_query_limit": 50,
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"], ["ev_1", "oom", "--per-query-limit", "50"],
        )

    def test_artifact_read_window_args(self):
        captured = self._capture_run()
        mcp_server.redmine_artifact_read_tool({
            "artifact_id": "ART", "offset": 4096, "limit": 8192,
        })
        self.assertEqual(
            captured["args"], ["ART", "--offset", "4096", "--limit", "8192"],
        )

    def test_artifact_read_accepts_offsets_beyond_one_billion(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.redmine_artifact_read_tool({
            "artifact_id": "ART", "offset": 2_000_000_000,
        })
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["ART", "--offset", "2000000000"])

    def test_artifact_search_pagination_args(self):
        captured = self._capture_run()
        _text, is_error = mcp_server.redmine_artifact_search_tool({
            "snapshot_id": "ev_1", "query": "lmkd", "offset": 200,
        })
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["ev_1", "lmkd", "--offset", "200"])
        _text, is_error = mcp_server.redmine_artifact_search_batch_tool({
            "snapshot_id": "ev_1", "queries": ["lmkd"], "per_query_offset": 100,
        })
        self.assertFalse(is_error)
        self.assertEqual(captured["args"], ["ev_1", "lmkd", "--per-query-offset", "100"])

    def test_artifact_read_accepts_large_but_valid_offset(self):
        # Controller 端 offset 无上限：适配层只做合理性防呆（1e9）。
        captured = self._capture_run()
        _text, is_error = mcp_server.redmine_artifact_read_tool({
            "artifact_id": "ART", "offset": 50_000_000,
        })
        self.assertFalse(is_error)
        self.assertEqual(
            captured["args"], ["ART", "--offset", "50000000"],
        )

    def test_artifact_read_rejects_non_integer_offset(self):
        text, is_error = mcp_server.redmine_artifact_read_tool({
            "artifact_id": "ART", "offset": "abc",
        })
        self.assertTrue(is_error)
        self.assertIn("integer", text)

    def test_image_tool_returns_mcp_image_content(self):
        captured = self._capture_run()

        def fake_run(command, args=None, **_kwargs):
            captured["command"] = command
            captured["args"] = args
            return json.dumps({
                "ok": True,
                "data": {
                    "artifact_id": "ART",
                    "mime_type": "image/png",
                    "base64": "aVZCUg==",
                    "sha256": "ff" * 32,
                    "size_bytes": 95,
                    "scaled": False,
                },
            }), False

        mcp_server.run_cli = fake_run
        result = mcp_server.redmine_image_tool({"artifact_id": "ART"})
        self.assertIsInstance(result, mcp_server.ToolContent)
        types = [item["type"] for item in result.items]
        self.assertEqual(types, ["text", "image"])
        image = result.items[1]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(image["data"], "aVZCUg==")
        self.assertFalse(result.is_error)
        self.assertEqual(captured["command"], "gms-rt-redmine-artifact-image")

    def test_image_tool_requires_artifact(self):
        result = mcp_server.redmine_image_tool({})
        self.assertTrue(result.is_error)

    def test_image_tool_empty_payload_retries_then_returns_error(self):
        original = mcp_server.run_cli
        calls = []

        def fake_run(command, args=None, **_kwargs):
            calls.append((command, args))
            return json.dumps({
                "ok": True,
                "data": {"artifact_id": "ART", "mime_type": "image/png", "base64": ""},
            }), False

        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.redmine_image_tool({"artifact_id": "ART"})
        self.assertEqual(len(calls), 2)
        self.assertTrue(result.is_error)
        self.assertIn("已自动重试 1 次", result.items[0]["text"])

    def test_image_tool_non_json_output_remains_diagnostic_text(self):
        original = mcp_server.run_cli
        mcp_server.run_cli = lambda *_a, **_k: ("plain diagnostic", False)
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.redmine_image_tool({"artifact_id": "ART"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.items, [{"type": "text", "text": "plain diagnostic"}])

    @staticmethod
    def _fake_image_envelope():
        return json.dumps({
            "ok": True,
            "data": {
                "artifact_id": "ART",
                "mime_type": "image/png",
                "base64": base64.b64encode(b"\x89PNG-fixture").decode(),
                "sha256": "ff" * 32,
                "size_bytes": 12,
                "scaled": False,
            },
        }), False

    def test_image_tool_as_file_returns_path_without_image_content(self):
        original = mcp_server.run_cli
        mcp_server.run_cli = lambda *a, **k: self._fake_image_envelope()
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.redmine_image_tool({"artifact_id": "ART", "as_file": True})
        self.assertFalse(result.is_error)
        # Exactly one text item — no inline image content is delivered.
        self.assertEqual([item["type"] for item in result.items], ["text"])
        payload = json.loads(result.items[0]["text"].splitlines()[0])
        self.addCleanup(os.unlink, payload["path"])
        self.assertEqual(payload["artifact_id"], "ART")
        self.assertEqual(payload["mime_type"], "image/png")
        self.assertEqual(payload["size_bytes"], len(b"\x89PNG-fixture"))
        self.assertEqual(
            payload["file_sha256"],
            hashlib.sha256(b"\x89PNG-fixture").hexdigest(),
        )
        # The artifact's own hash from the envelope must survive untouched.
        self.assertEqual(payload["sha256"], "ff" * 32)
        path = Path(payload["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(path.read_bytes(), b"\x89PNG-fixture")
        self.assertIn("as_file", result.items[0]["text"])

    def test_image_tool_as_file_rejects_corrupt_base64(self):
        original = mcp_server.run_cli

        def fake_run(*_a, **_k):
            return json.dumps({
                "ok": True,
                "data": {"artifact_id": "ART", "mime_type": "image/png",
                         "base64": "!!!not-base64!!!"},
            }), False

        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.redmine_image_tool({"artifact_id": "ART", "as_file": True})
        self.assertTrue(result.is_error)
        self.assertIn("base64", result.items[0]["text"])

    def test_screencap_tool_as_file_returns_path_without_image_content(self):
        original = mcp_server.run_cli

        def fake_run(*_a, **_k):
            return json.dumps({
                "ok": True,
                "data": {
                    "device_id": "RK3562GMS7",
                    "mime_type": "image/png",
                    "base64": base64.b64encode(b"\x89PNG-screen").decode(),
                },
            }), False

        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.devices_screencap_tool({
            "device": "RK3562GMS7", "as_file": True,
        })
        payload = json.loads(result.items[0]["text"].splitlines()[0])
        self.addCleanup(os.unlink, payload["path"])
        self.assertFalse(result.is_error)
        self.assertEqual([item["type"] for item in result.items], ["text"])
        self.assertEqual(payload["device_id"], "RK3562GMS7")
        self.assertTrue(Path(payload["path"]).is_file())

    def test_screencap_tool_default_still_returns_inline_image(self):
        original = mcp_server.run_cli

        def fake_run(*_a, **_k):
            return json.dumps({
                "ok": True,
                "data": {
                    "device_id": "RK3562GMS7",
                    "mime_type": "image/png",
                    "base64": "aVZCUg==",
                },
            }), False

        mcp_server.run_cli = fake_run
        self.addCleanup(lambda: setattr(mcp_server, "run_cli", original))
        result = mcp_server.devices_screencap_tool({"device": "RK3562GMS7"})
        self.assertEqual(
            [item["type"] for item in result.items], ["text", "image"]
        )

    def test_image_tools_declare_as_file_in_schema(self):
        by_name = {tool["name"]: tool for tool in mcp_server.tools()}
        for name in ("gms_rt_devices_screencap", "gms_rt_redmine_image"):
            schema = by_name[name]["inputSchema"]
            self.assertIn("as_file", schema["properties"])
            self.assertEqual(schema["properties"]["as_file"]["type"], "boolean")

    def test_sdk_read_requires_result_id_only(self):
        # read 只接受自包含 opaque result_id。
        text, is_error = mcp_server.sdk_read_tool({})
        self.assertTrue(is_error)
        self.assertIn("result_id", text)
        captured = self._capture_run()
        mcp_server.sdk_read_tool({
            "result_id": "src1_AAAA_BBBB", "offset": 10, "limit": 50,
        })
        self.assertEqual(captured["args"], ["src1_AAAA_BBBB", "--offset", "10", "--limit", "50"])
        self.assertEqual(captured["command"], "gms-rt-sdk-read")

    def test_sdk_search_and_read_args(self):
        captured = self._capture_run()
        mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main", "query": "testMethod",
        })
        self.assertEqual(
            captured["args"],
            ["--source", "android14", "--query", "testMethod", "--revision", "main"],
        )
        mcp_server.sdk_read_tool({
            "result_id": "src_x", "source": "android14", "path": "a.java",
            "commit": "c" * 40, "offset": 10, "limit": 100,
        })
        self.assertEqual(
            captured["args"][-4:], ["--offset", "10", "--limit", "100"],
        )

    def test_targeted_sdk_repair_forces_exact_cli_search(self):
        captured = self._capture_run()
        target = "frameworks/base/Test.java"
        arguments = {"source": "sdk", "revision": "HEAD", "query": "Test",
                     "path": target, "repo_scope": "frameworks/base"}
        with (
            patch.dict(os.environ, {"GMS_MCP_REPAIR_POLICY": json.dumps({"mode": "targeted_exact", "targets": [target]})}),
            patch.object(mcp_server, "_REPAIR_ISSUED_SEARCHES", set()),
            patch.object(mcp_server, "response"),
        ):
            mcp_server.handle({"id": 42, "method": "tools/call",
                               "params": {"name": "gms_rt_sdk_search", "arguments": arguments}})
        self.assertEqual(captured["command"], "gms-rt-sdk-search")
        self.assertEqual(captured["args"][-5:], ["--path", target, "--repo-scope", "frameworks/base", "--exact-path"])

    def test_sdk_search_accepts_keywords_alias(self):
        # codesearch 风格的 keywords 等价于 query；两者都缺时报错点名别名。
        captured = self._capture_run()
        mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main", "keywords": "getDataType",
        })
        self.assertEqual(
            captured["args"],
            ["--source", "android14", "--query", "getDataType", "--revision", "main"],
        )
        _, is_error = mcp_server.sdk_search_tool({"source": "s"})
        self.assertTrue(is_error)
        self.assertIn("keywords", "provide exactly one of query/keywords, not both")
        by_name = {tool["name"]: tool for tool in mcp_server.tools()}
        schema = by_name["gms_rt_sdk_search"]["inputSchema"]
        self.assertIn("keywords", schema["properties"])
        self.assertNotIn("query", schema["required"])
        self.assertIn(
            {"required": ["keywords"]}, schema["oneOf"],
        )

    def test_sdk_search_rejects_both_aliases_and_out_of_range_limit(self):
        # query/keywords 严格互斥（schema oneOf 同约束）：同传是调用方
        # 请求歧义，必须显式报错而不是静默挑一个。
        text, is_error = mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main",
            "query": "getDataType", "keywords": "getDataType",
        })
        self.assertTrue(is_error)
        self.assertIn("exactly one", text)
        # limit 与 Controller 契约对齐（le=200）：越界显式报错，不静默截断。
        text, is_error = mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main",
            "query": "test", "limit": 400,
        })
        self.assertTrue(is_error)
        self.assertIn("limit", text)
        captured = self._capture_run()
        mcp_server.sdk_search_tool({
            "source": "android14", "revision": "main", "query": "test", "limit": 200,
        })
        self.assertEqual(captured["args"][-2:], ["--limit", "200"])
        by_name = {tool["name"]: tool for tool in mcp_server.tools()}
        limit_schema = by_name["gms_rt_sdk_search"]["inputSchema"]["properties"]["limit"]
        self.assertEqual(limit_schema["maximum"], 200)

    def test_knowledge_search_args(self):
        # ADR 0014: background-only knowledge search maps to the CLI verb.
        text, is_error = mcp_server.knowledge_search_tool({})
        self.assertTrue(is_error)
        self.assertIn("query", text)
        captured = self._capture_run()
        mcp_server.knowledge_search_tool({"query": "LMKD PRESSURE_AFTER_KILL"})
        self.assertEqual(
            captured["args"], ["--query", "LMKD PRESSURE_AFTER_KILL"],
        )
        self.assertEqual(captured["command"], "gms-rt-knowledge-search")
        mcp_server.knowledge_search_tool({
            "query": "Binder timeout", "limit": 3, "android_api_level": 34,
        })
        self.assertEqual(
            captured["args"], [
                "--query", "Binder timeout", "--limit", "3",
                "--android-api-level", "34",
            ],
        )
        mcp_server.knowledge_search_tool({"query": "ANR", "limit": 99})
        self.assertEqual(captured["args"][-1], "10")  # clamped to the cap
        mcp_server.knowledge_search_tool({"query": "ANR", "android_api_level": 5000})
        self.assertEqual(captured["args"][-1], "1000")

    def test_apk_attachment_and_source_tools(self):
        captured = self._capture_run()
        mcp_server.apk_analyze_attachment_tool({
            "snapshot_id": "SNAP", "artifact_id": "ART",
        })
        self.assertEqual(
            captured["args"], ["SNAP", "ART"],
        )
        mcp_server.apk_source_search_tool({
            "task_id": "T1", "query": "AssertionError", "path": "com/example",
        })
        self.assertEqual(
            captured["args"],
            ["T1", "AssertionError", "--mode", "content", "--path", "com/example"],
        )
        mcp_server.apk_source_read_tool({
            "task_id": "T1", "path": "a.java", "offset": 5, "limit": 50,
        })
        self.assertEqual(
            captured["args"], ["T1", "a.java", "--offset", "5", "--limit", "50"],
        )

    def test_registered_in_tools_and_handlers(self):
        names = {tool["name"] for tool in mcp_server.tools()}
        for name in (
            "gms_rt_redmine_issue_fetch", "gms_rt_redmine_issue",
            "gms_rt_redmine_triage",
            "gms_rt_redmine_history_search",
            "gms_rt_redmine_journals", "gms_rt_redmine_attachments",
            "gms_rt_redmine_attachment_download",
            "gms_rt_redmine_artifact_failures",
            "gms_rt_redmine_artifact_search", "gms_rt_redmine_artifact_read",
            "gms_rt_redmine_image", "gms_rt_apk_analyze_attachment",
            "gms_rt_apk_source_search", "gms_rt_apk_source_read",
            "gms_rt_sdk_sources", "gms_rt_sdk_search", "gms_rt_sdk_read",
            "gms_rt_knowledge_search",
        ):
            self.assertIn(name, names)
            self.assertIn(name, mcp_server._TOOL_HANDLERS)

    def test_triage_tool_maps_arguments_to_cli(self):
        captured: dict[str, Any] = {}

        def fake_run_cli(command: str, args: list[str] | None = None):
            captured["command"] = command
            captured["args"] = list(args or [])
            return "{}", False

        with patch.object(mcp_server, "run_cli", fake_run_cli):
            mcp_server.redmine_triage_tool({})
            self.assertEqual(captured, {"command": "gms-rt-redmine-triage", "args": []})

            mcp_server.redmine_triage_tool({
                "stale_days": 5, "list_limit": 50, "refresh": True,
            })
            self.assertEqual(captured["args"], [
                "--stale-days", "5", "--list-limit", "50", "--refresh",
            ])

            # 越界参数被 clamp 到 schema 上限，不透传原始输入。
            mcp_server.redmine_triage_tool({"stale_days": 999})
            self.assertEqual(captured["args"], ["--stale-days", "30"])

    def test_history_search_tool_maps_arguments_to_cli(self):
        captured: dict[str, Any] = {}

        def fake_run_cli(command: str, args: list[str] | None = None):
            captured["command"] = command
            captured["args"] = list(args or [])
            return "{}", False

        with patch.object(mcp_server, "run_cli", fake_run_cli):
            self.assertIn(
                "gms_rt_redmine_history_search", mcp_server._TOOL_HANDLERS,
            )
            # 空 query 是参数错误，不发起 CLI 调用。
            mcp_server.redmine_history_search_tool({})
            self.assertNotIn("command", captured)
            mcp_server.redmine_history_search_tool({"q": "  "})
            self.assertNotIn("command", captured)

            mcp_server.redmine_history_search_tool({
                "q": "RK3562 Android16 SSI", "limit": 10,
                "exclude_issue_id": 650761, "resolved_only": True,
            })
            self.assertEqual(captured, {
                "command": "gms-rt-redmine-history-search",
                "args": ["RK3562 Android16 SSI", "--limit", "10",
                         "--exclude-issue-id", "650761", "--resolved-only"],
            })

    def test_schemas_use_closed_objects(self):
        for tool in mcp_server.tools():
            if tool["name"].startswith(("gms_rt_redmine_", "gms_rt_sdk_")) or tool["name"] in (
                "gms_rt_apk_source_search", "gms_rt_apk_source_read",
                "gms_rt_apk_analyze_attachment",
            ):
                self.assertEqual(
                    tool["inputSchema"].get("additionalProperties"), False,
                    tool["name"],
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
