"""Typed-tool JSON-RPC declarations (name / description / inputSchema).

Extracted from mcp_server.py after the adapter file carried
~1400 lines of static schema literals). mcp_server imports ALL_TOOLS and
applies service-token/toolset filtering plus catalog-derived annotations;
the declarations themselves are pure data.

This module is part of the plugin payload — edit under
agent/gms-remote-test/runtime/ and re-run tools/scripts/agent/sync_package.py.
"""

from __future__ import annotations

from typing import Any


ALL_TOOLS: list[dict[str, Any]] = [
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
                    "profile": {
                        "type": "string",
                        "description": (
                            "Optional profile name. On hosts with multiple "
                            "registered profiles the token file must match "
                            "the caller's MCP registration; pass the profile "
                            "name so enroll resolves its token_file path."
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
                "One-call job follow: current status + events "
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
                "One-shot device state snapshot: build "
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
                "APK/JAR artifact in the latest CTS/CTS-V/VTS/GTS/STS suites. "
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
                            "(default cts,cts-v,vts,gts,sts)."
                        ),
                    },
                    "prefer": {
                        "type": "string",
                        "description": "Preferred artifact type: apk (default) or jar.",
                    },
                    "suite_path": {
                        "type": "string",
                        "description": (
                            "Explicit suite root on the local suites host; "
                            "scans only that suite instead of the latest "
                            "per type."
                        ),
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
                            "(default cts,cts-v,vts,gts,sts)."
                        ),
                    },
                    "prefer": {
                        "type": "string",
                        "description": "Preferred artifact type: apk (default) or jar.",
                    },
                    "suite_path": {
                        "type": "string",
                        "description": (
                            "Explicit suite root on the local suites host; "
                            "scans only that suite instead of the latest "
                            "per type."
                        ),
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
                "task, or only its declared permissions with "
                "permissions=true. CLI: gms-rt-apk-manifest [--permissions]."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "permissions": {
                        "type": "boolean",
                        "description": "List only declared permissions.",
                    },
                },
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_search",
            "description": (
                "Search decompiled sources by filename substring (mode=name, "
                "default), file content (mode=content, path:line:column + "
                "snippet), or Java symbol definition (mode=symbol). CLI: "
                "gms-rt-apk-search <task_id> <query> [--mode ...]."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "query": {
                        "type": "string",
                        "description": "Filename substring (name), fixed content query, or symbol name.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["name", "content", "symbol"],
                        "description": "Search dimension (default name).",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 400},
                    "path": {
                        "type": "string",
                        "description": "Path substring filter (content/symbol modes only).",
                    },
                    "line": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Line hint for symbol mode.",
                    },
                },
                "required": ["task_id", "query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_apk_source",
            "description": (
                "Browse the decompiled source tree. view=true reads one "
                "file's first window through gms-rt-apk-source-read (use "
                "gms_rt_apk_source_read for offset/limit paging)."
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
                        "description": "Read the file content instead of the listing.",
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
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "Only validate preconditions (base_url + credentials); "
                            "do not create a snapshot."
                        ),
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
            "name": "gms_rt_redmine_history_search",
            "description": (
                "Search ALL historical Redmine issues (owner's local archive "
                "plus Redmine site search) for the same or a similar problem "
                "and reusable fixes: returns issue id, subject, resolution "
                "status, solution/patch_direction when archived. Read-only. "
                "Use exclude_issue_id to skip the issue currently being "
                "analyzed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                        "description": "Keyword query, e.g. 'RK3562 Android16 SSI'.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "exclude_issue_id": {"type": "integer", "minimum": 0},
                    "resolved_only": {"type": "boolean"},
                },
                "required": ["q"],
                "additionalProperties": False,
            },
        },
        {
            "name": "gms_rt_redmine_triage",
            "description": (
                "List TODAY's pending Redmine issues for the owner account: "
                "waiting_my_reply + no_reply_3_days buckets, deduped with "
                "priority and fingerprint. Read-only entry point for daily "
                "brief analysis; source of truth is the personal dashboard "
                "workload statistics."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "stale_days": {
                        "type": "integer", "minimum": 1, "maximum": 30,
                        "description": "Days after which an unreplied issue counts as stale (default 3).",
                    },
                    "list_limit": {
                        "type": "integer", "minimum": 1, "maximum": 100,
                        "description": "Max issues per bucket (default 100).",
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "Bypass the workload statistics cache for this call.",
                    },
                },
                "required": [],
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
                "controller and not leased by another client. Hosts whose "
                "model has no vision input: pass as_file=true to get the "
                "image saved to a local file path instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device serial, e.g. RK3562GMS7.",
                    },
                    "as_file": {
                        "type": "boolean",
                        "description": (
                            "Save the image to a local 0600 file and return "
                            "its path as text instead of inline image "
                            "content (for models without vision input)."
                        ),
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
                "are never silently cropped. Hosts whose model has no "
                "vision input: pass as_file=true to get the image saved "
                "to a local file path instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 0, "maxLength": 2048},
                    "as_file": {
                        "type": "boolean",
                        "description": (
                            "Save the image to a local 0600 file and return "
                            "its path as text instead of inline image "
                            "content (for models without vision input)."
                        ),
                    },
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
                "fixed query. Returns path:line:column + snippet. CLI: "
                "gms-rt-apk-search <task_id> <query> --mode content."
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
        {
            "name": "gms_rt_knowledge_search",
            "description": (
                "Search background-only Android system-mechanism knowledge "
                "(android-internals wiki; ADR 0014). Hits explain how a "
                "mechanism (LMKD, Binder, Choreographer, ...) is supposed to "
                "work and carry provenance (chapter, applicable_versions, "
                "last_verified, license). Background only: never verified "
                "root-cause evidence, never a substitute for gms_rt_sdk_* "
                "source forensics. CLI equivalent: gms-rt-knowledge-search."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 256,
                        "description": "Mechanism keywords, e.g. 'LMKD PRESSURE_AFTER_KILL'.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    "android_api_level": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "description": "Optional target Android API level for compatibility-aware ranking.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        }
]
