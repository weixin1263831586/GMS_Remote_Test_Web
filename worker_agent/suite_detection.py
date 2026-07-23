from __future__ import annotations

import re
from pathlib import Path


def suite_details(path: Path) -> tuple[str, str]:
    """Return the suite family and release parsed from a Tradefed path."""
    lowered = str(path).lower()
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
        r"(?:android-)?(?:cts|gts|vts|sts)[-_]([0-9]+(?:_r[0-9]+)?)",
        lowered,
    )
    return suite_type, match.group(1) if match else ""
