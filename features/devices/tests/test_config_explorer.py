import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.devices.configuration import config_explorer
from foundation.command_result import CommandResult


class ConfigExplorerTests(unittest.TestCase):
    def test_aapt2_path_accepts_separate_build_tools_config(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "aapt2"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            with patch.dict(
                "os.environ", {"GMS_AAPT2_PATH": str(binary)}, clear=False
            ):
                self.assertEqual(config_explorer._aapt2_path(), str(binary))

    def test_resource_parse_cache_returns_independent_entries(self):
        output = """
resource 0x01040000 bool/config_example
  () true
"""
        completed = subprocess.CompletedProcess(
            ["aapt2"], 0, stdout=output, stderr=""
        )
        config_explorer._parse_apk_resource_records.cache_clear()
        with patch.object(
            config_explorer, "_aapt2_path", return_value="aapt2"
        ), patch.object(
            config_explorer.subprocess, "run", return_value=completed
        ) as run:
            first = config_explorer.parse_apk_resources("/tmp/content-key.apk")
            first[0].default_value = "mutated"
            second = config_explorer.parse_apk_resources("/tmp/content-key.apk")

        self.assertEqual(run.call_count, 1)
        self.assertEqual(second[0].default_value, "true")
        config_explorer._parse_apk_resource_records.cache_clear()

    def test_apk_cache_is_scoped_to_device_build_and_remote_file(self):
        pulls = []

        def run_command(command, timeout):
            if command[-3:] == ["shell", "getprop", "ro.build.fingerprint"]:
                fingerprint = "build-a" if "device-a" in command else "build-b"
                return CommandResult(stdout=fingerprint, stderr="", code=0)
            if command[3:5] == ["shell", "stat"]:
                return CommandResult(stdout="4096:1700000000", stderr="", code=0)
            if "pull" in command:
                destination = command[-1]
                Path(destination).write_bytes(b"apk")
                pulls.append(command)
                return CommandResult(stdout="pulled", stderr="", code=0)
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            config_explorer, "_APK_CACHE_DIR", directory
        ), patch.object(config_explorer, "_adb_path", return_value="adb"), patch.object(
            config_explorer, "run_local_command", side_effect=run_command
        ):
            first = config_explorer._pull_apk(
                "device-a", "/system/framework/framework-res.apk", "android"
            )
            second = config_explorer._pull_apk(
                "device-b", "/system/framework/framework-res.apk", "android"
            )
            repeated = config_explorer._pull_apk(
                "device-a", "/system/framework/framework-res.apk", "android"
            )

        self.assertNotEqual(first, second)
        self.assertEqual(first, repeated)
        self.assertEqual(len(pulls), 2)

    def test_failed_apk_pull_does_not_publish_partial_cache_file(self):
        attempts = 0

        def run_command(command, timeout):
            nonlocal attempts
            if command[-3:] == ["shell", "getprop", "ro.build.fingerprint"]:
                return CommandResult(stdout="build-a", stderr="", code=0)
            if command[3:5] == ["shell", "stat"]:
                return CommandResult(stdout="4096:1700000000", stderr="", code=0)
            if "pull" in command:
                attempts += 1
                destination = command[-1]
                Path(destination).write_bytes(
                    b"partial" if attempts == 1 else b"complete-apk"
                )
                return (
                    CommandResult(stdout="", stderr="transfer interrupted", code=1)
                    if attempts == 1 else CommandResult(stdout="pulled", stderr="", code=0)
                )
            raise AssertionError(command)

        with tempfile.TemporaryDirectory() as directory, patch.object(
            config_explorer, "_APK_CACHE_DIR", directory
        ), patch.object(config_explorer, "_adb_path", return_value="adb"), patch.object(
            config_explorer, "run_local_command", side_effect=run_command
        ):
            with self.assertRaisesRegex(RuntimeError, "transfer interrupted"):
                config_explorer._pull_apk(
                    "device-a", "/system/framework/framework-res.apk", "android"
                )
            self.assertEqual(list(Path(directory).iterdir()), [])
            cached = config_explorer._pull_apk(
                "device-a", "/system/framework/framework-res.apk", "android"
            )
            self.assertEqual(Path(cached).read_bytes(), b"complete-apk")
            self.assertEqual(attempts, 2)

    def test_enabled_overlays_are_grouped_by_target_package(self):
        overlay_output = """
com.android.networkstack
[x] com.rockchip.overlay.networkstack.aosp

android
[x] com.rockchip.overlay.framework.res.common
[ ] com.android.internal.systemui.navbar.threebutton

com.android.phone
[x] com.android.phone.auto_generated_characteristics_rro
"""

        with patch.object(config_explorer, "_adb_path", return_value="adb"), patch.object(
            config_explorer, "run_local_command",
            return_value=CommandResult(stdout=overlay_output, stderr="", code=0),
        ):
            grouped = config_explorer._enabled_overlays_by_target(None)

        self.assertEqual(
            grouped,
            {
                "com.android.networkstack": ["com.rockchip.overlay.networkstack.aosp"],
                "android": ["com.rockchip.overlay.framework.res.common"],
                "com.android.phone": ["com.android.phone.auto_generated_characteristics_rro"],
            },
        )

    def test_pull_keeps_paths_and_serial_as_literal_argv_elements(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = str(Path(directory) / 'download folder' / 'target.apk')
            remote = "/sdcard/file name';&literal.apk"

            def pull(argv, timeout):
                Path(argv[-1]).write_bytes(b'complete-apk')
                return CommandResult(stdout='pulled', stderr='', code=0)

            with patch.object(config_explorer, '_adb_path', return_value='/opt/android sdk/adb'), patch.object(
                config_explorer, 'run_local_command', side_effect=pull,
            ) as run:
                config_explorer.pull_device_file('serial with spaces', remote, destination, timeout=10)

            run.assert_called_once_with([
                '/opt/android sdk/adb', '-s', 'serial with spaces', 'pull', remote, destination,
            ], timeout=10)

    def test_file_size_preserves_device_shell_quoting(self):
        remote = "/sdcard/file name';literal.apk"
        with patch.object(config_explorer, '_adb_path', return_value='adb'), patch.object(
            config_explorer, 'run_local_command', return_value=CommandResult(stdout='123', stderr='', code=0),
        ) as run:
            self.assertEqual(config_explorer.device_file_size('serial', remote), 123)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:7], ['adb', '-s', 'serial', 'shell', 'stat', '-c', '%s'])
        self.assertEqual(argv[-1], "'/sdcard/file name'\\'';literal.apk'")


if __name__ == "__main__":
    unittest.main()
