"""Contract for the allowlisted on-host feature probes."""

import json
from pathlib import Path

from tools.scripts.testing.audit_live_modules import PROBES, verdict


def test_live_allowlist_uses_only_declared_static_get_routes():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "tests/contract/snapshots/routes.json").read_text())
    known = {x["path"] for x in data if "GET" in x.get("methods", [])}
    assert len(PROBES) == len({x[1] for x in PROBES})
    assert len({x[0] for x in PROBES}) >= 15
    assert all(p in known for _domain, p, _required in PROBES)
    assert all(p.startswith("/api/") and "{" not in p for _, p, _ in PROBES)


def test_live_verdicts_distinguish_unavailable_and_broken():
    assert verdict(200, True) == "PASS"
    assert verdict(500, False) == "FAIL"
    assert verdict(503, False) == "UNAVAILABLE"
    assert verdict(401, True) == "FAIL"
    assert verdict(None, True) == "FAIL"
