---
name: gms-remote-test
description: Operate and maintain the GMS Remote Test FastAPI platform and its CLI, including authenticated device management, CTS/GTS/VTS/STS execution, reports, firmware, VNC, SSH, VPN, USB/IP, ADB forwarding, and repository changes. Use when inspecting or changing this project, calling its APIs, running gms-rt-* commands, troubleshooting Authentication required responses, or listing supported GMS Remote Test operations.
---

# GMS Remote Test

Use the implementation in the current checkout as the source of truth. Do not rely on old endpoint counts or response fields.

## Operate the platform

On another Linux host, install or update the Skill and CLI from the Controller:

```bash
curl -kfsSL https://CONTROLLER:5001/api/system/skills/install.sh | bash
gms-rt-system-help
```

Before calling protected APIs, inspect and establish the CLI session:

```bash
gms-rt-capabilities --json
gms-rt-auth-status --json
printf '%s\n' "$PASSWORD" | gms-rt-auth-login USERNAME --password-stdin --non-interactive --json
gms-rt-devices-list --json
```

Run `gms-rt-update` to reinstall the latest Skill and command links from the
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

Set `GMS_REMOTE_TEST_SERVER` when the automatic server address is wrong. Set
`GMS_CURL_CA_CERT` for a trusted CA, or set `GMS_CURL_INSECURE=1` only for a
local self-signed deployment.

Read [references/api-catalog.md](references/api-catalog.md) for supported CLI
commands and examples. Read
[references/agent-integration.md](references/agent-integration.md) when wiring
the CLI into Codex, Claude Code, Kimi, or another terminal agent. For exact
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
