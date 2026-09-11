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
gms_rt_shell_exec with an approval token, ...) or a human-run CLI, never
prompt text. Interactive commands (terminal-open, terminal-push,
devices-scrcpy) are denied outright.

Authentication model (2026-09-09 audit, 10.txt §四/§五):
- Agents authenticate exclusively with an Agent Service Token
  (GMS_AUTH_TOKEN_FILE, 0600, enrolled via gms_rt_agent_enroll). No
  platform or admin password ever flows through MCP.
- gms_rt_auth_login / gms_rt_auth_elevate are HUMAN-session tools. They are
  not registered at all when the server runs in service-token mode
  (GMS_AGENT_AUTH_MODE=service-token, set by the installer); even when
  registered, only call them with credentials the user explicitly provided.
- gms_rt_approval_create requires the user's human session; agents only
  relay the minted one-shot token into gms_rt_shell_exec /
  gms_rt_burn_firmware.

Tools:
- gms_rt_run         run any agent-safe gms-rt-* command (escape hatch)
- gms_rt_describe    describe one command (usage, risk mode, requirements)
- gms_rt_commands    compact command inventory (token-cheap discovery)
- gms_rt_devices     list devices
- gms_rt_auth_status inspect the CLI session's authentication state
- gms_rt_agent_enroll  exchange a one-shot code for the 0600 service token
- gms_rt_auth_login / gms_rt_auth_elevate  human-session tools (hidden in
  service-token mode)
- gms_rt_burn_firmware  burn update.img to devices (approval-token gated)
- gms_rt_test_start / gms_rt_jobs_list / gms_rt_jobs_wait / gms_rt_jobs_events / gms_rt_jobs_status
- gms_rt_reports_list
- gms_rt_apk_resolve / gms_rt_apk_analyze / gms_rt_apk_status / gms_rt_apk_manifest
- gms_rt_apk_search / gms_rt_apk_source  (suite module -> jadx decompilation)
- gms_rt_shell       read-only device shell (allowlisted diagnostics)
- gms_rt_logcat      capture device logcat via adb shell logcat -v time
  (v0.13.0: dump-mode only — clearing the buffer destroys diagnostic
  evidence and is human-only via the CLI; clear=true and raw -c are denied)
- gms_rt_shell_exec  one-shot device shell command (server-issued approval
  token required; v0.8.0+)
- gms_rt_devices_screencap  capture one device screenshot as MCP image
  content (base64 PNG; read-only UI diagnosis)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any


SERVER_NAME = "gms-remote-test"
SERVER_VERSION = "0.17.1"
# Long enough for gms-rt-jobs-wait --max-wait and firmware uploads.
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
MAX_OUTPUT_BYTES = 1024 * 1024
# The catalog only changes across gms-rt-system-update; refresh it lazily.
SAFETY_CACHE_TTL_SECONDS = 300

# Agent authentication mode (2026-09-09 audit, 10.txt §五). When set to
# "service-token" (the installer writes it into every agent MCP env), the
# password-based tools (gms_rt_auth_login, gms_rt_auth_elevate) and the
# human-session approval mint (gms_rt_approval_create) are NOT registered:
# an agent context then cannot even express a password login or a
# self-minted approval. Agents enroll once via gms_rt_agent_enroll and
# authenticate with GMS_AUTH_TOKEN_FILE.
#
# 15.txt 审核 P1-1: two independent signals put the server into
# service-token mode. mcp_launcher.py now FORCES GMS_AGENT_AUTH_MODE
# (setdefault() let an ambient auth-mode variable leak through) and stamps
# GMS_AGENT_PROCESS=1, which only the launcher sets — either signal alone
# is sufficient, so a forged auth-mode env var alone cannot widen the tool
# catalog on a launcher-launched agent.
_SERVICE_TOKEN_MODE = (
    str(os.environ.get("GMS_AGENT_PROCESS", "")).strip() == "1"
    or str(os.environ.get("GMS_AGENT_AUTH_MODE", "")).strip().lower()
    == "service-token"
)

# Tools hidden in service-token mode (human-session credential tools).
_HUMAN_SESSION_TOOLS = (
    "gms_rt_auth_login",
    "gms_rt_auth_elevate",
    "gms_rt_approval_create",
)

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

# ---------------------------------------------------------------------------
# Phase 2 (10.txt §五): direct-HTTP fast path through the gms_agent SDK.
#
# The MCP server is a protocol adapter, not a second business layer. When the
# CLI command maps 1:1 to a Controller REST endpoint (read-only listing and
# status commands), the SDK call skips the subprocess + jq pipeline entirely
# and returns the same JSON envelope. Anything not in the table — or any SDK
# failure — falls back to the historical CLI path, so behavior never changes
# for commands the SDK does not cover yet.
# ---------------------------------------------------------------------------
_SDK_CLI_ROUTES: dict[str, tuple[str, str]] = {
    # CLI command -> (HTTP method, Controller /api endpoint)
    "gms-rt-cluster-workers": ("GET", "/cluster/workers"),
    "gms-rt-cluster-devices": ("GET", "/cluster/devices"),
    "gms-rt-jobs-list": ("GET", "/cluster/jobs"),
    "gms-rt-jobs-status": ("GET", "/cluster/jobs/{job_id}"),
    "gms-rt-jobs-events": ("GET", "/cluster/jobs/{job_id}/events"),
    "gms-rt-reports-list": ("GET", "/reports/list"),
    "gms-rt-devices-list": ("GET", "/devices/list"),
    "gms-rt-auth-status": ("GET", "/auth/status"),
}

_sdk_client = None


def _sdk_fast_call(command: str, args: list[str]) -> tuple[str, bool] | None:
    """Try the SDK fast path for one CLI command; None means fall back."""
    global _sdk_client
    route = _SDK_CLI_ROUTES.get(normalize_command(command))
    if route is None:
        return None
    method, endpoint = route
    params: dict[str, Any] = {}
    positional: list[str] = []
    index = 0
    # Boolean flags the dispatcher injects itself; must never consume the
    # next positional (code review 2026-08: they previously fell into the
    # "--" branch and swallowed a positional argument).
    _PASSTHROUGH_FLAGS = {"--json", "--non-interactive"}
    while index < len(args):
        token = str(args[index])
        if token in _PASSTHROUGH_FLAGS:
            pass
        elif token.startswith("--"):
            if "=" in token:
                key, _, value = token[2:].partition("=")
            else:
                key = token[2:]
                index += 1
                value = str(args[index]) if index < len(args) else ""
            params[key.replace("-", "_")] = value
        else:
            positional.append(token)
        index += 1

    try:
        if _sdk_client is None:
            from gms_agent import GmsClient

            _sdk_client = GmsClient()
        if "{job_id}" in endpoint:
            if not positional:
                return None
            endpoint = endpoint.format(job_id=urllib.parse.quote(positional[0], safe=""))
            positional = positional[1:]
        # jobs-list / jobs-events carry the numeric filters as positionals.
        if normalize_command(command) == "gms-rt-jobs-list" and positional:
            params.setdefault("limit", positional.pop(0))
        if normalize_command(command) == "gms-rt-jobs-events" and positional:
            params.setdefault("after", positional.pop(0))
            if positional:
                params.setdefault("limit", positional.pop(0))
        envelope = _sdk_client.request(method, endpoint, params=params or None)
    except Exception:
        # SDK unavailable/misconfigured/endpoint mismatch: silently fall back
        # to the CLI path, which remains the authoritative implementation.
        return None
    envelope["command"] = normalize_command(command)
    return json.dumps(envelope, ensure_ascii=False), False



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
    3: "authenticate with the Agent Service Token: enroll once with "
    "gms_rt_agent_enroll (one-shot code from the web UI), then every call "
    "authenticates via GMS_AUTH_TOKEN_FILE — no password",
    4: "needs admin elevation — a human step: ask the user to run "
    "gms-rt-auth-elevate in their own CLI session (agents never hold "
    "admin credentials)",
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
    env_extra: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Run one CLI invocation and return (text, is_error).

    ``env_extra``：附加到子进程环境的一次性标记（如
    ``GMS_RT_TYPED_READONLY``），仅由本文件内的 typed 工具设置。
    """
    if normalize_command(command) in _DENIED_COMMANDS:
        return (
            f"denied: {normalize_command(command)} opens an interactive "
            "session and is not available through this MCP tool",
            True,
        )
    args_list = args if isinstance(args, list) else ([args] if args else [])
    sdk_result = _sdk_fast_call(command, args_list)
    if sdk_result is not None:
        text = _compact_envelope(sdk_result[0])
        if text is None:
            text = sdk_result[0]
        return text, is_error_text(text)
    try:
        argv = build_argv(command, args)
    except ValueError as error:
        return f"invalid arguments: {error}", True

    effective_timeout = DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout
    if effective_timeout <= 0:
        effective_timeout = DEFAULT_TIMEOUT_SECONDS
    child_env = None
    if env_extra:
        child_env = {**os.environ, **env_extra}
    try:
        completed = subprocess.run(
            argv,
            cwd=os.getcwd(),
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return (
            f"gms-rt command timed out after {effective_timeout} seconds: "
            f"{normalize_command(command)}",
            True,
        )
    except OSError as error:
        return f"failed to launch gms-rt CLI: {error}", True

    # R17: parse the JSON envelope BEFORE truncating. bounded_text() used to
    # cut the raw stdout first, which turned oversized-but-valid envelopes
    # into invalid JSON that then fell through to the plain-text branch with
    # is_error=False — agents received silently corrupted data.  Now: try the
    # full stdout as JSON; only when it is not parseable (genuinely not an
    # envelope) fall back to bounded raw text.
    raw_stdout = completed.stdout or ""
    raw_stderr = completed.stderr or ""
    parsed_envelope = None
    try:
        candidate = json.loads(raw_stdout)
        if isinstance(candidate, dict) and "ok" in candidate:
            parsed_envelope = candidate
    except ValueError:
        parsed_envelope = None
    if parsed_envelope is not None:
        # Trim oversized data/output STRING fields, not the envelope text:
        # the returned value must stay valid JSON (R17).
        for key in ("output", "diagnostics"):
            value = parsed_envelope.get(key)
            if isinstance(value, str) and len(value) > MAX_OUTPUT_BYTES:
                parsed_envelope[key] = (
                    value[: MAX_OUTPUT_BYTES // 2]
                    + "\n...[truncated by gms-remote-test MCP adapter]...\n"
                    + value[-MAX_OUTPUT_BYTES // 2 :]
                )
        data = parsed_envelope.get("data")
        if isinstance(data, dict):
            for key, value in list(data.items()):
                # base64 图片载荷(device screencap / evidence images)必须
                # 原样保留:截断会产生无效 base64,下游解码直接损坏。
                if key == "base64":
                    continue
                if isinstance(value, str) and len(value) > MAX_OUTPUT_BYTES:
                    data[key] = (
                        value[: MAX_OUTPUT_BYTES // 2]
                        + "\n...[truncated by gms-remote-test MCP adapter]...\n"
                        + value[-MAX_OUTPUT_BYTES // 2 :]
                    )
        stdout = json.dumps(parsed_envelope, ensure_ascii=False).strip()
    else:
        stdout = bounded_text(raw_stdout).strip()
    stderr = bounded_text(raw_stderr).strip()
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


def context_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Return the compact, secret-free environment readiness report."""

    return run_cli("gms-rt-system-selfcheck")


def device_console_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    port_key = str(arguments.get("port_key") or "").strip()
    if port_key:
        args.append(port_key)
    if arguments.get("tail") is not None:
        try:
            tail = int(arguments["tail"])
        except (TypeError, ValueError):
            return "invalid tail: expected an integer from 1 to 10000", True
        if not 1 <= tail <= 10_000:
            return "invalid tail: expected an integer from 1 to 10000", True
        args.extend(["--tail", str(tail)])
    date = str(arguments.get("date") or "").strip()
    if date:
        if not re.fullmatch(r"\d{8}", date):
            return "invalid date: expected YYYYMMDD", True
        args.extend(["--date", date])
    return run_cli("gms-rt-devices-console", args)


def device_info_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    devices = arguments.get("devices")
    if isinstance(devices, str):
        values = [devices]
    elif isinstance(devices, list):
        values = [str(item) for item in devices if str(item).strip()]
    else:
        values = []
    if not values:
        return "missing required field: devices", True
    return run_cli("gms-rt-devices-info", values)


def device_wait_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    devices = arguments.get("devices")
    if isinstance(devices, str):
        values = [devices]
    elif isinstance(devices, list):
        values = [str(item) for item in devices if str(item).strip()]
    else:
        values = []
    if not values:
        return "missing required field: devices", True
    state = str(arguments.get("state") or "online")
    if state not in {"online", "fastboot", "any"}:
        return "invalid state: expected online, fastboot, or any", True
    args = [*values, "--state", state]
    if arguments.get("interval") is not None:
        try:
            interval = int(arguments["interval"])
        except (TypeError, ValueError):
            return "invalid interval: expected an integer from 1 to 300", True
        if not 1 <= interval <= 300:
            return "invalid interval: expected an integer from 1 to 300", True
        args.extend(["--interval", str(interval)])
    if arguments.get("max_wait") is not None:
        try:
            max_wait = int(arguments["max_wait"])
        except (TypeError, ValueError):
            return "invalid max_wait: expected an integer from 1 to 86400", True
        if not 1 <= max_wait <= 86_400:
            return "invalid max_wait: expected an integer from 1 to 86400", True
        args.extend(["--max-wait", str(max_wait)])
    try:
        timeout = int(arguments.get("timeout", 330))
    except (TypeError, ValueError):
        return "invalid timeout: expected an integer from 1 to 86500", True
    if not 1 <= timeout <= 86_500:
        return "invalid timeout: expected an integer from 1 to 86500", True
    return run_cli("gms-rt-devices-wait", args, timeout=timeout)


def auth_status_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    return run_cli("gms-rt-auth-status")


def auth_login_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    if _SERVICE_TOKEN_MODE:
        return (
            "denied: this MCP server runs in service-token mode "
            "(GMS_AGENT_AUTH_MODE=service-token). Agents authenticate via "
            "GMS_AUTH_TOKEN_FILE — enroll once with gms_rt_agent_enroll; "
            "password login belongs to a human CLI session.",
            True,
        )
    username = str(arguments.get("username") or "").strip()
    password = arguments.get("password_stdin")
    if not username:
        return "Missing required argument: username", True
    if not isinstance(password, str) or not password:
        return (
            "Missing required argument: password_stdin (never place the "
            "password in args or prompts). This is a HUMAN-session tool: "
            "agents must use GMS_AUTH_TOKEN_FILE instead — enroll once "
            "with gms_rt_agent_enroll, then no password ever flows "
            "through MCP.",
            True,
        )
    return run_cli(
        "gms-rt-auth-login",
        [username, "--password-stdin"],
        stdin_text=f"{password}\n",
    )


def agent_enroll_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Exchange a one-shot enrollment code for a 0600 agent token file."""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return "Missing required argument: code (one-shot enrollment code)", True
    args: list[str] = [code]
    out_file = str(arguments.get("out_file") or "").strip()
    if out_file:
        args.extend(["--out", out_file])
    return run_cli("gms-rt-agent-enroll", args)


def approval_create_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Create a one-shot approval token (must run under a human session)."""
    if _SERVICE_TOKEN_MODE:
        return (
            "denied: gms_rt_approval_create is hidden in service-token mode "
            "because it must run under the user's own human session "
            "(cookie), never an agent token. Ask the user to run "
            "'gms-rt-approval-create ...' in their own CLI session, then "
            "pass the returned token as approval_token.",
            True,
        )
    tool = str(arguments.get("tool") or "").strip()
    device = str(arguments.get("device") or "").strip()
    command = str(arguments.get("command") or "")
    if not tool or not device:
        return "Missing required arguments: tool, device", True
    args = ["--tool", tool, "--device", device, "--command", command]
    # 4.txt P1 精确绑定：burn 审批必须绑定固件 SHA256（服务端据此派生
    # 命令串），否则服务端 400。
    if tool == "gms_rt_burn_firmware":
        sha = str(arguments.get("firmware_sha256") or "").strip().lower()
        if not sha:
            return (
                "Missing required argument: firmware_sha256 (SHA-256 of the "
                "exact update.img to burn; compute it with sha256sum before "
                "requesting approval)",
                True,
            )
        args = ["--tool", tool, "--device", device,
                "--firmware-sha256", sha]
        wipe = arguments.get("wipe_data")
        if wipe is not None:
            args += ["--wipe-data",
                     "false" if wipe is False or str(wipe).lower() in ("0", "false", "no") else "true"]
        mode = str(arguments.get("burn_mode") or "").strip()
        if mode:
            args += ["--burn-mode", mode]
    return run_cli("gms-rt-approval-create", args)


def auth_elevate_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Re-authenticate as admin (step-up) for the current CLI session."""
    if _SERVICE_TOKEN_MODE:
        return (
            "denied: this MCP server runs in service-token mode "
            "(GMS_AGENT_AUTH_MODE=service-token). Elevation is a human "
            "step: ask the user to run gms-rt-auth-elevate in their own "
            "CLI session (agents never hold admin credentials).",
            True,
        )
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


def _resolve_worker_for_device(
    device: str, worker_id: str | None
) -> tuple[str | None, str | None]:
    """Authoritative worker_id resolution for a device (audit §六).

    Returns (worker_id, error). Explicit worker_id wins. Otherwise the
    cluster inventory must match exactly one worker; zero/ambiguous matches
    are an error — never fall back to "the first/current worker".
    """
    if worker_id:
        return str(worker_id).strip(), None
    text, is_error = run_cli("gms-rt-cluster-resolve", ["--device", str(device)])
    if is_error:
        return None, text
    try:
        payload = json.loads(text)
    except ValueError:
        return None, text
    # run_cli returns the compacted CLI envelope {"ok": true, "data": {...}};
    # 4.txt P1b: the resolver used to read the top level, which is always
    # empty — read worker_id from the data object, with a top-level fallback
    # for non-envelope output.
    data = payload.get("data") if isinstance(payload, dict) else None
    source = data if isinstance(data, dict) else payload
    resolved = str(source.get("worker_id") or "").strip()
    if not resolved:
        return None, text
    return resolved, None


def burn_firmware_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Burn firmware to one or more devices (typed, requires elevation).

    The CLI copies the image to the worker host over SSH (direct mode) and
    posts /api/burn/firmware; wipes /data by default. Destructive: requires a
    one-shot approval token bound to this exact burn (server-enforced; the
    caller-side authorized flag was never a security boundary).

    异步模型（15.txt §十一，Kimi 等客户端单次 tool call 默认 60s）：
    wait=false（默认）立即后台启动 CLI 并返回 {operation_id, status: "running"}，
    之后用 gms_rt_burn_status 轮询；wait=true 保持旧的同步等待语义（≤timeout）。
    """
    firmware_path = str(arguments.get("firmware_path") or "").strip()
    device = str(arguments.get("device") or "").strip()
    if not firmware_path:
        return "Missing required argument: firmware_path", True
    if not device:
        return "Missing required argument: device", True
    approval_token = str(arguments.get("approval_token") or "").strip()
    if not approval_token:
        return (
            "denied: firmware burn is destructive and requires a one-shot "
            "approval token bound to the exact firmware. Ask the user to run "
            "'gms-rt-approval-create --tool gms_rt_burn_firmware --device "
            f"{device} --firmware-sha256 <sha256 of update.img>' under their "
            "own session (the approval binds the firmware digest, device "
            "list, wipe_data and burn_mode), then pass approval_token here.",
            True,
        )
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
    args = [firmware_path, device, wipe_str, "--approval-token", approval_token, *extra]
    if arguments.get("wait") is True:
        return run_cli(
            "gms-rt-burn-firmware",
            args,
            timeout=arguments.get("timeout") if arguments.get("timeout") is not None else 1800,
        )
    # 异步：后台启动 CLI，立即返回 operation_id；结果落盘供 burn_status 读取。
    return start_burn_operation("gms-rt-burn-firmware", args)


def _burn_operations_dir() -> Path:
    base = os.environ.get(
        "GMS_BURN_OPS_DIR",
        os.path.join(
            os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
            "gms-remote-test", "burn-ops",
        ),
    )
    return Path(base)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Detached children become zombies until reaped; a zombie means the CLI
    # already exited, so treat it as finished rather than "running".
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8", errors="ignore").split()
        return fields[2] != "Z"
    except (OSError, IndexError):
        return True


def start_burn_operation(command: str, args: list[str]) -> tuple[str, bool]:
    """Launch a long CLI op detached; return an operation id immediately.

    The CLI runs detached from this MCP subprocess (setsid, own process
    group) so a 60s client-side tool timeout can never kill a burn. Its
    stdout/stderr land in a spool file the status tool parses for the final
    JSON envelope.
    """
    ops_dir = _burn_operations_dir()
    try:
        ops_dir.mkdir(parents=True, exist_ok=True)
        op_id = f"burn-{int(time.time())}-{os.getpid()}-{os.urandom(3).hex()}"
        op_dir = ops_dir / op_id
        op_dir.mkdir(mode=0o700)
        meta = op_dir / "meta.json"
        log_path = op_dir / "output.log"
        argv = build_argv(command, args)
        # SIM115: the handle must stay open across Popen (the child writes to
        # it) and is closed deterministically in the finally block below.
        log_handle = open(log_path, "w")  # noqa: SIM115
        try:
            # setsid: decouple from the MCP process group so client-side
            # cancellation of the tool call leaves the burn running.
            process = subprocess.Popen(
                argv,
                cwd=os.getcwd(),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        meta.write_text(json.dumps({
            "operation_id": op_id,
            "command": normalize_command(command),
            "pid": process.pid,
            "started_at": time.time(),
        }), encoding="utf-8")
        return json.dumps({
            "ok": True,
            "operation_id": op_id,
            "status": "running",
            "command": normalize_command(command),
            "hint": (
                "poll gms_rt_burn_status(operation_id=...) every 20-30s; "
                "each MCP call stays short"
            ),
        }), False
    except OSError as error:
        return f"failed to start background burn: {error}", True


def burn_status_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Poll a background burn operation started with wait=false."""
    operation_id = str(arguments.get("operation_id") or "").strip()
    # First check: cheap charset bound. The second check below must mirror
    # the generator in start_burn_operation ("burn-<epoch>-<pid>-<hex3>"):
    # digits are inside [0-9a-f-], so the loose class happens to accept the
    # generated ids, but the exact pattern keeps the two in visible lockstep.
    if not operation_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", operation_id):
        return "Missing or invalid required argument: operation_id", True
    op_dir = _burn_operations_dir() / operation_id
    meta_path = op_dir / "meta.json"
    log_path = op_dir / "output.log"
    if not meta_path.is_file() or not re.fullmatch(
        r"burn-[0-9]+-[0-9]+-[0-9a-f]{6}", operation_id
    ):
        return f"operation not found: {operation_id}", True
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"operation metadata unreadable: {operation_id}", True
    pid = int(meta.get("pid") or 0)
    running = bool(pid) and _process_alive(pid)
    try:
        log_text = log_path.read_text(
            encoding="utf-8", errors="ignore"
        ) if log_path.is_file() else ""
    except OSError:
        log_text = ""
    payload: dict[str, Any] = {
        "ok": True,
        "operation_id": operation_id,
        "command": meta.get("command"),
        "status": "running" if running else "finished",
    }
    if not running:
        # CLI 已退出：从日志尾部提取 JSON envelope 作为最终结果。
        final = None
        for line in reversed(log_text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and "ok" in candidate:
                    final = candidate
                    break
        if final is not None:
            payload["result"] = final
            payload["exit_code"] = final.get("exit_code")
        else:
            payload["diagnostics"] = bounded_text(log_text)[:MAX_OUTPUT_BYTES]
    else:
        # 运行中：给一段尾部日志帮助判断进度。
        payload["recent_output"] = bounded_text(log_text)
    return json.dumps(payload, ensure_ascii=False), False


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


def jobs_cancel_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return "missing required field: job_id", True
    return run_cli("gms-rt-jobs-cancel", [job_id])


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
    # worker_id (audit §六): resolve the owning worker authoritatively when
    # not explicit; ambiguity is an error, never a guess.
    resolved_worker, worker_error = _resolve_worker_for_device(
        device, arguments.get("worker_id")
    )
    if worker_error:
        return worker_error, True
    args = [device]
    for key in ("type", "module", "case", "suite"):
        value = str(arguments.get(key) or "").strip()
        if value:
            args.append(value)
    if resolved_worker:
        args.extend(["--worker", resolved_worker])
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


def jobs_follow_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """11.txt P1 §3: one-call status+events+failure-summary follow."""
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        return "Missing required argument: job_id", True
    args = [job_id]
    if arguments.get("after") is not None:
        try:
            args.extend(["--after", str(int(arguments["after"]))])
        except (TypeError, ValueError):
            return "after must be an integer (sequence offset)", True
    if arguments.get("limit") is not None:
        try:
            args.extend(["--limit", str(max(1, min(400, int(arguments["limit"]))))])
        except (TypeError, ValueError):
            return "limit must be an integer between 1 and 400", True
    return run_cli("gms-rt-jobs-follow", args)


def test_suites_list_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """11.txt 中优先级 §7: discover suite names for gms_rt_test_start."""
    return run_cli("gms-rt-test-suites", [])


def devices_ui_dump_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """11.txt P0 §2: structured UI layout tree (uiautomator dump path)."""
    device = str(arguments.get("device") or "").strip()
    if not device:
        return "device (serial) is required", True
    return run_cli("gms-rt-devices-ui-dump", [device])


def devices_snapshot_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """11.txt P1 §5: one-shot device state snapshot."""
    device = str(arguments.get("device") or "").strip()
    if not device:
        return "device (serial) is required", True
    return run_cli("gms-rt-devices-snapshot", [device])


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
        guidance = (
            "Use the dedicated typed MCP tool with explicit confirmation, "
            "or run it manually via the gms-rt CLI."
        )
        if normalized == "gms-rt-devices-shell":
            guidance = (
                "For device diagnosis use the read-only gms_rt_shell tool; "
                "for an approved one-shot command use gms_rt_shell_exec "
                "with an approval_token from gms_rt_approval_create."
            )
        return (
            f"denied: {normalized} is not agent-safe for unattended "
            f"execution (mode={descriptor.get('mode')}, "
            f"requires_explicit_authorization="
            f"{descriptor.get('requires_explicit_authorization')}). "
            f"{guidance}",
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
    # Read-only diagnostics added after the CTS profiling investigation
    # (2026-09-07): process/pattern lookup, log post-processing on files
    # already readable via cat, kernel ring buffer, and device_config reads.
    "pgrep", "grep", "wc", "head", "tail", "dmesg", "id", "printenv",
    "device_config", "cmd", "am",
    # 11.txt 高优先级 §3: high-frequency read-only diagnostics. Only the
    # explicit subcommands in _SHELL_CMD_READONLY_SUBCOMMANDS run; pm/dpm/
    # content also stay in _SHELL_SMUGGLING_BINARIES so they can never be
    # ARGUMENTS to another command (and can never be a pipe filter).
    "pm", "dpm", "content",
})
# Characters that enable chaining/redirection/substitution; none of the
# allowlisted read-only commands need them.
_SHELL_FORBIDDEN_CHARS = frozenset(";|&><`(){}[]$\\'\"\n\r\t*?")
# dumpsys service subcommands known to mutate state.
_SHELL_DUMPSYS_MUTATING = frozenset({
    "unplug", "reset", "disable", "enable", "whitelist", "set-debug-app",
    "force-stop", "kill", "suspend", "resume", "reset-role",
    # battery/simulation setters mutate device state (R12):
    # dumpsys battery set level 1 was previously allowed through.
    "set", "plug", "charge", "nocharge", "persist", "import",
})
# Binaries that may smuggle arbitrary execution; never allow as arguments.
# NOTE: 'cmd' and 'am' are in _SHELL_READONLY_BINARIES but only for the
# explicit read-only subcommands in _SHELL_CMD_READONLY_SUBCOMMANDS; they
# must stay here so they are rejected as arguments to other binaries.
_SHELL_SMUGGLING_BINARIES = frozenset({
    "sh", "bash", "su", "toybox", "toolbox", "nohup", "xargs", "run-as",
    "pm", "input", "svc", "reboot", "sync", "dd", "rm", "mv",
    "cp", "mkdir", "touch", "chmod", "chown", "kill",
})
# 'cmd' / 'am' / 'pm' / 'dpm' / 'content' read-only subcommand allowlist
# (exact prefix match on the joined argument string).
# 11.txt 高优先级 §3: these are the high-frequency diagnostics agents were
# forced to SSH for (device-owner inspection, intent resolution, package
# inventory). Everything mutating (pm trim-caches/clear, dpm force-*,
# content insert/update/delete) stays OUT of the list.
_SHELL_CMD_READONLY_SUBCOMMANDS = {
    "cmd": (
        "list", "help",
        "package list", "package path", "package dump", "package help",
        "package query-activities", "package query-services",
        "package query-receivers", "package query-content-providers",
    ),
    "am": ("stack list", "get-current-user", "get-standby-bucket"),
    "pm": (
        "list users", "list packages", "list permissions",
        "list permission-groups", "list features", "list libraries",
        "list instrumentation", "list jobs", "path", "help",
    ),
    "dpm": ("list-owners",),
    "content": ("query",),
}
# 11.txt 高优先级 §3: the only binaries allowed on the RIGHT side of the
# single restricted pipe ("readonly_cmd | filter_cmd"). grep/wc/head/tail
# are already individually allowlisted as leading binaries; this set gates
# their use as pipe filters.
_SHELL_FILTER_BINARIES = frozenset({"grep", "wc", "head", "tail"})
# device_config subcommands that mutate device state.
_SHELL_DEVICE_CONFIG_MUTATING = frozenset({
    "put", "delete", "edit", "reset", "set-sync-disabled-for-test",
})
_SHELL_MAX_COMMAND_CHARS = 2000
_DEVICE_ID_PATTERN = None  # compiled lazily


def _split_short_option(token: str) -> list[str]:
    """Split a combined short-option token ('-dc' → ['-d', '-c']).

    getopt_short clusters: ``-dc`` is exactly equivalent to ``-d -c`` for
    every binary using getopt_short (logcat, dmesg, toolbox applets), so
    the gate must evaluate each cluster letter as its own flag. A leading
    '-' followed by multiple letters, or '-' + letters + attached value,
    all expand here.
    """
    if not token.startswith("-") or token == "-" or token.startswith("--"):
        return [token]
    body = token[1:]
    # Attached value form: -fPATH → the whole token is the flag -f plus a
    # value; keep it as one token (callers check startswith) but also
    # expose the leading flag.
    return [f"-{ch}" for ch in body]


def _expand_option_tokens(tokens: list[str]) -> list[str]:
    """Expand clustered short options into individual flags.

    Only clusters of KNOWN short-flag letters expand: '-dc' for logcat is
    -d + -c. Unknown-letter clusters stay intact so value tokens (file
    names like '-some-file') are not misread as flags.
    """
    expanded: list[str] = []
    for token in tokens:
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            expanded.extend(_split_short_option(token))
        else:
            expanded.append(token)
    return expanded


def _is_long_option_prefix(token: str, long_name: str) -> bool:
    """True when token is --name or --name=value or an unambiguous
    abbreviation that getopt_long would still accept as --name."""
    if not token.startswith("--"):
        return False
    body = token[2:].split("=", 1)[0]
    return long_name.startswith(body) and body  # non-empty prefix of long_name


def _validate_single_shell_command(command: str) -> tuple[bool, str]:
    """Validate ONE pipe-free command against the structured allowlist."""
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
        # Positive structural allowlist: expand clustered shorts, then every
        # option token must be a known read-only flag. '-c' inside '-dc',
        # abbreviated '--cle', and attached-value '-fPATH' all fail here.
        allowed_logcat_flags = {
            "-d", "-v", "-t", "-T", "-g", "-b", "-s", "-e", "-m",
            "-n", "-r", "-P", "-Q", "-p", "-L", "-D", "-B", "-G", "-S",
            "--dividers", "--buffer", "--format", "--tag", "--uid",
            "--pid", "--print", "--statistics", "--help",
        }
        needs_value = {"-v", "-b", "-t", "-T", "-e", "-m", "-n", "-r", "-D", "-G", "-s"}
        i = 0
        has_dump_mode = False
        while i < len(rest):
            token = rest[i]
            if token.startswith("--"):
                if _is_long_option_prefix(token, "clear") or _is_long_option_prefix(token, "file"):
                    return False, (
                        "logcat --clear/--file mutate or write and are not allowed"
                    )
                matched = any(
                    _is_long_option_prefix(token, flag[2:])
                    for flag in allowed_logcat_flags
                    if flag.startswith("--")
                )
                if not matched:
                    if token in ("--filename",):
                        return False, "logcat --filename writes files"
                    return False, (
                        f"logcat option '{token}' is not in the read-only allowlist"
                    )
                if token in ("--print", "--statistics", "--dividers"):
                    has_dump_mode = has_dump_mode or token == "--print"
                i += 1
                continue
            cluster = _split_short_option(token)
            for flag in cluster:
                if flag == "-c":
                    return False, (
                        "logcat -c clears the log buffer and is not allowed "
                        "(including inside clustered flags like -dc)"
                    )
                if flag == "-f":
                    return False, (
                        "logcat -f writes files and is not allowed "
                        "(including attached-value forms like -f/path)"
                    )
                if flag not in allowed_logcat_flags:
                    return False, (
                        f"logcat option '{flag}' (from '{token}') is not in "
                        "the read-only allowlist"
                    )
                if flag == "-d":
                    has_dump_mode = True
            # Skip the value consumed by value-taking flags.
            value_takers = [f for f in cluster if f in needs_value]
            i += 1 + len(value_takers)
        if not has_dump_mode and "-t" not in rest and "-T" not in rest:
            return False, (
                "streaming logcat is not allowed; add -d/-t/-T (dump mode)"
            )
        has_dump_mode = has_dump_mode or any(
            t in rest for t in ("-d", "-t", "-T")
        )
        if not has_dump_mode:
            return False, (
                "streaming logcat is not allowed; add -d/-t/-T (dump mode)"
            )
    elif binary == "dmesg":
        # R12: dmesg -c (and -C, including inside clusters like -tc) clear
        # the kernel ring buffer; positively allow only read-only flags.
        allowed_dmesg = {"-T", "-t", "-r", "-H", "-e", "-n", "--color=never"}
        for token in rest:
            for flag in _split_short_option(token):
                if flag in ("-c", "-C"):
                    return False, (
                        "dmesg -c/-C clear the kernel ring buffer and are "
                        "not allowed (including clustered forms)"
                    )
                if flag not in allowed_dmesg:
                    return False, (
                        f"dmesg option '{flag}' is not in the read-only allowlist"
                    )
    elif binary == "dumpsys":
        # Expand clusters so 'dumpsys battery set' style mutating args and
        # combined flags cannot smuggle through.
        for token in rest:
            for flag in _split_short_option(token):
                if flag in _SHELL_DUMPSYS_MUTATING:
                    return False, "dumpsys service arguments may mutate device state"
    elif binary == "device_config":
        if not rest:
            return False, "device_config requires a subcommand"
        if rest[0] in _SHELL_DEVICE_CONFIG_MUTATING:
            return False, f"device_config {rest[0]} mutates device state"
        if rest[0] not in ("get", "list"):
            return False, "only 'device_config get/list' is allowed"
    elif binary in _SHELL_CMD_READONLY_SUBCOMMANDS:
        joined = " ".join(rest)
        if not any(joined == sub or joined.startswith(sub + " ")
                   for sub in _SHELL_CMD_READONLY_SUBCOMMANDS[binary]):
            allowed = "; ".join(
                f"'{binary} {sub}'" for sub in _SHELL_CMD_READONLY_SUBCOMMANDS[binary]
            )
            return False, f"only {allowed} are allowed for '{binary}'"
    return True, ""


def _validate_shell_command(command: str) -> tuple[bool, str]:
    """Return (allowed, reason) for a proposed device shell command.

    R12: the gate is STRUCTURED and POSITIVE per binary; every option token
    must match an explicit per-binary allowlist. 11.txt 高优先级 §3 adds ONE
    restricted pipe: ``<readonly command> | <grep|wc|head|tail ...>`` —
    both sides must independently pass the full structured allowlist, so
    this covers ~90% of the SSH-bypass motive (log/text post-filtering)
    without opening chaining, redirection, or output capture.
    """
    segments = [segment.strip() for segment in command.split("|")]
    if len(segments) > 2:
        return False, "at most one pipe is allowed"
    if len(segments) == 2:
        if not segments[0] or not segments[1]:
            return False, "empty pipe segment (note: '||' chaining is denied)"
        ok, reason = _validate_single_shell_command(segments[0])
        if not ok:
            return False, f"pipe head: {reason}"
        ok, reason = _validate_single_shell_command(segments[1])
        if not ok:
            return False, f"pipe tail: {reason}"
        tail_binary = segments[1].split()[0]
        if tail_binary not in _SHELL_FILTER_BINARIES:
            return False, (
                f"only {', '.join(sorted(_SHELL_FILTER_BINARIES))} are allowed "
                f"as pipe filters, got '{tail_binary}'"
            )
        return True, ""
    return _validate_single_shell_command(command)


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
    # 12.txt P0: gms-rt-shell is the typed read-only surface — the CLI's
    # service-token approval gate must not block allowlisted probes, while
    # every non-allowlisted command still requires a one-shot approval.
    return run_cli(
        "gms-rt-devices-shell",
        [device, command],
        timeout=timeout,
        env_extra={"GMS_RT_TYPED_READONLY": "1"},
    )


# ---------------------------------------------------------------------------
# Device logcat capture (typed tool, v0.7.0)
# ---------------------------------------------------------------------------

# The CLI catalog lists gms-rt-devices-logcat as read_only and agent-safe
# (dump mode under --non-interactive). This typed tool adds an adapter-side
# argument gate (dump flag, no -f, no metacharacters) so agents skip the
# describe+run round trip and cannot smuggle destructive flags. Buffer
# clearing (logcat -c) destroys diagnostic evidence — this platform's CTS/
# GTS/VTS incident data — so it is human-only via the CLI since v0.13.0
# (10.txt §六): the tool denies clear=true and any -c/--clear form in args.
_LOGCAT_FORBIDDEN_CHARS = frozenset(";|&><`(){}[]$\\'\"\n\r")
_LOGCAT_MAX_ARGS = 16


def _is_clear_request(arguments: dict[str, Any]) -> bool:
    value = arguments.get("clear")
    return value is True or (
        isinstance(value, str) and value.strip().lower() == "true"
    )


def logcat_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    import re

    global _DEVICE_ID_PATTERN
    device = str(arguments.get("device") or "").strip()
    if not device:
        return "Missing required argument: device", True
    if _DEVICE_ID_PATTERN is None:
        _DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
    if not _DEVICE_ID_PATTERN.match(device):
        return "denied: device id must match [A-Za-z0-9._:-]{1,64}", True
    args = arguments.get("args")
    items: list[str] = []
    if args is not None:
        if isinstance(args, str):
            items = args.split()
        elif isinstance(args, list):
            items = [str(item) for item in args if str(item).strip()]
        else:
            return "args must be a string or a list of logcat arguments", True
    if len(items) > _LOGCAT_MAX_ARGS:
        return f"too many logcat arguments (max {_LOGCAT_MAX_ARGS})", True
    # R08: 专用 logcat 工具与 gms_rt_shell 的 logcat 分支共用同一套解析
    # 与正向允许策略。组合短选项（-dc）、长选项缩写（--cle）和附着值
    # （-df/tmp/x）都会被拦截。R17（10.txt §六）：清空 logcat 缓冲会销毁
    # 诊断证据，MCP Agent 侧一律拒绝——无论来自 clear=true 还是 args 里
    # 的 -c/--clear（含缩写/组合形式）；清日志是人工 CLI 步骤
    # （gms-rt-devices-logcat DEVICE -c）。
    if _is_clear_request(arguments):
        return (
            "denied: clearing the device log buffer (logcat -c) destroys "
            "diagnostic evidence and is human-only. Ask the user to run "
            "'gms-rt-devices-logcat DEVICE -c' in their own CLI session, "
            "then capture the fresh dump here without the clear flag.",
            True,
        )
    for item in items:
        if (
            item == "-c"
            or item == "--clear"
            or item.startswith("--cle")
            or (item.startswith("-") and not item.startswith("--") and "-c" in item[1:])
        ):
            return (
                "denied: clearing the logcat buffer destroys diagnostic "
                "evidence and is human-only. Ask the user to run "
                "'gms-rt-devices-logcat DEVICE -c' in their own CLI "
                "session; this tool captures dumps only (no -c anywhere "
                "in args).",
                True,
            )
    if items:
        # since/until 会在后续追加 -t <time>，这里先按最终形态校验：
        # -t/-T 的值由本工具控制，校验时预留其位置。
        probe_items = list(items)
        since_probe = arguments.get("since")
        if since_probe is not None and str(since_probe).strip() \
                and "-t" not in probe_items and "-T" not in probe_items:
            probe_items.extend(["-t", "00-00 00:00:00"])
        # 校验器按空格切分字符串，无法识别"带空格的单个 argv 值"
        # （如 -t 的时间戳）；把取值选项的后续 token 替换为占位符，
        # 值本身的格式已由本工具/校验器另行限制。
        _VALUE_TAKING = ("--format", "--buffer", "-v", "-b", "-t", "-T",
                         "-e", "-m", "-n", "-r", "-D", "-G", "-s")
        masked: list[str] = []
        expect_value = False
        for token in probe_items:
            if expect_value:
                masked.append("TIME")
                expect_value = False
                continue
            masked.append(token)
            if token in _VALUE_TAKING:
                expect_value = True
        probe_command = "logcat " + " ".join(masked)
        # 工具会在没有 dump 标志时注入 -d，校验应针对最终 argv 形态。
        if not any(
            flag in probe_items
            for flag in ("-d", "-t", "-T", "-g", "-L", "-p", "-print")
        ):
            probe_command = "logcat -d " + " ".join(masked)
        allowed, reason = _validate_shell_command(probe_command)
        if not allowed:
            return f"denied: {reason}", True
    for item in items:
        if any(ch in _LOGCAT_FORBIDDEN_CHARS for ch in item):
            return (
                "denied: logcat arguments must not contain shell "
                "metacharacters or quotes",
                True,
            )

    # Native time-window support (2026-09-07): 'since' maps to logcat -t
    # <time> (dump entries at/after the timestamp, device-side filtering);
    # 'until' trims the captured output client-side. Validated against the
    # logcat time format so the value is always a safe single argument.
    since_value = arguments.get("since")
    until_value = arguments.get("until")
    timestamp_pattern = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,3})?$")
    since_text: str | None = None
    until_text: str | None = None
    for label, raw in (("since", since_value), ("until", until_value)):
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if not timestamp_pattern.match(text):
            return (
                f"denied: {label} must match 'MM-DD HH:MM:SS[.mmm]' "
                "(logcat time format), got: " + text,
                True,
            )
        if label == "since":
            since_text = text
        else:
            until_text = text
    if since_text is not None:
        if "-t" in items or "-T" in items:
            return (
                "denied: -t/-T in args conflicts with the since parameter; "
                "use only one of them",
                True,
            )
        items.extend(["-t", since_text])
        if len(items) > _LOGCAT_MAX_ARGS + 2:
            return f"too many logcat arguments (max {_LOGCAT_MAX_ARGS})", True
    if until_text is not None and since_text is None and not any(
        flag in items for flag in ("-d", "-t", "-T", "-g", "-L", "-p", "-print")
    ):
        # -d is added below anyway; keep the check consistent with the
        # existing dump-mode logic.
        pass
    # Dump mode keeps the call bounded for unattended agents; the CLI adds
    # -d itself in --non-interactive mode when no dump flag is present.
    if not any(
        flag in items for flag in ("-d", "-t", "-T", "-g", "-L", "-p", "-print")
    ):
        items = ["-d", *items]
    # R17 (10.txt §六): no -c is ever injected from the adapter — buffer
    # clearing is human-only and every clear form was rejected above.
    timeout = 180
    if arguments.get("timeout") is not None:
        try:
            timeout = min(600, max(1, int(arguments["timeout"])))
        except (TypeError, ValueError):
            return "timeout must be an integer (seconds)", True
    # 12.txt P0: dump-mode logcat is a typed read-only surface; the CLI
    # service-token gate must not demand an approval for it (the tool
    # already denies -c and -f, which are the destructive forms).
    text, is_error = run_cli(
        "gms-rt-devices-logcat",
        [device, *items],
        timeout=timeout,
        env_extra={"GMS_RT_TYPED_READONLY": "1"},
    )
    if is_error or until_text is None:
        return text, is_error
    # Client-side 'until' trim: logcat has no end-time flag in dump mode, so
    # drop entries strictly after the given timestamp. Line timestamps sort
    # lexicographically within the same year ("09-07 10:52:00.000"); header
    # lines ("--------- ...") carry no timestamp and are kept verbatim.
    kept: list[str] = []
    dropped = 0
    for line in text.splitlines():
        stamp = line[0:18] if len(line) >= 18 else ""
        if len(stamp) == 18 and stamp[2] == "-" and stamp[5] == " ":
            if stamp >= until_text:
                dropped += 1
                continue
        kept.append(line)
    if dropped:
        kept.append(f"...[until trim: dropped {dropped} entries after {until_text}]")
    return "\n".join(kept), False


# ---------------------------------------------------------------------------
# Approved one-shot device shell (typed tool, v0.9.0)
# ---------------------------------------------------------------------------

# The CLI catalog deliberately marks gms-rt-devices-shell manual: the bare
# form opens an interactive shell and arbitrary commands are state-changing
# by nature, so the generic runner (gms_rt_run) denies it outright. This
# typed tool is the approval-token escape hatch for one-shot commands: the
# caller must pass a one-shot approval token that the SERVER validates
# against tool+device+SHA256(command), TTL and single use (2026-09-08 audit
# §五). A client-declared authorized=true boolean was never a security
# boundary — any MCP client could pass true itself. Read-only diagnosis
# should still go through gms_rt_shell (allowlist, no approval needed).
_SHELL_EXEC_MAX_COMMAND_CHARS = 2000


def shell_exec_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    import re

    global _DEVICE_ID_PATTERN
    device = str(arguments.get("device") or "").strip()
    command = str(arguments.get("command") or "").strip()
    if not device:
        return "Missing required argument: device", True
    if not command:
        return (
            "Missing required argument: command (one-shot only; the "
            "interactive device shell is not available to agents)",
            True,
        )
    approval_token = str(arguments.get("approval_token") or "").strip()
    if not approval_token:
        return (
            "denied: server-side approval required. Ask the user to run "
            "'gms-rt-approval-create --tool gms_rt_shell_exec --device "
            f"{device} --command {command}' under their own session "
            "(the token is valid 5 minutes and single-use), then pass "
            "approval_token here. For read-only diagnosis use gms_rt_shell "
            "instead (no approval needed).",
            True,
        )
    if _DEVICE_ID_PATTERN is None:
        _DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
    if not _DEVICE_ID_PATTERN.match(device):
        return "denied: device id must match [A-Za-z0-9._:-]{1,64}", True
    if len(command) > _SHELL_EXEC_MAX_COMMAND_CHARS:
        return (
            f"denied: command exceeds {_SHELL_EXEC_MAX_COMMAND_CHARS} "
            "characters; split the work into smaller commands",
            True,
        )
    timeout = 120
    if arguments.get("timeout") is not None:
        try:
            timeout = min(600, max(1, int(arguments["timeout"])))
        except (TypeError, ValueError):
            return "timeout must be an integer (seconds)", True
    # The CLI validates-and-consumes the approval server-side before running
    # the command (gms-rt-devices-shell --approval-token ...).
    return run_cli(
        "gms-rt-devices-shell",
        [device, "--approval-token", approval_token, command],
        timeout=timeout,
    )


def tools() -> list[dict[str, Any]]:
    all_tools = _all_tools()
    if _SERVICE_TOKEN_MODE:
        # Service-token mode: the human-session credential tools are not even
        # advertised, so an agent context cannot express a password login or
        # self-mint an approval (2026-09-09 audit, 10.txt §五).
        all_tools = [
            tool
            for tool in all_tools
            if tool.get("name") not in _HUMAN_SESSION_TOOLS
        ]
    requested = {
        value.strip().lower()
        for value in os.environ.get("GMS_MCP_TOOLSETS", "").split(",")
        if value.strip()
    }
    if requested:
        always = {
            "gms_rt_context",
            "gms_rt_commands",
            "gms_rt_describe",
            "gms_rt_auth_status",
        }
        all_tools = [
            tool
            for tool in all_tools
            if tool["name"] in always
            or bool(_TOOLSETS.get(tool["name"], {"core"}) & requested)
        ]
    return [_decorate_tool(tool) for tool in all_tools]


_TOOLSETS = {
    "gms_rt_test_start": {"test"},
    "gms_rt_jobs_list": {"test"},
    "gms_rt_jobs_status": {"test"},
    "gms_rt_jobs_wait": {"test"},
    "gms_rt_jobs_events": {"test"},
    "gms_rt_jobs_follow": {"test"},
    "gms_rt_jobs_cancel": {"test"},
    "gms_rt_test_suites_list": {"test"},
    "gms_rt_reports_list": {"test"},
    "gms_rt_burn_firmware": {"admin"},
    "gms_rt_burn_status": {"admin"},
    "gms_rt_shell_exec": {"admin"},
    "gms_rt_approval_create": {"admin"},
    **{
        name: {"evidence"}
        for name in (
            "gms_rt_apk_resolve",
            "gms_rt_apk_analyze",
            "gms_rt_apk_status",
            "gms_rt_apk_manifest",
            "gms_rt_apk_search",
            "gms_rt_apk_source",
            "gms_rt_redmine_issue_fetch",
            "gms_rt_redmine_issue",
            "gms_rt_redmine_journals",
            "gms_rt_redmine_attachments",
            "gms_rt_redmine_artifact_search",
            "gms_rt_redmine_artifact_read",
            "gms_rt_redmine_image",
            "gms_rt_apk_analyze_attachment",
            "gms_rt_apk_source_search",
            "gms_rt_apk_source_read",
            "gms_rt_sdk_sources",
            "gms_rt_sdk_search",
            "gms_rt_sdk_read",
        )
    },
}


def _decorate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Add MCP safety hints without changing existing input contracts."""

    result = dict(tool)
    name = str(result.get("name", ""))
    mutating = name in {
        "gms_rt_test_start",
        "gms_rt_jobs_cancel",
        "gms_rt_burn_firmware",
        "gms_rt_shell_exec",
        "gms_rt_agent_enroll",
        "gms_rt_approval_create",
        "gms_rt_auth_login",
        "gms_rt_auth_elevate",
        "gms_rt_apk_analyze",
        "gms_rt_apk_analyze_attachment",
    }
    destructive = name in {"gms_rt_burn_firmware", "gms_rt_shell_exec"}
    result.setdefault(
        "annotations",
        {
            "readOnlyHint": not mutating,
            "destructiveHint": destructive,
            "idempotentHint": not mutating,
            "openWorldHint": True,
        },
    )
    return result


def _all_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "gms_rt_context",
            "description": (
                "Run the secret-free GMS environment self-check. Call this first: "
                "it reports CLI version, selected Controller, credential mode, "
                "health, visible devices, local suites, and actionable hints. "
                "CLI equivalent: gms-rt-system-selfcheck."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
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
                            "Optional secret forwarded on stdin — HUMAN "
                            "sessions only (gms-rt-auth-login / "
                            "gms-rt-auth-elevate with the user's explicit "
                            "credentials). Agents authenticate via "
                            "GMS_AUTH_TOKEN_FILE and never pass passwords. "
                            "Never log it."
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
                    "command": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_devices",
            "description": (
                "List Android devices known to the Controller with state, "
                "serials, and transport. For cluster deployments prefer "
                "gms_rt_cluster_devices, which includes the owning worker_id "
                "needed to target devices unambiguously. CLI equivalent: "
                "gms-rt-devices-list. MCP tool names use underscores; CLI "
                "command names use hyphens."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_device_console",
            "description": (
                "List Controller serial-console ports, or read retained logs "
                "for one stable port key. Interactive serial input remains "
                "human/Web-UI only. CLI equivalent: gms-rt-devices-console."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "port_key": {
                        "type": "string",
                        "description": "Stable port key; omit to list ports.",
                    },
                    "tail": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                        "description": "Return at most this many retained lines.",
                    },
                    "date": {
                        "type": "string",
                        "pattern": "^[0-9]{8}$",
                        "description": "Optional retained-log date in YYYYMMDD.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_device_info",
            "description": (
                "Read detailed properties for one or more devices. Device "
                "prefixes must resolve uniquely. CLI equivalent: "
                "gms-rt-devices-info."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "devices": {
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "minItems": 1,
                            },
                        ]
                    }
                },
                "required": ["devices"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_device_wait",
            "description": (
                "Wait until one or more devices reach online, fastboot, or "
                "either state. CLI equivalent: gms-rt-devices-wait."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "devices": {
                        "oneOf": [
                            {"type": "string", "minLength": 1},
                            {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "minItems": 1,
                            },
                        ]
                    },
                    "state": {
                        "type": "string",
                        "enum": ["online", "fastboot", "any"],
                        "default": "online",
                    },
                    "interval": {"type": "integer", "minimum": 1, "maximum": 300},
                    "max_wait": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86400,
                    },
                    "timeout": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 86500,
                    },
                },
                "required": ["devices"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_cluster_workers",
            "description": (
                "List cluster workers (id, status, device counts). Call "
                "before targeting devices when multiple build servers "
                "(workers) are attached."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_cluster_devices",
            "description": (
                "List the cluster-wide device inventory including each "
                "device's owning worker_id (authoritative for multi-worker "
                "deployments). Filter by --worker or a serial substring."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "worker_id": {
                        "type": "string",
                        "description": "Optional worker id filter.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive serial substring filter."
                        ),
                    },
                },
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
                "cookie (gms-rt-auth-login USERNAME --password-stdin). "
                "HUMAN-session tool: only call with credentials the user "
                "explicitly provided, and prefer the Agent Service Token "
                "(gms_rt_agent_enroll + GMS_AUTH_TOKEN_FILE) so no password "
                "ever flows through MCP. Not registered in service-token "
                "mode; the password travels via stdin and is never logged."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "username": {"type": "string", "minLength": 0, "maxLength": 2048},
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
                "Unlocks elevated operations such as firmware burn. "
                "HUMAN-session tool: only call with admin credentials the "
                "user explicitly provided, and prefer GMS_AUTH_TOKEN_FILE "
                "agent-token auth so no password ever flows through MCP. "
                "Not registered in service-token mode; the password "
                "travels via stdin and is never logged."
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
            "name": "gms_rt_agent_enroll",
            "description": (
                "Exchange a one-shot enrollment code for a permanent Agent "
                "Service Token stored as a 0600 file (gms-rt-agent-enroll). "
                "The admin mints the code in the web UI (5-minute TTL); "
                "after enrollment set GMS_AUTH_TOKEN_FILE to the token file "
                "so every CLI/MCP call authenticates without any password."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "One-shot enrollment code, e.g. 7K3M-FG9A-WX21.",
                    },
                    "out_file": {
                        "type": "string",
                        "description": (
                            "Optional token file path (default "
                            "~/.local/state/gms-remote-test/<profile>.token, 0600)."
                        ),
                    },
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_approval_create",
            "description": (
                "Create a one-shot approval token for a destructive action "
                "(gms-rt-approval-create). MUST run under the user's own "
                "human session (cookie), never an agent token. Bindings: "
                "tool + device + exact command, 5-minute TTL, single use. "
                "For gms_rt_burn_firmware the approval additionally binds "
                "the firmware SHA-256, wipe_data and burn_mode (server-"
                "derived operation string), so it is valid for exactly that "
                "firmware. The agent then passes the token to "
                "gms_rt_shell_exec / gms_rt_burn_firmware as approval_token."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": (
                            "gms_rt_shell_exec or gms_rt_burn_firmware."
                        ),
                    },
                    "device": {
                        "type": "string",
                        "description": (
                            "Target device serial, comma-separated for "
                            "multi-device burns."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Exact command being approved (shell_exec only; "
                            "ignored for burn, which derives its binding "
                            "from firmware_sha256/wipe_data/burn_mode)."
                        ),
                    },
                    "firmware_sha256": {
                        "type": "string",
                        "description": (
                            "Required for gms_rt_burn_firmware: SHA-256 of "
                            "the exact update.img to burn (compute with "
                            "sha256sum)."
                        ),
                    },
                    "wipe_data": {
                        "type": "boolean",
                        "description": (
                            "Burn only: wipe userdata (default true)."
                        ),
                    },
                    "burn_mode": {
                        "type": "string",
                        "enum": ["auto", "uf"],
                        "description": (
                            "Burn only: burn_mode binding (default auto)."
                        ),
                    },
                },
                "required": ["tool", "device"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_burn_firmware",
            "description": (
                "Burn firmware (update.img) to one or more devices via "
                "gms-rt-burn-firmware. Destructive: requires a one-shot "
                "approval token (gms_rt_approval_create; agent tokens can "
                "never self-approve). Default wait=false returns an "
                "operation_id immediately; poll gms_rt_burn_status every "
                "20-30s so no single MCP call runs long."
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
                    "approval_token": {
                        "type": "string",
                        "description": (
                            "One-shot approval token from "
                            "gms_rt_approval_create (tool="
                            "gms_rt_burn_firmware, bound to this firmware's "
                            "SHA-256 + device list + wipe_data + burn_mode). "
                            "Server-enforced; a burn without it is denied "
                            "for agent tokens."
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
                        "description": (
                            "Per-call timeout in seconds (default 1800, only "
                            "used with wait=true)."
                        ),
                    },
                    "wait": {
                        "type": "boolean",
                        "description": (
                            "false (default): start the burn in the "
                            "background and return operation_id immediately "
                            "(recommended for 60s-capped MCP clients like "
                            "Kimi); poll with gms_rt_burn_status. true: "
                            "legacy synchronous wait (may exceed client "
                            "tool-call timeouts)."
                        ),
                    },
                },
                "required": ["firmware_path", "device", "approval_token"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_burn_status",
            "description": (
                "Poll a background firmware burn started with "
                "gms_rt_burn_firmware (wait=false). Returns "
                "status=running with recent output, or status=finished with "
                "the final JSON envelope and exit_code. Cheap: safe to call "
                "every 20-30s."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "operation_id": {
                        "type": "string",
                        "description": (
                            "operation_id returned by gms_rt_burn_firmware."
                        ),
                    },
                },
                "required": ["operation_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_test_start",
            "description": (
                "Start a GMS test (CTS/GTS/VTS/STS) on a device, or retry a "
                "previous report (retry=<timestamp> from a failed report). "
                "Returns a cluster_job_id; follow up with gms_rt_jobs_status "
                "polling (every 20-30s) and gms_rt_jobs_events instead of "
                "long blocking waits — MCP clients often cap a single tool "
                "call at 60s, so prefer wait=false plus polling."
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
                    "worker_id": {
                        "type": "string",
                        "description": (
                            "Owning cluster worker. Optional: auto-resolved "
                            "via the cluster inventory; ambiguous devices "
                            "fail with exit 5 instead of guessing. Pass it "
                            "explicitly when multiple workers share serials."
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
                    "job_id": {"type": "string", "minLength": 0, "maxLength": 2048},
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
                    "job_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "max_wait": {"type": "integer", "minimum": 0, "maximum": 21600},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_cancel",
            "description": (
                "Request cancellation of one durable test job owned by the "
                "current principal. This is mutating: call only when the user "
                "explicitly asked to stop that job. Requires tests.cancel. "
                "CLI equivalent: gms-rt-jobs-cancel."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 1, "maxLength": 256}
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
                    "job_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "after": {"type": "integer", "minimum": 0, "maximum": 1000000000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_jobs_follow",
            "description": (
                "One-call job follow (11.txt P1): current status + events "
                "since a cursor + a compact failed-case summary when the job "
                "already reached a terminal state (server-parsed from "
                "test_result.xml, no raw log paging)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "after": {
                        "type": "integer", "minimum": 0, "maximum": 1000000000,
                        "description": "Event sequence cursor from a previous call (default -1).",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 400,
                        "description": "Max events returned per call (default 100).",
                    },
                },
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_test_suites_list",
            "description": (
                "List the test suites available on the controller "
                "(CTS/GTS/VTS/STS). Use the returned names as the `suite` "
                "argument of gms_rt_test_start instead of guessing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_devices_ui_dump",
            "description": (
                "Dump the current UI layout tree of one device as structured "
                "JSON elements (bounds/text/clickable). Read-only UI "
                "diagnosis for CTS-V/GTS interface issues."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                },
                "required": ["device"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_devices_snapshot",
            "description": (
                "One-shot device state snapshot (11.txt P1 §5): build "
                "fingerprint, focused activity, keyguard/lock state, and "
                "active device-admin/device-owner list in a single call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                },
                "required": ["device"],
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
                    "worker_id": {
                        "type": "string",
                        "description": (
                            "Owning cluster worker (optional; ambiguity "
                            "fails instead of guessing)."
                        ),
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
            "name": "gms_rt_logcat",
            "description": (
                "Capture device logcat via `adb shell logcat -v time` "
                "(gms-rt-devices-logcat). Runs in one-shot dump mode (-d) "
                "for unattended agents; -f (write device files) and shell "
                "metacharacters are denied. Clearing the log buffer is "
                "human-only (logcat -c destroys diagnostic evidence): this "
                "tool denies clear=true and raw -c/--clear in args. "
                "Optional logcat args, e.g. '-b crash', "
                "'-t 500', '-s ActivityManager'. Use for device log "
                "diagnosis; other log management needs the human CLI."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                    "worker_id": {
                        "type": "string",
                        "description": (
                            "Owning cluster worker (optional; ambiguity "
                            "fails instead of guessing)."
                        ),
                    },
                    "args": {
                        "description": (
                            "Optional logcat arguments as a list or one "
                            "string, e.g. \"-b crash -t 500\"."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Dump only entries at/after this time (device-"
                            "side logcat -t filter). Format "
                            "'MM-DD HH:MM:SS' or 'MM-DD HH:MM:SS.mmm', "
                            "e.g. '09-07 10:52:00.000'. Conflicts with "
                            "-t/-T in args."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "Drop captured entries after this time "
                            "(client-side trim; logcat has no end-time "
                            "flag). Same format as since. Combine with "
                            "since for a bounded time window."
                        ),
                    },
                    "clear": {
                        "type": "boolean",
                        "description": (
                            "Deprecated/denied: clearing the device log "
                            "buffer destroys diagnostic evidence and is "
                            "human-only; every clear request is rejected."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds (1-600, default 180).",
                    },
                },
                "required": ["device"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_shell_exec",
            "description": (
                "Run a USER-APPROVED one-shot shell command on a device "
                "via gms-rt-devices-shell DEVICE --approval-token TOKEN "
                "COMMAND. The approval token comes from "
                "gms_rt_approval_create (tool=gms_rt_shell_exec) run by the "
                "user under their own session; the server validates "
                "tool+device+command binding, 5-minute TTL and single use. "
                "Prefer gms_rt_shell (read-only allowlist, no approval) for "
                "diagnosis. Never opens an interactive shell."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                    "worker_id": {
                        "type": "string",
                        "description": (
                            "Owning cluster worker (optional; ambiguity "
                            "fails instead of guessing)."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "One-shot shell command, e.g. "
                            "'am broadcast -a android.intent.action.BOOT_COMPLETED'."
                        ),
                    },
                    "approval_token": {
                        "type": "string",
                        "description": (
                            "One-shot approval token created by the user via "
                            "gms_rt_approval_create; replaces the old "
                            "client-declared authorized=true (not a security "
                            "boundary)."
                        ),
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds (1-600, default 120).",
                    },
                },
                "required": ["device", "command", "approval_token"],
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
        {
            "name": "gms_rt_apk_resolve",
            "description": (
                "Resolve a test module keyword (e.g. CtsCamera) to its "
                "APK/JAR artifact in the latest CTS/VTS/GTS/STS suites. "
                "Returns module, suite path, and the analyze_path consumed "
                "by gms_rt_apk_analyze. Cheap read-only lookup."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Module keyword, e.g. CtsCamera.",
                    },
                    "suite_types": {
                        "type": "string",
                        "description": (
                            "Comma-separated suite types "
                            "(default cts,vts,gts,sts)."
                        ),
                    },
                    "prefer": {
                        "type": "string",
                        "description": "Preferred artifact type: apk (default) or jar.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_analyze",
            "description": (
                "One-shot: resolve a test module to its APK/JAR in the "
                "latest suites, copy the artifact, and start jadx "
                "decompilation. Default (wait=false) returns task_id plus "
                "status=analyzing immediately — poll with gms_rt_apk_status "
                "(every 10-20s). wait=true blocks until completed/error or "
                "max_wait (default 300s; MCP clients often cap a single "
                "tool call at 60s, so prefer wait=false plus polling)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Module keyword, e.g. CtsCamera.",
                    },
                    "suite_types": {
                        "type": "string",
                        "description": (
                            "Comma-separated suite types "
                            "(default cts,vts,gts,sts)."
                        ),
                    },
                    "prefer": {
                        "type": "string",
                        "description": "Preferred artifact type: apk (default) or jar.",
                    },
                    "wait": {
                        "type": "boolean",
                        "description": "Block until a terminal analysis state.",
                    },
                    "max_wait": {
                        "type": "integer",
                        "description": "Seconds to wait when wait=true (default 300).",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_status",
            "description": (
                "Get the state of one APK/JAR decompilation task "
                "(uploaded/analyzing/completed/error, progress, filename, "
                "error). Cheap: safe to poll."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "task_id returned by gms_rt_apk_analyze.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_manifest",
            "description": (
                "Show the parsed AndroidManifest.xml (package, "
                "permissions, activities) of a completed decompilation "
                "task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_search",
            "description": (
                "Search decompiled source files by filename substring "
                "(min 2 chars, max 50 results). Returns paths consumable "
                "by gms_rt_apk_source with view=true."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "query": {
                        "type": "string",
                        "description": "Filename substring, e.g. Permission.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["task_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_source",
            "description": (
                "Browse the decompiled source tree (view=false, default) "
                "or print one file's content (view=true). Without path, "
                "lists the sources root."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the decompiled sources.",
                    },
                    "view": {
                        "type": "boolean",
                        "description": "Print file content instead of the listing.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_issue_fetch",
            "description": (
                "Create or refresh a FULL evidence snapshot of a Redmine "
                "issue (raw JSON, untruncated journals, attachments with "
                "SHA-256). Read-only against Redmine. Returns snapshot_id; "
                "with wait=true polls every ~3s until ready/partial/failed "
                "(bounded by max_wait, default 300s)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "issue": {
                        "type": "string",
                        "description": "Numeric issue id or /issues/<id> URL.",
                    },
                    "download": {
                        "type": "string",
                        "enum": ["none", "analyzable", "all"],
                        "description": "Attachment download policy (default all).",
                    },
                    "no_refresh": {
                        "type": "boolean",
                        "description": "Reuse a recent ready snapshot (cache_hit flag).",
                    },
                    "wait": {"type": "boolean"},
                    "max_wait": {"type": "integer", "minimum": 0, "maximum": 21600},
                },
                "required": ["issue"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_issue",
            "description": (
                "Show one evidence snapshot: status, completeness flags, "
                "journal/attachment counts, content SHA-256, and the "
                "description head."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["snapshot_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_journals",
            "description": (
                "Read COMPLETE journals (no 2,000-char truncation) with "
                "cursor pagination: limit<=100, next_cursor for the next "
                "page. Check data.total vs returned."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                    "cursor": {"type": "string", "minLength": 0, "maxLength": 512},
                },
                "required": ["snapshot_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_attachments",
            "description": (
                "List evidence artifacts with artifact_id, kind, size, "
                "SHA-256, and per-artifact status/error. Use "
                "gms_rt_redmine_artifact_read for text windows."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["snapshot_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_artifact_search",
            "description": (
                "Search a fixed-string query across the description, all "
                "journals, and downloaded artifact text. Returns evidence "
                "refs (journal_id/artifact_id + snippet)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "query": {"type": "string", "minLength": 0, "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["snapshot_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_artifact_read",
            "description": (
                "Read a text/log artifact by character window "
                "(offset+limit, max 262144 chars). Response carries "
                "total_chars and truncated."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_devices_screencap",
            "description": (
                "Capture one Android device screenshot and return it as "
                "MCP image content (base64 PNG) with device metadata. "
                "Read-only UI diagnosis; the device must be visible to the "
                "controller and not leased by another client."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                },
                "required": ["device"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_image",
            "description": (
                "Return one image artifact as MCP image content (base64) "
                "plus its metadata (sha256, size, scaled flag). Oversized "
                "images return an error with a download hint; originals "
                "are never silently cropped."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["artifact_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_analyze_attachment",
            "description": (
                "Import a Redmine .apk evidence artifact into the JADX "
                "analysis pipeline (owner-scoped, resource intensive). "
                "Returns task_id; poll with gms_rt_apk_status every ~5s."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "artifact_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                },
                "required": ["snapshot_id", "artifact_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_source_search",
            "description": (
                "Search decompiled source CONTENT (not filenames) for a "
                "fixed query. Returns path:line:column + snippet."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "query": {"type": "string", "minLength": 0, "maxLength": 256},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                    "path": {"type": "string", "description": "Path substring filter."},
                },
                "required": ["task_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_source_read",
            "description": (
                "Read a line window of one decompiled source file "
                "(task-relative path, offset+limit, max 4000 lines)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "path": {"type": "string", "minLength": 0, "maxLength": 512},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["task_id", "path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_sdk_sources",
            "description": (
                "List admin-configured SDK source providers and their "
                "default revisions. Sources and roots are server-side only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_sdk_search",
            "description": (
                "Search an SDK source pinned to a revision (branch, tag, "
                "or commit). Every match carries the resolved commit and a "
                "signed result_id for gms_rt_sdk_read."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "revision": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "query": {"type": "string", "minLength": 0, "maxLength": 256},
                    "path": {"type": "string", "description": "Path substring filter."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                },
                "required": ["source", "revision", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_sdk_read",
            "description": (
                "Read a commit-pinned SDK source window using the "
                "self-contained opaque result_id from gms_rt_sdk_search. "
                "source/path/commit are bound inside the token; the client "
                "never passes free-form paths. Returns the resolved commit "
                "and blob SHA-256 for traceable citations."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "result_id": {
                        "type": "string",
                        "minLength": 8,
                        "maxLength": 2048,
                    },
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10000000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 4000},
                },
                "required": ["result_id"],
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


def cluster_devices_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    worker_id = str(arguments.get("worker_id") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if worker_id:
        args.extend(["--worker", worker_id])
    if query:
        args.extend(["--query", query])
    return run_cli("gms-rt-cluster-devices", args)


def apk_resolve_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "query is required", True
    args.append(query)
    suite_types = str(arguments.get("suite_types") or "").strip()
    if suite_types:
        args.extend(["--types", suite_types])
    prefer = str(arguments.get("prefer") or "").strip()
    if prefer:
        args.extend(["--prefer", prefer])
    return run_cli("gms-rt-apk-resolve", args)


def apk_analyze_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    args: list[str] = []
    query = str(arguments.get("query") or "").strip()
    if not query:
        return "query is required", True
    args.append(query)
    suite_types = str(arguments.get("suite_types") or "").strip()
    if suite_types:
        args.extend(["--types", suite_types])
    prefer = str(arguments.get("prefer") or "").strip()
    if prefer:
        args.extend(["--prefer", prefer])
    if arguments.get("wait"):
        args.append("--wait")
        max_wait = arguments.get("max_wait")
        if max_wait:
            args.extend(["--max-wait", str(int(max_wait))])
    return run_cli("gms-rt-apk-analyze", args)


def _apk_task_id_argument(arguments: dict[str, Any]) -> str:
    return str(arguments.get("task_id") or "").strip()


def apk_status_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    return run_cli("gms-rt-apk-status", [task_id])


def apk_manifest_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    return run_cli("gms-rt-apk-manifest", [task_id])


def apk_search_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    query = str(arguments.get("query") or "").strip()
    if not task_id or not query:
        return "task_id and query are required", True
    args: list[str] = [task_id, query]
    limit = arguments.get("limit")
    if limit:
        args.extend(["--limit", str(max(1, min(int(limit), 50)))])
    return run_cli("gms-rt-apk-search", args)


def apk_source_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = _apk_task_id_argument(arguments)
    if not task_id:
        return "task_id is required", True
    args: list[str] = [task_id]
    path = str(arguments.get("path") or "").strip()
    if path:
        args.append(path)
    if arguments.get("view"):
        args.append("--view")
    return run_cli("gms-rt-apk-source", args)


class ToolContent:
    """Rich tool result: multiple MCP content items (text + image)."""

    def __init__(self, items: list[dict[str, Any]], is_error: bool = False):
        self.items = items
        self.is_error = is_error


# --------------------------------------------------------------------------
# Redmine evidence / APK / SDK typed tools (2026-09-08 plan §9-§12)
# --------------------------------------------------------------------------

def _int_arg(arguments: dict[str, Any], name: str, default: int, minimum: int, maximum: int) -> int:
    value = arguments.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def redmine_issue_fetch_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    issue = str(arguments.get("issue") or arguments.get("issue_id") or "").strip()
    if not issue:
        return "issue (id or URL) is required", True
    args: list[str] = [issue]
    download = str(arguments.get("download") or "").strip()
    if download:
        if download not in ("none", "analyzable", "all"):
            return "download must be none, analyzable, or all", True
        args.extend(["--download", download])
    if arguments.get("no_refresh"):
        args.append("--no-refresh")
    if arguments.get("wait"):
        args.append("--wait")
        args.extend(["--max-wait", str(_int_arg(arguments, "max_wait", 300, 5, 3600))])
    return run_cli("gms-rt-redmine-issue-fetch", args)


def redmine_issue_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    return run_cli("gms-rt-redmine-issue-show", [snapshot_id])


def redmine_journals_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    args: list[str] = [snapshot_id]
    limit = arguments.get("limit")
    if limit:
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 100))])
    cursor = str(arguments.get("cursor") or "").strip()
    if cursor:
        args.extend(["--cursor", cursor])
    return run_cli("gms-rt-redmine-journals", args)


def redmine_attachments_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    if not snapshot_id:
        return "snapshot_id is required", True
    return run_cli("gms-rt-redmine-attachments", [snapshot_id])


def redmine_artifact_search_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not snapshot_id or not query:
        return "snapshot_id and query are required", True
    args: list[str] = [snapshot_id, query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 200))])
    return run_cli("gms-rt-artifact-search", args)


def redmine_artifact_read_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return "artifact_id is required", True
    args: list[str] = [artifact_id]
    if arguments.get("offset") is not None:
        args.extend(["--offset", str(_int_arg(arguments, "offset", 0, 0, 10_000_000))])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 65536, 1, 262144))])
    return run_cli("gms-rt-artifact-read", args)


def devices_screencap_tool(arguments: dict[str, Any]) -> ToolContent:
    """Capture one device screenshot and return MCP image content.

    Read-only UI diagnosis for agents: replaces the manual
    screencap -> pull -> read-file loop. The device must be visible to the
    controller; a device currently leased by another client is rejected
    server-side with a conflict error.
    """
    device = str(arguments.get("device") or "").strip()
    if not device:
        return ToolContent(
            [{"type": "text", "text": "device (serial) is required"}], is_error=True
        )
    text, is_error = run_cli("gms-rt-devices-screencap", [device])
    if is_error:
        return ToolContent([{"type": "text", "text": text}], is_error=True)
    # The CLI envelope carries data.base64/mime_type; convert to image content.
    try:
        envelope = json.loads(text)
        data = envelope.get("data") or {}
        image_b64 = str(data.get("base64") or "")
        mime = str(data.get("mime_type") or "image/png")
    except (ValueError, AttributeError):
        return ToolContent([{"type": "text", "text": text}], is_error=False)
    if not image_b64:
        return ToolContent(
            [{"type": "text", "text": "controller returned no image payload"}],
            is_error=True,
        )
    meta = {"device_id": str(data.get("device_id") or device)}
    return ToolContent([
        {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
        {"type": "image", "mimeType": mime, "data": image_b64},
    ])


def redmine_image_tool(arguments: dict[str, Any]) -> ToolContent:
    """Fetch an evidence artifact image and return MCP image content.

    The Controller endpoint enforces size limits and returns base64 with the
    original artifact metadata; oversized images surface as tool errors with
    a download hint instead of being silently cropped (plan §10).
    """
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not artifact_id:
        return ToolContent(
            [{"type": "text", "text": "artifact_id is required"}], is_error=True
        )
    text, is_error = run_cli("gms-rt-redmine-artifact-image", [artifact_id])
    if is_error:
        return ToolContent([{"type": "text", "text": text}], is_error=True)
    # The CLI envelope carries data.base64/mime_type; convert to image content.
    try:
        envelope = json.loads(text)
        data = envelope.get("data") or {}
        image_b64 = str(data.get("base64") or "")
        mime = str(data.get("mime_type") or "image/png")
    except (ValueError, AttributeError):
        return ToolContent([{"type": "text", "text": text}], is_error=False)
    if not image_b64:
        return ToolContent(
            [{"type": "text", "text": "controller returned no image payload"}],
            is_error=True,
        )
    meta = {
        k: v for k, v in data.items()
        if k in ("artifact_id", "size_bytes", "sha256", "scaled", "derived_sha256")
    }
    return ToolContent([
        {"type": "text", "text": json.dumps(meta, ensure_ascii=False)},
        {"type": "image", "mimeType": mime, "data": image_b64},
    ])


def apk_analyze_attachment_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    snapshot_id = str(arguments.get("snapshot_id") or "").strip()
    artifact_id = str(arguments.get("artifact_id") or "").strip()
    if not snapshot_id or not artifact_id:
        return "snapshot_id and artifact_id are required", True
    return run_cli(
        "gms-rt-apk-analyze-attachment", [snapshot_id, artifact_id]
    )


def apk_source_search_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = str(arguments.get("task_id") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not task_id or not query:
        return "task_id and query are required", True
    args: list[str] = [task_id, query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 200))])
    path_filter = str(arguments.get("path") or "").strip()
    if path_filter:
        args.extend(["--path", path_filter])
    return run_cli("gms-rt-apk-source-search", args)


def apk_source_read_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    task_id = str(arguments.get("task_id") or "").strip()
    path = str(arguments.get("path") or "").strip()
    if not task_id or not path:
        return "task_id and path are required", True
    args: list[str] = [task_id, path]
    if arguments.get("offset") is not None:
        args.extend(["--offset", str(_int_arg(arguments, "offset", 0, 0, 10_000_000))])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 400, 1, 4000))])
    return run_cli("gms-rt-apk-source-read", args)


def sdk_sources_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    return run_cli("gms-rt-sdk-sources")


def sdk_search_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    source = str(arguments.get("source") or "").strip()
    revision = str(arguments.get("revision") or "").strip()
    query = str(arguments.get("query") or "").strip()
    if not source or not revision or not query:
        return "source, revision, and query are required", True
    args = ["--source", source, "--revision", revision, "--query", query]
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 50, 1, 200))])
    path_filter = str(arguments.get("path") or "").strip()
    if path_filter:
        args.extend(["--path", path_filter])
    return run_cli("gms-rt-sdk-search", args)


def sdk_read_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    result_id = str(arguments.get("result_id") or "").strip()
    if not result_id:
        return "missing required field: result_id", True
    # 计划 §12：只传 result_id；source/path/commit 已绑定在 token 内。
    args: list[str] = [result_id]
    if arguments.get("offset") is not None:
        args.extend(["--offset", str(_int_arg(arguments, "offset", 0, 0, 10_000_000))])
    if arguments.get("limit"):
        args.extend(["--limit", str(_int_arg(arguments, "limit", 400, 1, 4000))])
    return run_cli("gms-rt-sdk-read", args)


_TOOL_HANDLERS = {
    "gms_rt_context": context_tool,
    "gms_rt_run": run_tool,
    "gms_rt_commands": commands_tool,
    "gms_rt_describe": describe_tool,
    "gms_rt_devices": devices_tool,
    "gms_rt_device_console": device_console_tool,
    "gms_rt_device_info": device_info_tool,
    "gms_rt_device_wait": device_wait_tool,
    "gms_rt_cluster_workers": lambda args: run_cli("gms-rt-cluster-workers"),
    "gms_rt_cluster_devices": cluster_devices_tool,
    "gms_rt_auth_status": auth_status_tool,
    "gms_rt_auth_login": auth_login_tool,
    "gms_rt_auth_elevate": auth_elevate_tool,
    "gms_rt_agent_enroll": agent_enroll_tool,
    "gms_rt_approval_create": approval_create_tool,
    "gms_rt_burn_firmware": burn_firmware_tool,
    "gms_rt_burn_status": burn_status_tool,
    "gms_rt_test_start": test_start_tool,
    "gms_rt_jobs_list": jobs_list_tool,
    "gms_rt_jobs_status": jobs_status_tool,
    "gms_rt_jobs_wait": jobs_wait_tool,
    "gms_rt_jobs_cancel": jobs_cancel_tool,
    "gms_rt_jobs_events": jobs_events_tool,
    "gms_rt_jobs_follow": jobs_follow_tool,
    "gms_rt_test_suites_list": test_suites_list_tool,
    "gms_rt_devices_ui_dump": devices_ui_dump_tool,
    "gms_rt_devices_snapshot": devices_snapshot_tool,
    "gms_rt_reports_list": reports_tool,
    "gms_rt_apk_resolve": apk_resolve_tool,
    "gms_rt_apk_analyze": apk_analyze_tool,
    "gms_rt_apk_status": apk_status_tool,
    "gms_rt_apk_manifest": apk_manifest_tool,
    "gms_rt_apk_search": apk_search_tool,
    "gms_rt_apk_source": apk_source_tool,
    "gms_rt_redmine_issue_fetch": redmine_issue_fetch_tool,
    "gms_rt_redmine_issue": redmine_issue_tool,
    "gms_rt_redmine_journals": redmine_journals_tool,
    "gms_rt_redmine_attachments": redmine_attachments_tool,
    "gms_rt_redmine_artifact_search": redmine_artifact_search_tool,
    "gms_rt_redmine_artifact_read": redmine_artifact_read_tool,
    "gms_rt_redmine_image": redmine_image_tool,
    "gms_rt_devices_screencap": devices_screencap_tool,
    "gms_rt_apk_analyze_attachment": apk_analyze_attachment_tool,
    "gms_rt_apk_source_search": apk_source_search_tool,
    "gms_rt_apk_source_read": apk_source_read_tool,
    "gms_rt_sdk_sources": sdk_sources_tool,
    "gms_rt_sdk_search": sdk_search_tool,
    "gms_rt_sdk_read": sdk_read_tool,
    "gms_rt_shell": shell_tool,
    "gms_rt_logcat": logcat_tool,
    "gms_rt_shell_exec": shell_exec_tool,
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
        registered_names = {tool["name"] for tool in tools()}
        handler = _TOOL_HANDLERS.get(name) if name in registered_names else None
        if handler is None:
            response(
                request_id,
                error={"code": -32601, "message": f"Unknown tool: {name}"},
            )
            return
        try:
            result = handler(arguments)
        except Exception as error:  # MCP boundary: convert failures to tool errors.
            text = f"gms-rt tool failed: {error}"
            is_error = True
        else:
            # ToolContent-returning tools (device screencap, evidence images)
            # come back as rich content; tuple returns stay the text path.
            if isinstance(result, ToolContent):
                response(
                    request_id,
                    {
                        "content": result.items,
                        "isError": result.is_error,
                    },
                )
                return
            text, is_error = result
        result_payload: dict[str, Any] = {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }
        # Keep text for every MCP client, and additionally expose valid JSON
        # as structured content so capable clients do not need to parse it.
        try:
            structured = json.loads(text)
        except (TypeError, ValueError):
            structured = None
        if isinstance(structured, dict):
            result_payload["structuredContent"] = structured
        response(request_id, result_payload)
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
