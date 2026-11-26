"""Controller <-> worker import boundary gate (AGENTS.md hard rule).

``features/`` must not reach into ``worker/`` implementation except for
the audited same-host bridge surfaces listed below (ADR 0004: Controller and
Worker interact across the SSH execution boundary; these modules are the
documented same-host exceptions).  The allowlist is two-dimensional and
shrink-only: each features/ file names the *exact* ``worker.*`` modules
it may import — adding another worker module to an already-allowlisted file
still fails here until the set is extended deliberately in review.  Shrinking
(sinking shared logic into ``foundation/``) is the intended direction.

The reverse direction is also exact: ``worker`` must not import
``features/`` at all.  The shared device-action spec contract lives in
``foundation/device_action_spec.py`` precisely so it ships with the Worker
bundle (``features/`` is not part of the remote Worker deployment).
"""

import ast
import subprocess
import unittest
from pathlib import Path

from tests.architecture._import_refs import module_names


ROOT = Path(__file__).resolve().parents[2]

# features/ file -> exact worker modules it may import.  Any worker
# module outside a file's set is a violation even when the file itself is
# allowlisted (prevents silent boundary creep inside trusted files).
ALLOWED_FEATURE_WORKER_IMPORTS: dict[str, set[str]] = {
    # Same-host bridge: the documented exception in AGENTS.md.
    "features/cluster/local_bridge.py": {
        "worker.adb_proxy",
        "worker.device_actions",
        "worker.suite_actions",
    },
    # Cluster suite transfer/execution surfaces (worker-role execution).
    "features/cluster/api.py": {
        "worker.adb_proxy",
        "worker.config",
        "worker.device_actions",
        "worker.suite_actions",
    },
    "features/cluster/device_operations/api.py": {
        # Local AOA execution and ADB suppression safety checks.
        "worker.adb_proxy",
        "worker.device_actions",
    },
    "features/cluster/transfers/ingest.py": {
        "worker.android_inspection",
    },
    "features/cluster/transfers/api.py": {
        "worker.config",
        "worker.suite_actions",
    },
    # ADB proxy execution surfaces.
    "features/devices/adb/proxy_security.py": {
        "worker.adb_proxy",
    },
    "features/devices/adb/proxy_service.py": {
        "worker.adb_proxy",
    },
    "features/devices/api.py": {
        "worker.adb_proxy",
    },
    "features/devices/bootloader_api.py": {
        "worker.adb_proxy",
    },
    "features/firmware/api_helpers.py": {
        "worker.adb_proxy",
    },
}

# worker file -> exact features/ modules it may import.  Empty by
# design: shared contracts (e.g. the device-action spec) live in foundation/,
# which is part of the remote Worker bundle, features/ is not.
ALLOWED_WORKER_FEATURE_IMPORTS: dict[str, set[str]] = {}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = ".".join(path.relative_to(ROOT).parent.parts)
    return module_names(tree, package)


def worker_modules(modules: set[str]) -> set[str]:
    return {name for name in modules if name.split(".")[0] == "worker"}


def feature_modules(modules: set[str]) -> set[str]:
    return {name for name in modules if name.split(".")[0] == "features"}


def _iter_source_files(package: str):
    for path in (ROOT / package).rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if "/tests/" in relative or "__pycache__" in relative:
            continue
        yield relative, path


class ControllerWorkerBoundaryTests(unittest.TestCase):
    def test_features_import_worker_only_via_allowlist(self):
        offenders = []
        for relative, path in _iter_source_files("features"):
            imported = worker_modules(imported_modules(path))
            if not imported:
                continue
            allowed = ALLOWED_FEATURE_WORKER_IMPORTS.get(relative)
            if allowed is None:
                offenders.append(
                    f"{relative}: file not allowlisted to import worker"
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
            "features/ must import only its exact allowlisted worker "
            "modules; extend ALLOWED_FEATURE_WORKER_IMPORTS in "
            "tests/architecture/test_controller_worker_boundary.py only after "
            "review (preferred: sink shared logic into foundation/).",
        )

    def test_worker_imports_features_only_via_allowlist(self):
        offenders = []
        for relative, path in _iter_source_files("worker"):
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
            "worker/ must not import features/; shared contracts "
            "belong in foundation/ (which ships with the Worker bundle).",
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


    def test_retired_worker_package_references(self):
        retired = "worker" + "_agent"
        self.assertFalse((ROOT / retired / "__init__.py").exists())
        self.assertTrue((ROOT / "worker" / "__init__.py").is_file())
        # Scan every tracked text file, including configuration and docs.
        # No migration or compatibility exceptions are permitted.
        result = subprocess.run(
            ["git", "grep", "-I", "-n", "-F", retired, "--"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        self.assertEqual(result.stdout, "", "Retired Worker package references remain")



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
