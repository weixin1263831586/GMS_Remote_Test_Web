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
        sleep.assert_awaited_once_with(0.5)

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
