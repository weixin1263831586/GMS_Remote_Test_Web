#!/usr/bin/env python3
"""Fail when the GMS CLI, docs, and MCP adapter drift apart."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = ROOT / "agent" / "gms-remote-test"
CLI = AGENT_ROOT / "runtime" / "gms-remote-test.sh"
MCP = AGENT_ROOT / "runtime" / "mcp_server.py"
CATALOG = AGENT_ROOT / "skill" / "references" / "api-catalog.md"


def main() -> int:
    errors: list[str] = []
    source = CLI.read_text(encoding="utf-8")
    implemented = set(re.findall(r"^(gms-rt-[a-z0-9-]+)\(\)", source, re.M))
    documented = set(
        re.findall(
            r"`(gms-rt-[a-z0-9-]+)(?:\s[^`]*)?`",
            CATALOG.read_text(encoding="utf-8"),
        )
    )

    with tempfile.TemporaryDirectory() as temporary:
        env = {
            **os.environ,
            "XDG_STATE_HOME": str(Path(temporary) / "state"),
            "GMS_AUTH_COOKIE_JAR": str(Path(temporary) / "cookies"),
        }
        completed = subprocess.run(
            [
                "bash",
                str(CLI),
                "gms-rt-system-commands",
                "--json",
                "--non-interactive",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode:
        errors.append(f"CLI catalog failed: {completed.stderr.strip()}")
        catalog_commands: dict[str, object] = {}
    else:
        payload = json.loads(completed.stdout)
        items = payload.get("data", payload).get("commands", [])
        catalog_commands = {item["name"]: item for item in items}

    previous_auth_mode = os.environ.get("GMS_AGENT_AUTH_MODE")
    previous_agent_process = os.environ.get("GMS_AGENT_PROCESS")
    os.environ["GMS_AGENT_AUTH_MODE"] = "service-token"
    os.environ["GMS_AGENT_PROCESS"] = "1"
    try:
        spec = importlib.util.spec_from_file_location("gms_contract_mcp", MCP)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load MCP adapter")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous_auth_mode is None:
            os.environ.pop("GMS_AGENT_AUTH_MODE", None)
        else:
            os.environ["GMS_AGENT_AUTH_MODE"] = previous_auth_mode
        if previous_agent_process is None:
            os.environ.pop("GMS_AGENT_PROCESS", None)
        else:
            os.environ["GMS_AGENT_PROCESS"] = previous_agent_process

    tool_names = {tool["name"] for tool in module.tools()}
    handler_names = set(module._TOOL_HANDLERS)
    cli_references = set(
        re.findall(r"[\"'](gms-rt-[a-z0-9-]+)[\"']", MCP.read_text(encoding="utf-8"))
    )

    checks = {
        "documented_vs_implemented": documented ^ implemented,
        "catalog_vs_implemented": set(catalog_commands) ^ implemented,
        "tools_without_handlers": tool_names - handler_names,
        "handlers_without_schema": handler_names - {tool["name"] for tool in module._all_tools()},
        "unknown_cli_references": cli_references - implemented,
        "hyphenated_mcp_names": {name for name in tool_names if "-" in name},
    }
    for label, values in checks.items():
        if values:
            errors.append(f"{label}: {', '.join(sorted(values))}")

    result = {
        "ok": not errors,
        "cli_commands": len(implemented),
        "service_token_tools": len(tool_names),
        "documented_commands": len(documented),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
