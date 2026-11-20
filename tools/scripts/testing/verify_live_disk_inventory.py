#!/usr/bin/env python3
"""Read-only validation of the *page* disk scanner on the actual lab host.

Load the audited scanner from the checked-out commit with minimal environment
stubs so the Controller service and Worker stay untouched. Compare totals to
the independent whole-directory walk, not to an expected static fixture.
Only aggregate counts are logged; filenames and contents never leave the host.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root
from _live_runtime_roots import active_disk_root_environment
from scan_live_artifacts import APK_EXTENSIONS


def _load_scanner(checkout: Path, host_root: Path, data_root: Path, upload_base: Path):
    """Inject only the two filesystem configuration imports."""
    foundation = types.ModuleType("foundation")
    foundation.__path__ = []
    config = types.ModuleType("foundation.config")
    config.PROJECT_ROOT = str(host_root)
    config.settings = SimpleNamespace(data_root=data_root)
    uploads = types.ModuleType("foundation.uploads")
    uploads.upload_temp_root = lambda namespace="gms_uploads": str(
        upload_base / namespace
    )
    sys.modules.update({
        "foundation": foundation,
        "foundation.config": config,
        "foundation.uploads": uploads,
    })
    entry = checkout / "features" / "system" / "disk_artifacts.py"
    spec = importlib.util.spec_from_file_location("_disk_inventory_live_probe", entry)
    if spec is None or spec.loader is None:
        raise RuntimeError("audit scanner module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-root", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    home = Path(args.host_root).expanduser().absolute()
    if not (home / "actions-runner" / ".runner").is_file():
        raise SystemExit("not the requested GMS host/Runner")
    checkout = find_repo_root()
    expected = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    physical = {entry["root"]: entry for entry in expected["root_reports"]}
    runtime_env, configuration_scope = active_disk_root_environment()
    data_root = Path(runtime_env.get("GMS_DATA_ROOT") or home / "data").expanduser().absolute()
    upload_base = Path(
        runtime_env.get("GMS_UPLOAD_DIR") or data_root / "uploads"
    ).expanduser().absolute()
    custom_data = bool(runtime_env.get("GMS_DATA_ROOT"))
    data_label = "custom-data" if custom_data else "data"
    scanned_data = physical.get(data_label)
    if not scanned_data or not scanned_data["complete"]:
        raise SystemExit("independent Controller data root scan was not complete")
    category_bytes = {row["name"]: row["bytes"] for row in expected["categories"]}
    scanner = _load_scanner(checkout, home, data_root, upload_base)
    # Match the real page entry point: explicit test-root parameters suppress
    # /tmp report discovery and change how session transcripts are classified.
    # Adjust only this verifier process's log-root environment, never the service.
    os.environ["GMS_TEST_LOG_ROOT"] = runtime_env.get(
        "GMS_TEST_LOG_ROOT", "/tmp/test-logs"
    )
    audit = scanner.collect_disk_artifacts()
    if not audit["summary"]["controller_scan_complete"]:
        raise SystemExit("page inventory did not fully traverse Controller data")
    print("Configuration scope:", configuration_scope)
    rows = {item["id"]: item for item in audit["items"]}
    summary = audit["summary"]

    # Independent results and live directory contents can change slightly
    # between two non-atomic walks. Permit 256 MiB or 8% drift, whichever is
    # larger, but catch the old defect (1.3 vs 10.2 GiB).
    comparisons = [
        ("Controller data", summary["controller_data_bytes"], scanned_data["bytes"]),
        ("APK analysis", rows["apk"]["bytes"], category_bytes.get("apk_uploads", 0)),
        ("Redmine evidence", rows["redmine_evidence"]["bytes"],
         category_bytes.get("redmine/by_user/*/evidence", 0)),
        ("Local Worker jobs", rows["local_worker_jobs"]["bytes"],
         physical.get("worker-jobs", {}).get("bytes", 0)),
    ]
    mismatches = []
    for name, actual, reference in comparisons:
        tolerance = max(256 * 1024 ** 2, int(reference * 0.08))
        print(name + ": page=" + str(actual) + " bytes, independent=" +
              str(reference) + " bytes, delta=" + str(abs(actual - reference)))
        if abs(actual - reference) > tolerance:
            mismatches.append(name)
    summary_out = {
        "read_only": audit["readonly"],
        "generated_at": audit["generated_at"],
        "summary": summary,
        "category_checks": [
            {"name": label, "page_bytes": actual, "reference_bytes": reference}
            for label, actual, reference in comparisons
        ],
        "apk_file_types": [
            entry for entry in rows["apk"].get("breakdown", [])
            if entry["type"] in APK_EXTENSIONS or entry["type"] == "(无后缀)"
        ][:8],
        "category_count": len(audit["items"]),
        "configuration_scope": configuration_scope,
        "custom_data_root": custom_data,
        "custom_upload_root": bool(runtime_env.get("GMS_UPLOAD_DIR")),
    }
    Path(args.output).write_text(
        json.dumps(summary_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Page scan completed:", summary["scan_seconds"], "s;",
          summary["visited_entries"], "entries;",
          summary["scanned_categories"], "complete categories;",
          summary["incomplete_categories"], "incomplete categories")
    if mismatches:
        raise SystemExit("independent size mismatch: " + ", ".join(mismatches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
