from __future__ import annotations

import hashlib
import math
import os
from unittest.mock import MagicMock, patch

import pytest

from worker_agent.app import WorkerAgent, restart_local_vnc
from worker_agent.client import ControllerClient
from worker_agent.config import WorkerConfig
from worker_agent.runtime import WorkerRuntime


def worker_config(tmp_path):
    return WorkerConfig(
        worker_id="worker-test",
        controller_url="https://controller",
        token="token",
        data_root=tmp_path / "data",
        suite_roots=[tmp_path / "suites"],
    )


def test_registration_only_advertises_novnc_when_both_ports_are_ready(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))

    with patch("worker_agent.app.shutil.which", return_value="/bin/tool"), patch(
        "worker_agent.app._port_listening", return_value=False,
    ), patch(
        "worker_agent.app._rfb_handshake_ok", return_value=False,
    ):
        capabilities = agent.registration()["capabilities"]

    assert capabilities["adb"] is True
    assert capabilities["adb_proxy_logs"] is True
    assert "novnc_port" not in capabilities


def test_registration_advertises_novnc_when_rfb_handshake_succeeds(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))

    with patch("worker_agent.app.shutil.which", return_value="/bin/tool"), patch(
        "worker_agent.app._port_listening", return_value=True,
    ), patch(
        "worker_agent.app._rfb_handshake_ok", return_value=True,
    ):
        capabilities = agent.registration()["capabilities"]

    assert capabilities["novnc_port"] == 6080


def test_source_only_registration_disables_test_host_capabilities(tmp_path):
    config = worker_config(tmp_path)
    config.source_only = True
    agent = WorkerAgent(config)

    with patch(
        "worker_agent.app.adb_proxy_capability_status",
        return_value={"installed": True, "version": "adb-proxy 0.4.5"},
    ), patch(
        "worker_agent.app.shutil.which",
        side_effect=lambda name: f"/usr/bin/{name}" if name == "adb" else None,
    ):
        capabilities = agent.registration()["capabilities"]

    assert capabilities["adb"] is True
    assert capabilities["adb_proxy"] is True
    assert capabilities["adb_proxy_source_only"] is True
    assert capabilities["tradefed"] is False
    assert capabilities["fastboot"] is False
    assert capabilities["usbip_client"] is False


def test_restart_vnc_command_kills_and_restarts(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()

    with patch("worker_agent.app.restart_local_vnc", return_value={"rfb_ok": True}) as mock_fn:
        agent.handle({"id": "cmd-vnc", "command_type": "restart_vnc", "payload": {}})

    mock_fn.assert_called_once()
    agent.client.ack.assert_called_once()
    assert agent.client.ack.call_args.args[1] == "completed"


def test_usbip_command_runs_in_background_so_heartbeats_are_not_blocked(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()
    thread = MagicMock()
    command = {
        "id": "cmd-usbip",
        "command_type": "usbip_attach",
        "payload": {"source_host": "192.0.2.10", "busids": ["1-1"]},
    }

    with patch("worker_agent.app.threading.Thread", return_value=thread) as thread_cls, \
            patch("worker_agent.app.execute_usbip_action") as execute:
        agent.handle(command)

    execute.assert_not_called()
    thread_cls.assert_called_once_with(
        target=agent.run_usbip_action,
        args=(command,),
        name="USBIP-cmd-usbip",
        daemon=True,
    )
    thread.start.assert_called_once_with()
    assert agent.runtime.previous_command("cmd-usbip")["status"] == "running"
    agent.client.ack.assert_called_once_with("cmd-usbip", "running", {}, "")


def test_background_usbip_command_reports_completion(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()
    command = {
        "id": "cmd-usbip",
        "command_type": "usbip_attach",
        "payload": {"source_host": "192.0.2.10", "busids": ["1-1"]},
    }
    result = {"attached_busids": ["1-1"]}

    with patch("worker_agent.app.execute_usbip_action", return_value=result):
        agent.run_usbip_action(command)

    saved = agent.runtime.previous_command("cmd-usbip")
    assert saved["status"] == "completed"
    assert saved["result"] == result
    agent.client.ack.assert_called_once_with(
        "cmd-usbip", "completed", result, ""
    )
    assert [
        (item["source_host"], item["busid"], item["adb_server_socket"])
        for item in agent.runtime.usbip_assignments()
    ] == [("192.0.2.10", "1-1", "")]
    assert math.isfinite(agent.next_usbip_recovery_at)


def test_background_usbip_detach_disables_recovery_after_last_assignment(
    tmp_path,
):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()
    agent.runtime.remember_usbip_assignments(
        "192.0.2.10", ["1-1"], "tcp:127.0.0.1:5039"
    )
    command = {
        "id": "cmd-usbip-detach",
        "command_type": "usbip_detach",
        "payload": {"source_host": "192.0.2.10", "busids": ["1-1"]},
    }

    with patch(
        "worker_agent.app.execute_usbip_action",
        return_value={"detached_busids": ["1-1"]},
    ):
        agent.run_usbip_action(command)

    assert agent.runtime.usbip_assignments() == []
    assert math.isinf(agent.next_usbip_recovery_at)


def test_usbip_assignment_survives_runtime_restart_and_detach_clears_it(tmp_path):
    config = worker_config(tmp_path)
    runtime = WorkerRuntime(config)
    runtime.remember_usbip_assignments(
        "192.0.2.10", ["1-1", "1-2"], "tcp:127.0.0.1:5039"
    )

    restarted = WorkerRuntime(config)
    assert [
        (item["source_host"], item["busid"], item["adb_server_socket"])
        for item in restarted.usbip_assignments()
    ] == [
        ("192.0.2.10", "1-1", "tcp:127.0.0.1:5039"),
        ("192.0.2.10", "1-2", "tcp:127.0.0.1:5039"),
    ]

    restarted.forget_usbip_assignments("192.0.2.10", ["1-1"])
    assert [
        item["busid"] for item in restarted.usbip_assignments()
    ] == ["1-2"]


def test_worker_recovers_persisted_usbip_with_proxy_side_adb(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.runtime.remember_usbip_assignments(
        "192.0.2.10", ["1-1", "1-2"], "tcp:127.0.0.1:5039"
    )

    with patch(
        "worker_agent.app.execute_usbip_action",
        return_value={"attached_busids": ["1-1", "1-2"]},
    ) as execute:
        result = agent.recover_usbip_assignments()

    assert result == {
        "recovered": ["192.0.2.10:1-1", "192.0.2.10:1-2"],
        "errors": [],
    }
    execute.assert_called_once_with(
        "attach",
        "192.0.2.10",
        ["1-1", "1-2"],
        "tcp:127.0.0.1:5039",
    )


def test_uninstall_agent_acks_before_stopping_services(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()

    with patch("worker_agent.app.stop_local_worker_agent") as stop:
        agent.handle({"id": "cmd-uninstall", "command_type": "uninstall_agent", "payload": {}})
        stop.assert_called_once()

    agent.client.ack.assert_called_once()
    assert agent.client.ack.call_args.args[1] == "completed"


def test_worker_config_update_is_applied_without_agent_restart(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"max_jobs": 1}', encoding="utf-8")
    agent = WorkerAgent(worker_config(tmp_path))

    with patch.dict("os.environ", {"GMS_WORKER_CONFIG": str(config_path)}), patch(
        "worker_agent.app.subprocess.Popen"
    ) as restart:
        result = agent.update_worker_config({"max_jobs": 4})

    assert result == {
        "updated": {"max_jobs": 4},
        "applied": True,
        "restarted": False,
    }
    assert agent.config.max_jobs == 4
    restart.assert_not_called()


def test_restart_vnc_uses_managed_headless_units_when_installed(tmp_path):
    unit_root = tmp_path / ".config/systemd/user"
    unit_root.mkdir(parents=True)
    for name in (
        "gms-worker-xvfb.service",
        "gms-worker-x11vnc.service",
        "gms-worker-novnc.service",
    ):
        (unit_root / name).write_text("[Unit]\n", encoding="utf-8")

    completed = MagicMock(returncode=0, stderr="")
    with patch("worker_agent.app.Path.home", return_value=tmp_path), patch(
        "worker_agent.app.subprocess.run", return_value=completed
    ) as runner, patch("worker_agent.app.time.sleep"), patch(
        "worker_agent.app._rfb_handshake_ok", return_value=True
    ), patch("worker_agent.app._port_listening", return_value=True):
        result = restart_local_vnc()

    assert result["rfb_ok"] is True
    assert runner.call_args.args[0] == [
        "systemctl", "--user", "restart",
        "gms-worker-xvfb.service",
        "gms-worker-x11vnc.service",
        "gms-worker-novnc.service",
    ]


def test_redelivered_running_command_is_acknowledged_without_reexecution(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()
    agent.runtime.save_command("cmd-1", "running", {"worker_job_id": "wj-1"})

    agent.handle({"id": "cmd-1", "command_type": "start_test", "payload": {}})

    agent.client.ack.assert_called_once_with(
        "cmd-1", "running", {"worker_job_id": "wj-1"}, ""
    )


def test_restart_fails_interrupted_background_command_but_preserves_managed_job(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))
    runtime.save_command("cmd-flash", "running", {})
    runtime.save_command("cmd-test", "running", {"worker_job_id": "wj-1"})
    work_dir = runtime.config.data_root / "jobs" / "job-1" / "attempt-1"
    work_dir.mkdir(parents=True)
    with runtime.connect() as conn:
        conn.execute(
            """INSERT INTO jobs
               (worker_job_id,job_id,attempt_id,pid,pgid,status,devices_json,
                work_dir,exit_code,error,command_id)
               VALUES('wj-1','job-1','attempt-1',?,?,'running','[]',?,NULL,'','cmd-test')""",
            (os.getpid(), os.getpgid(0), str(work_dir)),
        )

    interrupted = runtime.fail_interrupted_commands()

    assert [item["id"] for item in interrupted] == ["cmd-flash"]
    assert runtime.previous_command("cmd-flash")["status"] == "failed"
    assert runtime.previous_command("cmd-test")["status"] == "running"


def test_unsynced_command_result_survives_restart_until_controller_accepts_it(tmp_path):
    config = worker_config(tmp_path)
    runtime = WorkerRuntime(config)
    runtime.save_command("cmd-1", "completed", {"count": 1})

    restarted = WorkerRuntime(config)
    assert restarted.unsynced_commands() == [{
        "id": "cmd-1", "status": "completed", "result": {"count": 1}, "error": "",
    }]
    restarted.mark_command_synced("cmd-1")
    assert restarted.unsynced_commands() == []


def test_worker_rejects_stale_device_fencing_generation(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))
    current = {
        "payload": {"lease_tokens": [{
            "device_id": "worker-test:ABC", "lease_id": "lease-2",
            "attempt_id": "attempt-2", "generation": 2,
        }]}
    }
    runtime.validate_fencing(current)
    runtime.validate_fencing(current)

    with pytest.raises(ValueError, match="stale fencing token"):
        runtime.validate_fencing({
            "payload": {"lease_tokens": [{
                "device_id": "worker-test:ABC", "lease_id": "lease-1",
                "attempt_id": "attempt-1", "generation": 1,
            }]}
        })


def test_worker_rejects_device_command_without_fencing_token(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))

    with pytest.raises(ValueError, match="requires a valid device fencing token"):
        runtime.validate_fencing({
            "command_type": "device_action",
            "payload": {"devices": ["worker-test:ABC"], "action": "reboot"},
        })


def test_worker_rejects_usbip_detach_without_fencing_token(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))

    with pytest.raises(ValueError, match="requires a valid device fencing token"):
        runtime.validate_fencing({
            "command_type": "usbip_detach",
            "payload": {
                "devices": ["worker-test:USBIP001"],
                "source_host": "192.0.2.10",
                "busids": ["1-1"],
            },
        })


def test_new_fencing_generation_revokes_previous_attempt(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))
    runtime.validate_fencing({
        "command_type": "device_action",
        "payload": {
            "devices": ["worker-test:ABC"],
            "lease_tokens": [{
                "device_id": "worker-test:ABC", "lease_id": "claim-1",
                "attempt_id": "attempt-old", "generation": 1,
            }],
        },
    })

    with patch.object(runtime, "revoke_attempt") as revoke:
        runtime.validate_fencing({
            "command_type": "device_action",
            "payload": {
                "devices": ["worker-test:ABC"],
                "lease_tokens": [{
                    "device_id": "worker-test:ABC", "lease_id": "claim-2",
                    "attempt_id": "operation-new", "generation": 2,
                }],
            },
        })

    revoke.assert_called_once_with(
        "attempt-old", "superseded by a newer device fencing generation"
    )


def test_completed_device_action_releases_its_fencing_generation(tmp_path):
    runtime = WorkerRuntime(worker_config(tmp_path))
    completed = {
        "command_type": "device_action",
        "payload": {
            "devices": ["worker-test:ABC"],
            "lease_tokens": [{
                "device_id": "worker-test:ABC", "lease_id": "claim-2",
                "attempt_id": "operation-done", "generation": 2,
            }],
        },
    }
    runtime.validate_fencing(completed)

    assert runtime.release_fencing(completed) == 1

    runtime.validate_fencing({
        "command_type": "device_action",
        "payload": {
            "devices": ["worker-test:ABC"],
            "lease_tokens": [{
                "device_id": "worker-test:ABC", "lease_id": "claim-1",
                "attempt_id": "operation-new", "generation": 1,
            }],
        },
    })


def test_heartbeat_replays_unsynced_command_state_after_reconnect(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.runtime.save_command("cmd-1", "completed", {"worker_job_id": "wj-1"})
    agent.client = MagicMock()
    agent.client.session_id = "session-1"
    agent.client.connection_generation = 3
    agent.client.heartbeat.return_value = {
        "success": True,
        "reconciled_command_ids": ["cmd-1"],
        "revoked_attempt_ids": ["attempt-old"],
    }
    agent.suites = [{"suite_key": "CTS:17"}]
    agent.last_suite_scan = float("inf")

    with patch.object(agent.runtime, "revoke_attempt") as revoke, patch(
        "worker_agent.app.host_metrics", return_value={}
    ), patch(
        "worker_agent.app.probe_devices", return_value=[]
    ), patch(
        "worker_agent.app.recover_adb_proxy_state",
        return_value={"recovered": [], "errors": []},
    ), patch("worker_agent.app.discover_tradefed_processes", return_value=[]):
        agent.heartbeat()

    payload = agent.client.heartbeat.call_args.args[0]
    assert payload["session_id"] == "session-1"
    assert payload["connection_generation"] == 3
    assert payload["command_states"][0]["id"] == "cmd-1"
    assert agent.runtime.unsynced_commands() == []
    revoke.assert_called_once()


def test_registration_forces_cached_suite_inventory_into_first_heartbeat(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.suites = [{"suite_key": "CTS:17", "tools_path": "/suite/tools"}]
    agent.last_suite_scan = float("inf")
    agent.client = MagicMock()
    agent.client.session_id = "session-1"
    agent.client.connection_generation = 1
    agent.client.heartbeat.side_effect = KeyboardInterrupt

    with patch.object(agent, "registration", return_value={"worker_id": "worker-test"}), patch(
        "worker_agent.app.scan_suites", return_value=agent.suites
    ), patch(
        "worker_agent.app.host_metrics", return_value={}
    ), patch(
        "worker_agent.app.probe_devices", return_value=[]
    ), patch(
        "worker_agent.app.discover_tradefed_processes", return_value=[]
    ), patch.object(
        agent.runtime, "recoverable_jobs", return_value=[]
    ), patch.object(
        agent.runtime, "fail_interrupted_commands", return_value=[]
    ):
        agent.run()

    payload = agent.client.heartbeat.call_args.args[0]
    assert payload["suites"] == agent.suites


def test_suite_failure_keeps_original_error_when_ack_is_retried(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()

    with patch("worker_agent.app.execute_suite_action", side_effect=RuntimeError("extract failed")):
        agent.run_suite_action({"id": "cmd-suite", "payload": {}})

    saved = agent.runtime.previous_command("cmd-suite")
    assert saved is not None
    assert saved["status"] == "failed"
    assert saved["error"] == "extract failed"
    agent.client.ack.assert_called_once_with(
        "cmd-suite", "failed", error="extract failed"
    )


def test_firmware_failure_cleans_worker_staging_directory(tmp_path):
    agent = WorkerAgent(worker_config(tmp_path))
    agent.client = MagicMock()
    agent.client.download.side_effect = RuntimeError("download failed")
    command = {
        "id": "cmd-firmware",
        "command_type": "flash_firmware",
        "payload": {
            "stage_id": "fw-123",
            "filename": "update.img",
            "size_bytes": 10,
            "sha256": "0" * 64,
            "devices": ["worker-test:ABC"],
        },
    }

    agent.run_firmware_flash(command)

    assert not (agent.config.data_root / "firmware" / "fw-123").exists()
    agent.client.ack.assert_called_once_with(
        "cmd-firmware", "failed", error="download failed"
    )


def test_start_process_closes_parent_log_descriptors(tmp_path):
    config = worker_config(tmp_path)
    executable = config.suite_roots[0] / "android-cts" / "tools" / "cts-tradefed"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime = WorkerRuntime(config)
    process = MagicMock(pid=1234)

    with patch("worker_agent.runtime.subprocess.Popen", return_value=process) as popen, patch(
        "worker_agent.runtime.os.getpgid", return_value=1234
    ):
        runtime.start_process({
            "id": "cmd-process",
            "job_id": "job-1",
            "attempt_id": "attempt-1",
            "payload": {"argv": [str(executable), "list", "devices"]},
        })

    assert popen.call_args.kwargs["stdout"].closed is True
    assert popen.call_args.kwargs["stderr"].closed is True


def test_artifact_upload_uses_resumable_bounded_chunks(tmp_path):
    path = tmp_path / "report.zip"
    path.write_bytes(b"abcdefghij")
    client = ControllerClient(worker_config(tmp_path))

    with patch.object(
        client,
        "_request_with_worker_header",
        side_effect=[
            {"success": True, "upload": {"id": "upload-1", "status": "uploading"},
             "uploaded_chunks": []},
            {"success": True},
            {"success": True, "artifact": {"id": "artifact-1"}},
        ],
    ) as request:
        client.upload_artifact("job-1", "attempt-1", path)

    assert request.call_count == 3
    chunk_call = request.call_args_list[1]
    assert chunk_call.args[0] == "PUT"
    assert chunk_call.args[2] == b"abcdefghij"
    assert chunk_call.kwargs["timeout"] == 300
    assert chunk_call.kwargs["extra_headers"]["X-Chunk-SHA256"] == hashlib.sha256(b"abcdefghij").hexdigest()
