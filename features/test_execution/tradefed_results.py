"""Local/remote Tradefed result collection behind one service boundary."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import re
import shlex
import time
from typing import Any

from . import runtime
from .suites import get_default_suites_path, is_config_host_local
from .tradefed import (
    execute_tradefed_command,
    execute_tradefed_command_local,
    find_tradefed_binary,
    find_tradefed_binary_local,
    parse_tradefed_list_results,
)


logger = logging.getLogger(__name__)

_FP_RE = re.compile(rb'build_fingerprint="([^"]{1,512})"')
# Case-insensitive: fingerprints may use lowercase (e.g. rockchip/rk3576_t/...).
# \d{4} captures the 4-digit chip code; (?:_\w+)? preserves variant suffixes
# such as "_Go" so that RK3572 and RK3572_Go are distinguished.
_FP_PROJECT_RE = re.compile(r'/(RK\d{4}(?:_\w+)?)', re.IGNORECASE)
# Fallback for the product field from tradefed output (e.g. "RK3572").
_PRODUCT_PROJECT_RE = re.compile(r'([Rr][Kk]\d{4}(?:_\w+)?)')

# ---- Result cache ----
# Caches parsed+enriched results per suite_path with a 120s TTL.
# The cache is checked BEFORE launching tradefed (the expensive JVM
# startup), so repeat opens return instantly. For local hosts the key
# also includes a lightweight result-dir probe (count + newest dir) so
# that a genuinely new test result invalidates the cache immediately.
_CACHE_TTL_SECONDS = 120
_result_cache: dict[str, dict[str, Any]] = {}


def _local_results_meta(suite_path: str) -> tuple[int, str, int, int, int]:
    """Probe result metadata without starting Tradefed."""
    suite_root = os.path.dirname(suite_path.rstrip("/"))
    results_root = os.path.join(suite_root, "results")
    try:
        entries = [
            e for e in os.listdir(results_root)
            if os.path.isdir(os.path.join(results_root, e))
        ]
    except OSError:
        return 0, "", 0, 0, 0
    newest = max(entries) if entries else ""
    newest_dir_mtime = 0
    newest_xml_mtime = 0
    newest_xml_size = 0
    if newest:
        newest_path = os.path.join(results_root, newest)
        try:
            newest_dir_mtime = os.stat(newest_path).st_mtime_ns
        except OSError:
            pass
        try:
            xml_stat = os.stat(os.path.join(newest_path, "test_result.xml"))
            newest_xml_mtime = xml_stat.st_mtime_ns
            newest_xml_size = xml_stat.st_size
        except OSError:
            pass
    return (
        len(entries),
        newest,
        newest_dir_mtime,
        newest_xml_mtime,
        newest_xml_size,
    )


def _build_cache_key(
    config: dict[str, Any], suite_path: str, is_local: bool
) -> str:
    """Build a host-scoped key with local result metadata for invalidation."""
    host_identity = "|".join(
        str(config.get(name) or "")
        for name in ("ubuntu_user", "ubuntu_host", "ubuntu_port")
    )
    if is_local:
        metadata = _local_results_meta(suite_path)
        return f"{host_identity}|{os.path.realpath(suite_path)}|{metadata!r}"
    return f"{host_identity}|{suite_path}"


def _cache_get(key: str) -> dict[str, Any] | None:
    entry = _result_cache.get(key)
    if not entry:
        return None
    if time.monotonic() - entry["_ts"] > _CACHE_TTL_SECONDS:
        _result_cache.pop(key, None)
        return None
    payload = entry.get("payload")
    return copy.deepcopy(payload) if payload is not None else None


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    cached_payload = copy.deepcopy(payload)
    cached_payload["cached"] = True
    _result_cache[key] = {"payload": cached_payload, "_ts": time.monotonic()}
    # Prevent unbounded growth: keep at most 20 entries.
    if len(_result_cache) > 20:
        oldest = min(_result_cache, key=lambda k: _result_cache[k]["_ts"])
        _result_cache.pop(oldest, None)


def _extract_project(fingerprint: str) -> str:
    """Extract a chip label (e.g. RK3576, RK3572_Go) from a build fingerprint.

    The fingerprint contains several RK-prefixed segments (product, device,
    incremental).  The actual project name is in the *incremental* field,
    which is always the last RK-prefixed segment, so we take the final match.
    Only the "RK" prefix is upper-cased so variant suffixes such as "_Go"
    keep their original casing.
    """
    if not fingerprint:
        return ""
    matches = _FP_PROJECT_RE.findall(fingerprint)
    if not matches:
        return ""
    raw = matches[-1]
    return raw[:2].upper() + raw[2:]


def extract_project_from_result_fields(item: dict[str, Any]) -> str:
    """Fallback: extract chip label from device_serial or product field.

    The fingerprint path may fail when the result XML is missing or the
    suite runs on a remote host without SSH access.  In that case we fall
    back to other fields that are always present in tradefed output:

    1. ``device_serial`` – reliably embeds the chip code, e.g.
       ``"RK3572GMS1"`` → ``RK3572``, ``"RK3588GMS4"`` → ``RK3588``.
    2. ``product`` – last resort; rarely contains an RK pattern.

    When the chip code is recovered from the serial (which has no variant
    suffix) we infer ``_Go`` from the product field suffix so that
    ``RK3572`` and ``RK3572_Go`` remain distinguished.
    """
    # 1. device_serial
    serial = str(item.get("device_serial") or "")
    m = _PRODUCT_PROJECT_RE.search(serial)
    if m:
        raw = m.group(1)
        project = raw[:2].upper() + raw[2:]
        product_lower = str(item.get("product") or "").lower()
        if "_go" in product_lower:
            project += "_Go"
        return project
    # 2. product field
    m = _PRODUCT_PROJECT_RE.search(str(item.get("product") or ""))
    if not m:
        return ""
    raw = m.group(1)
    return raw[:2].upper() + raw[2:]


# Compatibility for callers written before the helper became public.
_extract_project_from_fields = extract_project_from_result_fields


def _enrich_local_results(results: list[dict], suite_path: str) -> None:
    """Read build_fingerprint from each result's XML and extract project."""
    suite_root = os.path.dirname(suite_path.rstrip("/"))
    results_root = os.path.realpath(os.path.join(suite_root, "results"))
    for item in results:
        dirname = str(item.get("result_directory", ""))
        if (
            not dirname
            or dirname in {".", ".."}
            or os.path.basename(dirname) != dirname
        ):
            continue
        project = ""
        xml_path = os.path.realpath(
            os.path.join(results_root, dirname, "test_result.xml")
        )
        if os.path.commonpath([results_root, xml_path]) != results_root:
            continue
        try:
            with open(xml_path, "rb") as fh:
                data = fh.read(262144)  # 256 KB cap; test_result.xml is well under this.
        except OSError:
            pass
        else:
            fp_match = _FP_RE.search(data)
            if fp_match:
                fp = fp_match.group(1).decode("utf-8", errors="replace")
                project = _extract_project(fp)
        if not project:
            project = extract_project_from_result_fields(item)
        if project:
            item["project"] = project


def _enrich_remote_results(results: list[dict], ssh, suite_path: str) -> None:
    """Batch-extract build_fingerprint via a single SSH command."""
    suite_root = suite_path.rstrip("/").rsplit("/tools", 1)[0]
    results_root = f"{suite_root}/results"
    dirnames = [str(item.get("result_directory", "")) for item in results]
    dirnames = [
        dirname
        for dirname in dirnames
        if dirname
        and dirname not in {".", ".."}
        and os.path.basename(dirname) == dirname
    ]
    if not dirnames:
        return
    script = (
        'for d in "$@"; do '
        'fp=$(head -c 262144 -- "$d/test_result.xml" 2>/dev/null '
        '| grep -ao -m1 \'build_fingerprint="[^"]*"\'); '
        'echo "$d|$fp"; '
        "done"
    )
    result_paths = [f"{results_root}/{dirname}" for dirname in dirnames]
    cmd = shlex.join(["bash", "-c", script, "gms-result-enrich", *result_paths])
    enrich_result = runtime.ssh_manager.execute_command(ssh, cmd, timeout=30)
    mapping: dict[str, str] = {}
    for line in (enrich_result.stdout or "").splitlines():
        parts = line.split("|", 1)
        if len(parts) != 2:
            continue
        d = os.path.basename(parts[0].strip().rstrip("/"))
        fp_raw = parts[1].strip()
        fp = fp_raw.split('"')[1] if '"' in fp_raw else ""
        project = _extract_project(fp)
        if project:
            mapping[d] = project
    for item in results:
        d = item.get("result_directory", "")
        if d in mapping:
            item["project"] = mapping[d]
        elif not item.get("project"):
            project = extract_project_from_result_fields(item)
            if project:
                item["project"] = project


def _within(path: str, root: str, label: str) -> str:
    resolved_root = os.path.realpath(os.path.expanduser(root))
    resolved = os.path.realpath(os.path.expanduser(path))
    if os.path.commonpath([resolved_root, resolved]) != resolved_root:
        raise ValueError(f"{label} must stay inside suites_path")
    return resolved


def _payload(output: str) -> dict[str, Any]:
    parsed = parse_tradefed_list_results(output)
    results = parsed.get("results", [])
    return {
        "success": True,
        "columns": parsed.get("columns", []),
        "results": results,
        "count": len(results),
        "raw_output": output,
        "cached": False,
    }


async def collect_tradefed_results(
    config: dict[str, Any],
    suite_path: str,
    tradefed_bin: str | None = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    is_local = is_config_host_local(config)

    if is_local:
        suite_path = _within(
            suite_path, get_default_suites_path(config), "suite_path"
        )

    # Build cache key (includes lightweight result-dir probe for local).
    key = _build_cache_key(config, suite_path, is_local)

    # Pre-command cache check: skip the expensive tradefed JVM launch
    # entirely when we already have fresh enriched results.
    if not force_refresh:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    if is_local:
        launcher = (
            _within(tradefed_bin, suite_path, "tradefed_bin")
            if tradefed_bin
            else find_tradefed_binary_local(suite_path)
        )
        if not launcher:
            return {
                "success": False,
                "status_code": 404,
                "error": f"No tradefed binary found in {suite_path}",
            }
        output, error, code = await asyncio.to_thread(
            execute_tradefed_command_local, suite_path, launcher
        )
    else:
        ssh = await asyncio.to_thread(runtime.ssh_manager.get_connection, config)
        if not ssh:
            return {"success": False, "status_code": 500, "error": "SSH connection failed"}
        try:
            launcher = tradefed_bin or find_tradefed_binary(ssh, suite_path)
            if not launcher:
                return {
                    "success": False,
                    "status_code": 404,
                    "error": f"No tradefed binary found in {suite_path}",
                }
            output, error, code = await asyncio.to_thread(
                execute_tradefed_command, ssh, suite_path, launcher
            )
        finally:
            await asyncio.to_thread(runtime.ssh_manager.return_connection, ssh)

    if code != 0:
        return {
            "success": False,
            "status_code": 500,
            "error": error or f"Command failed with exit code: {code}",
            "raw_output": output,
        }
    payload = _payload(output)
    results = payload.get("results", [])

    # Enrich with project labels (reads XML headers).
    if results:
        if is_local:
            _enrich_local_results(results, suite_path)
        else:
            ssh2 = await asyncio.to_thread(runtime.ssh_manager.get_connection, config)
            try:
                if ssh2:
                    _enrich_remote_results(results, ssh2, suite_path)
            finally:
                if ssh2:
                    await asyncio.to_thread(runtime.ssh_manager.return_connection, ssh2)

    _cache_put(key, payload)
    return payload
