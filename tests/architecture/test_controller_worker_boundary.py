"""Controller -> worker_agent import boundary gate (AGENTS.md hard rule).

``features/`` must not reach into ``worker_agent/`` implementation except for
the audited same-host bridge surfaces listed below (ADR 0004: Controller and
Worker interact across the SSH execution boundary; these modules are the
documented same-host exceptions).  New imports of ``worker_agent`` from
``features/`` fail here until the allowlist is extended deliberately in
review — shrinking the allowlist (sinking shared logic into ``foundation/``)
is the intended direction.

The reverse direction is also snapshotted: ``worker_agent`` must not import
``features/`` except for the shared device-action spec contract, which is a
pure specification module without feature-internal dependencies.
"""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Exact allowlist of features/ files that may import worker_agent modules.
ALLOWED_FEATURES_IMPORTING_WORKER = frozenset(
    {
        # Same-host bridge: the documented exception in AGENTS.md.
        "features/cluster/local_bridge.py",
        # Cluster suite transfer/execution surfaces (worker-role execution).
        "features/cluster/api.py",
        "features/cluster/deployment_api.py",
        "features/cluster/device_actions_api.py",
        "features/cluster/transfer_ingest_api.py",
        "features/cluster/transfers_api.py",
        # adb proxy + fastboot workflow execution surfaces.
        "features/devices/adb_proxy_security.py",
        "features/devices/adb_proxy_service.py",
        "features/devices/api.py",
        "features/devices/bootloader_api.py",
        "features/firmware/api_helpers.py",
        "features/firmware/gsi_transport.py",
    }
)

# worker_agent files allowed to import features/ modules.  Only the shared
# device-action spec contract (a pure spec module) is exempt today.
ALLOWED_WORKER_IMPORTING_FEATURES = frozenset(
    {
        "worker_agent/device_actions.py",
    }
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def worker_modules(modules: set[str]) -> set[str]:
    return {name for name in modules if name.split(".")[0] == "worker_agent"}


def feature_modules(modules: set[str]) -> set[str]:
    return {name for name in modules if name.split(".")[0] == "features"}


class ControllerWorkerBoundaryTests(unittest.TestCase):
    def test_features_import_worker_agent_only_via_allowlist(self):
        offenders = []
        for path in (ROOT / "features").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            if not worker_modules(imported_modules(path)):
                continue
            if relative not in ALLOWED_FEATURES_IMPORTING_WORKER:
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            "features/ must not import worker_agent/; extend the allowlist in "
            "tests/architecture/test_controller_worker_boundary.py only after "
            "review (preferred: sink shared logic into foundation/).",
        )

    def test_worker_agent_imports_features_only_via_allowlist(self):
        offenders = []
        for path in (ROOT / "worker_agent").rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in relative or "__pycache__" in relative:
                continue
            if not feature_modules(imported_modules(path)):
                continue
            if relative not in ALLOWED_WORKER_IMPORTING_FEATURES:
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            "worker_agent/ must not import features/; the only sanctioned "
            "dependency is the shared device-action spec contract.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
