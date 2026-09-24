"""Tests for deterministic Daily Brief evidence preflight."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
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

    def test_triage_baseline_collects_redmine_without_device(self):
        """#653167 复盘：triage 也要有确定性 Redmine 基线兜底，但不含设备
        取证（triage 证据门禁没有设备维度）。"""

        async def scenario():
            fetched = ToolTrace(tool_name="gms_rt_redmine_issue_fetch", status="succeeded")
            journals = ToolTrace(tool_name="gms_rt_redmine_journals", status="succeeded")
            attachments = ToolTrace(tool_name="gms_rt_redmine_attachments", status="succeeded")
            with patch.object(
                evidence_preflight, "_collect",
                AsyncMock(side_effect=[
                    (fetched, {"snapshot_id": "ev_t1"}), (journals, {}), (attachments, {}),
                ]),
            ) as collect:
                result = await evidence_preflight.collect_deep_analysis_evidence(
                    issue_id=653167, device_serial="RK3576GMS1",
                    env_extra={"GMS_RT_PROFILE": "p"}, include_device=False,
                )
            return result, collect

        result, collect = asyncio.run(scenario())
        self.assertEqual(result.snapshot_id, "ev_t1")
        self.assertEqual(result.device_status, "not_requested")
        self.assertEqual([call.kwargs["tool_name"] for call in collect.call_args_list], [
            "gms_rt_redmine_issue_fetch", "gms_rt_redmine_journals",
            "gms_rt_redmine_attachments",
        ])

    def test_diagnostic_keeps_device_snapshot_by_default(self):
        async def scenario():
            fetched = ToolTrace(tool_name="gms_rt_redmine_issue_fetch", status="succeeded")
            journals = ToolTrace(tool_name="gms_rt_redmine_journals", status="succeeded")
            attachments = ToolTrace(tool_name="gms_rt_redmine_attachments", status="succeeded")
            device = ToolTrace(tool_name="gms_rt_devices_snapshot", status="succeeded")
            with patch.object(
                evidence_preflight, "_collect",
                AsyncMock(side_effect=[
                    (fetched, {"snapshot_id": "ev_1"}), (journals, {}),
                    (attachments, {}), (device, {}),
                ]),
            ):
                return await evidence_preflight.collect_deep_analysis_evidence(
                    issue_id=1, device_serial="RK3576GMS1",
                    env_extra={"GMS_RT_PROFILE": "p"},
                )

        result = asyncio.run(scenario())
        self.assertEqual(result.device_status, "succeeded")

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
        run.assert_not_awaited()

    def test_cancel_interrupts_running_preflight_command(self):
        """评审 P2：communicate 期间请求停止须在轮询间隔级（≈0.25s）终止
        preflight CLI，而不是等满 90s 超时。"""

        async def scenario():
            with TemporaryDirectory() as tmp:
                script = Path(tmp) / "hang.sh"
                script.write_text("#!/bin/sh\necho started\nsleep 30\n", encoding="utf-8")
                flags = {"cancelled": False}

                def should_cancel():
                    return flags["cancelled"]

                async def flip_later():
                    await asyncio.sleep(0.6)
                    flags["cancelled"] = True

                flipper = asyncio.ensure_future(flip_later())
                loop = asyncio.get_running_loop()
                started = loop.time()
                raised = False
                try:
                    await evidence_preflight._run_readonly_command(
                        ["bash", str(script)], {},
                        timeout_seconds=60, should_cancel=should_cancel,
                    )
                except evidence_preflight.PreflightCancelledError:
                    raised = True
                elapsed = loop.time() - started
                flipper.cancel()
                with suppress(asyncio.CancelledError):
                    await flipper
                return raised, elapsed

        raised, elapsed = asyncio.run(scenario())
        self.assertTrue(raised)
        # 30s 的 sleep 子进程被整树终止；余量放宽给慢 CI。
        self.assertLess(elapsed, 5.0)

    def test_timeout_reaps_communicate_task(self):
        async def scenario():
            with TemporaryDirectory() as tmp:
                script = Path(tmp) / "hang.sh"
                script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
                current = asyncio.current_task()
                before = set(asyncio.all_tasks())
                result = await evidence_preflight._run_readonly_command(
                    ["bash", str(script)], {}, timeout_seconds=0.1
                )
                await asyncio.sleep(0)
                leaked = [
                    task for task in asyncio.all_tasks()
                    if task is not current and task not in before and not task.done()
                ]
                return result, leaked

        (exit_code, _output, error), leaked = asyncio.run(scenario())
        self.assertEqual(exit_code, evidence_preflight.NETWORK_EXIT_CODE)
        self.assertIn("timed out", error)
        self.assertEqual(leaked, [])

    def test_collect_maps_midflight_cancel_to_failed_trace(self):
        """运行中取消与"尝试前取消"同构：failed 轨迹 + 不再重试。"""

        async def scenario():
            with patch.object(
                evidence_preflight, "_gms_command", return_value=["command"],
            ), patch.object(
                evidence_preflight, "_run_readonly_command",
                AsyncMock(side_effect=evidence_preflight.PreflightCancelledError),
            ) as run:
                trace, _ = await evidence_preflight._collect(
                    tool_name="gms_rt_devices_snapshot",
                    arguments=["RK3576GMS1", "--json"],
                    tool_input={"device": "RK3576GMS1"}, env_extra={},
                    should_cancel=lambda: False,
                )
            return trace, run

        trace, run = asyncio.run(scenario())
        self.assertEqual(trace.status, "failed")
        self.assertIn("cancelled during", trace.output_preview)
        self.assertEqual(run.await_count, 1)
