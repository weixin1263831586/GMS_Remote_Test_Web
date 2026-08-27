from __future__ import annotations

import json

from features.devices.physical_identity import resolve_physical_device_identity
from features.devices.usbip_identity import (
    query_usbipd_busid_instance_ids,
    query_windows_usb_identities,
)


def test_android_serial_keeps_physical_id_stable_when_busid_changes():
    first = resolve_physical_device_identity(
        source_host="user@10.0.0.1",
        current_usb_busid="1-2",
        logical_android_serial="ANDROID-001",
    )
    second = resolve_physical_device_identity(
        source_host="user@10.0.0.2",
        current_usb_busid="3-4",
        logical_android_serial="ANDROID-001",
    )

    assert first.physical_device_id == second.physical_device_id
    assert first.identity_source == "android_serial"
    assert first.identity_stable is True
    assert second.current_usb_busid == "3-4"


def test_busid_only_identity_is_explicitly_transient_and_host_scoped():
    identity = resolve_physical_device_identity(
        source_host="user@10.0.0.1",
        current_usb_busid="1-2",
    )

    assert identity.identity_source == "usb_busid"
    assert identity.identity_stable is False
    assert identity.source_host == "user@10.0.0.1"


def test_usbipd_state_maps_same_vid_pid_devices_by_busid():
    class FakeSshManager:
        @staticmethod
        def execute_command(_ssh, command, timeout=None, get_pty=False):
            assert command == "usbipd state"
            return (
                json.dumps({
                    "Devices": [
                        {
                            "BusId": "1-2",
                            "InstanceId": "USB\\VID_2207&PID_0006\\SERIAL-A",
                        },
                        {
                            "BusId": "1-3",
                            "InstanceId": "USB\\VID_2207&PID_0006\\SERIAL-B",
                        },
                    ]
                }),
                "",
                0,
            )

    assert query_usbipd_busid_instance_ids(FakeSshManager(), object()) == {
        "1-2": "USB\\VID_2207&PID_0006\\SERIAL-A",
        "1-3": "USB\\VID_2207&PID_0006\\SERIAL-B",
    }


def test_duplicate_vid_pid_pnp_details_remain_addressable_by_instance_id():
    class FakeSshManager:
        @staticmethod
        def execute_command(_ssh, _command, timeout=None, get_pty=False):
            return (
                "USB\\VID_2207&PID_0006\\SERIAL-A|PCIROOT(0)#USBROOT(0)#USB(2)|CID-A\n"
                "USB\\VID_2207&PID_0006\\SERIAL-B|PCIROOT(0)#USBROOT(0)#USB(3)|CID-B\n",
                "",
                0,
            )

    identities = query_windows_usb_identities(
        FakeSshManager(), object(), {"2207"}
    )

    assert "2207:0006" not in identities
    assert identities[
        "pnp:usb\\vid_2207&pid_0006\\serial-a"
    ]["container_id"] == "CID-A"
    assert identities[
        "pnp:usb\\vid_2207&pid_0006\\serial-b"
    ]["location_path"].endswith("USB(3)")
