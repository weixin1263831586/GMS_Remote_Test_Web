"""Device shell and logcat safety policy and typed tool execution."""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Read-only device shell (typed tool)
# ---------------------------------------------------------------------------

# The Controller catalog marks gms-rt-devices-shell "manual" because arbitrary
# shell access is interactive by definition. This typed tool exposes a strictly
# read-only subset so agents can diagnose devices (props, services, logs,
# filesystem state) without weakening the catalog security boundary.
_SHELL_READONLY_BINARIES = frozenset({
    "cat", "df", "dumpsys", "getprop", "logcat", "ls", "pidof", "ps",
    "settings", "stat", "uptime", "vmstat", "wm",
    # Read-only diagnostics: process/pattern lookup, log post-processing on files
    # already readable via cat, kernel ring buffer, and device_config reads.
    "pgrep", "grep", "wc", "head", "tail", "dmesg", "id", "printenv",
    "device_config", "cmd", "am",
    # high-frequency read-only diagnostics. Only the
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
    # battery/simulation setters mutate device state:
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
# these are the high-frequency diagnostics agents were
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
# The only binaries allowed on the RIGHT side of the
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


_LOGCAT_DUMP_FLAGS = frozenset({"-d", "-t", "-g", "-p", "-L", "-S", "--last", "--statistics"})


def _validate_logcat_arguments(args: list[str]) -> tuple[bool, str]:
    short_options = "dvtTgbsemnrQpLDBS"
    long_options = {
        "dividers", "buffer", "format", "tag", "uid", "pid",
        "last", "print", "statistics", "help",
    }
    short_values = "vbtTemnr"
    long_values = {"buffer", "format", "tag", "uid", "pid"}
    dump = False
    expect_value = False
    for arg in args:
        if expect_value:
            if arg.startswith("-"):
                return False, "logcat option requires a value"
            expect_value = False
            continue
        if arg.startswith("--"):
            name, separator, _ = arg[2:].partition("=")
            if name not in long_options:
                return False, f"logcat option '--{name}' is outside the read-only allowlist"
            expect_value = name in long_values and not separator
            dump |= f"--{name}" in _LOGCAT_DUMP_FLAGS
        elif arg.startswith("-") and arg != "-":
            for index, flag in enumerate(arg[1:], 2):
                if flag not in short_options:
                    return False, f"logcat option '-{flag}' is outside the read-only allowlist"
                dump |= f"-{flag}" in _LOGCAT_DUMP_FLAGS
                if flag in short_values:
                    expect_value = index == len(arg)
                    break
    if expect_value:
        return False, "logcat option requires a value"
    if not dump and args != ["--help"]:
        return False, "streaming logcat is not allowed; add -d or -t (dump mode)"
    return True, ""


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
        return _validate_logcat_arguments(rest)
    elif binary == "dmesg":
        # dmesg -c (and -C, including inside clusters like -tc) clear
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

    The gate is STRUCTURED and POSITIVE per binary; every option token
    must match an explicit per-binary allowlist. The gate allows ONE
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


def shell_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    global _DEVICE_ID_PATTERN
    if arguments.get("worker_id"):
        return "worker_id is unsupported by this host-local shell command; no device command was run", True
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
    # gms-rt-shell is the typed read-only surface — the CLI's
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
# GTS/VTS incident data — so it is human-only via the CLI since v0.13.0:
# the tool denies clear=true and any -c/--clear form in args.
_LOGCAT_FORBIDDEN_CHARS = frozenset(";|&><`(){}[]$\\'\"\n\r")
_LOGCAT_MAX_ARGS = 16


def _is_clear_request(arguments: dict[str, Any]) -> bool:
    value = arguments.get("clear")
    return value is True or (
        isinstance(value, str) and value.strip().lower() == "true"
    )


def logcat_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    if arguments.get("worker_id"):
        return "worker_id is unsupported by this host-local logcat command; no device command was run", True
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
    # 专用 logcat 工具与 gms_rt_shell 的 logcat 分支共用同一套解析
    # 与正向允许策略。组合短选项（-dc）、长选项缩写（--cle）和附着值
    # （-df/tmp/x）都会被拦截。清空 logcat 缓冲会销毁
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
        if any(ch in _LOGCAT_FORBIDDEN_CHARS for ch in item):
            return (
                "denied: logcat arguments must not contain shell "
                "metacharacters or quotes",
                True,
            )

    # Native time-window support: 'since' maps to logcat -t
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
            until_text = (text + ".999") if "." not in text else text.ljust(18, "0")
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
    if not any(flag in _LOGCAT_DUMP_FLAGS for flag in items):
        items = ["-d", *items]
    allowed, reason = _validate_logcat_arguments(items)
    if not allowed:
        return f"denied: {reason}", True
    timeout = 180
    if arguments.get("timeout") is not None:
        try:
            timeout = min(600, max(1, int(arguments["timeout"])))
        except (TypeError, ValueError):
            return "timeout must be an integer (seconds)", True
    # dump-mode logcat is a typed read-only surface; the CLI
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
            if stamp > until_text:
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
# against tool+device+SHA256(command), TTL and single use (ADR 0006).
# A client-declared authorized=true boolean was never a security
# boundary — any MCP client could pass true itself. Read-only diagnosis
# should still go through gms_rt_shell (allowlist, no approval needed).
_SHELL_EXEC_MAX_COMMAND_CHARS = 2000


def shell_exec_tool(run_cli, arguments: dict[str, Any]) -> tuple[str, bool]:
    global _DEVICE_ID_PATTERN
    if arguments.get("worker_id"):
        return "worker_id is unsupported by this host-local shell command; no device command was run", True
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
    # the command (gms-rt-devices-shell). Token travels via env, never argv:
    # the CLI process may outlive this tool call and /proc/<pid>/cmdline is
    # readable by same-host users (devices.sh reads GMS_RT_APPROVAL_TOKEN).
    return run_cli(
        "gms-rt-devices-shell",
        [device, command],
        timeout=timeout,
        env_extra={"GMS_RT_APPROVAL_TOKEN": approval_token},
    )
