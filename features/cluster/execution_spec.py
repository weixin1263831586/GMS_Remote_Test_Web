"""ExecutionSpec → argv builder for structured Cluster Job test dispatch."""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException


_VALID_TEST_TYPES = {"cts", "gsi", "gts", "gts-root", "sts", "vts", "apts"}
_SUITE_TEST_TYPES = {
    "cts": {"cts", "gsi"},
    "gts": {"gts", "gts-root", "apts"},
    "sts": {"sts"},
    "vts": {"vts"},
}
_DEVICE_SERIAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_LOCAL_SERVER_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9.:[\]_-]{0,254}"
)


def _device_serials(devices: list[str], worker_id: str = "") -> list[str]:
    prefix = f"{worker_id}:" if worker_id else ""
    serials: list[str] = []
    for item in devices:
        serial = str(item or "").strip()
        if prefix and serial.startswith(prefix):
            serial = serial[len(prefix) :]
        if not _DEVICE_SERIAL_RE.fullmatch(serial):
            raise HTTPException(400, f"invalid device serial in execution_spec: {serial}")
        if serial in serials:
            raise HTTPException(400, f"duplicate device in execution_spec: {serial}")
        serials.append(serial)
    return serials


def canonicalize_execution_spec(
    spec: dict,
    *,
    suite_path: str,
    suite_type: str,
    devices: list[str],
    worker_id: str,
) -> dict:
    """Bind a client spec to the suite inventory and leased device request."""
    canonical = dict(spec)
    requested_serials = _device_serials(list(canonical.get("devices") or []), worker_id)
    leased_serials = _device_serials(devices, worker_id)
    if not leased_serials:
        raise HTTPException(409, "execution_spec requires at least one leased device")
    if requested_serials and requested_serials != leased_serials:
        raise HTTPException(
            409,
            "execution_spec devices must match the job's leased devices",
        )
    test_type = str(canonical.get("test_type") or "").lower()
    compatible_types = _SUITE_TEST_TYPES.get(str(suite_type or "").lower(), set())
    if compatible_types and test_type not in compatible_types:
        raise HTTPException(
            409,
            f"execution_spec test_type {test_type} is incompatible with {suite_type}",
        )
    canonical["suite_path"] = suite_path
    canonical["devices"] = leased_serials
    return canonical


def build_default_argv(
    *,
    suite_path: str,
    suite_key: str,
    worker_id: str,
    local_worker_id: str,
    available_suites: list[dict],
) -> tuple[list[str], str, str]:
    """Build the harmless default command used when no test argv is supplied."""
    suite_type = ""
    if not suite_path:
        suites = [
            item
            for item in available_suites
            if item["suite_key"] == suite_key and item["available"]
        ]
        if not suites:
            raise HTTPException(409, "suite is not available on worker")
        suite_path = suites[0]["tools_path"]
        suite_key = suites[0]["suite_key"]
        suite_type = suites[0]["suite_type"].lower()
    executable = (
        str(Path(suite_path) / f"{suite_type}-tradefed") if suite_type else ""
    )
    if not executable and worker_id == local_worker_id:
        executable = next(
            (
                str(Path(suite_path) / name)
                for name in (
                    "cts-tradefed",
                    "gts-tradefed",
                    "vts-tradefed",
                    "sts-tradefed",
                )
                if (Path(suite_path) / name).exists()
            ),
            "",
        )
    if not executable:
        raise HTTPException(409, "suite executable not found")
    return [executable, "list", "devices"], suite_path, suite_key


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
    module = str(spec.get("module") or "").strip()
    test_case = str(spec.get("test_case") or "").strip()
    if retry_dir and (module or test_case):
        raise HTTPException(400, "retry_dir cannot be combined with module or test_case")
    if test_case and not module:
        raise HTTPException(400, "test_case requires module in execution_spec")
    if retry_dir:
        retry_name = os.path.basename(retry_dir.rstrip("/"))
        if retry_name in {"", ".", ".."}:
            raise HTTPException(400, "invalid retry_dir in execution_spec")
        cmd_parts.extend(["retry", retry_name])
    else:
        if module:
            cmd_parts.append(module)
        if test_case:
            cmd_parts.append(test_case)
    devices = spec.get("devices") or []
    serials = _device_serials(list(devices))
    if not serials:
        raise HTTPException(400, "execution_spec requires at least one device")
    device_args: list[str] = []
    if len(serials) > 1:
        device_args.extend(["--shard-count", str(len(serials))])
    for serial in serials:
        device_args.extend(["-s", serial])
    cmd_parts.extend(["--device-args", " ".join(device_args), "--test-suite", suite_path])
    local_server = str(spec.get("local_server") or "").strip()
    if local_server:
        if not _LOCAL_SERVER_RE.fullmatch(local_server):
            raise HTTPException(400, "invalid local_server in execution_spec")
        cmd_parts.extend(["--local-server", local_server])
    if spec.get("copy_remote"):
        if not local_server:
            raise HTTPException(
                400,
                "copy_remote requires local_server in execution_spec",
            )
        cmd_parts.append("--copy-remote")
    if spec.get("no_retry"):
        cmd_parts.append("--no-retry")
    return cmd_parts
