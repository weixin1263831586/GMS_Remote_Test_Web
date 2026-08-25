# AI agent integration

The CLI is the portable integration surface for Codex, Claude Code, Kimi, and
other agents that can execute local processes. Agent-specific prompt systems
may load `SKILL.md`, but automation must rely on the CLI contract instead of
prompt text.

## Recommended bootstrap

```bash
gms-rt-capabilities --json
gms-rt-auth-status --json --non-interactive
```

Authenticate outside the agent when practical. When unattended login is
necessary, pass the secret through stdin:

```bash
printf '%s\n' "$PASSWORD" |
  gms-rt-auth-login "$USERNAME" --password-stdin --non-interactive --json
gms-rt-system-doctor test --json --non-interactive
gms-rt-devices-list --json --non-interactive
```

For an explicitly authorized sensitive operation:

```bash
printf '%s\n' "$ADMIN_PASSWORD" |
  gms-rt-auth-elevate "$ADMIN_USERNAME" \
    --password-stdin --non-interactive --json
```

Never put passwords directly in prompts or command arguments.

## Execution rules

1. Discover commands through `gms-rt-capabilities --json`; do not scrape help.
2. Use `--json --non-interactive` for every unattended command.
3. Treat `ok` and the process exit code as authoritative.
4. Retry only exit code `6`, and only with a bounded retry count.
5. Do not automatically retry exit codes `4` or `5`; inspect elevation, locks,
   ownership, and running work first.
6. Require explicit user authorization before commands marked `mutating`.
7. Do not launch commands marked `interactive` in an unattended workflow.
8. Use a trusted CA through `GMS_CURL_CA_CERT`; reserve
   `GMS_CURL_INSECURE=1` for controlled self-signed deployments.
9. After `gms-rt-test-start`, read `data.data.cluster_job_id` from the JSON
   envelope and use `gms-rt-jobs-wait`; do not scrape progress text.

## Build server workflow

Install from the Controller while logged in to the build server:

```bash
curl -kfsSL "https://CONTROLLER:5001/api/system/skills/install.sh" | bash
export PATH="$HOME/.local/bin:$PATH"
gms-rt-system-health --json --non-interactive
gms-rt-auth-status --json --non-interactive
```

The installer binds the standalone commands to that Controller. Use
`--server https://OTHER-CONTROLLER:5001` for a one-off override. A local
firmware artifact can be transferred directly to the test host with SSH/rsync;
if direct transfer is unavailable, `gms-rt-burn-firmware` falls back to the
authenticated HTTP upload path. After authenticating, run
`gms-rt-system-doctor test --json --non-interactive` before starting work.
For unattended direct transfer, provision an SSH key and trusted host key in
advance; non-interactive mode does not prompt for passwords or accept a new
host key. GSI has no HTTP fallback.

Typical Agent-safe observation flow:

```bash
gms-rt-devices-list --json --non-interactive
gms-rt-devices-wait DEVICE --state online --max-wait 300 --json --non-interactive
gms-rt-test-start DEVICE CTS MODULE /path/to/tools --json --non-interactive
gms-rt-jobs-wait JOB_ID --max-wait 21600 --json --non-interactive
gms-rt-jobs-events JOB_ID -1 500 --json --non-interactive
```

Firmware burning is elevated and mutating. Check readiness first, obtain
explicit user authorization, establish elevation, then execute:

```bash
printf '%s\n' "$ADMIN_PASSWORD" |
  gms-rt-auth-elevate "$ADMIN_USERNAME" --password-stdin --non-interactive --json
gms-rt-system-doctor firmware --json --non-interactive
gms-rt-burn-firmware /path/to/update.img DEVICE true --json --non-interactive
# GSI requires direct SSH transfer to the test host (no HTTP fallback):
gms-rt-system-doctor gsi --json --non-interactive
gms-rt-burn-gsi /path/to/system.img DEVICE true --json --non-interactive
```

## JSON envelope

```json
{
  "ok": true,
  "command": "gms-rt-system-health",
  "exit_code": 0,
  "data": {
    "success": true
  }
}
```

When a command does not produce JSON, `output` contains its text. Error details
written by the CLI are returned as `diagnostics`.

## Installation locations

The installer defaults to the Codex skill directory for backward
compatibility. Set `GMS_SKILLS_DIR` to an agent-managed skills directory when
the target agent supports the same Skill package. The standalone `gms-rt-*`
commands are always the compatibility layer and do not require Skill loading.
