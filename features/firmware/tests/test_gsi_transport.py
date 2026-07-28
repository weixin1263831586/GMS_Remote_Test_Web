from __future__ import annotations

import shlex
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware.gsi_transport import prepare_gsi_command


def test_prepare_gsi_command_keeps_decisions_in_python() -> None:
    ssh_manager = SimpleNamespace()
    prepared = SimpleNamespace(oem_argument=lambda action: "board:unlock")

    with patch(
        "features.firmware.gsi_transport.FastbootPreparer"
    ) as preparer:
        preparer.return_value.prepare_bootloader.return_value = prepared
        command = prepare_gsi_command(
            ssh=object(),
            ssh_manager=ssh_manager,
            remote_script="/suite/run_GSI_Burn.sh",
            device="RK3572GMS1",
            system_img="/suite/system.img",
            misc_img="/suite/misc.img",
            vendor_img="/suite/boot-debug.img",
        )

    assert shlex.split(command) == [
        "/suite/run_GSI_Burn.sh",
        "RK3572GMS1",
        "board:unlock",
        "/suite/system.img",
        "/suite/misc.img",
        "boot",
        "/suite/boot-debug.img",
    ]
