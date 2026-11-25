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

Authentication model (ADR 0006):
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

The typed tool catalog is declared in mcp_tool_schemas.py. Device shell and
logcat policy lives in mcp_device_shell_tools.py; evidence, APK, SDK and
image adapters live in mcp_evidence_tools.py.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import importlib.util
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


def _load_tool_schemas():
    """Load sibling schema declarations in both script and dynamic-load modes."""
    try:
        import mcp_tool_schemas

        return mcp_tool_schemas
    except ModuleNotFoundError as exc:
        if exc.name != "mcp_tool_schemas":
            raise
    spec = importlib.util.spec_from_file_location(
        "mcp_tool_schemas", Path(__file__).with_name("mcp_tool_schemas.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load MCP tool schemas")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mcp_tool_schemas = _load_tool_schemas()


def _load_typed_tools(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name + ".py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load typed tools: " + name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcp_deployment_tools = _load_typed_tools("mcp_deployment_tools")
mcp_repair_policy = _load_typed_tools("mcp_repair_policy")
mcp_debug_tools = _load_typed_tools("mcp_debug_tools")
mcp_device_shell_tools = _load_typed_tools("mcp_device_shell_tools")
mcp_evidence_tools = _load_typed_tools("mcp_evidence_tools")
version_guard = _load_typed_tools("version_guard")


def _follow_current_version() -> None:
    """Re-exec into the version ``current`` points at, in an idle gap.

    Daemon-held resident servers (singleton spawned once, pipes kept
    open across client sessions) outlive package upgrades and would
    serve stale code forever. Checking before each ``readline`` means
    no request payload is ever consumed across the exec boundary; the
    daemon's pipes and the PID are preserved, so the very next request
    runs the freshly-followed image. Repo checkouts without a
    versions/ + current layout are never touched.
    """
    info = version_guard.staleness(__file__)
    if info:
        version_guard.follow(info, argv=sys.argv)


SERVER_NAME = "gms-remote-test"
SERVER_VERSION = "0.22.96"
# Long enough for gms-rt-jobs-wait --max-wait and firmware uploads.
DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
MAX_OUTPUT_BYTES = 1024 * 1024
# The catalog only changes across gms-rt-system-update; refresh it lazily.
SAFETY_CACHE_TTL_SECONDS = 300

# Agent authentication mode (ADR 0006). When set to
# "service-token" (the installer writes it into every agent MCP env), the
# password-based tools (gms_rt_auth_login, gms_rt_auth_elevate) and the
# human-session approval mint (gms_rt_approval_create) are NOT registered:
# an agent context then cannot even express a password login or a
# self-minted approval. Agents enroll once via gms_rt_agent_enroll and
# authenticate with GMS_AUTH_TOKEN_FILE.
#
# Two independent signals put the server into
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

# Token-file freshness: the launcher freezes
# GMS_AUTH_TOKEN_FILE at MCP startup from the profile TOML. When enroll (or
# an operator) rewrites the profile's token_file — e.g. the code was
# enrolled under a different profile name — this process keeps pointing at
# the stale path while fresh CLI invocations resolve the new one, so MCP
# and CLI disagree about authentication. Re-resolve the selected profile's
# token_file before each CLI call, cached by (profile name, TOML content
# fingerprint): mtime/size are too coarse — a same-second rewrite to an
# equally long token_file (old.token -> new.token) keeps both identical, so
# a stat-based stamp would serve the stale path forever. Hashing the bytes
# makes the refresh exactly as reliable as the file itself; still exactly
# one profile per server, no routing change.
_TOKEN_FILE_CACHE: dict[str, tuple[str, str]] = {}


def _current_profile_token_file() -> str:
    """token_file from the selected profile TOML, "" when unavailable."""

    profile = str(os.environ.get("GMS_RT_PROFILE", "")).strip()
    if not profile or profile == "default":
        return ""
    try:
        from gms_agent import profile_store

        if not profile_store.validate_profile_name(profile):
            return ""
        path = profile_store.profile_path(profile)
        raw = path.read_bytes() if path.is_file() else b""
        stamp = hashlib.sha256(raw).hexdigest()
    except Exception:
        return ""
    cached = _TOKEN_FILE_CACHE.get(profile)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        token_file = str(profile_store.load_profile(profile).get("token_file", ""))
    except Exception:
        token_file = ""
    _TOKEN_FILE_CACHE[profile] = (stamp, token_file)
    return token_file


def _fresh_token_file_env() -> dict[str, str]:
    """Env overrides that keep GMS_AUTH_TOKEN_FILE in sync with the profile."""

    resolved = _current_profile_token_file()
    if resolved:
        return {"GMS_AUTH_TOKEN_FILE": resolved}
    return {}

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
# Phase 2: direct-HTTP fast path through the gms_agent SDK.
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
    # Boolean flags injected by the dispatcher never consume a positional.
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

    # The shell command exposes --worker and performs --query filtering
    # client-side.  The HTTP inventory route accepts worker_id only; retain
    # the shell implementation whenever query semantics are requested.
    if normalize_command(command) == "gms-rt-cluster-devices":
        if "query" in params:
            return None
        if "worker" in params:
            params["worker_id"] = params.pop("worker")

    try:
        if _sdk_client is None:
            from gms_agent import GmsClient

            _sdk_client = GmsClient()
        # Profile TOML moved the token file: follow it, then
        # request() re-reads the file contents itself.
        fresh_token = _fresh_token_file_env().get("GMS_AUTH_TOKEN_FILE")
        if fresh_token and fresh_token != _sdk_client.token_path:
            _sdk_client.refresh_token(fresh_token)
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


def _bash_executable() -> str:
    configured = os.environ.get("SHELL", "")
    if (
        Path(configured).name.lower() in {"bash", "bash.exe"}
        and Path(configured).is_file()
    ):
        return configured
    return "bash"


def build_argv(command: str, args: list[str] | str | None) -> list[str]:
    argv = [_bash_executable(), cli_script().as_posix(), normalize_command(command)]
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
    merged_env_extra: dict[str, str] = {}
    if env_extra:
        merged_env_extra.update(env_extra)
    # Token-file freshness: explicit env_extra wins over the profile
    # re-resolution so typed tools keep their explicit overrides.
    merged_env_extra.update(_fresh_token_file_env())
    if merged_env_extra:
        child_env = {**os.environ, **merged_env_extra}
    try:
        completed = subprocess.run(
            argv,
            cwd=os.getcwd(),
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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

    # Parse the complete JSON envelope; bound raw text only when parsing fails.
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
        # the returned value must stay valid JSON.
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
    device = str(arguments.get("device") or "").strip()
    worker_id = str(arguments.get("worker_id") or "").strip()
    if port_key and device:
        return "port_key and device are mutually exclusive", True
    if worker_id and not device:
        return "worker_id requires device", True
    if device and (arguments.get("tail") is not None or arguments.get("date")):
        return "tail/date require port_key; assess device first, then read the selected port", True
    if port_key:
        args.append(port_key)
    if device:
        args.extend(["--device", device])
    if worker_id:
        args.extend(["--worker", worker_id])
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
            return "invalid interval: expected an integer from 1 to 60", True
        if not 1 <= interval <= 60:
            return "invalid interval: expected an integer from 1 to 60", True
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


def _enroll_state_dir() -> Path:
    """Token state dir, mirroring gms_agent.profile_store.STATE_DIR."""
    return Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    ) / "gms-remote-test"


def agent_enroll_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Exchange a one-shot enrollment code for a 0600 agent token file."""
    code = str(arguments.get("code") or "").strip()
    if not code:
        return "Missing required argument: code (one-shot enrollment code)", True
    args: list[str] = [code]
    out_file = str(arguments.get("out_file") or "").strip()
    if out_file:
        # The MCP server is an unattended agent surface: a caller-chosen
        # --out path would let a client-side prompt steer the freshly
        # minted service token anywhere on the host. Restrict the target
        # to the agent state dir (resolve() folds '..' and symlinks, so
        # lexical escapes cannot slip through). The interactive human CLI
        # keeps the unrestricted --out.
        state_dir = _enroll_state_dir().resolve()
        try:
            candidate = Path(out_file).expanduser().resolve()
            candidate.relative_to(state_dir)
        except (ValueError, OSError):
            return (
                "denied: out_file must stay inside the agent token state "
                f"dir ({state_dir}); unattended enrollment cannot write "
                "elsewhere. Omit out_file to use the profile's default "
                "token path.",
                True,
            )
        args.extend(["--out", out_file])
    profile = str(arguments.get("profile") or "").strip()
    if profile:
        args.extend(["--profile", profile])
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
    # burn 审批必须绑定固件 SHA256（服务端据此派生
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
    """Authoritative worker_id resolution for a device (ADR 0006).

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
    # Read worker_id from the envelope data, or from non-envelope output.
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

    异步模型（Kimi 等客户端单次 tool call 默认 60s）：
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
    args = [firmware_path, device, wipe_str, *extra]
    # Approval token 经 env 传入 CLI（GMS_RT_BURN_APPROVAL_TOKEN），绝不放
    # 进子进程 argv：wait=false 的烧录进程 detached 存活整个拷贝/烧写窗口，
    # /proc/<pid>/cmdline 对同机用户可读。CLI 侧 firmware.sh 优先读 env。
    env_extra = {"GMS_RT_BURN_APPROVAL_TOKEN": approval_token}
    if arguments.get("wait") is True:
        timeout = 1800
        if arguments.get("timeout") is not None:
            try:
                timeout = max(1, int(arguments["timeout"]))
            except (TypeError, ValueError):
                return "timeout must be an integer (seconds)", True
        return run_cli("gms-rt-burn-firmware", args, timeout=timeout, env_extra=env_extra)
    # 异步：后台启动 CLI，立即返回 operation_id；结果落盘供 burn_status 读取。
    return start_burn_operation("gms-rt-burn-firmware", args, env_extra=env_extra)


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


def start_burn_operation(
    command: str, args: list[str], env_extra: dict[str, str] | None = None
) -> tuple[str, bool]:
    """Launch a long CLI op detached; return an operation id immediately.

    The CLI runs detached from this MCP subprocess (setsid, own process
    group) so a 60s client-side tool timeout can never kill a burn. Its
    stdout/stderr land in a spool file the status tool parses for the final
    JSON envelope. ``env_extra`` carries one-shot secrets (approval tokens)
    so they never appear in the detached child's argv.
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
        child_env = {**os.environ, **env_extra} if env_extra else None
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
                env=child_env,
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
    # worker_id (ADR 0006): resolve the owning worker authoritatively when
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
    """One-call status+events+failure-summary follow."""
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
    """Discover suite names for gms_rt_test_start."""
    return run_cli("gms-rt-test-suites", [])


def devices_ui_dump_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """Structured UI layout tree (uiautomator dump path)."""
    device = str(arguments.get("device") or "").strip()
    if not device:
        return "device (serial) is required", True
    return run_cli("gms-rt-devices-ui-dump", [device])


def devices_snapshot_tool(arguments: dict[str, Any]) -> tuple[str, bool]:
    """One-shot device state snapshot."""
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
    # Security boundary: the generic runner only
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
    # A profile owns both the Controller origin and its service token.  The
    # generic read-only runner must never let prompt/tool input retarget that
    # token (nor relax the profile's TLS policy).  Typed tools do not accept
    # these transport switches in the first place.
    if args is None:
        args = []
    args_error = ('args must be an array of strings, e.g. '
                  '{"command":"devices-info","args": ["DEVICE"]}; do not pass a shell string')
    # Older clients saw an untyped schema and may stringify the entire array.
    # Decode JSON once, then apply the same shape and transport checks. Never
    # split a shell command or recursively decode strings into arguments.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            return args_error, True
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        return args_error, True
    transport_switches = (
        "--server", "--ca-cert", "--insecure", "--profile",
        "--token", "--password-stdin",
    )
    for item in args:
        if item in transport_switches or any(item.startswith(f"{name}=") for name in transport_switches):
            return "denied: MCP cannot override profile, authentication, server, or TLS settings", True
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


# Per-process call budget for an approved repair: a target file may be
# searched once and read once through a signed result_id.
_REPAIR_ISSUED_SEARCHES: set[str] = set()
_REPAIR_ISSUED_READS: set[str] = set()


def tools() -> list[dict[str, Any]]:
    all_tools = _all_tools()
    if _SERVICE_TOKEN_MODE:
        # Service-token mode: the human-session credential tools are not even
        # advertised, so an agent context cannot express a password login or
        # self-mint an approval (ADR 0006).
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
    # Claim review may run under a Controller-set repair policy: only the
    # approved source tools stay visible (pre-dispatch fence, not advice).
    policy = mcp_repair_policy.current_policy()
    all_tools = [
        tool for tool in all_tools
        if mcp_repair_policy.tool_visible(tool["name"], policy)
    ]
    return [_decorate_tool(tool) for tool in all_tools]


_TOOLSETS = {
    "gms_rt_apk_deploy": {"admin"},
    "gms_rt_apk_deployment_status": {"core", "admin"},
    "gms_rt_debug_session_request": {"admin"},
    "gms_rt_debug_session_status": {"core", "admin"},
    "gms_rt_debug_session_revoke": {"admin"},
    "gms_rt_debug_action": {"admin"},
    "gms_rt_devices_db_query": {"device_evidence"},
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
            "gms_rt_redmine_triage",
            "gms_rt_redmine_history_search",
            "gms_rt_redmine_references",
            "gms_rt_redmine_journals",
            "gms_rt_redmine_attachments",
            "gms_rt_redmine_attachment_download",
            "gms_rt_redmine_artifact_failures",
            "gms_rt_redmine_artifact_search",
            "gms_rt_redmine_artifact_search_batch",
            "gms_rt_redmine_artifact_read",
            "gms_rt_redmine_image",
            "gms_rt_apk_analyze_attachment",
            "gms_rt_apk_source_search",
            "gms_rt_apk_source_read",
            "gms_rt_sdk_sources",
            "gms_rt_sdk_search",
            "gms_rt_sdk_read",
            "gms_rt_knowledge_search",
        )
    },
    # 只读设备实证工具集：晨报分析器绑定 device_serial 时与 evidence 一并
    # 放行（daily_brief_config.analyzer_env_extra）。全部是只读诊断：
    # 截图/UI 树/快照/logcat dump/只读 shell 白名单/设备信息与等待。
    # gms_rt_shell_exec（需用户 approval token）不放入本集合；
    # gms_rt_device_console 只读保留日志，属于设备证据。
    **{
        name: {"device_evidence"}
        for name in (
            "gms_rt_devices_screencap",
            "gms_rt_devices_snapshot",
            "gms_rt_devices_ui_dump",
            "gms_rt_shell",
            "gms_rt_device_info",
            "gms_rt_device_wait",
            "gms_rt_logcat",
            "gms_rt_device_console",
        )
    },
}


def _decorate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Add MCP safety hints without changing existing input contracts.

    Safety metadata comes from the CLI CommandSpec catalog
    (gms-rt-system-commands → gms-rt-system-command-describe, the single
    source of command metadata). The static sets below only
    cover the small number of MCP tools with no CLI counterpart
    (approval/enroll/password flows); anything bound to a gms-rt-*
    command derives its hints from the catalog so a new command cannot
    drift from its declared mode/agent-safety.
    """

    result = dict(tool)
    name = str(result.get("name", ""))
    suffix = name[len("gms_rt_"):] if name.startswith("gms_rt_") else name
    cli_command = "gms-rt-" + suffix.replace("_", "-")
    descriptor = _command_safety(cli_command)
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
        "gms_rt_apk_deploy",
        "gms_rt_debug_session_request",
        "gms_rt_debug_session_revoke",
        "gms_rt_debug_action",
    }
    destructive = name in {"gms_rt_burn_firmware", "gms_rt_shell_exec"}
    if descriptor is not None:
        # Catalog-derived: mutating iff the CLI declares the command
        # mutating or requiring explicit authorization/elevation.
        mutating = mutating or bool(
            descriptor.get("mode") == "mutating"
            or descriptor.get("requires_explicit_authorization")
            or descriptor.get("requires_elevation")
        )
        destructive = destructive or (
            descriptor.get("external_side_effects") is True
            and descriptor.get("resource_intensive") is True
        )
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
    return [dict(tool) for tool in mcp_tool_schemas.ALL_TOOLS + mcp_deployment_tools.SCHEMAS + mcp_debug_tools.SCHEMAS]




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


def _call_cli(*args, **kwargs):
    return run_cli(*args, **kwargs)


ToolContent = mcp_evidence_tools.ToolContent
_validate_shell_command = mcp_device_shell_tools._validate_shell_command
shell_tool = functools.partial(mcp_device_shell_tools.shell_tool, _call_cli)
logcat_tool = functools.partial(mcp_device_shell_tools.logcat_tool, _call_cli)
shell_exec_tool = functools.partial(mcp_device_shell_tools.shell_exec_tool, _call_cli)
apk_resolve_tool = functools.partial(mcp_evidence_tools.apk_resolve_tool, _call_cli)
apk_analyze_tool = functools.partial(mcp_evidence_tools.apk_analyze_tool, _call_cli)
apk_status_tool = functools.partial(mcp_evidence_tools.apk_status_tool, _call_cli)
apk_manifest_tool = functools.partial(mcp_evidence_tools.apk_manifest_tool, _call_cli)
apk_search_tool = functools.partial(mcp_evidence_tools.apk_search_tool, _call_cli)
apk_source_tool = functools.partial(mcp_evidence_tools.apk_source_tool, _call_cli)
redmine_issue_fetch_tool = functools.partial(mcp_evidence_tools.redmine_issue_fetch_tool, _call_cli)
redmine_issue_tool = functools.partial(mcp_evidence_tools.redmine_issue_tool, _call_cli)
redmine_triage_tool = functools.partial(mcp_evidence_tools.redmine_triage_tool, _call_cli)
redmine_references_tool = functools.partial(mcp_evidence_tools.redmine_references_tool, _call_cli)
redmine_history_search_tool = functools.partial(mcp_evidence_tools.redmine_history_search_tool, _call_cli)
redmine_journals_tool = functools.partial(mcp_evidence_tools.redmine_journals_tool, _call_cli)
redmine_attachments_tool = functools.partial(mcp_evidence_tools.redmine_attachments_tool, _call_cli)
redmine_artifact_failures_tool = functools.partial(mcp_evidence_tools.redmine_artifact_failures_tool, _call_cli)
redmine_attachment_download_tool = functools.partial(mcp_evidence_tools.redmine_attachment_download_tool, _call_cli)
redmine_artifact_search_tool = functools.partial(mcp_evidence_tools.redmine_artifact_search_tool, _call_cli)
redmine_artifact_search_batch_tool = functools.partial(mcp_evidence_tools.redmine_artifact_search_batch_tool, _call_cli)
redmine_artifact_read_tool = functools.partial(mcp_evidence_tools.redmine_artifact_read_tool, _call_cli)
devices_screencap_tool = functools.partial(mcp_evidence_tools.devices_screencap_tool, _call_cli)
redmine_image_tool = functools.partial(mcp_evidence_tools.redmine_image_tool, _call_cli)
apk_analyze_attachment_tool = functools.partial(mcp_evidence_tools.apk_analyze_attachment_tool, _call_cli)
apk_source_search_tool = functools.partial(mcp_evidence_tools.apk_source_search_tool, _call_cli)
apk_source_read_tool = functools.partial(mcp_evidence_tools.apk_source_read_tool, _call_cli)
sdk_sources_tool = functools.partial(mcp_evidence_tools.sdk_sources_tool, _call_cli)
sdk_search_tool = functools.partial(mcp_evidence_tools.sdk_search_tool, _call_cli)
sdk_read_tool = functools.partial(mcp_evidence_tools.sdk_read_tool, _call_cli)
knowledge_search_tool = functools.partial(mcp_evidence_tools.knowledge_search_tool, _call_cli)


_TOOL_HANDLERS = {
    **mcp_deployment_tools.handlers(run_cli),
    **mcp_debug_tools.handlers(run_cli),
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
    "gms_rt_redmine_triage": redmine_triage_tool,
    "gms_rt_redmine_history_search": redmine_history_search_tool,
    "gms_rt_redmine_references": redmine_references_tool,
    "gms_rt_redmine_journals": redmine_journals_tool,
    "gms_rt_redmine_attachments": redmine_attachments_tool,
    "gms_rt_redmine_attachment_download": redmine_attachment_download_tool,
    "gms_rt_redmine_artifact_failures": redmine_artifact_failures_tool,
    "gms_rt_redmine_artifact_search": redmine_artifact_search_tool,
    "gms_rt_redmine_artifact_search_batch": redmine_artifact_search_batch_tool,
    "gms_rt_redmine_artifact_read": redmine_artifact_read_tool,
    "gms_rt_redmine_image": redmine_image_tool,
    "gms_rt_devices_screencap": devices_screencap_tool,
    "gms_rt_apk_analyze_attachment": apk_analyze_attachment_tool,
    "gms_rt_apk_source_search": apk_source_search_tool,
    "gms_rt_apk_source_read": apk_source_read_tool,
    "gms_rt_sdk_sources": sdk_sources_tool,
    "gms_rt_sdk_search": sdk_search_tool,
    "gms_rt_sdk_read": sdk_read_tool,
    "gms_rt_knowledge_search": knowledge_search_tool,
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
        repair_policy = mcp_repair_policy.current_policy()
        violation = mcp_repair_policy.authorize(
            name, arguments, repair_policy,
            _REPAIR_ISSUED_SEARCHES, _REPAIR_ISSUED_READS,
        )
        if violation:
            response(request_id, {
                "content": [{"type": "text", "text": violation}],
                "isError": True,
            })
            return
        try:
            if repair_policy is not None and name == "gms_rt_sdk_search":
                result = sdk_search_tool(arguments, exact_path=True)
            else:
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
    while True:
        # Follow an upgraded install BEFORE reading a line: exec in the
        # idle gap so no buffered request is lost across the exec.
        _follow_current_version()
        line = sys.stdin.readline()
        if not line:
            break
        message = None
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                handle(message)
        except Exception as error:
            print(f"gms-remote-test MCP protocol error: {error}", file=sys.stderr)
            # Registry/schema/dispatch failures happen outside the tool's
            # exception boundary too. Always answer requests with an id so
            # clients receive the failure instead of waiting for a timeout.
            if isinstance(message, dict) and message.get("id") is not None:
                response(message["id"], error={
                    "code": -32603,
                    "message": "GMS MCP request failed; reconnect the GMS MCP server or run gms-agent doctor --json",
                })


if __name__ == "__main__":
    main()
