from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _pinned_version(package: str) -> tuple[int, ...]:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    match = re.search(
        rf"(?m)^{re.escape(package)}==(?P<version>[0-9]+(?:\.[0-9]+)+)$",
        requirements,
    )
    assert match, f"{package} must have an auditable exact version"
    return tuple(int(part) for part in match.group("version").split("."))


def test_security_sensitive_http_and_crypto_pins_include_upstream_fixes():
    assert _pinned_version("cryptography") >= (49, 0, 0)
    assert _pinned_version("requests") >= (2, 33, 0)
    assert _pinned_version("requests-toolbelt") >= (1, 0, 0)
