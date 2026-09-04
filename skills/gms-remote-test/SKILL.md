---
name: gms-remote-test
description: Operate and maintain the GMS Remote Test FastAPI platform and its CLI, including authenticated device management, CTS/GTS/VTS/STS execution, durable job status, reports, firmware, VNC, SSH, VPN, USB/IP, ADB forwarding, remote build-host use, and repository changes. Use when inspecting or changing this project, calling its APIs, running gms-rt-* commands from a build server or AI agent, troubleshooting Authentication required responses, or listing supported GMS Remote Test operations.
---

# GMS Remote Test

Use the implementation in the current checkout as the source of truth. Do not rely on old endpoint counts or response fields.

## Operate the platform

On another Linux host, install or update the Skill and CLI from the Controller:

```bash
curl -kfsSL "https://CONTROLLER:5001/api/system/skills/install.sh" | bash
gms-rt-system-health --json --non-interactive
```

Before calling protected APIs, inspect and establish the CLI session:

```bash
gms-rt-system-capabilities --json
gms-rt-system-commands --json
gms-rt-auth-status --json
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
