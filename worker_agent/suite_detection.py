from __future__ import annotations

import re
from pathlib import Path


def suite_details(path: Path) -> tuple[str, str]:
    """Return the suite family and release parsed from a Tradefed path."""
    lowered = str(path).lower()
    # cts-v-host-tradefed 是 CTS Verifier 的主机端启动器，归入 cts-v 家族
    # （与 Controller 端 test_execution.suites 的映射一致）。
    if "cts-v-host-tradefed" in path.name.lower() or "cts-verifier" in lowered:
        suite_type = "CTS_V"
    else:
        suite_type = next(
            (
                name
                for name in ("CTS", "GTS", "VTS", "STS")
                if f"{name.lower()}-tradefed" in path.name.lower()
                or f"android-{name.lower()}" in lowered
            ),
            "XTS",
        )
    match = re.search(
        r"(?:android-)?(?:cts|gts|vts|sts)[-_]?(?:verifier-)?([0-9]+(?:_r[0-9]+)?)",
        lowered,
    )
    return suite_type, match.group(1) if match else ""
