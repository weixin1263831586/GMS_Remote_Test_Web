from __future__ import annotations

import os
import subprocess
from pathlib import Path

from features.firmware.gsi_diagnostics import diagnose_gsi_burn_failure


PROJECT_ROOT = Path(__file__).resolve().parents[3]
GSI_SCRIPT = PROJECT_ROOT / "scripts" / "run_GSI_Burn.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_gsi_script_stops_when_fastbootd_transition_fails(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "fastboot.log"
    _write_executable(
        bin_dir / "fastboot",
        """#!/bin/bash
printf '%s\\n' "$*" >> "$FASTBOOT_LOG"
if [[ "$3" == "reboot" && "$4" == "fastboot" ]]; then
    exit 1
fi
exit 0
""",
    )

    result = subprocess.run(
        [
            "bash",
            str(GSI_SCRIPT),
            "RK3572GMS1",
            "board:unlock",
            "/images/system.img",
            "/images/misc.img",
            "vendor_boot",
            "/images/vendor_boot-debug.img",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FASTBOOT_LOG": str(command_log),
        },
        check=False,
    )

    assert result.returncode != 0
    assert command_log.read_text().splitlines() == [
        "-s RK3572GMS1 getvar is-userspace",
        "-s RK3572GMS1 oem board:unlock",
        "-s RK3572GMS1 reboot fastboot",
    ]


def test_gsi_script_waits_for_fastbootd_before_flashing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "fastboot.log"
    _write_executable(
        bin_dir / "fastboot",
        """#!/bin/bash
printf '%s\\n' "$*" >> "$FASTBOOT_LOG"
if [[ "$3" == "getvar" && "$4" == "is-userspace" ]]; then
    printf 'is-userspace: yes\\n' >&2
fi
exit 0
""",
    )

    result = subprocess.run(
        [
            "bash",
            str(GSI_SCRIPT),
            "RK3572GMS1",
            "board:unlock",
            "/images/system.img",
            "/images/misc.img",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FASTBOOT_LOG": str(command_log),
        },
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text().splitlines()
    assert commands[:2] == [
        "-s RK3572GMS1 getvar is-userspace",
        "-s RK3572GMS1 delete-logical-partition product",
    ]
    assert "-s RK3572GMS1 reboot fastboot" not in commands


def test_locked_fastboot_output_has_actionable_diagnosis() -> None:
    message = diagnose_gsi_burn_failure(
        "Deleting 'product' FAILED "
        "(remote: 'Command not available on locked devices')\n"
        "fastboot: error: Command failed"
    )

    assert "自动解锁未成功" in message
    assert "OEM 解锁" in message
