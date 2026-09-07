"""Guard: the default Controller Worker ID must stay out of business logic.

``ats-worker-controller`` is only the *default* value of
``foundation.cluster_port.DEFAULT_LOCAL_WORKER_ID``. Deployments may configure
a different ``local_worker_id`` in the cluster config, so any business code
that compares a worker ID against the literal silently breaks ownership,
report-host fallbacks and shell targeting for those deployments.

Allowed locations are exactly: the constant definition itself (foundation),
config/example fixtures, scripts, tooling, and tests. Python business modules
must use ``foundation.cluster_port.get_local_worker_id()`` instead; client
scripts keep the literal only as a bootstrap-missing last resort.
"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARDCODED_WORKER_ID = "ats-worker-controller"

# Business code must route through foundation.cluster_port.get_local_worker_id().
_PYTHON_ALLOWED = {
    "foundation/cluster_port.py",  # the single source of truth
}

# Client scripts keep the literal as a bootstrap-missing fallback only; they
# must not use it as a standalone assignment of a business variable.
_JS_ALLOWED = {
    "web/static/js/workspace-context.js",  # fallback inside bootstrap chain
    "web/static/js/shell/workspace-devices.js",  # fallback at chain tail
    "features/automation/ui/page.js",  # fallback inside bootstrap chain
    "features/cluster/ui/page.js",  # fallback inside bootstrap chain
}


def _iter_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for base in ("features", "foundation", "web", "workflows"):
        for path in (ROOT / base).rglob("*"):
            if not path.is_file():
                continue
            relative = str(path.relative_to(ROOT)).replace("\\", "/")
            if "__pycache__" in relative or "/tests/" in relative:
                continue
            if path.suffix not in (".py", ".js", ".html"):
                continue
            try:
                sources.append((relative, path.read_text(encoding="utf-8")))
            except UnicodeDecodeError:
                continue
    return sources


class LocalWorkerIdHardcodeTests(unittest.TestCase):
    def test_python_business_code_has_no_hardcoded_local_worker_id(self):
        offenders: list[str] = []
        for relative, text in _iter_sources():
            if not relative.endswith(".py"):
                continue
            if relative in _PYTHON_ALLOWED:
                continue
            if "ats-worker-controller" in text:
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            "hardcoded Controller worker ID found outside the allowlist; "
            "use foundation.cluster_port.get_local_worker_id() instead. "
            f"Offenders: {offenders}",
        )

    def test_js_business_code_has_no_standalone_hardcoded_assignment(self):
        offenders: list[str] = []
        for relative, text in _iter_sources():
            if not relative.endswith(".js"):
                continue
            if relative in _JS_ALLOWED:
                continue
            if "ats-worker-controller" in text:
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            "hardcoded Controller worker ID found outside the allowlist; "
            "read window.__GMS_BOOTSTRAP__.localWorkerId instead. "
            f"Offenders: {offenders}",
        )

    def test_get_local_worker_id_falls_back_to_default(self):
        from foundation.cluster_port import (
            DEFAULT_LOCAL_WORKER_ID,
            get_local_worker_id,
        )

        # In unit-test context the cluster service is typically not
        # configured; the accessor must degrade to the default instead of
        # raising.
        try:
            worker_id = get_local_worker_id()
        except Exception as exc:  # pragma: no cover - failure mode guard
            self.fail(f"get_local_worker_id() raised: {exc}")
        self.assertEqual(worker_id, DEFAULT_LOCAL_WORKER_ID)
        self.assertEqual(DEFAULT_LOCAL_WORKER_ID, "ats-worker-controller")

    def test_config_default_derives_from_foundation_constant(self):
        from features.cluster.config import ClusterConfig
        from foundation.cluster_port import DEFAULT_LOCAL_WORKER_ID

        self.assertEqual(
            ClusterConfig().local_worker_id, DEFAULT_LOCAL_WORKER_ID
        )


if __name__ == "__main__":
    unittest.main()
