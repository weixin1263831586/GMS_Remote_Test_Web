import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from worker_agent import adb_proxy


class WorkerAdbProxyTests(unittest.TestCase):
    def test_managed_hub_uses_single_user_aggregator(self):
        """Strict readiness must accept a healthy daemon boot lifecycle.

        The restart flow only proceeds when the *fresh* hub log (written
        after the captured offset) contains the daemon's listening marker.
        The fake Popen simulates exactly that boot: the hub appends its
        startup banner plus the readiness marker to its own log, so the
        real ``_hub_log_offset`` / ``_wait_hub_listening`` contract runs
        end-to-end instead of being mocked away.
        """
        process = SimpleNamespace(pid=1234, poll=lambda: None)

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp)
            log_path = state_root / "logs" / "hub.log"

            def fake_popen(command, **kwargs):
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write("adb-hub starting with config /tmp/hub.toml\n")
                    stream.write("adb server version 41 listening on 127.0.0.1:5037\n")
                return process

            with (
                patch.object(adb_proxy, "_state_root", return_value=state_root),
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
                patch.object(
                    adb_proxy.subprocess, "Popen", side_effect=fake_popen
                ) as popen,
                patch.object(adb_proxy, "_write_pid"),
                # The early-exit/bind-error observer polls in real time;
                # its behaviour is covered by dedicated hub-log tests and
                # a healthy boot reports no early exit.
                patch.object(
                    adb_proxy, "_hub_startup_exited", return_value=(False, "")
                ),
                patch.object(adb_proxy.time, "sleep"),
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
        # log_offset is the pre-boot log size (0: no log yet) — the fresh
        # marker written by the fake daemon must be visible after it.
        wait_adb.assert_called_once_with(process, timeout=45.0, log_offset=0)

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
