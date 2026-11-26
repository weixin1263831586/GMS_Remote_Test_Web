import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from features.auth import CurrentUser
from features.devices.models import USBIPDisconnectRequest, USBIPStartRequest
from features.devices.tests.usbip_runtime_fixture import UsbipRuntimeTestCase, global_state
from features.devices.usbip.manager import USBIPManager, parse_usbipd_android_busids
from foundation.command_result import CommandResult
from foundation.ssh import find_device_host_password


class UsbipConnectionTests(UsbipRuntimeTestCase):
    def test_usbip_request_models_reject_shell_metacharacters_in_busids(self):
        for model in (USBIPStartRequest, USBIPDisconnectRequest):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model(busids=["1-1 & whoami"])
    def test_usbipd_persisted_guid_is_not_treated_as_busid(self):
        output = """
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-13   0403:6001  USB Serial Converter                                          Not shared
Persisted:
GUID                                  DEVICE
85aba5e0-8dbc-4d80-9d24-23778558f81e  Android ADB Interface
"""
        self.assertEqual(parse_usbipd_android_busids(output), [])
    def test_usbipd_connected_android_adb_shared_busid_is_detected(self):
        output = """
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-1    2207:0006  Android ADB Interface                                         Shared
1-13   0403:6001  USB Serial Converter                                          Not shared

Persisted:
GUID                                  DEVICE
466f3f47-c2c9-4ea1-bb28-333847ee3c00  Android ADB Interface
"""
        self.assertEqual(parse_usbipd_android_busids(output), ["1-1"])

    def test_usbipd_legacy_table_without_connected_header_is_detected(self):
        output = """
BUSID  VID:PID    DEVICE                                                        STATE
1-1    2207:0006  Android ADB Interface                                         Shared
1-13   0403:6001  USB Serial Converter                                          Not shared
"""
        self.assertEqual(parse_usbipd_android_busids(output), ["1-1"])

    def test_find_android_devices_parses_stderr_output(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                self.cmd = cmd
                self.get_pty = get_pty
                return CommandResult(
            stdout="",
            stderr="Connected:\n""BUSID  VID:PID    DEVICE                                                        STATE\n""1-1    2207:0006  Android ADB Interface                                         Shared\n",
            code=0,
        )
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        self.assertEqual(manager._find_android_devices(object(), {}), ["1-1"])

    def test_detach_source_sessions_clears_selected_export_without_unbind(self):
        commands = []

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret"}

            def find_device_host_password(self, device_host, config):
                return "secret"

        class FakeSsh:
            def close(self):
                pass

        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                commands.append(cmd)
                if cmd.startswith("tasklist "):
                    return CommandResult(stdout="INFO: No tasks are running", stderr="", code=0)
                return CommandResult(stdout="", stderr="", code=0)

        manager = USBIPManager(FakeSshManager(), FakeConfigManager())
        manager._create_windows_ssh = lambda *args: FakeSsh()
        manager._is_windows_host = lambda ssh: True
        manager.check_usbipd_installed = lambda ssh: (True, "5.3.0")

        result = manager.detach_source_sessions(
            "hcq@172.16.14.66",
            ["1-1"],
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["detached_busids"], ["1-1"])
        self.assertEqual(commands, [
            "usbipd detach --busid 1-1",
        ])
        self.assertFalse(any("unbind" in command for command in commands))

    def test_bind_source_devices_rejects_partial_bind_results(self):
        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret"}

            def find_device_host_password(self, device_host, config):
                return "secret"

        class FakeSsh:
            def close(self):
                pass

        manager = USBIPManager(config_manager=FakeConfigManager())
        manager._create_windows_ssh = lambda *args: FakeSsh()
        manager._is_windows_host = lambda ssh: True
        manager.check_usbipd_installed = lambda ssh: (True, "5.3.0")
        manager._find_android_devices = lambda ssh, config: ["1-1", "1-2"]
        manager._stop_windows_adb = lambda ssh: {"success": True}
        manager._bind_devices = lambda ssh, busids: ["1-1"]

        result = manager.bind_source_devices(
            "hcq@172.16.14.66",
            ["1-1", "1-2"],
        )

        self.assertFalse(result["success"])
        self.assertIn("1-2", result["error"])

    def test_stop_windows_adb_fails_if_server_restarts_immediately(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd.startswith("tasklist "):
                    return CommandResult(stdout="adb.exe 123 Console", stderr="", code=0)
                return CommandResult(stdout="SUCCESS", stderr="", code=0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.manager.time.sleep", return_value=None):
            result = manager._stop_windows_adb(object())

        self.assertFalse(result["success"])
        self.assertIn("仍在运行", result["error"])

    def test_detach_source_sessions_rejects_invalid_busid(self):
        manager = USBIPManager()
        result = manager.detach_source_sessions(
            "hcq@172.16.14.66",
            ["1-1 & whoami"],
        )
        self.assertFalse(result["success"])
        self.assertIn("BUSID", result["error"])

    def test_detach_source_sessions_accepts_already_detached_device(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "ver 2>&1":
                    return CommandResult(stdout="Microsoft Windows", stderr="", code=0)
                if cmd == "usbipd --version":
                    return CommandResult(stdout="5.2.0", stderr="", code=0)
                if cmd.startswith("taskkill "):
                    return CommandResult(stdout="", stderr="", code=0)
                if cmd.startswith("tasklist "):
                    return CommandResult(stdout="INFO: No tasks are running", stderr="", code=0)
                if cmd == "usbipd detach --busid 1-1":
                    return CommandResult(
        stdout="",
        stderr="usbipd: info: Device with busid '1-1' is not attached.",
        code=1,
    )
                return CommandResult(stdout="", stderr="", code=0)

        class FakeSSH:
            def close(self):
                pass

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        manager._create_windows_ssh = lambda *args, **kwargs: FakeSSH()
        manager.config_manager = SimpleNamespace(
            load_config=lambda: {
                "device_pswd": "secret",
                "client_ssh_credentials": [],
            },
            find_device_host_password=lambda *args: "secret",
        )

        result = manager.detach_source_sessions(
            "hcq@172.16.14.66",
            ["1-1"],
            "secret",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["detached_busids"], ["1-1"])

    def test_manual_remote_connect_reclaims_busy_export_once(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations
        from features.cluster import api as cluster_api

        runtime_config = {}

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

            def save_runtime_config(self, data):
                runtime_config.clear()
                runtime_config.update(data)
                return True

        class FakeUsbipManager:
            def __init__(self):
                self.detach_calls = []
                self.bind_calls = []

            def bind_source_devices(self, device_host, busids, device_password):
                self.bind_calls.append(
                    (device_host, list(busids), device_password)
                )
                return {
                    "success": True,
                    "source_host": "172.16.14.66",
                    "busids": list(busids),
                }

            def detach_source_sessions(self, device_host, busids, device_password):
                self.detach_calls.append((device_host, list(busids), device_password))
                return {"success": True, "detached_busids": list(busids)}

        fake_manager = FakeUsbipManager()
        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=SimpleNamespace(
                get_worker=lambda worker_id: {
                    "capabilities": {"usbip_client": True},
                }
            ),
        )
        run_worker = AsyncMock(side_effect=[
            HTTPException(
                status_code=502,
                detail=(
                    "USB设备仍被其他Worker或残留USB/IP会话占用；"
                    "1-1: Device busy (exported)"
                ),
            ),
            {
                "attached_busids": ["1-1"],
                "devices": [{"serial": "USBIP001"}],
                "new_devices": ["USBIP001"],
            },
        ])
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

        with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch("features.devices.usbip.connection_api.usbip_manager", fake_manager), \
                patch.object(
                    integrations.runtime.get_runtime(),
                    "get_client_id_from_request",
                    return_value="hcq@172.16.14.66",
                ), \
                patch.object(cluster_module, "get_cluster_service", return_value=fake_cluster), \
                patch.object(cluster_api, "_require_cluster_enabled"), \
                patch.object(cluster_api, "_run_worker_command", run_worker), \
                patch.object(integrations.asyncio, "sleep", AsyncMock()):
            response = asyncio.run(integrations.start_usbip(
                req=USBIPStartRequest(
                    device_host="hcq@172.16.14.66",
                    worker_id="ats-worker-246",
                    busids=["1-1"],
                    manual_connect=True,
                ),
                request=request,
                help=False,
            ))

        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["success"])
        self.assertTrue(body["recovered_stale_session"])
        self.assertEqual(run_worker.await_count, 2)
        self.assertEqual(
            fake_manager.detach_calls,
            [("hcq@172.16.14.66", ["1-1"], "secret")],
        )
        self.assertEqual(
            fake_manager.bind_calls,
            [
                ("hcq@172.16.14.66", ["1-1"], "secret"),
                ("hcq@172.16.14.66", ["1-1"], "secret"),
            ],
        )
        assignment = runtime_config["usbip_cluster_assignments"][
            "hcq@172.16.14.66|1-1"
        ]
        self.assertEqual(assignment["worker_id"], "ats-worker-246")
        self.assertEqual(assignment["status"], "attached")

    def test_device_error_state_is_recoverable_for_manual_remote_attach(self):
        import features.devices.integrations_api as integrations

        error = HTTPException(
            status_code=502,
            detail=(
                "1-1: usbip: error: Attach Request for 1-1 failed "
                "- Device in error state"
            ),
        )
        self.assertTrue(
            integrations._is_usbip_recoverable_attach_error(error)
        )
        self.assertFalse(integrations._is_usbip_export_busy(error))

    def test_cluster_inventory_is_annotated_from_usbip_assignment(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "device_serials": ["USBIP001"],
                    "status": "attached",
                },
            },
        }

        with patch.object(
            integrations.runtime.get_runtime(),
            "config_manager",
            SimpleNamespace(
                get_runtime_config=lambda: dict(runtime_config)
            ),
        ):
            devices = integrations.annotate_cluster_usbip_devices(
                [{
                    "id": "ats-worker-246:USBIP001",
                    "worker_id": "ats-worker-246",
                    "serial": "USBIP001",
                    "transport": "local_usb",
                    "properties": {"usb": "3-1"},
                }],
                "ats-worker-246",
            )

        self.assertEqual(devices[0]["transport"], "usbip")
        self.assertTrue(devices[0]["properties"]["is_usbip"])
        self.assertEqual(
            devices[0]["properties"]["usbip_source_host"],
            "hcq@172.16.14.66",
        )
        self.assertEqual(devices[0]["properties"]["usbip_busids"], ["1-1"])

    def test_cluster_heartbeat_marks_missing_usbip_device_unknown_and_recovers(
        self,
    ):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-8": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-8",
                    "device_serials": ["RK3576GMS6"],
                    "status": "attached",
                    "timestamp": 1,
                }
            }
        }

        with patch.object(
            integrations.runtime.get_runtime(),
            "config_manager",
            SimpleNamespace(
                get_runtime_config=lambda: runtime_config,
                update_runtime_config=(
                    lambda updates: runtime_config.update(updates) or True
                ),
            ),
        ):
            changed = integrations.reconcile_cluster_usbip_heartbeat(
                "ats-worker-controller",
                [],
            )
            self.assertTrue(changed)
            assignment = runtime_config["usbip_cluster_assignments"][
                "hcq@172.16.14.66|1-8"
            ]
            self.assertEqual(assignment["status"], "unknown")

            changed = integrations.reconcile_cluster_usbip_heartbeat(
                "ats-worker-controller",
                [{"serial": "RK3576GMS6", "state": "available"}],
            )
            self.assertTrue(changed)
            self.assertEqual(assignment["status"], "unknown")
            self.assertEqual(
                runtime_config["usbip_cluster_assignments"][
                    "hcq@172.16.14.66|1-8"
                ]["status"],
                "attached",
            )

    def test_cluster_heartbeat_allows_recent_attach_to_finish_adb_enumeration(
        self,
    ):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["RK3576GMS1"],
                    "status": "attached",
                    "timestamp": 1_000,
                }
            }
        }

        with patch.object(
            integrations.runtime.get_runtime(),
            "config_manager",
            SimpleNamespace(
                get_runtime_config=lambda: runtime_config,
                update_runtime_config=(
                    lambda updates: runtime_config.update(updates) or True
                ),
            ),
        ), patch.object(integrations.time, "time", return_value=1_015):
            changed = integrations.reconcile_cluster_usbip_heartbeat(
                "ats-worker-controller",
                [],
            )

        self.assertFalse(changed)
        self.assertEqual(
            runtime_config["usbip_cluster_assignments"][
                "hcq@172.16.14.66|1-1"
            ]["status"],
            "attached",
        )

    def test_terminal_detach_ack_reconciles_inventory_after_http_timeout(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "device_serials": ["USBIP001"],
                    "status": "attached",
                },
            },
        }

        class FakeConfigManager:
            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        repository = MagicMock()
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ):
            integrations.reconcile_cluster_usbip_command({
                "worker_id": "ats-worker-246",
                "command_type": "usbip_detach",
                "status": "completed",
                "payload": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "busids": ["1-1"],
                },
                "result": {
                    "detached_ports": ["00"],
                    "devices": [{"serial": "LOCAL001"}],
                },
            }, repository)

        repository.refresh_worker_devices.assert_called_once_with(
            "ats-worker-246", [{"serial": "LOCAL001"}]
        )
        self.assertEqual(
            runtime_config["usbip_cluster_assignments"],
            {},
        )

    def test_terminal_attach_ack_persists_new_adb_serial(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "status": "unknown",
                },
            },
        }

        class FakeConfigManager:
            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ):
            integrations.reconcile_cluster_usbip_command({
                "worker_id": "ats-worker-246",
                "command_type": "usbip_attach",
                "status": "completed",
                "payload": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "busids": ["1-1"],
                },
                "result": {
                    "new_devices": ["USBIP001"],
                    "devices": [{"serial": "USBIP001"}],
                },
            }, MagicMock())

        assignment = runtime_config["usbip_cluster_assignments"][
            "hcq@172.16.14.66|1-1"
        ]
        self.assertEqual(assignment["status"], "attached")
        self.assertEqual(assignment["device_serials"], ["USBIP001"])
        self.assertEqual(assignment["source_host"], "172.16.14.66")

    def test_remote_detach_claims_devices_and_applies_empty_snapshot(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations
        from features.cluster import api as cluster_api

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "device_serials": ["USBIP001"],
                    "status": "attached",
                    "timestamp": 1,
                },
            },
        }

        class FakeConfigManager:
            def load_config(self):
                return {}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        repository = MagicMock()
        repository.get_worker.return_value = {"status": "online"}
        repository.acquire_device_operation_claim.return_value = [{
            "id": "claim-1",
            "device_key": "ats-worker-246:USBIP001",
            "generation": 2,
        }]
        repository.claim_fencing_tokens.return_value = [{
            "lease_id": "claim-1",
            "device_id": "ats-worker-246:USBIP001",
            "generation": 2,
            "attempt_id": "operation-1",
        }]
        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=repository,
            effective_enabled=False,
        )
        run_worker = AsyncMock(return_value={
            "detached_ports": ["00"],
            "devices": [],
        })
        request = SimpleNamespace(
            headers={},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        user = CurrentUser(id="admin-id", username="admin", role="admin")

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=fake_cluster
        ), patch.object(
            cluster_api, "_run_worker_command", run_worker
        ):
            response = asyncio.run(integrations.stop_usbip(
                request=request,
                req=USBIPDisconnectRequest(
                    device_host="hcq@172.16.14.66",
                    source_host="172.16.14.66",
                    worker_id="ats-worker-246",
                    busids=["1-1"],
                ),
                _elevated=user,
            ))

        self.assertEqual(response.status_code, 200)
        repository.acquire_device_operation_claim.assert_called_once()
        command_payload = run_worker.await_args.args[2]
        self.assertEqual(command_payload["devices"], ["USBIP001"])
        self.assertTrue(command_payload["lease_tokens"])
        repository.refresh_worker_devices.assert_called_once_with(
            "ats-worker-246", []
        )
        self.assertNotIn(
            "hcq@172.16.14.66|1-1",
            runtime_config["usbip_cluster_assignments"],
        )

    def test_remote_detach_refuses_an_active_device_claim(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations
        from features.cluster import api as cluster_api

        class FakeConfigManager:
            def load_config(self):
                return {}

            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "hcq@172.16.14.66|1-1": {
                            "device_host": "hcq@172.16.14.66",
                            "source_host": "172.16.14.66",
                            "worker_id": "ats-worker-246",
                            "busid": "1-1",
                            "device_serials": ["USBIP001"],
                            "status": "attached",
                        },
                    },
                }

        repository = MagicMock()
        repository.get_worker.return_value = {"status": "busy"}
        repository.acquire_device_operation_claim.side_effect = ValueError(
            "device is already claimed by active-test"
        )
        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=repository,
        )
        run_worker = AsyncMock()
        user = CurrentUser(id="admin-id", username="admin", role="admin")

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=fake_cluster
        ), patch.object(
            cluster_api, "_require_cluster_enabled"
        ), patch.object(
            cluster_api, "_run_worker_command", run_worker
        ):
            response = asyncio.run(integrations.stop_usbip(
                request=SimpleNamespace(headers={}, client=None),
                req=USBIPDisconnectRequest(
                    device_host="hcq@172.16.14.66",
                    source_host="172.16.14.66",
                    worker_id="ats-worker-246",
                    busids=["1-1"],
                ),
                _elevated=user,
            ))

        self.assertEqual(response.status_code, 409)
        run_worker.assert_not_awaited()

    def test_remote_detach_rejects_duplicate_request_while_detaching(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations
        from features.cluster import api as cluster_api

        class FakeConfigManager:
            def load_config(self):
                return {}

            def get_runtime_config(self):
                return {
                    "usbip_cluster_assignments": {
                        "hcq@172.16.14.66|1-1": {
                            "device_host": "hcq@172.16.14.66",
                            "source_host": "172.16.14.66",
                            "worker_id": "ats-worker-246",
                            "busid": "1-1",
                            "device_serials": ["USBIP001"],
                            "status": "detaching",
                        },
                    },
                }

        repository = MagicMock()
        repository.get_worker.return_value = {"status": "online"}
        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=repository,
        )
        run_worker = AsyncMock()

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=fake_cluster
        ), patch.object(
            cluster_api, "_run_worker_command", run_worker
        ):
            response = asyncio.run(integrations.stop_usbip(
                request=SimpleNamespace(headers={}, client=None),
                req=USBIPDisconnectRequest(
                    device_host="hcq@172.16.14.66",
                    source_host="172.16.14.66",
                    worker_id="ats-worker-246",
                    busids=["1-1"],
                ),
                _elevated=CurrentUser(
                    id="admin-id", username="admin", role="admin"
                ),
            ))

        self.assertEqual(response.status_code, 409)
        repository.acquire_device_operation_claim.assert_not_called()
        run_worker.assert_not_awaited()

    def test_remote_detach_without_serial_mapping_does_not_claim_other_devices(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations
        from features.cluster import api as cluster_api

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-246",
                    "busid": "1-1",
                    "status": "attached",
                },
            },
        }

        class FakeConfigManager:
            def load_config(self):
                return {}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        repository = MagicMock()
        repository.get_worker.return_value = {"status": "busy"}
        fake_cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=repository,
        )
        run_worker = AsyncMock(return_value={
            "detached_ports": [],
            "already_detached": True,
            "devices": [{"serial": "LOCAL001"}],
        })

        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=fake_cluster
        ), patch.object(
            cluster_api, "_require_cluster_enabled"
        ), patch.object(
            cluster_api, "_run_worker_command", run_worker
        ):
            response = asyncio.run(integrations.stop_usbip(
                request=SimpleNamespace(headers={}, client=None),
                req=USBIPDisconnectRequest(
                    device_host="hcq@172.16.14.66",
                    source_host="172.16.14.66",
                    worker_id="ats-worker-246",
                    busids=["1-1"],
                ),
                _elevated=CurrentUser(
                    id="admin-id", username="admin", role="admin"
                ),
            ))

        self.assertEqual(response.status_code, 200)
        repository.acquire_device_operation_claim.assert_not_called()
        command_payload = run_worker.await_args.args[2]
        self.assertEqual(command_payload["devices"], [])
        self.assertEqual(command_payload["lease_tokens"], [])
        repository.refresh_worker_devices.assert_called_once_with(
            "ats-worker-246", [{"serial": "LOCAL001"}]
        )

    def test_attach_devices_reports_only_successful_attach_commands(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return CommandResult(stdout="List of devices attached\n", stderr="", code=0)
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="", stderr="failed to attach", code=1)
                return CommandResult(stdout="", stderr="", code=0)
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        attached, devices = manager._attach_devices(object(), "172.16.14.66", ["85aba5e0-8dbc"])
        self.assertEqual(attached, [])
        self.assertEqual(devices, [])

    def test_attach_devices_accepts_existing_adb_visibility_after_attach_failure(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return CommandResult(stdout="List of devices attached\nUSBIP001\tdevice\n", stderr="", code=0)
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="", stderr="device already attached", code=1)
                if cmd == "sudo -n /usr/bin/usbip port":
                    return CommandResult(
            stdout="Port 00: <Port in Use>\n""  1-1 -> usbip://172.16.14.64:3240/1-1\n",
            stderr="",
            code=0,
        )
                return CommandResult(stdout="", stderr="", code=0)
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        manager.device_sources["USBIP001"] = {"attach_host": "172.16.14.64", "busid": "1-1"}
        with patch("features.devices.usbip.manager.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(
                object(), "172.16.14.64", ["1-1"]
            )
        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])

    def test_successful_attach_does_not_reuse_another_online_serial(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return CommandResult(
                        stdout="List of devices attached\nOLD001\tdevice\n",
                        stderr="",
                        code=0,
                    )
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="attached", stderr="", code=0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    return CommandResult(
                        stdout=(
                            "Port 00: <Port in Use>\n"
                            "  1-1 -> usbip://172.16.14.64:3240/1-1\n"
                        ),
                        stderr="",
                        code=0,
                    )
                return CommandResult(stdout="", stderr="", code=0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        manager.device_sources["OLD001"] = {"attach_host": "172.16.14.65", "busid": "1-1"}
        clock = {"now": 1_000_000.0}

        def advance():
            clock["now"] += 10
            return clock["now"]

        with patch("features.devices.usbip.manager.time.sleep", return_value=None), \
                patch("features.devices.usbip.manager.time.time", side_effect=advance):
            attached, devices = manager._attach_devices(
                object(), "172.16.14.64", ["1-1"]
            )
        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, [])

    def test_attach_devices_waits_until_adb_serial_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls < 4:
                        return CommandResult(stdout="List of devices attached\nLOCAL001\tdevice\n", stderr="", code=0)
                    return CommandResult(stdout="List of devices attached\nLOCAL001\tdevice\nUSBIP001\tdevice\n", stderr="", code=0)
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="attached", stderr="", code=0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    return CommandResult(
            stdout="Port 00: <Port in Use>\n""  1-1 -> usbip://172.16.14.66:3240/1-1\n",
            stderr="",
            code=0,
        )
                return CommandResult(stdout="", stderr="", code=0)
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.manager.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])
        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])
        self.assertGreaterEqual(manager.ssh_manager.adb_calls, 4)

    def test_attach_devices_returns_when_fastboot_protocol_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.fastboot_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return CommandResult(stdout="List of devices attached\n", stderr="", code=0)
                if cmd == "fastboot devices":
                    self.fastboot_calls += 1
                    if self.fastboot_calls < 2:
                        return CommandResult(stdout="", stderr="", code=0)
                    return CommandResult(stdout="FB001\tfastboot\n", stderr="", code=0)
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="attached", stderr="", code=0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    return CommandResult(
            stdout="Port 00: <Port in Use>\n""  1-1 -> usbip://172.16.14.66:3240/1-1\n",
            stderr="",
            code=0,
        )
                return CommandResult(stdout="", stderr="", code=0)
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.manager.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])
        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, [])
        self.assertEqual(manager.ssh_manager.fastboot_calls, 2)

    def test_attach_devices_returns_when_recovery_protocol_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls < 2:
                        return CommandResult(stdout="List of devices attached\n", stderr="", code=0)
                    return CommandResult(stdout="List of devices attached\nREC001\trecovery\n", stderr="", code=0)
                if cmd.startswith("sudo usbip attach"):
                    return CommandResult(stdout="attached", stderr="", code=0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    return CommandResult(
            stdout="Port 00: <Port in Use>\n""  1-1 -> usbip://172.16.14.66:3240/1-1\n",
            stderr="",
            code=0,
        )
                return CommandResult(stdout="", stderr="", code=0)
        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.manager.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])
        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, [])
        self.assertEqual(manager.ssh_manager.adb_calls, 2)

    def test_device_host_password_requires_exact_encrypted_host_record(self):
        config = {
            "client_ssh_credentials": [
                {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "encrypted_password": "enc66"},
                {"device_host": "hcq@172.16.14.67", "username": "hcq", "host": "172.16.14.67", "encrypted_password": "enc67"},
                {"username": "legacy", "password": "legacy-pw"},
            ]
        }
        with patch(
            "foundation.secrets.decrypt_secret",
            side_effect=lambda value: {"enc66": "pw66", "enc67": "pw67"}[value],
        ):
            self.assertEqual(find_device_host_password("hcq@172.16.14.66", config), "pw66")
            self.assertEqual(find_device_host_password("hcq@172.16.14.67", config), "pw67")
        self.assertIsNone(find_device_host_password("legacy@10.0.0.8", config))

    def test_usbip_connect_persists_submitted_password_after_success(self):
        import features.devices.integrations_api as integrations

        saved = {}
        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "", "client_ssh_credentials": []}

            def upsert_device_host_password(self, device_host, password):
                saved["device_host"] = device_host
                saved["password"] = password
                return True

            def get_runtime_config(self):
                return {}

            def update_runtime_config(self, updates):
                saved.setdefault("runtime", {}).update(updates)
                return True

            def save_runtime_config(self, data):
                saved["runtime"] = data
                return True

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None, **kwargs):
                saved["start_args"] = (device_host, device_password, usbip_attach_host)
                return {"success": True, "message": "ok", "device_list": ["USBIP001"]}
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        req = USBIPStartRequest(device_host="hcq@172.16.14.66", device_password="secret")
        with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch("features.devices.usbip.connection_api.usbip_manager", FakeUsbipManager()), \
                patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(saved["start_args"], ("hcq@172.16.14.66", "secret", None))
        self.assertEqual(saved["device_host"], "hcq@172.16.14.66")
        self.assertEqual(saved["password"], "secret")

    def test_usbip_connect_does_not_overwrite_existing_source_from_other_host(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_devices_source": {
                "RK3576GMS3": {"source": "cp2-share@172.16.14.65", "timestamp": 1},
            }
        }

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_pswd": "secret",
                    "client_ssh_credentials": [],
                }

            def get_runtime_config(self):
                return runtime_config

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

            def save_runtime_config(self, data):
                saved = dict(data)
                runtime_config.clear()
                runtime_config.update(saved)
                return True

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None, **kwargs):
                return {
                    "success": True,
                    "message": "ok",
                    "device_list": ["RK3576GMS3"],
                }

        old_sources = dict(global_state.usbip_devices_source)
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source["RK3576GMS3"] = {
                    "source": "cp2-share@172.16.14.65",
                    "timestamp": 1,
                }
            with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                    patch("features.devices.usbip.connection_api.usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="waha@172.16.14.64"):
                response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="waha@172.16.14.64"),
                    request=request,
                    help=False,
                ))

            self.assertEqual(response.status_code, 200)
            with global_state.usbip_devices_source_lock:
                self.assertEqual(
                    global_state.usbip_devices_source["RK3576GMS3"]["source"],
                    "cp2-share@172.16.14.65",
                )
            self.assertEqual(
                runtime_config["usbip_devices_source"]["RK3576GMS3"]["source"],
                "cp2-share@172.16.14.65",
            )
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_usbip_connect_accepts_transport_before_adb_device_is_ready(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None, **kwargs):
                return {
                    "success": True,
                    "message": "attached",
                    "devices": ["1-1"],
                    "device_list": [],
                    "transport_connected": True,
                    "protocol_status": {"mode": "fastboot", "fastboot": ["FB001"]},
                }

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        req = USBIPStartRequest(device_host="hcq@172.16.14.66")

        with patch.object(integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()), \
                patch("features.devices.usbip.connection_api.usbip_manager", FakeUsbipManager()), \
                patch.object(integrations.runtime.get_runtime(), "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertTrue(body["transport_connected"])
        self.assertFalse(body["adb_ready"])
        self.assertEqual(body["protocol_status"]["mode"], "fastboot")

    def test_usbip_connect_refuses_same_serial_as_active_adb_proxy(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        manager = MagicMock()
        manager.list_source_devices.return_value = {
            "success": True,
            "devices": [{
                "busid": "1-8",
                "serial": "RK3576GMS6",
            }],
        }
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
        )
        request = SimpleNamespace(
            headers={}, client=SimpleNamespace(host="127.0.0.1")
        )
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch("features.devices.usbip.connection_api.usbip_manager", manager
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=cluster
        ), patch("features.devices.usbip.connection_api._adb_proxy_target_assignments",
            return_value=[{
                "source_worker_id": "ats-worker-118",
                "devices": ["RK3576GMS6"],
            }],
        ):
            response = asyncio.run(integrations.start_usbip(
                req=USBIPStartRequest(
                    device_host="hcq@172.16.14.66",
                    worker_id="ats-worker-controller",
                    busids=["1-8"],
                    manual_connect=True,
                ),
                request=request,
                help=False,
            ))

        self.assertEqual(response.status_code, 409)
        body = json.loads(response.body.decode("utf-8"))
        self.assertIn("序列号冲突", body["error"])
        manager.start_usbip.assert_not_called()

    def test_usbip_connect_defers_unknown_serial_to_target_side_adb(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        runtime_config = {}

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

            def save_runtime_config(self, data):
                runtime_config.clear()
                runtime_config.update(data)
                return True

        manager = MagicMock()
        manager.list_source_devices.return_value = {
            "success": True,
            "devices": [{"busid": "1-8", "serial": ""}],
        }
        manager.start_usbip.return_value = {
            "success": True,
            "devices": ["1-8"],
            "device_list": ["RK3576GMS6"],
            "transport_connected": True,
        }
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
        )
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch("features.devices.usbip.connection_api.usbip_manager", manager
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=cluster
        ), patch("features.devices.usbip.connection_api._adb_proxy_target_assignments",
            return_value=[{
                "source_worker_id": "ats-worker-246",
                "devices": ["ATS357629"],
            }],
        ):
            response = asyncio.run(integrations.start_usbip(
                req=USBIPStartRequest(
                    device_host="hcq@172.16.14.66",
                    worker_id="ats-worker-controller",
                    busids=["1-8"],
                    manual_connect=True,
                ),
                request=SimpleNamespace(headers={}, client=None),
                help=False,
            ))

        self.assertEqual(response.status_code, 200)
        manager.start_usbip.assert_called_once_with(
            "hcq@172.16.14.66",
            "secret",
            usbip_attach_host=None,
            selected_busids=["1-8"],
            adb_server_socket="tcp:127.0.0.1:5039",
        )

    def test_usbip_unknown_serial_conflict_is_rolled_back_after_attach(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        manager = MagicMock()
        manager.list_source_devices.return_value = {
            "success": True,
            "devices": [{"busid": "1-8", "serial": ""}],
        }
        manager.start_usbip.return_value = {
            "success": True,
            "devices": ["1-8"],
            "device_list": ["ATS357629"],
            "transport_connected": True,
        }
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
        )
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch("features.devices.usbip.connection_api.usbip_manager", manager
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=cluster
        ), patch("features.devices.usbip.connection_api._adb_proxy_target_assignments",
            return_value=[{
                "source_worker_id": "ats-worker-246",
                "devices": ["ATS357629"],
            }],
        ), patch("features.devices.usbip.connection_api._rollback_local_usbip_attach",
            return_value={"success": True, "errors": []},
        ) as rollback:
            response = asyncio.run(integrations.start_usbip(
                req=USBIPStartRequest(
                    device_host="hcq@172.16.14.66",
                    worker_id="ats-worker-controller",
                    busids=["1-8"],
                    manual_connect=True,
                ),
                request=SimpleNamespace(headers={}, client=None),
                help=False,
            ))

        self.assertEqual(response.status_code, 409)
        self.assertIn(
            "已自动回滚",
            json.loads(response.body.decode("utf-8"))["error"],
        )
        rollback.assert_called_once()

    def test_local_usbip_coexists_with_distinct_adb_proxy_serials(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        runtime_config = {}

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        manager = MagicMock()
        manager.start_usbip.return_value = {
            "success": True,
            "device_list": ["RK3576GMS6"],
            "devices": ["1-8"],
            "transport_connected": True,
        }
        manager.list_source_devices.return_value = {
            "success": True,
            "devices": [{
                "busid": "1-8",
                "serial": "RK3576GMS6",
            }],
        }
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
        )
        request = SimpleNamespace(
            headers={}, client=SimpleNamespace(host="127.0.0.1")
        )
        with patch.object(
            integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
        ), patch("features.devices.usbip.connection_api.usbip_manager", manager
        ), patch.object(
            integrations.runtime.get_runtime(),
            "get_client_id_from_request",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            cluster_module, "get_cluster_service", return_value=cluster
        ), patch("features.devices.usbip.connection_api._adb_proxy_target_assignments",
            return_value=[{
                "source_worker_id": "ats-worker-246",
                "devices": ["ATS357629", "ATS357631"],
            }],
        ):
            response = asyncio.run(integrations.start_usbip(
                req=USBIPStartRequest(
                    device_host="hcq@172.16.14.66",
                    worker_id="ats-worker-controller",
                    busids=["1-8"],
                ),
                request=request,
                help=False,
            ))

        self.assertEqual(response.status_code, 200)
        assignment = runtime_config["usbip_cluster_assignments"][
            "hcq@172.16.14.66|1-8"
        ]
        self.assertEqual(assignment["device_serials"], ["RK3576GMS6"])

    def test_local_usbip_persists_source_serial_before_adb_enumerates(self):
        import features.cluster as cluster_module
        import features.devices.integrations_api as integrations

        runtime_config = {}

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        manager = MagicMock()
        manager.start_usbip.return_value = {
            "success": True,
            "devices": ["1-1"],
            "device_list": [],
            "transport_connected": True,
        }
        manager.list_source_devices.return_value = {
            "success": True,
            "devices": [{"busid": "1-1", "serial": "RK3576GMS1"}],
        }
        manager.device_sources = {}
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
        )
        old_sources = {}
        with global_state.usbip_devices_source_lock:
            old_sources.update(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
        try:
            with patch.object(
                integrations.runtime.get_runtime(), "config_manager", FakeConfigManager()
            ), patch("features.devices.usbip.connection_api.usbip_manager", manager
            ), patch.object(
                integrations.runtime.get_runtime(),
                "get_client_id_from_request",
                return_value="hcq@172.16.14.66",
            ), patch.object(
                cluster_module, "get_cluster_service", return_value=cluster
            ), patch("features.devices.usbip.connection_api._adb_proxy_target_assignments",
                return_value=[],
            ):
                response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(
                        device_host="hcq@172.16.14.66",
                        worker_id="ats-worker-controller",
                        busids=["1-1"],
                    ),
                    request=SimpleNamespace(headers={}, client=None),
                    help=False,
                ))

            body = json.loads(response.body.decode("utf-8"))
            self.assertEqual(response.status_code, 200)
            self.assertFalse(body["adb_ready"])
            self.assertEqual(body["device_serials"], ["RK3576GMS1"])
            self.assertIn("设备：RK3576GMS1", body["message"])
            self.assertEqual(
                runtime_config["usbip_cluster_assignments"][
                    "hcq@172.16.14.66|1-1"
                ]["device_serials"],
                ["RK3576GMS1"],
            )
            self.assertEqual(
                runtime_config["usbip_devices_source"]["RK3576GMS1"]["source"],
                "hcq@172.16.14.66",
            )
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
