from __future__ import annotations

import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCK_SCRIPT = PROJECT_ROOT / "scripts" / "run_Device_Lock.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_lock_script_does_not_issue_second_reboot_when_transition_fails(
    tmp_path: Path,
) -> None:
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
""",
    )

    result = subprocess.run(
        ["bash", str(LOCK_SCRIPT), "RK3572GMS1", "board:unlock"],
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
    assert command_log.read_text().splitlines() == [
        "-s RK3572GMS1 oem board:unlock",
        "-s RK3572GMS1 reboot fastboot",
    ]


def test_lock_script_waits_for_fastbootd_before_final_reboot(
    tmp_path: Path,
) -> None:
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
        ["bash", str(LOCK_SCRIPT), "RK3572GMS1", "board:unlock"],
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
    assert command_log.read_text().splitlines() == [
        "-s RK3572GMS1 oem board:unlock",
        "-s RK3572GMS1 reboot fastboot",
        "-s RK3572GMS1 getvar is-userspace",
        "-s RK3572GMS1 reboot",
    ]
