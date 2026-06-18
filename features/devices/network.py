from __future__ import annotations

import subprocess


def run_local_shell_command(
    command: str,
    timeout: int = 30,
) -> tuple[str, str, int]:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", -1
    except Exception as exc:
        return "", str(exc), -1
