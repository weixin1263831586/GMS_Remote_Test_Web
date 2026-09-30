"""Controller <-> worker_agent import boundary gate (AGENTS.md hard rule).

``features/`` must not reach into ``worker_agent/`` implementation except for
the audited same-host bridge surfaces listed below (ADR 0004: Controller and
Worker interact across the SSH execution boundary; these modules are the
documented same-host exceptions).  The allowlist is two-dimensional and
shrink-only: each features/ file names the *exact* ``worker_agent.*`` modules
it may import — adding another worker module to an already-allowlisted file
still fails here until the set is extended deliberately in review.  Shrinking
(sinking shared logic into ``foundation/``) is the intended direction.

The reverse direction is also exact: ``worker_agent`` must not import
``features/`` except for the shared device-action spec contract, which is a
pure specification module without feature-internal dependencies.
"""

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# features/ file -> exact worker_agent modules it may import.  Any worker
# module outside a file's set is a violation even when the file itself is
# allowlisted (prevents silent boundary creep inside trusted files).
ALLOWED_FEATURE_WORKER_IMPORTS: dict[str, set[str]] = {
    # Same-host bridge: the documented exception in AGENTS.md.
    "features/cluster/local_bridge.py": {
        "worker_agent.adb_proxy",
        "worker_agent.inventory",
        "worker_agent.process_inventory",
        "worker_agent.suite_actions",
        "worker_agent.suite_detection",
    },
    # Cluster suite transfer/execution surfaces (worker-role execution).
    "features/cluster/api.py": {
        "worker_agent.adb_proxy",
        "worker_agent.config",
        "worker_agent.device_actions",
        "worker_agent.inventory",
    },
    "features/cluster/deployment_api.py": {
        "worker_agent.process_inventory",
    },
    "features/cluster/device_actions_api.py": {
        "worker_agent.inventory",
    },
    "features/cluster/transfer_ingest_api.py": {
        "worker_agent.android_inspection",
    },
    "features/cluster/transfers_api.py": {
        "worker_agent.config",
        "worker_agent.inventory",
    },
    # adb proxy + fastboot workflow execution surfaces.
    "features/devices/adb_proxy_security.py": {
        "worker_agent.adb_proxy",
    },
    "features/devices/adb_proxy_service.py": {
        "worker_agent.adb_proxy",
    },
    "features/devices/api.py": {
        "worker_agent.adb_proxy",
    },
    "features/devices/bootloader_api.py": {
        "worker_agent.adb_proxy",
        "worker_agent.fastboot_workflow",
    },
    "features/firmware/api_helpers.py": {
        "worker_agent.adb_proxy",
    },
    "features/firmware/gsi_transport.py": {
        "worker_agent.fastboot_workflow",
    },
}

# worker_agent file -> exact features/ modules it may import.  Only the
# shared device-action spec contract (a pure spec module) is exempt today.
ALLOWED_WORKER_FEATURE_IMPORTS: dict[str, set[str]] = {
    "worker_agent/device_actions.py": {
        "features.cluster.device_action_spec",
    },
}


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


def _iter_source_files(package: str):
    for path in (ROOT / package).rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if "/tests/" in relative or "__pycache__" in relative:
            continue
        yield relative, path


class ControllerWorkerBoundaryTests(unittest.TestCase):
    def test_features_import_worker_agent_only_via_allowlist(self):
        offenders = []
        for relative, path in _iter_source_files("features"):
            imported = worker_modules(imported_modules(path))
            if not imported:
                continue
            allowed = ALLOWED_FEATURE_WORKER_IMPORTS.get(relative)
            if allowed is None:
                offenders.append(
                    f"{relative}: file not allowlisted to import worker_agent"
                )
                continue
            unexpected = imported - allowed
            if unexpected:
                offenders.append(
                    f"{relative}: worker modules outside the exact allowlist: "
                    f"{sorted(unexpected)}"
                )
        self.assertEqual(
            offenders,
            [],
            "features/ must import only its exact allowlisted worker_agent "
            "modules; extend ALLOWED_FEATURE_WORKER_IMPORTS in "
            "tests/architecture/test_controller_worker_boundary.py only after "
            "review (preferred: sink shared logic into foundation/).",
        )

    def test_worker_agent_imports_features_only_via_allowlist(self):
        offenders = []
        for relative, path in _iter_source_files("worker_agent"):
            imported = feature_modules(imported_modules(path))
            if not imported:
                continue
            allowed = ALLOWED_WORKER_FEATURE_IMPORTS.get(relative)
            if allowed is None:
                offenders.append(
                    f"{relative}: file not allowlisted to import features"
                )
                continue
            unexpected = imported - allowed
            if unexpected:
                offenders.append(
                    f"{relative}: features modules outside the exact "
                    f"allowlist: {sorted(unexpected)}"
                )
        self.assertEqual(
            offenders,
            [],
            "worker_agent/ must import only its exact allowlisted features "
            "modules; the only sanctioned dependency today is the shared "
            "device-action spec contract.",
        )

    def test_allowlist_entries_point_at_real_dependencies(self):
        # Ratchet hygiene: allowlist entries that no longer match a real
        # import must be removed, so the gate shrinks with the code.
        stale = []
        for relative, allowed in ALLOWED_FEATURE_WORKER_IMPORTS.items():
            path = ROOT / relative
            if not path.exists():
                stale.append(f"{relative}: file missing")
                continue
            unused = allowed - worker_modules(imported_modules(path))
            if unused:
                stale.append(f"{relative}: unused entries {sorted(unused)}")
        for relative, allowed in ALLOWED_WORKER_FEATURE_IMPORTS.items():
            path = ROOT / relative
            if not path.exists():
                stale.append(f"{relative}: file missing")
                continue
            unused = allowed - feature_modules(imported_modules(path))
            if unused:
                stale.append(f"{relative}: unused entries {sorted(unused)}")
        self.assertEqual(
            stale,
            [],
            "allowlist entries must match real imports; delete stale entries "
            "to keep the boundary shrink-only.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
