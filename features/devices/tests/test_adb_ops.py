import unittest
from unittest.mock import patch

from features.devices.adb_ops import (
    fastboot_reboot_with_runner,
    reboot_with_runner,
)
from features.devices.manager import DeviceManager


class RecordingRunner:
    """按命令记录调用并返回预设结果的 adb/fastboot runner。

    adb runner 以 (device_id, args, timeout) 调用，fastboot runner 以
    (args, timeout) 调用，命令名都位于倒数第二个位置参数。
    """

    def __init__(self, results):
        self.results = dict(results)
        self.calls = []

    def __call__(self, *call_args):
        self.calls.append(call_args)
        return self.results[call_args[-2]]


class FastbootRebootRunnerTests(unittest.TestCase):
    def test_fastboot_reboot_waits_for_adb_device_state(self):
        run_adb = RecordingRunner({'get-state': ('device', 0)})
        run_fastboot = RecordingRunner({'reboot': ('Rebooting', 0)})

        result = fastboot_reboot_with_runner(
            run_fastboot, run_adb, 'FB001', wait_for_online=True,
        )

        self.assertTrue(result.success)
        self.assertTrue(result.back_online)
        self.assertEqual(run_fastboot.calls, [('reboot', 30)])

    def test_fastboot_reboot_failure_reports_command_output(self):
        run_adb = RecordingRunner({})
        run_fastboot = RecordingRunner({'reboot': ("< waiting for any device >", 1)})

        result = fastboot_reboot_with_runner(
            run_fastboot, run_adb, 'FB001', wait_for_online=True,
        )

        self.assertFalse(result.success)
        self.assertFalse(result.back_online)
        self.assertEqual(run_adb.calls, [])

    def test_fastboot_reboot_without_wait_returns_immediately(self):
        run_adb = RecordingRunner({})
        run_fastboot = RecordingRunner({'reboot': ('Rebooting', 0)})

        result = fastboot_reboot_with_runner(
            run_fastboot, run_adb, 'FB001', wait_for_online=False,
        )

        self.assertTrue(result.success)
        self.assertFalse(result.back_online)
        self.assertEqual(run_adb.calls, [])

    def test_adb_error_text_does_not_count_as_online(self):
        # 设备未枚举时 adb 报错文本含 "device" 字样，不能误判为已上线。
        run_adb = RecordingRunner(
            {"get-state": ("adb: device 'FB001' not found", 1)},
        )
        run_fastboot = RecordingRunner({'reboot': ('Rebooting', 0)})

        result = fastboot_reboot_with_runner(
            run_fastboot, run_adb, 'FB001',
            wait_for_online=True, wait_timeout=0, poll_interval=0.01,
        )

        self.assertTrue(result.success)
        self.assertFalse(result.back_online)

    def test_adb_reboot_still_waits_for_online(self):
        run_adb = RecordingRunner({'reboot': ('', 0), 'get-state': ('device', 0)})

        result = reboot_with_runner(run_adb, 'ADB001', wait_for_online=True)

        self.assertTrue(result.success)
        self.assertTrue(result.back_online)


class _FakeSshManager:
    def __init__(self, outputs):
        self.outputs = dict(outputs)
        self.commands = []

    def get_connection(self, _config):
        return object()

    def return_connection(self, _ssh):
        pass

    def execute_command(self, _ssh, command, timeout=None):
        self.commands.append(command)
        return self.outputs.get(command, ('', '', 0))


class _FakeConfigManager:
    def load_config(self):
        return {'ubuntu_host': '192.0.2.10'}


class RebootDeviceFastbootPathTests(unittest.TestCase):
    def test_reboot_device_uses_fastboot_channel_for_fastbootd_device(self):
        ssh_manager = _FakeSshManager({
            'fastboot -s rk3572test reboot': ('Rebooting', '', 0),
            'adb -s rk3572test get-state': ('device', '', 0),
        })
        manager = DeviceManager(
            ssh_manager=ssh_manager,
            config_manager=_FakeConfigManager(),
        )

        with patch.object(
            DeviceManager, 'get_fastboot_devices', return_value=['rk3572test'],
        ):
            result = manager.reboot_device('rk3572test', wait_for_online=True)

        self.assertTrue(result['success'])
        self.assertTrue(result['back_online'])
        self.assertEqual(
            ssh_manager.commands,
            ['fastboot -s rk3572test reboot', 'adb -s rk3572test get-state'],
        )

    def test_reboot_device_keeps_adb_channel_for_online_device(self):
        ssh_manager = _FakeSshManager({
            'adb -s ADB001 reboot': ('', '', 0),
            'adb -s ADB001 get-state': ('device', '', 0),
        })
        manager = DeviceManager(
            ssh_manager=ssh_manager,
            config_manager=_FakeConfigManager(),
        )

        with patch.object(DeviceManager, 'get_fastboot_devices', return_value=[]):
            result = manager.reboot_device('ADB001', wait_for_online=True)

        self.assertTrue(result['success'])
        self.assertTrue(result['back_online'])
        self.assertEqual(
            ssh_manager.commands,
            ['adb -s ADB001 reboot', 'adb -s ADB001 get-state'],
        )


if __name__ == '__main__':
    unittest.main()
