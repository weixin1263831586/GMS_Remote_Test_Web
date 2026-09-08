---
name: gms-remote-test
description: Operate and maintain the GMS Remote Test FastAPI platform and its CLI, including authenticated device management, CTS/GTS/VTS/STS execution, durable job status, reports, firmware, VNC, SSH, VPN, USB/IP, ADB forwarding, remote build-host use, and repository changes. Use when inspecting or changing this project, calling its APIs, running gms-rt-* commands from a build server or AI agent, troubleshooting Authentication required responses, or listing supported GMS Remote Test operations.
---

# GMS Remote Test

Use the implementation in the current checkout as the source of truth. Do not rely on old endpoint counts or response fields.

## Operate the platform

On another Linux host, install or update the Skill and CLI from the Controller.
Never pipe the install script through `curl -k`: `-k` breaks the bootstrap
trust chain (an attacker who can MITM install.sh can also replace the embedded
signing key). Use a trusted CA bundle instead:

```bash
curl --cacert /etc/gms/controller-ca.pem -fsSL \
  "https://CONTROLLER:5001/api/system/skills/install.sh" -o /tmp/gms-agent-install.sh
bash /tmp/gms-agent-install.sh --client auto   # auto-detects codex / kimi / kkagent
gms-rt-system-health --json --non-interactive
```

`--client` registers the MCP server for the detected agent and writes a
per-agent env profile (`GMS_RT_PROFILE`, `GMS_AUTH_TOKEN_FILE` reference) under
`~/.local/share/gms-remote-test/mcp/`.

Before calling protected APIs, inspect and establish the CLI session:

```bash
gms-rt-system-capabilities --json
gms-rt-system-commands --json
gms-rt-auth-status --json
# Agents (preferred): enroll once with a one-shot code from the web UI, then
# every CLI/MCP call authenticates via the 0600 token file — no password:
#   GMS_RT_PROFILE=codex-build01 gms-rt-agent-enroll 7K3M-FG9A-WX21
# Humans (interactive):
printf '%s\n' "$PASSWORD" | gms-rt-auth-login USERNAME --password-stdin --non-interactive --json
gms-rt-system-doctor test --json --non-interactive
gms-rt-devices-list --json
```

Run `gms-rt-system-update` to reinstall the latest Skill and command links from the
same Controller. Commands are installed as standalone `gms-rt-*` executables so
shell PATH completion can list them. Only those standalone commands are exposed;
the shared dispatcher stays in the private runtime directory.
Inside a source checkout, the bundled helper remains available directly as
`skills/gms-remote-test/scripts/gms-remote-test.sh gms-rt-system-help`.

Prompt for the password by default. Agents should prefer `--password-stdin`
together with `--non-interactive`; use `GMS_REMOTE_TEST_USERNAME` and
`GMS_REMOTE_TEST_PASSWORD` only in a controlled environment. Never print, log,
commit, or persist passwords. The helper stores only the server-issued session
cookie in `GMS_AUTH_COOKIE_JAR`, defaulting beneath
`${XDG_STATE_HOME:-$HOME/.local/state}`.

Use `--json` for automation. It emits exactly one JSON envelope with `ok`,
`command`, `exit_code`, structured `data` when recoverable, and optional
`diagnostics`. Honor the documented exit codes; do not infer success from text.
Use `--non-interactive` for unattended execution and add `--yes` only when the
requested operation explicitly authorizes supported confirmations.

Use `gms-rt-system-command-describe COMMAND --json` for a command's usage, risk mode,
authentication requirement, and elevation requirement. Test starts return a
`cluster_job_id`; follow it with `gms-rt-jobs-status`, `gms-rt-jobs-events`, or
`gms-rt-jobs-wait` instead of inferring completion from log text. Agents can
also pass `--wait [--max-wait SECONDS]` to `gms-rt-test-start` so the command
itself blocks until the durable job reaches a terminal state.

Short names are accepted for suites and devices: `android-cts-17_r1` resolves
to the suite tools path in `gms-rt-test-start` and
`gms-rt-test-suites-result` (both CLI-side and inside `/api/test/parse-args`),
and a unique serial prefix such as `RK3572` expands to the full device serial
in the device commands. After firmware or GSI burns, add
`--wait-online[=SECONDS]` to block until devices return to the `online` state.

For unattended device diagnosis inside kkagent, prefer the typed
`gms_rt_shell` tool (plugin >= 0.5.0): it runs a strictly read-only
allowlist of shell commands (getprop, dumpsys, logcat dump mode, ls, cat,
ps, pidof, settings get, stat, uptime, vmstat, df, wm) on a device without
weakening the mutating-command gate. See
[references/agent-workflows.md](references/agent-workflows.md) section 5.1
for the full allowlist and a worked ANR-diagnosis loop.

For timestamped device logs, use the typed `gms_rt_logcat` tool
(plugin >= 0.7.0): it captures `adb shell logcat -v time` in one-shot dump
mode (`-d`) with optional logcat filters, and `clear=true` (CLI `-c`) runs
`logcat -c` first so only fresh logs are captured; `-f` and shell
metacharacters are denied. The human CLI command is
`gms-rt-devices-logcat DEVICE [-c] [args]` (live streaming without flags).
See [references/agent-workflows.md](references/agent-workflows.md)
section 5.2.

For a state-changing device command (`am`, `pm`, `cmd`, `input`,
`settings put`, ...), use the typed `gms_rt_shell_exec` tool
(plugin >= 0.9.0): it forwards one-shot
`gms-rt-devices-shell DEVICE --approval-token TOKEN 'COMMAND'` only when the
caller passes a one-shot approval token minted by the user via
`gms-rt-approval-create --tool gms_rt_shell_exec --device SERIAL --command
'COMMAND'` (web UI or human CLI session). The server validates the
tool+device+command binding, a 5-minute TTL, and single use — a client-side
`authorized=true` boolean was never a security boundary and is no longer
accepted. The interactive device shell itself remains human-only. See
[references/agent-workflows.md](references/agent-workflows.md) section 5.3.

Multi-worker deployments: discover the owning worker authoritatively with
`gms_rt_cluster_devices` / `gms-rt-cluster-resolve --device SERIAL` before
targeting anything; pass `worker_id` explicitly when serials repeat across
workers. Never assume "the first worker".

Set `GMS_REMOTE_TEST_SERVER` when the automatic server address is wrong. Set
`GMS_CURL_CA_CERT` for a trusted CA, or set `GMS_CURL_INSECURE=1` only for a
local self-signed deployment. For one invocation, prefer `--server URL`,
`--ca-cert PATH`, or the explicit `--insecure` override.

Read [references/api-catalog.md](references/api-catalog.md) for supported CLI
commands and examples. Read
[references/agent-workflows.md](references/agent-workflows.md) for
end-to-end verified playbooks: session bootstrap, test lifecycle with
incremental event polling, the elevation matrix, error recovery per exit
code, and the plugin security gate. Read
[references/agent-integration.md](references/agent-integration.md) when wiring
the CLI into Codex, Claude Code, Kimi, or another terminal agent. Inside
kkagent, prefer the bundled MCP plugin (`gms_rt_*` tools in
`plugins/gms-remote-test`) over raw CLI calls: it injects
`--json --non-interactive`, compacts envelopes, caches the safety catalog,
and gates mutating commands. For exact
request or response fields, inspect the current route and its service call path.

## Handle failures

- On `Authentication required`, run `gms-rt-auth-status`, then
  `gms-rt-auth-login`. Do not disable server authentication.
- On `Permission denied` or `Elevation required`, verify the account role and
  run `gms-rt-auth-elevate ADMIN --password-stdin --non-interactive --json`.
- Exit codes are stable: `2` usage, `3` authentication, `4` permission or
  elevation, `5` conflict or busy, `6` network or timeout, and `7` operation
  failure.
- On connection failure, verify the resolved server URL, health endpoint,
  certificate settings, service status, and firewall.
- On device failures, inspect device state and ownership before retrying. Do not
  bypass device locks.
- Targeting (R29): the platform HTTP API accepts an explicit `worker_id` for
  every cluster operation. The CLI convenience commands (`gms-rt-devices-shell`,
  `gms-rt-devices-logcat`, test start helpers) resolve targets via
  `_resolve_ssh_host()` / the local worker — this is a local-maintenance
  fallback, NOT an expression of the cluster execution context. For anything
  that targets a specific Worker or device, prefer the typed tools and HTTP
  APIs that carry `worker_id`/`device` explicitly; never assume the "first"
  or local host.
- Direct `adb`/OS SSH access from the test host (R11) is outside the
  platform's device-claim and fencing system. It is retained for interactive
  maintenance only; automated flows MUST use the controlled device APIs so
  leases, ownership and audit apply.
- Treat test, firmware, SSH, VPN, USB/IP, and allocation changes as
  security-sensitive. Trace the full backend call path before modifying them.

## Maintain the repository

Read [references/project-maintenance.md](references/project-maintenance.md)
before editing application code.

1. Inspect the current implementation and repository instructions.
2. Trace routes through their services before changing behavior.
3. Make minimal, API-compatible changes.
4. Run the relevant syntax checks and targeted tests.
5. Report every changed file and why it changed.

When updating this skill, verify every helper endpoint still exists in the current
FastAPI routes, run `bash -n` on the helper, exercise authentication with a test
server, and run the skill validator.
