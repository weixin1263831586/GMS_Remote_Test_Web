#!/usr/bin/env python3
"""Minimal stdio MCP adapter for the bundled gms-rt CLI.

The adapter drives the CLI script that lives beside it
(scripts/gms-remote-test.sh), so the plugin is self-contained: installing
the plugin directory is enough. stdout of the MCP process is reserved for
newline-delimited JSON-RPC.

Token discipline (v0.3.0):
- Every CLI subprocess runs with --json --non-interactive injected, so tool
  output is the CLI's stable JSON envelope instead of human text.
- The envelope is compacted before it is returned as MCP tool content:
  {ok, exit_code?+hint (errors only), data|output, diagnostics (when
  present)}. The redundant command field, exit_code=0, and empty/null data
  fields are dropped; known error exit codes gain a one-line next-action
  hint so agents can react without documentation.
- The command/safety catalog is fetched once and cached (TTL), because every
  gms_rt_run call needs it; gms_rt_commands serves a one-line-per-command
  compact inventory instead of the ~6x larger full JSON catalog.

Security boundary: the generic runner only executes commands the CLI marks
agent_safe_unattended (read-only, non-interactive). Mutating/high-risk
operations must go through the dedicated typed tools (gms_rt_test_start,
gms_rt_auth_login, ...) or a human-run CLI, never prompt text. Interactive
commands (terminal-open, terminal-push, devices-scrcpy) are denied outright.

Tools:
- gms_rt_run         run any agent-safe gms-rt-* command (escape hatch)
- gms_rt_describe    describe one command (usage, risk mode, requirements)
- gms_rt_commands    compact command inventory (token-cheap discovery)
- gms_rt_devices     list devices
- gms_rt_auth_status inspect the CLI session's authentication state
- gms_rt_auth_login  establish the CLI session (username + password_stdin)
- gms_rt_auth_elevate step-up admin re-auth (unlocks burn and elevated ops)
- gms_rt_burn_firmware  burn update.img to devices (requires elevation)
- gms_rt_test_start / gms_rt_jobs_list / gms_rt_jobs_wait / gms_rt_jobs_events / gms_rt_jobs_status
- gms_rt_reports_list
- gms_rt_shell       read-only device shell (allowlisted diagnostics)

Passwords are never handled here beyond forwarding on stdin to
gms-rt-auth-login; the CLI stores the session cookie itself.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SERVER_NAME = "gms-remote-test"
SERVER_VERSION = "0.6.0"
# Long enough for gms-rt-jobs-wait --max-wait and firmware uploads.
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
MAX_OUTPUT_BYTES = 1024 * 1024
# The catalog only changes across gms-rt-system-update; refresh it lazily.
SAFETY_CACHE_TTL_SECONDS = 300

# Commands that must never be executed through the generic runner even if a
# caller asks for them; interactive editors and raw shells are out of scope.
_DENIED_COMMANDS = {
    "gms-rt-terminal-open",
    "gms-rt-terminal-push",
    "gms-rt-devices-scrcpy",
}

# Flags injected into every CLI invocation. The dispatcher accepts global
# options at any position, so appending them is safe for every command.
_INJECTED_FLAGS = ("--json", "--non-interactive")

_CATALOG_CACHE: dict[str, Any] = {"loaded_at": 0.0, "commands": None}


def cli_script() -> Path:
    script = Path(__file__).resolve().parent / "gms-remote-test.sh"
    if not script.is_file():
        raise FileNotFoundError(
            f"bundled gms-rt CLI is missing: {script}"
        )
    return script


def bounded_text(data: Any) -> str:
    """Truncate long output, keeping head and tail. Accepts str or bytes."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    text = str(data)
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    head = text[: MAX_OUTPUT_BYTES // 2]
    tail = text[-MAX_OUTPUT_BYTES // 2 :]
    return (
        head
        + "\n...[output truncated by gms-remote-test MCP adapter]...\n"
        + tail
    )


def normalize_command(command: str) -> str:
    """Accept bare, gms-rt-prefixed, or human command names."""
    value = str(command or "").strip().replace("_", "-").lower()
    if value.startswith("gms-rt-"):
        return value
    if value.startswith("rt-"):
        return f"gms-{value}"
    return f"gms-rt-{value}"


def build_argv(command: str, args: list[str] | str | None) -> list[str]:
    argv = ["bash", str(cli_script()), normalize_command(command)]
    items: list[str] = []
    if args is not None:
        if isinstance(args, str):
            items = shlex.split(args)
        else:
            for item in args:
                if isinstance(item, (list, tuple, dict)):
                    raise ValueError(f"flat argument list expected, got: {item!r}")
                items.append(str(item))
    argv.extend(items)
    # JSON mode keeps tool output stable and free of progress text; the
    # dispatcher strips these flags wherever they appear, so appending is
    # safe and deduplicated when the caller already passed them.
    for flag in _INJECTED_FLAGS:
        if flag not in items:
            argv.append(flag)
    return argv


# Short next-action hints surfaced on failed envelopes so agents can react
# without consulting documentation. Keys are the CLI's documented exit codes.
_EXIT_HINTS = {
    2: "check usage with gms_rt_describe",
    3: "authenticate with gms_rt_auth_login",
    4: "needs admin elevation (gms_rt_auth_elevate with admin credentials, "
       "or human-run gms-rt-auth-elevate)",
    5: "conflict/busy or selection unavailable: check gms_rt_jobs_list and "
       "diagnostics, retry when free",
    6: "network/timeout: safe to retry (bounded)",
    7: "operation failed: inspect diagnostics",
}


def _prune_empty(value: Any) -> Any:
    """Recursively drop null / "" / [] / {} values from JSON payloads.

    Pure compaction: absence and emptiness carry the same information for
    API responses, and empty fields dominate the list endpoints (devices,
    jobs, reports) that agents poll most.
    """
    if isinstance(value, dict):
        pruned = {
            key: pruned_child
            for key, item in value.items()
            # Prune children first, then drop whatever became empty.
            for pruned_child in (_prune_empty(item),)
            if pruned_child not in (None, "", [], {})
        }
        return pruned
    if isinstance(value, list):
        return [_prune_empty(item) for item in value]
    return value


_DOCS_COMPACT_COMMANDS = ("gms-rt-system-docs",)


def _compact_docs(data: Any) -> Any:
    """Render /api/system/docs listings as one line per endpoint.

    The real controller returns ~97 entries (~24KB JSON). Agents need the
    method/path/description/skill mapping, not the JSON scaffolding; the
    line form is ~80% smaller and still machine-greppable. `params` arrays
    are folded into the description.
    """
    if not isinstance(data, dict):
        return data
    apis = data.get("apis")
    if not isinstance(apis, list) or not apis:
        return data
    lines = [
        f"# {len(apis)} endpoints | columns:"
        " method path | description | cli_command"
    ]
    for item in apis:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "?")
        path = str(item.get("path") or "?")
        desc = str(item.get("description") or "").replace("\n", " ").strip()
        params = item.get("params") or []
        if isinstance(params, list) and params:
            names = ",".join(
                str(p.get("name"))
                for p in params
                if isinstance(p, dict) and p.get("name")
            )
            if names:
                desc = f"{desc} ({names})".strip()
        skill = str(item.get("skill") or "").strip()
        line = f"{method} {path} | {desc}"
        if skill:
            line += f" | {skill}"
        lines.append(line)
    return "\n".join(lines)


_JOBS_COLUMNS = (
    "job_id | status | attempt | devices | module | case | created | finished | error"
)


def _job_row(job: dict[str, Any]) -> list[str]:
    """Extract the summary fields of one durable job for the line renderer."""
    request = job.get("request") or {}
    attempt = job.get("attempt") or {}
    if not isinstance(request, dict):
        request = {}
    if not isinstance(attempt, dict):
        attempt = {}
    devices = ",".join(request.get("devices") or []) or "-"
    error = str(job.get("error") or attempt.get("error") or "-")
    return [
        str(job.get("id") or "?"),
        str(job.get("status") or "?"),
        str(attempt.get("status") or "-"),
        devices,
        str(request.get("test_module") or request.get("module") or "-"),
        str(request.get("test_case") or request.get("case") or "-"),
        str(job.get("created_at") or "-"),
        str(job.get("finished_at") or attempt.get("finished_at") or "-"),
        error.replace("\n", " "),
    ]


def _compact_jobs_list(data: Any) -> Any:
    """Render gms-rt-jobs-list data as one line per job.

    The raw payload nests request/attempt/leases per job (~1KB each); agents
    polling "what is running / what finished" need id, state, device, module
    and error, which fits in one line (~1/8 the tokens).
    """
    if not isinstance(data, dict):
        return data
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return data
    lines = [f"# {len(jobs)} jobs | columns: {_JOBS_COLUMNS}"]
    for job in jobs:
        if isinstance(job, dict):
            lines.append(" | ".join(_job_row(job)))
    return "\n".join(lines)


def _compact_job_single(data: Any) -> Any:
    """Trim a single-job payload (jobs-status / jobs-wait) to key fields."""
    if not isinstance(data, dict) or not data.get("id") or not data.get("status"):
        return data
    request = data.get("request") or {}
    attempt = data.get("attempt") or {}
    if not isinstance(request, dict):
        request = {}
    if not isinstance(attempt, dict):
        attempt = {}
    result = attempt.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    trimmed: dict[str, Any] = {"id": data["id"], "status": data["status"]}
    if attempt.get("status"):
        trimmed["attempt_status"] = attempt["status"]
    if request.get("devices"):
        trimmed["devices"] = request["devices"]
    for key in ("test_module", "test_case"):
        if request.get(key):
            trimmed[key] = request[key]
    for key in ("created_at", "started_at", "finished_at"):
        if data.get(key):
            trimmed[key] = data[key]
    if data.get("error"):
        trimmed["error"] = data["error"]
    elif attempt.get("error"):
        trimmed["error"] = attempt["error"]
    if result.get("exit_code") is not None:
        trimmed["attempt_exit_code"] = result["exit_code"]
    if result.get("work_dir"):
        trimmed["work_dir"] = result["work_dir"]
    return trimmed


def _render_data_envelope(text: str, render: Any) -> str:
    """Apply a data-field renderer to a compacted envelope."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    data = payload.get("data")
    rendered = render(data)
    if rendered is data:
        return text  # shape mismatch; keep the JSON form
    payload["data"] = rendered
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _compact_envelope(text: str) -> str | None:
    """Compact a CLI JSON envelope for return as tool content.

    Returns None when the text is not a CLI envelope (caller falls back to
    the raw text). Drops the redundant command field and exit_code on
    success; keeps exit_code and diagnostics for errors and adds a short
    next-action hint for known exit codes; prunes empty data fields.
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict) or "ok" not in payload:
        return None
    compact: dict[str, Any] = {"ok": payload.get("ok")}
    exit_code = payload.get("exit_code")
    if exit_code not in (None, 0):
        compact["exit_code"] = exit_code
        hint = _EXIT_HINTS.get(exit_code)
        if hint:
            compact["hint"] = hint
    for key in ("data", "output"):
        value = payload.get(key)
        if value not in (None, ""):
            compact[key] = _prune_empty(value) if key == "data" else value
            break
    if payload.get("diagnostics"):
        compact["diagnostics"] = payload["diagnostics"]
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def run_cli(
    command: str,
    args: list[str] | str | None = None,
    stdin_text: str | None = None,
    timeout: int | None = None,
) -> tuple[str, bool]:
    """Run one CLI invocation and return (text, is_error)."""
    if normalize_command(command) in _DENIED_COMMANDS:
        return (
            f"denied: {normalize_command(command)} opens an interactive "
            "session and is not available through this MCP tool",
            True,
        )
    try:
        argv = build_argv(command, args)
    except ValueError as error:
        return f"invalid arguments: {error}", True

    effective_timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    if effective_timeout <= 0:
        effective_timeout = DEFAULT_TIMEOUT_SECONDS
    try:
        completed = subprocess.run(
            argv,
            cwd=os.getcwd(),
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            f"gms-rt command timed out after {effective_timeout} seconds: "
            f"{normalize_command(command)}",
            True,
        )
    except OSError as error:
        return f"failed to launch gms-rt CLI: {error}", True

    stdout = bounded_text(completed.stdout).strip()
    stderr = bounded_text(completed.stderr).strip()
    # The CLI emits exactly one JSON envelope on stdout in --json mode; use
    # the compacted form when present, otherwise fall back to joined text.
    normalized = normalize_command(command)
    text = _compact_envelope(stdout)
    if text is not None and not is_error_text(text):
        # Size-sensitive payloads are rendered to compact forms instead of
        # the full nested JSON scaffolding (docs: one line per endpoint;
        # jobs: one line per job / trimmed single job).
        if normalized in _DOCS_COMPACT_COMMANDS:
            text = _render_data_envelope(text, _compact_docs)
        elif normalized == "gms-rt-jobs-list":
            text = _render_data_envelope(text, _compact_jobs_list)
        elif normalized in ("gms-rt-jobs-status", "gms-rt-jobs-wait"):
            text = _render_data_envelope(text, _compact_job_single)
    else:
        parts = [part for part in (stdout, stderr) if part]
        text = "\n".join(parts) if parts else "{}"
    # The CLI's exit code is authoritative; JSON envelopes with ok=false
    # carry exit codes 2-7 and must surface as tool errors.
    is_error = completed.returncode != 0
    return text, is_error


def is_error_text(text: str) -> bool:
    """True when a compacted envelope carries ok=false."""
    try:
        return bool(json.loads(text).get("ok") is False)
    except (ValueError, AttributeError):
        return False


def _render_docs_envelope(text: str) -> str:
    """Backwards-compatible wrapper: apply _compact_docs to the data field."""
    return _render_data_envelope(text, _compact_docs)


def _load_catalog(force: bool = False) -> dict[str, Any] | None:
    """Load and cache the CLI command/safety catalog (name -> descriptor).

    Returns None when no catalog is available (fresh load failed and no
    cached copy exists). Stale cache is preferred over nothing.
    """
    cache = _CATALOG_CACHE
    now = time.monotonic()
    if (
        not force
        and cache["commands"]
        and now - cache["loaded_at"] < SAFETY_CACHE_TTL_SECONDS
    ):
        return cache["commands"]
    text, _is_error = run_cli("gms-rt-system-commands")
    try:
        payload = json.loads(text)
        commands = payload["data"]["commands"]
        catalog = {
            item["name"]: item
            for item in commands
            if isinstance(item, dict) and item.get("name")
        }
        if not catalog:
            raise ValueError("empty command catalog")
    except (ValueError, KeyError, TypeError):
        return cache["commands"] or None
    cache["commands"] = catalog
    cache["loaded_at"] = now
    return catalog


def _suggest(command: str, catalog: dict[str, Any], limit: int = 3) -> list[str]:
    return difflib.get_close_matches(command, catalog.keys(), n=limit, cutoff=0.5)


def _command_safety(command: str) -> dict[str, Any] | None:
    """Return the cached safety descriptor for one command, or None."""
    catalog = _load_catalog()
    if catalog is None:
        return None
    return catalog.get(normalize_command(command))


def _catalog_lines(catalog: dict[str, Any], group: str | None = None) -> str:
    """Render the compact one-line-per-command inventory."""
    wanted = (group or "").strip().lower()
    lines: list[str] = []
    counts: dict[str, int] = {}
    for name in sorted(catalog):
        descriptor = catalog[name]
        if wanted and wanted not in name and wanted not in str(
            descriptor.get("category", "")
        ):
            continue
        mode = str(descriptor.get("mode", "read_only"))
        counts[mode] = counts.get(mode, 0) + 1
        flags: list[str] = []
        if descriptor.get("requires_elevation"):
            flags.append("elev")
        if not descriptor.get("agent_safe_unattended"):
            flags.append("manual")
        usage = str(descriptor.get("usage") or "")
        # The CLI emits "<name> [arguments]" as a fallback when no usage is
        # registered; that prefix carries no information, so drop it.
        if usage == f"{name} [arguments]":
            usage = ""
        lines.append(
            f"{name} | {mode} | {' '.join(flags) or '-'}"
            + (f" | {usage}" if usage else "")
        )
    header = (
        f"# {len(lines)} commands"
        f" ({', '.join(f'{v} {k}' for k, v in sorted(counts.items()))})"
        " | columns: name | mode | flags | usage"
        " | flags: elev=requires admin elevation,"
        " manual=not agent-safe unattended (typed tools or human CLI only)"
        " | all commands except auth-*/system-* need a session;"
        " get details with gms_rt_describe"
    )
    return "\n".join([header, *lines]) if lines else header


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def devices_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    return run_cli("gms-rt-devices-list")


def auth_status_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    return run_cli("gms-rt-auth-status")


def auth_login_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    username = str(arguments.get("username") or "").strip()
    password = arguments.get("password_stdin")
    if not username:
        return "Missing required argument: username", True
    if not isinstance(password, str) or not password:
        return (
            "Missing required argument: password_stdin (never place the "
            "password in args or prompts)",
            True,
        )
    return run_cli(
        "gms-rt-auth-login",
        [username, "--password-stdin"],
        stdin_text=f"{password}\n",
    )


def auth_elevate_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Re-authenticate as admin (step-up) for the current CLI session."""
    username = str(arguments.get("username") or "").strip()
    password = arguments.get("password_stdin")
    if not username:
        return "Missing required argument: username (admin account)", True
    if not isinstance(password, str) or not password:
        return (
            "Missing required argument: password_stdin (never place the "
            "password in args or prompts)",
            True,
        )
    return run_cli(
        "gms-rt-auth-elevate",
        [username, "--password-stdin"],
        stdin_text=f"{password}\n",
    )


def burn_firmware_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Burn firmware to one or more devices (typed, requires elevation).

    The CLI copies the image to the worker host over SSH (direct mode) and
    posts /api/burn/firmware; wipes /data by default. Long-running: bump the
    default timeout.
    """
    firmware_path = str(arguments.get("firmware_path") or "").strip()
    device = str(arguments.get("device") or "").strip()
    if not firmware_path:
        return "Missing required argument: firmware_path", True
    if not device:
        return "Missing required argument: device", True
    wipe_data = arguments.get("wipe_data")
    if wipe_data is None:
        wipe_str = "true"
    elif isinstance(wipe_data, bool):
        wipe_str = "true" if wipe_data else "false"
    else:
        wipe_str = "false" if str(wipe_data).strip().lower() in ("0", "false", "no") else "true"
    wait_online = arguments.get("wait_online")
    extra: list[str] = []
    if wait_online:
        extra.append("--wait-online")
        if arguments.get("wait_online_max") is not None:
            try:
                extra.append(
                    f"--wait-online={max(1, int(arguments['wait_online_max']))}"
                )
            except (TypeError, ValueError):
                return "wait_online_max must be an integer (seconds)", True
    args = [firmware_path, device, wipe_str, *extra]
    return run_cli(
        "gms-rt-burn-firmware",
        args,
        timeout=arguments.get("timeout") if arguments.get("timeout") is not None else 1800,
    )


def reports_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    # gms-rt-reports-list takes no arguments; the pre-0.2.0 --query/--limit
    # options were silently ignored by the CLI, so they are gone.
    return run_cli("gms-rt-reports-list")


def jobs_list_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    # Read-only, agent-safe; the most common pre-flight check ("is anything
    # already running on this device?") deserves a typed tool so agents
    # skip the describe+run round trip.
    args: list[str] = []
    if arguments.get("limit") is not None:
        try:
            args.append(str(max(1, min(500, int(arguments["limit"])))))
        except (TypeError, ValueError):
            return "limit must be an integer between 1 and 500", True
    return run_cli("gms-rt-jobs-list", args)


def test_start_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    device = str(arguments.get("device") or "").strip()
    retry = str(arguments.get("retry") or "").strip()
    if not device and not retry:
        return "Missing required argument: device", True
    if retry:
        # Retry mode: gms-rt-test-start --retry <timestamp> [device] [type]
        # [suite] -- module/case are not part of the retry contract.
        args = ["--retry", retry]
        for key in ("device", "type", "suite"):
            value = str(arguments.get(key) or "").strip()
            if value:
                args.append(value)
        if arguments.get("wait"):
            args.append("--wait")
            if arguments.get("max_wait") is not None:
                try:
                    args.extend(
                        ["--max-wait", str(max(1, int(arguments["max_wait"])))]
                    )
                except (TypeError, ValueError):
                    return "max_wait must be an integer", True
        return run_cli("gms-rt-test-start", args)
    args = [device]
    for key in ("type", "module", "case", "suite"):
        value = str(arguments.get(key) or "").strip()
        if value:
            args.append(value)
    if arguments.get("wait"):
        args.append("--wait")
        if arguments.get("max_wait") is not None:
            try:
                max_wait = max(1, int(arguments["max_wait"]))
            except (TypeError, ValueError):
                return "max_wait must be an integer", True
            args.extend(["--max-wait", str(max_wait)])
    return run_cli("gms-rt-test-start", args)


def jobs_wait_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return "Missing required argument: job_id", True
    args = [job_id]
    if arguments.get("max_wait") is not None:
        try:
            max_wait = max(1, int(arguments["max_wait"]))
        except (TypeError, ValueError):
            return "max_wait must be an integer", True
        args.extend(["--max-wait", str(max_wait)])
    return run_cli("gms-rt-jobs-wait", args)


def jobs_status_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return "Missing required argument: job_id", True
    return run_cli("gms-rt-jobs-status", [job_id])


def jobs_events_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return "Missing required argument: job_id", True
    # gms-rt-jobs-events takes POSITIONAL [after_sequence] [limit]; the
    # flags form was a pre-0.2.0 bug that always failed with exit code 2.
    args = [job_id]
    if arguments.get("after") is not None:
        try:
            args.append(str(int(arguments["after"])))
        except (TypeError, ValueError):
            return "after must be an integer (sequence offset)", True
        if arguments.get("limit") is not None:
            try:
                args.append(str(max(1, min(2000, int(arguments["limit"])))))
            except (TypeError, ValueError):
                return "limit must be an integer between 1 and 2000", True
    return run_cli("gms-rt-jobs-events", args)


def describe_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return "Missing required argument: command", True
    normalized = normalize_command(command)
    catalog = _load_catalog()
    if catalog is None:
        return (
            "denied: unable to load the CLI command catalog; check the "
            "bundled CLI (jq required) and GMS_REMOTE_TEST_SERVER settings",
            True,
        )
    descriptor = catalog.get(normalized)
    if descriptor is None:
        suggestions = ", ".join(_suggest(normalized, catalog)) or "none"
        return (
            f"unknown command: {normalized}. Closest matches: {suggestions}. "
            "Call gms_rt_commands to list commands.",
            True,
        )
    return json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")), False


def commands_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    catalog = _load_catalog(force=bool(arguments.get("refresh")))
    if catalog is None:
        return (
            "failed to load the command catalog; check the bundled CLI "
            "(jq required) and GMS_REMOTE_TEST_SERVER settings",
            True,
        )
    return _catalog_lines(catalog, group=arguments.get("group")), False


def run_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return "Missing required argument: command", True
    normalized = normalize_command(command)
    if normalized in _DENIED_COMMANDS:
        return (
            f"denied: {normalized} opens an interactive session and is not "
            "available through this MCP tool",
            True,
        )
    # Security boundary (2026-09-03 audit §13): the generic runner only
    # executes commands the CLI itself marks agent_safe_unattended.
    # Mutating/high-risk operations must go through the dedicated typed
    # tools (gms_rt_test_start, ...) or a human-run CLI, never prompt text.
    catalog = _load_catalog()
    if catalog is None:
        return (
            "denied: unable to load the CLI safety catalog; retry, or use a "
            "typed tool for this operation",
            True,
        )
    descriptor = catalog.get(normalized)
    if descriptor is None:
        suggestions = ", ".join(_suggest(normalized, catalog)) or "none"
        return (
            f"unknown command: {normalized}. Closest matches: {suggestions}. "
            "Call gms_rt_commands to list commands.",
            True,
        )
    if not descriptor.get("agent_safe_unattended"):
        return (
            f"denied: {normalized} is not agent-safe for unattended "
            f"execution (mode={descriptor.get('mode')}, "
            f"requires_explicit_authorization="
            f"{descriptor.get('requires_explicit_authorization')}). "
            "Use the dedicated typed MCP tool with explicit confirmation, "
            "or run it manually via the gms-rt CLI.",
            True,
        )
    args = arguments.get("args")
    stdin_text = arguments.get("password_stdin")
    if stdin_text is not None and not isinstance(stdin_text, str):
        return "password_stdin must be a string", True
    timeout = None
    if arguments.get("timeout") is not None:
        try:
            timeout = max(1, int(arguments["timeout"]))
        except (TypeError, ValueError):
            return "timeout must be an integer (seconds)", True
    return run_cli(
        command,
        args,
        stdin_text=None if stdin_text is None else f"{stdin_text}\n",
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Read-only device shell (typed tool)
# ---------------------------------------------------------------------------

# The Controller catalog marks gms-rt-devices-shell "manual" because arbitrary
# shell access is interactive by definition. This typed tool exposes a strictly
# read-only subset so agents can diagnose devices (props, services, logs,
# filesystem state) without weakening the catalog security boundary
# (2026-09-05 audit follow-up).
_SHELL_READONLY_BINARIES = frozenset({
    "cat", "df", "dumpsys", "getprop", "logcat", "ls", "pidof", "ps",
    "settings", "stat", "uptime", "vmstat", "wm",
})
# Characters that enable chaining/redirection/substitution; none of the
# allowlisted read-only commands need them.
_SHELL_FORBIDDEN_CHARS = frozenset(";|&><`(){}[]$\\'\"\n\r\t*?")
# dumpsys service subcommands known to mutate state.
_SHELL_DUMPSYS_MUTATING = frozenset({
    "unplug", "reset", "disable", "enable", "whitelist", "set-debug-app",
    "force-stop", "kill", "suspend", "resume", "reset-role",
})
# Binaries that may smuggle arbitrary execution; never allow as arguments.
_SHELL_SMUGGLING_BINARIES = frozenset({
    "sh", "bash", "su", "toybox", "toolbox", "nohup", "xargs", "run-as",
    "am", "pm", "cmd", "input", "svc", "reboot", "sync", "dd", "rm", "mv",
    "cp", "mkdir", "touch", "chmod", "chown", "kill",
})
_SHELL_MAX_COMMAND_CHARS = 2000
_DEVICE_ID_PATTERN = None  # compiled lazily


def _validate_shell_command(command: str) -> tuple[bool, str]:
    """Return (allowed, reason) for a proposed device shell command."""
    if not command or not command.strip():
        return False, "empty command"
    if len(command) > _SHELL_MAX_COMMAND_CHARS:
        return False, f"command exceeds {_SHELL_MAX_COMMAND_CHARS} characters"
    bad = sorted(set(command) & _SHELL_FORBIDDEN_CHARS)
    if bad:
        return False, f"forbidden characters in command: {' '.join(bad)}"
    tokens = command.split()
    binary = tokens[0]
    if "/" in binary:
        return False, "absolute or relative binary paths are not allowed"
    if binary not in _SHELL_READONLY_BINARIES:
        return False, (
            f"binary '{binary}' is not in the read-only allowlist "
            f"({', '.join(sorted(_SHELL_READONLY_BINARIES))})"
        )
    if any(t in _SHELL_SMUGGLING_BINARIES for t in tokens[1:]):
        return False, "command references a mutating binary as an argument"
    rest = tokens[1:]
    if binary == "settings":
        if len(rest) < 2 or rest[0] != "get":
            return False, "only 'settings get <namespace> <key>' is allowed"
    elif binary == "wm":
        if len(rest) != 1 or rest[0] not in ("size", "density"):
            return False, "only 'wm size' / 'wm density' (read-only) is allowed"
    elif binary == "logcat":
        if "-c" in rest or "-f" in rest:
            return False, "logcat -c/-f mutate or write files and are not allowed"
        if not any(flag in rest for flag in ("-d", "-t", "-T", "-g", "-L", "-p", "-print")):
            return False, (
                "streaming logcat is not allowed; add -d/-t/-T (dump mode)"
            )
    elif binary == "dumpsys":
        if any(arg in _SHELL_DUMPSYS_MUTATING for arg in rest):
            return False, "dumpsys service arguments may mutate device state"
    elif binary == "ip" or binary == "ifconfig":
        if any(arg in ("set", "add", "del", "flush", "up", "down") for arg in rest):
            return False, "network configuration commands are not allowed"
    return True, ""


def shell_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    import re

    global _DEVICE_ID_PATTERN
    device = str(arguments.get("device") or "").strip()
    command = str(arguments.get("command") or "").strip()
    if not device:
        return "Missing required argument: device", True
    if not command:
        return "Missing required argument: command", True
    if _DEVICE_ID_PATTERN is None:
        _DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
    if not _DEVICE_ID_PATTERN.match(device):
        return "denied: device id must match [A-Za-z0-9._:-]{1,64}", True
    allowed, reason = _validate_shell_command(command)
    if not allowed:
        return f"denied: {reason}", True
    timeout = 120
    if arguments.get("timeout") is not None:
        try:
            timeout = min(600, max(1, int(arguments["timeout"])))
        except (TypeError, ValueError):
            return "timeout must be an integer (seconds)", True
    return run_cli("gms-rt-devices-shell", [device, command], timeout=timeout)


def tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "gms_rt_run",
            "description": (
                "Run a gms-rt-* CLI command that is agent-safe unattended "
                "(read-only). Returns a compact JSON envelope {ok, "
                "exit_code?, data|output, diagnostics?}. Mutating/elevated "
                "commands are denied - use typed tools. Discover commands "
                "with gms_rt_commands; details with gms_rt_describe."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Command name, with or without the gms-rt- prefix "
                            "(underscores accepted), e.g. devices-info."
                        ),
                    },
                    "args": {
                        "description": (
                            "Argument list or one shell-like string, e.g. "
                            "\"RK3572 --state online --max-wait 300\"."
                        ),
                    },
                    "password_stdin": {
                        "type": "string",
                        "description": (
                            "Optional secret forwarded on stdin (only for "
                            "gms-rt-auth-login / gms-rt-auth-elevate; typed "
                            "tools are preferred). Never log it."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Per-call timeout in seconds.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_commands",
            "description": (
                "Compact command inventory, one line per command: name | "
                "mode | flags | usage. ~6x cheaper than the full catalog. "
                "Filter with group (e.g. devices, jobs, burn). Use "
                "gms_rt_describe for risk details of one command."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": "Category or name substring filter.",
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Force a catalog refresh.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_describe",
            "description": (
                "Describe one gms-rt command: usage, risk mode, auth and "
                "elevation requirements, agent-safety. Serves from the "
                "cached catalog."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_devices",
            "description": (
                "List Android devices known to the Controller with state, "
                "serials, and transport."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_auth_status",
            "description": "Inspect the current CLI session's authentication state.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_auth_login",
            "description": (
                "Log in to the Controller and persist the CLI session "
                "cookie (gms-rt-auth-login USERNAME --password-stdin). Only "
                "call with credentials the user explicitly provided; the "
                "password travels via stdin and is never logged."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "username": {"type": "string"},
                    "password_stdin": {
                        "type": "string",
                        "description": "Secret forwarded on stdin; never log it.",
                    },
                },
                "required": ["username", "password_stdin"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_auth_elevate",
            "description": (
                "Step-up re-authentication as admin for the current CLI "
                "session (gms-rt-auth-elevate USERNAME --password-stdin). "
                "Unlocks elevated operations such as firmware burn. Only "
                "call with admin credentials the user explicitly provided; "
                "the password travels via stdin and is never logged."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Admin account username.",
                    },
                    "password_stdin": {
                        "type": "string",
                        "description": "Admin secret forwarded on stdin; never log it.",
                    },
                },
                "required": ["username", "password_stdin"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_burn_firmware",
            "description": (
                "Burn firmware (update.img) to one or more devices via "
                "gms-rt-burn-firmware. Requires an elevated session "
                "(gms_rt_auth_elevate first); wipes /data by default. The "
                "CLI copies the image to the worker host over SSH. Long "
                "running: default timeout 1800s. Follow with "
                "gms-rt-devices / gms_rt_shell to verify boot."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "firmware_path": {
                        "type": "string",
                        "description": "Local path to update.img.",
                    },
                    "device": {
                        "type": "string",
                        "description": (
                            "Device serial or unique prefix, comma-separated "
                            "for multiple devices, e.g. RK3562GMS7."
                        ),
                    },
                    "wipe_data": {
                        "type": "boolean",
                        "description": "Wipe /data during burn (default true).",
                    },
                    "wait_online": {
                        "type": "boolean",
                        "description": "Block until devices come back online after burn.",
                    },
                    "wait_online_max": {
                        "type": "integer",
                        "description": "Max seconds for --wait-online (default 600).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Per-call timeout in seconds (default 1800).",
                    },
                },
                "required": ["firmware_path", "device"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_test_start",
            "description": (
                "Start a GMS test (CTS/GTS/VTS/STS) on a device, or retry a "
                "previous report (retry=<timestamp> from a failed report). "
                "Returns a cluster_job_id; follow up with gms_rt_jobs_wait "
                "and gms_rt_jobs_events instead of scraping logs."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": (
                            "Device serial or unique prefix (not needed in "
                            "retry mode)."
                        ),
                    },
                    "type": {
                        "type": "string",
                        "description": "Test type, e.g. CTS, GTS, VTS.",
                    },
                    "module": {"type": "string", "description": "Module name."},
                    "case": {"type": "string", "description": "Optional case filter."},
                    "suite": {
                        "type": "string",
                        "description": "Suite short name, e.g. android-cts-17_r1.",
                    },
                    "retry": {
                        "type": "string",
                        "description": (
                            "Retry mode: report timestamp, e.g. "
                            "2026.04.11_17.27.04.421_2920. Takes precedence "
                            "over module/case."
                        ),
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "Block until a terminal job state.",
                    },
                    "max_wait": {
                        "type": "integer",
                        "description": "Seconds to wait when wait=true.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_list",
            "description": (
                "List durable test jobs visible to the session; the cheap "
                "pre-flight check for busy devices and recent runs. Output "
                "is one line per job: job_id | status | attempt | devices | "
                "module | case | created | finished | error."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max jobs to return (1-500, CLI default applies).",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_status",
            "description": (
                "Get the authoritative state of one durable test job "
                "(cheaper than events for polling). Output trimmed to key "
                "fields (id/status/attempt/devices/module/error)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_wait",
            "description": (
                "Wait for a durable test job to reach a terminal state and "
                "return the authoritative final status."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "max_wait": {"type": "integer"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_events",
            "description": "Read incremental durable test job events.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "after": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_shell",
            "description": (
                "Run a READ-ONLY diagnostic shell command on a device via "
                "gms-rt-devices-shell. Allowlist only: getprop, dumpsys, "
                "logcat (dump mode), ls, cat, ps, pidof, settings get, "
                "stat, uptime, vmstat, wm, df. Chaining/redirection/"
                "mutating commands are denied. Use for device diagnosis "
                "(props, ANR traces, service state); reboot/push/log-mgmt "
                "need the human CLI."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Read-only shell command, e.g. "
                            "'getprop ro.build.fingerprint' or "
                            "'logcat -d -b crash -v threadtime'."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds (1-600, default 120).",
                    },
                },
                "required": ["device", "command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_reports_list",
            "description": (
                "List finished test reports visible to the session (client, "
                "type, pass/fail counts, timestamps)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    ]


def response(request_id: Any, result: Any = None, error: Any = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        payload["result"] = result
    else:
        payload["error"] = error
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


_TOOL_HANDLERS = {
    "gms_rt_run": run_tool,
    "gms_rt_commands": commands_tool,
    "gms_rt_describe": describe_tool,
    "gms_rt_devices": devices_tool,
    "gms_rt_auth_status": auth_status_tool,
    "gms_rt_auth_login": auth_login_tool,
    "gms_rt_auth_elevate": auth_elevate_tool,
    "gms_rt_burn_firmware": burn_firmware_tool,
    "gms_rt_test_start": test_start_tool,
    "gms_rt_jobs_list": jobs_list_tool,
    "gms_rt_jobs_status": jobs_status_tool,
    "gms_rt_jobs_wait": jobs_wait_tool,
    "gms_rt_jobs_events": jobs_events_tool,
    "gms_rt_reports_list": reports_tool,
    "gms_rt_shell": shell_tool,
}


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return

    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        response(
            request_id,
            {
                "protocolVersion": requested or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return
    if method == "ping":
        response(request_id, {})
        return
    if method == "tools/list":
        response(request_id, {"tools": tools()})
        return
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handler = _TOOL_HANDLERS.get(name)
        if handler is None:
            response(
                request_id,
                error={"code": -32601, "message": f"Unknown tool: {name}"},
            )
            return
        try:
            text, is_error = handler(arguments)
        except Exception as error:  # MCP boundary: convert failures to tool errors.
            text, is_error = f"gms-rt tool failed: {error}", True
        response(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        )
        return
    response(
        request_id,
        error={"code": -32601, "message": f"Method not found: {method}"},
    )


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                handle(message)
        except Exception as error:
            print(f"gms-remote-test MCP protocol error: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
