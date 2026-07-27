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
gms-rt-auth-status
gms-rt-auth-login USERNAME
gms-rt-devices-list
```

Run `gms-rt-update` to reinstall the latest Skill and command links from the
same Controller. Commands are installed as standalone `gms-rt-*` executables so
shell PATH completion can list them. Only those standalone commands are exposed;
the shared dispatcher stays in the private runtime directory.
Inside a source checkout, the bundled helper remains available directly as
`skills/gms-remote-test/scripts/gms-remote-test.sh gms-rt-system-help`.

Prompt for the password by default. Use `GMS_REMOTE_TEST_USERNAME` and
`GMS_REMOTE_TEST_PASSWORD` only for controlled non-interactive execution. Never
print, log, commit, or persist passwords. The helper stores only the server-issued
session cookie in `GMS_AUTH_COOKIE_JAR`, defaulting beneath
`${XDG_STATE_HOME:-$HOME/.local/state}`.

Set `GMS_REMOTE_TEST_SERVER` when the automatic server address is wrong. Set
`GMS_CURL_CA_CERT` for a trusted CA, or set `GMS_CURL_INSECURE=1` only for a
local self-signed deployment.

Read [references/api-catalog.md](references/api-catalog.md) for supported CLI
commands and examples. For exact request or response fields, inspect the current
route and its service call path.

## Handle failures

- On `Authentication required`, run `gms-rt-auth-status`, then
  `gms-rt-auth-login`. Do not disable server authentication.
- On `Permission denied` or `Elevation required`, verify the account role and
  use the web UI's administrator elevation flow for sensitive operations.
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
