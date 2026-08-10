import os
import shlex
import subprocess
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_GMS_Test_Auto.sh"


def _write_result(path: Path, *, module: str, case: str, serial: str) -> None:
    path.mkdir(parents=True)
    (path / "test_result.xml").write_text(
        '<Result command_line_args="vts '
        f'-m {module} -t {case} -s {serial} --disable-reboot">\n'
        '  <Summary pass="0" failed="1" />\n'
        '</Result>\n',
        encoding="utf-8",
    )


def test_vts_result_fallback_matches_current_invocation(tmp_path):
    suite_root = tmp_path / "android-vts"
    tools_path = suite_root / "tools"
    tools_path.mkdir(parents=True)
    matching = suite_root / "results" / "2026.07.16_21.07.12"
    unrelated = suite_root / "results" / "2026.07.16_21.07.13"
    _write_result(
        matching,
        module="vts_generic_boot_image_test",
        case="GenericBootImageTest#GenericRamdisk",
        serial="c3d9b8674f4b94f6",
    )
    _write_result(
        unrelated,
        module="VtsOtherTest",
        case="OtherTest#testOther",
        serial="other-device",
    )
    started_at = int(time.time()) - 5
    os.utime(matching / "test_result.xml", (started_at + 1, started_at + 1))
    os.utime(unrelated / "test_result.xml", (started_at + 2, started_at + 2))
    log_path = tmp_path / "tradefed.log"
    log_path.touch()

    assignments = {
        "LOG_FILE": str(log_path),
        "SUITE_PATH": str(tools_path),
        "TEST_COMMAND": "vts",
        "Test_Module": "vts_generic_boot_image_test",
        "Test_Case": "GenericBootImageTest#GenericRamdisk",
        "DEVICE_ARGS": "-s c3d9b8674f4b94f6",
        "RUN_STARTED_EPOCH": str(started_at),
    }
    shell = [f"source {shlex.quote(str(SCRIPT))}"]
    shell.extend(f"{name}={shlex.quote(value)}" for name, value in assignments.items())
    shell.extend(["analyze_result", 'printf "resolved=%s pass=%s fail=%s\\n" "$RESULT_DIR" "$PASS_COUNT" "$FAIL_COUNT"'])
    completed = subprocess.run(
        ["bash", "-c", "\n".join(shell)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"RESULT DIRECTORY: {matching}" in completed.stdout
    assert f"resolved={matching} pass=0 fail=1" in completed.stdout
    assert str(unrelated) not in completed.stdout


def test_tradefed_arguments_are_not_evaluated_as_shell_code(tmp_path):
    suite_path = tmp_path / "tools"
    suite_path.mkdir()
    capture_path = tmp_path / "argv.bin"
    marker_path = tmp_path / "injected"
    tradefed = suite_path / "cts-tradefed"
    tradefed.write_text(
        '#!/bin/bash\nprintf "%s\\0" "$@" > "$CAPTURE_PATH"\n',
        encoding="utf-8",
    )
    tradefed.chmod(0o755)
    log_path = tmp_path / "tradefed.log"

    assignments = {
        "LOG_FILE": str(log_path),
        "CAPTURE_PATH": str(capture_path),
        "SUITE_PATH": str(suite_path),
        "SUITE_PREFIX": "cts",
        "TEST_COMMAND": "cts",
        "Test_Type": "cts",
        "Test_Module": f"CtsSecurityTestCases; touch {marker_path}",
        "Test_Case": "SecurityTest#testArgumentBoundary",
        "DEVICE_ARGS": "-s SAFE-SERIAL",
        "PROCESS_GROUP_ID": "",
    }
    shell = [f"source {shlex.quote(str(SCRIPT))}"]
    shell.extend(f"{name}={shlex.quote(value)}" for name, value in assignments.items())
    shell.append("export CAPTURE_PATH")
    shell.append("run_tradefed run")

    subprocess.run(
        ["bash", "-c", "\n".join(shell)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert not marker_path.exists()
    argv = capture_path.read_bytes().split(b"\0")
    assert f"CtsSecurityTestCases; touch {marker_path}".encode() in argv
    assert b"SecurityTest#testArgumentBoundary" in argv
    assert b"SAFE-SERIAL" in argv
