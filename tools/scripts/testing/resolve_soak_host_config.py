"""Resolve a single approved physical DUT from this runner's private local config.

Reads only configs/local/*.json on the host (never checked-out templates or
configs/secrets). Prints key availability, not values; exports a temporary
0600 shell file for the device job. No host/device auto-discovery or writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

KEYS = (
    "GMS_SOAK_BASE_URL",
    "GMS_SOAK_USERNAME",
    "GMS_SOAK_PASSWORD",
    "GMS_SOAK_SESSION_TOKEN",
    "GMS_SOAK_DEVICE_HOST",
    "GMS_SOAK_DEVICE_SERIAL",
    "GMS_SOAK_BUSIDS",
    "GMS_SOAK_WORKER_ID",
    "GMS_SOAK_SUITE_KEY",
    "GMS_SOAK_MODULE",
)
REQUIRED = (
    "GMS_SOAK_BASE_URL",
    "GMS_SOAK_DEVICE_HOST",
    "GMS_SOAK_BUSIDS",
    "GMS_SOAK_WORKER_ID",
)
BUSID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _read_json_object(path: Path) -> dict:
    """Read only a regular file without dereferencing an unrelated symlink."""
    if path.is_symlink() or not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return data


def _value(data: dict, key: str) -> str:
    item = data.get(key)
    return item.strip() if isinstance(item, str) else ""


def _busids(value: object) -> str:
    if isinstance(value, list):
        parts = [str(item).strip() for item in value]
    elif isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    else:
        return ""
    return ",".join(p for p in parts if p)


def resolve(root: Path, environment: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Explicit env > exact local GMS_SOAK entries > dedicated soak > safe config."""

    local_env = _read_json_object(root / "environment.json")
    app = _read_json_object(root / "config.json")
    deploy = _read_json_object(root / "deployment.json")
    cluster = _read_json_object(root / "cluster.json")
    # Private, workflow-specific target; never changes Controller settings.
    soak = _read_json_object(root / "soak.json") or app.get("soak") or deploy.get("soak") or {}
    if not isinstance(soak, dict):
        raise ValueError("soak settings must be a JSON object")

    resolved: dict[str, str] = {}
    origins: dict[str, str] = {}

    for key in KEYS:
        value = _value(environment, key)
        if value:
            resolved[key], origins[key] = value, "Actions"
        elif value := _value(local_env, key):
            resolved[key], origins[key] = value, "local/environment.json"

    inferred = {
        "GMS_SOAK_BASE_URL": (
            _value(soak, "base_url")
            or _value(cluster, "controller_url")
            or _value(deploy, "controller_url")
        ),
        "GMS_SOAK_DEVICE_HOST": (
            _value(soak, "device_host")
            or _value(app, "usbip_device_host")
            or _value(app, "device_host")
            or _value(deploy, "usbip_device_host")
            or _value(deploy, "device_host")
        ),
        "GMS_SOAK_DEVICE_SERIAL": _value(soak, "device_serial"),
        "GMS_SOAK_BUSIDS": _busids(soak.get("busids")),
        "GMS_SOAK_SUITE_KEY": _value(soak, "suite_key"),
        "GMS_SOAK_MODULE": _value(soak, "module"),
        # A multi-Worker cluster needs explicit target selection; never
        # silently choose the Controller local Worker in that mode.
        "GMS_SOAK_WORKER_ID": (
            _value(soak, "worker_id")
            or (_value(cluster, "local_worker_id") if not cluster.get("enabled") else "")
        ),
    }
    for key, value in inferred.items():
        if key not in resolved and value:
            resolved[key], origins[key] = value, "local/deployment configuration"
    return resolved, origins


def validate(config: dict[str, str]) -> list[str]:
    problems = [f"missing {key}" for key in REQUIRED if not config.get(key)]
    if not config.get("GMS_SOAK_SESSION_TOKEN") and not (
        config.get("GMS_SOAK_USERNAME") and config.get("GMS_SOAK_PASSWORD")
    ):
        problems.append("missing GMS_SOAK_USERNAME/GMS_SOAK_PASSWORD or GMS_SOAK_SESSION_TOKEN")

    url = config.get("GMS_SOAK_BASE_URL", "")
    if url:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            problems.append("invalid GMS_SOAK_BASE_URL (http(s) URL required)")
    busids = config.get("GMS_SOAK_BUSIDS", "")
    if busids:
        parts = [part.strip() for part in busids.split(",")]
        if len(parts) != 1 or not BUSID.fullmatch(parts[0]):
            problems.append("GMS_SOAK_BUSIDS must identify exactly one valid USB/IP BUSID")
    if config.get("GMS_SOAK_DEVICE_HOST") and (
        any(c.isspace() for c in config["GMS_SOAK_DEVICE_HOST"])
        or len(config["GMS_SOAK_DEVICE_HOST"]) > 512
    ):
        problems.append("invalid GMS_SOAK_DEVICE_HOST")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.local_root
    if not root.is_dir():
        print("Missing host configs/local directory", flush=True)
        return 2
    try:
        config, origins = resolve(root, dict(os.environ))
        errors = validate(config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Never print an exception's data: malformed JSON errors may include
        # config fragments and secret content.
        print(f"Unable to parse local configuration ({type(exc).__name__})")
        return 2

    for key in KEYS:
        print(f"{key}: {'available from ' + origins[key] if key in origins else 'not configured'}")
    for error in errors:
        print(f"::error title=Soak configuration::{error}")
    if errors:
        return 2

    # The output is never uploaded or checked in. It is sourced only by the
    # physical test shell, and removed by the workflow's always() cleanup.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        for key in KEYS:
            if key in config:
                output.write(f"export {key}={shlex.quote(config[key])}\n")
    print("Explicit single-DUT lab configuration resolved; credentials not logged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
