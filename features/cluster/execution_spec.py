"""ExecutionSpec → argv builder for structured Cluster Job test dispatch."""

from __future__ import annotations

import os

from fastapi import HTTPException


_VALID_TEST_TYPES = {"cts", "gsi", "gts", "gts-root", "sts", "vts", "apts"}


def build_argv_from_spec(spec: dict) -> list[str]:
    """Build a run_GMS_Test_Auto.sh argv from a structured ExecutionSpec.

    This mirrors the logic in ``workflows/cluster_test_execution.py`` so that
    callers who supply an ``execution_spec`` get identical argv without needing
    to construct raw command strings themselves.
    """
    test_type = str(spec.get("test_type") or "").lower()
    if test_type not in _VALID_TEST_TYPES:
        raise HTTPException(400, f"invalid test_type in execution_spec: {test_type}")
    suite_path = str(spec.get("suite_path") or "").strip()
    if not suite_path:
        raise HTTPException(400, "suite_path is required in execution_spec")
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(suite_path))),
        "run_GMS_Test_Auto.sh",
    )
    # Try to locate the script via GMS-Suite root like start_cluster_test does.
    parts = os.path.normpath(suite_path).split(os.sep)
    try:
        root_index = next(i for i, part in enumerate(parts) if part == "GMS-Suite")
        script = os.path.join(os.sep.join(parts[: root_index + 1]), "run_GMS_Test_Auto.sh")
    except StopIteration:
        pass
    cmd_parts = [script, test_type]
    retry_dir = str(spec.get("retry_dir") or "").strip()
    if retry_dir:
        cmd_parts.extend(["retry", os.path.basename(retry_dir.rstrip("/"))])
    else:
        module = str(spec.get("module") or "").strip()
        test_case = str(spec.get("test_case") or "").strip()
        if module:
            cmd_parts.append(module)
        if test_case:
            cmd_parts.append(test_case)
    devices = spec.get("devices") or []
    serials = [
        item.split(":", 1)[1] if ":" in item else item
        for item in devices
        if item
    ]
    device_args: list[str] = []
    if len(serials) > 1:
        device_args.extend(["--shard-count", str(len(serials))])
    for serial in serials:
        device_args.extend(["-s", serial])
    cmd_parts.extend(["--device-args", " ".join(device_args), "--test-suite", suite_path])
    local_server = str(spec.get("local_server") or "").strip()
    if local_server:
        cmd_parts.extend(["--local-server", local_server])
    if spec.get("copy_remote"):
        cmd_parts.append("--copy-remote")
    if spec.get("no_retry"):
        cmd_parts.append("--no-retry")
    return cmd_parts
