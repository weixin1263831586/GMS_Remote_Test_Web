import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker_agent import adb_proxy


class WorkerAdbProxyTests(unittest.TestCase):
    def test_managed_hub_uses_single_user_aggregator(self):
        process = SimpleNamespace(pid=1234, poll=lambda: None)

        with (
            patch.object(adb_proxy, "_state_root", return_value=Path("/tmp/adb-state")),
            patch.object(adb_proxy, "_stop_managed"),
            patch.object(adb_proxy, "_binary", return_value="/opt/adb-hub"),
            patch.object(
                adb_proxy,
                "_read_hub_config",
                return_value={
                    "backend": [
                        {"name": "one"},
                        {"name": "two"},
                    ],
                },
            ),
            patch.object(adb_proxy.subprocess, "run"),
            patch.object(adb_proxy.subprocess, "Popen", return_value=process) as popen,
            patch.object(adb_proxy, "_write_pid"),
            patch.object(adb_proxy, "_wait_tcp", return_value=True),
            patch.object(
                adb_proxy, "_wait_adb_server", return_value=(True, "")
            ) as wait_adb,
        ):
            adb_proxy._restart_hub(Path("/tmp/hub.toml"))

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/opt/adb-hub")
        self.assertIn("--daemon", command)
        self.assertIn("--single-user", command)
        wait_adb.assert_called_once_with(process, timeout=45.0)

    def test_wait_adb_server_retries_transient_connection_refused(self):
        process = SimpleNamespace(poll=lambda: None)
        with (
            patch.object(
                adb_proxy,
                "_adb_devices_safe",
                side_effect=[
                    RuntimeError(
                        "cannot connect to daemon at tcp:5037: Connection refused"
                    ),
                    [],
                ],
            ) as devices,
            patch.object(adb_proxy.time, "sleep"),
        ):
            ready, error = adb_proxy._wait_adb_server(process, timeout=1)

        self.assertTrue(ready)
        self.assertEqual(error, "")
        self.assertEqual(devices.call_count, 2)


if __name__ == "__main__":
    unittest.main()
