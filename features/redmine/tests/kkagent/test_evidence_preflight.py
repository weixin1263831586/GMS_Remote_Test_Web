"""Tests for deterministic Daily Brief evidence preflight."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent import evidence_preflight
from features.redmine.kkagent.trace import ToolTrace


class EvidencePreflightTests(unittest.TestCase):
    def test_deep_preflight_uses_selected_serial_and_snapshot_id(self):
        async def scenario():
            fetch = ToolTrace(
                tool_name="gms_rt_redmine_issue_fetch", status="succeeded",
                snapshot_ids=["ev_1"],
            )
            journals = ToolTrace(tool_name="gms_rt_redmine_journals", status="succeeded")
            attachments = ToolTrace(tool_name="gms_rt_redmine_attachments", status="succeeded")
            device = ToolTrace(tool_name="gms_rt_devices_snapshot", status="succeeded")
            with patch.object(
                evidence_preflight, "_collect",
                AsyncMock(side_effect=[
                    (fetch, {"snapshot_id": "ev_1"}), (journals, {}),
                    (attachments, {}), (device, {}),
                ]),
            ) as collect:
                result = await evidence_preflight.collect_deep_analysis_evidence(
                    issue_id=652654, device_serial="RK3576GMS1",
                    env_extra={"GMS_RT_PROFILE": "kkagent-profile"},
                )
            return result, collect

        result, collect = asyncio.run(scenario())
        self.assertEqual(result.snapshot_id, "ev_1")
        self.assertEqual(result.device_status, "succeeded")
        self.assertEqual([call.kwargs["tool_name"] for call in collect.call_args_list], [
            "gms_rt_redmine_issue_fetch", "gms_rt_redmine_journals",
            "gms_rt_redmine_attachments", "gms_rt_devices_snapshot",
        ])
        self.assertEqual(collect.call_args_list[0].kwargs["arguments"][0], "652654")
        self.assertEqual(collect.call_args_list[-1].kwargs["arguments"][0], "RK3576GMS1")
        self.assertEqual(result.prompt_context(), {
            "snapshot_id": "ev_1", "device_status": "succeeded",
        })

    def test_network_exit_is_retried_but_usage_error_is_not(self):
        async def scenario():
            with patch.object(
                evidence_preflight, "_gms_command",
                return_value=["gms-rt-devices-snapshot", "RK3576GMS1"],
            ), patch.object(
                evidence_preflight, "_run_readonly_command",
                AsyncMock(side_effect=[
                    (6, b"", "connection refused"),
                    (0, b'{"ok":true,"data":{"device":"RK3576GMS1"}}', ""),
                ]),
            ), patch.object(evidence_preflight.asyncio, "sleep", AsyncMock()) as sleep:
                trace, _ = await evidence_preflight._collect(
                    tool_name="gms_rt_devices_snapshot",
                    arguments=["RK3576GMS1", "--json"],
                    tool_input={"device": "RK3576GMS1"}, env_extra={},
                )
            return trace, sleep

        trace, sleep = asyncio.run(scenario())
        self.assertEqual(trace.status, "succeeded")
        sleep.assert_awaited_once_with(1.0)

        async def usage_scenario():
            with patch.object(
                evidence_preflight, "_gms_command", return_value=["command"],
            ), patch.object(
                evidence_preflight, "_run_readonly_command",
                AsyncMock(return_value=(2, b"", "device is required")),
            ) as run:
                return await evidence_preflight._collect(
                    tool_name="gms_rt_devices_snapshot", arguments=[], tool_input={}, env_extra={},
                ), run

        (failed, _), run = asyncio.run(usage_scenario())
        self.assertEqual(failed.status, "failed")
        self.assertEqual(run.await_count, 1)

    def test_attachments_preflight_records_manifest_metadata(self):
        """附件清单元数据必须记账：否则 gate 对预采集的"成功"调用误报
        manifest 未解析，与修复提示"不要重复取证"互相矛盾，烧光修复轮。"""

        async def scenario():
            fetched = ToolTrace(tool_name="gms_rt_redmine_issue_fetch", status="succeeded")
            journals = ToolTrace(tool_name="gms_rt_redmine_journals", status="succeeded")
            attachments = ToolTrace(tool_name="gms_rt_redmine_attachments", status="succeeded")
            device = ToolTrace(tool_name="gms_rt_devices_snapshot", status="succeeded")
            manifest = {"artifacts": [
                {"artifact_id": "a1", "kind": "log", "status": "ready"},
                {"artifact_id": "a2", "kind": "image", "status": "ready"},
            ]}
            with patch.object(
                evidence_preflight, "_collect",
                AsyncMock(side_effect=[
                    (fetched, {"snapshot_id": "ev_9"}), (journals, {}),
                    (attachments, manifest), (device, {}),
                ]),
            ):
                return await evidence_preflight.collect_deep_analysis_evidence(
                    issue_id=7, device_serial="S1", env_extra={"GMS_RT_PROFILE": "p"},
                )

        result = asyncio.run(scenario())
        attachments = next(
            trace for trace in result.traces
            if trace.tool_name == "gms_rt_redmine_attachments"
        )
        self.assertTrue(attachments.attachment_manifest_parsed)
        self.assertEqual(attachments.attachment_count, 2)
        self.assertEqual(attachments.text_artifact_ids, ["a1"])
        self.assertEqual(attachments.all_artifact_ids, ["a1", "a2"])

    def test_cancel_poll_short_circuits_before_new_attempt(self):
        async def scenario():
            with patch.object(
                evidence_preflight, "_gms_command",
                return_value=["gms-rt-devices-snapshot", "RK3576GMS1"],
            ), patch.object(
                evidence_preflight, "_run_readonly_command",
                AsyncMock(return_value=(6, b"", "connection refused")),
            ) as run:
                trace, _ = await evidence_preflight._collect(
                    tool_name="gms_rt_devices_snapshot",
                    arguments=["RK3576GMS1", "--json"],
                    tool_input={"device": "RK3576GMS1"}, env_extra={},
                    should_cancel=lambda: True,
                )
            return trace, run

        trace, run = asyncio.run(scenario())
        self.assertEqual(trace.status, "failed")
        self.assertIn("cancelled", trace.output_preview)
        self.assertEqual(trace.failure_kind, "cancelled")
        run.assert_not_awaited()

    def test_cancel_interrupts_current_cli_process(self):
        """停止按钮必须终止正在 communicate 的 CLI，而非等 90s timeout。"""

        class FakeProcess:
            def __init__(self):
                self.pid = 12345
                self.returncode = None
                self.started = asyncio.Event()

            async def communicate(self):
                self.started.set()
                await asyncio.Event().wait()
                return b"", b""

        async def scenario():
            process = FakeProcess()
            checks = 0

            def cancelled():
                nonlocal checks
                checks += 1
                return checks >= 2

            with patch.object(
                evidence_preflight.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=process),
            ), patch.object(
                evidence_preflight,
                "terminate_process_tree",
                AsyncMock(),
            ) as terminate, patch.object(
                evidence_preflight,
                "CANCEL_POLL_INTERVAL_SECONDS",
                0.001,
            ):
                result = await evidence_preflight._run_readonly_command(
                    ["gms-rt-redmine-issue-fetch", "7"],
                    {},
                    timeout_seconds=10.0,
                    should_cancel=cancelled,
                )
            return result, terminate

        result, terminate = asyncio.run(scenario())
        self.assertEqual(result[0], evidence_preflight.CANCELLED_EXIT_CODE)
        self.assertIn("cancelled", result[2])
        terminate.assert_awaited_once()

    def test_cancel_interrupts_retry_backoff(self):
        """网络失败后的 1s/3s backoff 也应响应停止且不启动下一次 CLI。"""

        async def scenario():
            checks = 0

            def cancelled():
                nonlocal checks
                checks += 1
                # attempt-0 precheck=False, backoff precheck=False,
                # first sleep chunk afterwards sees True.
                return checks >= 3

            with patch.object(
                evidence_preflight, "_gms_command", return_value=["command"],
            ), patch.object(
                evidence_preflight, "_run_readonly_command",
                AsyncMock(return_value=(6, b"", "connection refused")),
            ) as run, patch.object(
                evidence_preflight.asyncio, "sleep", AsyncMock()
            ):
                trace, _ = await evidence_preflight._collect(
                    tool_name="gms_rt_redmine_issue_fetch",
                    arguments=["7"], tool_input={"issue_id": 7}, env_extra={},
                    should_cancel=cancelled,
                )
            return trace, run

        trace, run = asyncio.run(scenario())
        self.assertEqual(trace.failure_kind, "cancelled")
        self.assertEqual(run.await_count, 1)


if __name__ == "__main__":
    unittest.main()
