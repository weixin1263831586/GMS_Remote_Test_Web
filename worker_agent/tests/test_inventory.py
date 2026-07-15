from unittest.mock import patch

from worker_agent.inventory import probe_devices


def test_detailed_probe_enriches_management_properties_in_one_adb_shell_call():
    def run(argv, timeout=10):
        if argv[:3] == ["adb", "devices", "-l"]:
            return "List of devices attached\nABC device product:rk model:Box transport_id:1\n"
        if argv[:4] == ["adb", "-s", "ABC", "shell"]:
            return (
                "__MODEL__\nLiving Room Box\n"
                "__ANDROID__\n14\n"
                "__BATTERY__\n  level: 82\n"
                "__SOC__\nRK3588S\n"
            )
        if argv[:2] == ["fastboot", "devices"]:
            return ""
        raise AssertionError(argv)

    with patch("worker_agent.inventory._run", side_effect=run) as runner:
        devices = probe_devices(include_details=True)

    assert devices[0]["properties"] == {
        "product": "rk",
        "model": "Living Room Box",
        "transport_id": "1",
        "android_version": "14",
        "battery_level": "82",
        "soc_model": "RK3588S",
    }
    assert sum(call.args[0][:4] == ["adb", "-s", "ABC", "shell"] for call in runner.call_args_list) == 1


def test_lightweight_probe_skips_detail_shell_call():
    with patch(
        "worker_agent.inventory._run",
        side_effect=[
            "List of devices attached\nABC device product:rk model:Box\n",
            "",
        ],
    ) as runner:
        devices = probe_devices()

    assert devices[0]["properties"]["model"] == "Box"
    assert runner.call_count == 2
