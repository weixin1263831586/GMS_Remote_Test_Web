# AI agent integration

The CLI is the portable integration surface for Codex, Claude Code, Kimi, and
other agents that can execute local processes. Agent-specific prompt systems
may load `SKILL.md`, but automation must rely on the CLI contract instead of
prompt text.

## Typed MCP plugin (Codex, Kimi, and kkagent)

The repository ships a self-contained plugin in `plugins/gms-remote-test`
that wraps this CLI as typed MCP tools. It injects `--json
--non-interactive` into every subprocess, compacts envelopes (drops
`command`/`exit_code:0`, prunes empty fields, adds a next-action `hint` on
errors), caches the safety catalog, and denies non-agent-safe commands at the
tool boundary.

| MCP tool | CLI equivalent |
|---|---|
| `gms_rt_context()` | `gms-rt-system-selfcheck` (call first) |
| `gms_rt_run(command, args)` | any agent-safe `gms-rt-*` command |
| `gms_rt_commands(group?)` | `gms-rt-system-commands` (compact) |
| `gms_rt_describe(command)` | `gms-rt-system-command-describe` |
| `gms_rt_devices()` | `gms-rt-devices-list` |
| `gms_rt_device_console(port_key?, tail?, date?)` | `gms-rt-devices-console` |
| `gms_rt_device_info(devices)` | `gms-rt-devices-info` |
| `gms_rt_device_wait(devices, state?, interval?, max_wait?)` | `gms-rt-devices-wait` |
| `gms_rt_auth_status()` | `gms-rt-auth-status` |
| `gms_rt_agent_enroll(code)` | `gms-rt-agent-enroll` (service token — the agent auth path; `gms_rt_auth_login`/`gms_rt_auth_elevate` are human-session tools only) |
| `gms_rt_test_start(device, type, module?, case?, suite?, retry?, wait?, max_wait?)` | `gms-rt-test-start` (incl. `--retry`) |
| `gms_rt_jobs_list(limit?)` | `gms-rt-jobs-list` |
| `gms_rt_jobs_status(job_id)` | `gms-rt-jobs-status` |
| `gms_rt_jobs_wait(job_id, max_wait?)` | `gms-rt-jobs-wait` |
| `gms_rt_jobs_events(job_id, after?, limit?)` | `gms-rt-jobs-events` |
| `gms_rt_jobs_cancel(job_id)` | `gms-rt-jobs-cancel` |
| `gms_rt_reports_list()` | `gms-rt-reports-list` |
| `gms_rt_apk_resolve(query, suite_types?, prefer?)` | `gms-rt-apk-resolve` |
| `gms_rt_apk_analyze(query, suite_types?, prefer?, wait?, max_wait?)` | `gms-rt-apk-analyze` (module artifact → jadx) |
| `gms_rt_apk_status(task_id)` | `gms-rt-apk-status` |
| `gms_rt_apk_manifest(task_id)` | `gms-rt-apk-manifest` |
| `gms_rt_apk_search(task_id, query, limit?)` | `gms-rt-apk-search` |
| `gms_rt_apk_source(task_id, path?, view?)` | `gms-rt-apk-source` |

For a repository checkout, use `python tools/gms_agent_dev.py install
--client codex --server https://CONTROLLER:5001`; then run `python
tools/gms_agent_dev.py doctor --client codex --json`. The shipped
`gms-agent install --client auto` path supports all three clients. Restart
the client or open a new session after changing its MCP registration.

Set `GMS_MCP_TOOLSETS` in a registration env block to a comma-separated
subset of `core,test,evidence,admin` when a client should receive a smaller
schema catalog. Discovery/context/auth status stay visible; calls to omitted
tools are rejected, not merely hidden.

## Recommended bootstrap (raw CLI)

```bash
gms-rt-system-capabilities --json
gms-rt-system-commands --json
gms-rt-auth-status --json --non-interactive
```

Authenticate outside the agent when practical. **Agents use an Agent
Service Token, never a password**: mint a one-shot enrollment code in the
web UI and run:

```bash
GMS_RT_PROFILE=<agent-host-user> gms-rt-agent-enroll "$CODE" --json --non-interactive
gms-rt-system-doctor test --json --non-interactive
gms-rt-devices-list --json --non-interactive
```

The human user, in their own shell, may authenticate a session with a
password when interactivity is genuinely needed:

```bash
printf '%s\n' "$PASSWORD" |
  gms-rt-auth-login "$USERNAME" --password-stdin --non-interactive --json
```

For an explicitly authorized sensitive operation, the human runs the
elevation in their own session:

```bash
printf '%s\n' "$ADMIN_PASSWORD" |
  gms-rt-auth-elevate "$ADMIN_USERNAME" \
    --password-stdin --non-interactive --json
```

Never put passwords directly in prompts, command arguments, or AI-agent
context.

## Execution rules

1. Inspect the runtime contract with `gms-rt-system-capabilities --json`, then
   discover commands through `gms-rt-system-commands --json`; do not scrape help.
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
curl --cacert /etc/gms/controller-ca.pem -fsSL \
  "https://CONTROLLER:5001/api/agent/install" -o /tmp/gms-agent
python3 /tmp/gms-agent install --client auto
export PATH="$HOME/.local/bin:$PATH"
gms-rt-system-health --json --non-interactive
gms-rt-auth-status --json --non-interactive
```

After installation, `gms-agent enroll <CODE>` exchanges a one-shot enrollment
code from the Web UI for a 0600 Agent Service Token. Never use `curl -k` for
the bootstrap: a TLS man-in-the-middle could replace both the downloaded
bootstrap and the signing key embedded in it. Self-signed deployments must
distribute the Controller CA and pass it with `--cacert` (and later through
`GMS_INSTALL_CA_CERT`).

`--client auto` additionally installs the self-contained Skill+MCP plugin
for every detected agent (Codex/Kimi/kkagent), reconciles each client's MCP
registration (update-in-place; corrupt client configs fail with a backup
rather than being overwritten), and writes per-agent TOML profiles under
`~/.config/gms-agent/profiles/`. The profiles are loaded automatically by
`scripts/mcp_launcher.py`, which is
what the plugin manifests use to start the MCP server — nothing to `source`.
Agents authenticate with an Agent Service
Token instead of a password: mint a one-shot enrollment code in the web UI
(and run `gms-rt-agent-enroll CODE` once, or `python3 gms-agent enroll CODE`
after a registry install); the token file (0600) is referenced
by `GMS_AUTH_TOKEN_FILE` and no platform password ever reaches the agent.

Enterprise deployment path (Agent Package Registry): the Controller also
serves the runtime itself — `curl ... /api/agent/install -o gms-agent &&
python3 gms-agent install --server https://CONTROLLER:5001`, then
`gms-agent update` / `gms-agent rollback <version>` / `gms-agent status`
for upgrades with instant rollback (`versions/<ver>/` + `current` symlink).

Admins can drive the whole enrollment lifecycle from a terminal:
`gms-rt-agent-enroll-code` mints a one-shot pairing code (admin + elevation),
`gms-rt-agent-tokens` lists issued tokens (metadata only — raw tokens are
never stored server-side), and `gms-rt-agent-token-revoke <ID>` instantly
cuts an agent off.

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

Firmware burning is destructive and mutating. Agents never hold admin
passwords: the burn path is gated by a server-side one-shot approval token
bound to tool + device + burn command (5-minute TTL, single use), minted by
the user under their own session:

```bash
# 1. User (human session) mints the approval — the agent only learns the token:
gms-rt-approval-create --tool gms_rt_burn_firmware --device RK3572GMS1 \
  --command "burn_firmware:RK3572GMS1" --json --non-interactive
# 2. Agent executes the burn with that approval token; without it the
#    server rejects any agent burn with 403:
gms-rt-burn-firmware /path/to/update.img RK3572GMS1 true \
  --approval-token "$APPROVAL_TOKEN" --json --non-interactive
# GSI requires direct SSH transfer to the test host (no HTTP fallback):
gms-rt-system-doctor gsi --json --non-interactive
gms-rt-burn-gsi /path/to/system.img DEVICE true --json --non-interactive
```

Over MCP, prefer the asynchronous model (single tool calls stay short for
clients that cap them at 60s, e.g. Kimi):
`gms_rt_burn_firmware(wait=false)` returns an `operation_id` immediately;
poll `gms_rt_burn_status(operation_id=...)` every 20-30s until
`status: "finished"`. `wait=true` keeps the legacy synchronous wait.

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
