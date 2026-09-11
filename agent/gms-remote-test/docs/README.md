# GMS Remote Test agent plugin

Drive the GMS Remote Test Controller from Codex, Kimi, or kkagent as an MCP plugin: device
inventory, CTS/GTS/VTS/STS execution, durable job status, reports, firmware
burn, USB/IP, and VPN — all through the bundled, versioned `gms-rt` CLI.

The plugin is **self-contained**: `scripts/gms-remote-test.sh` is a generated
copy of the repository CLI (`agent/gms-remote-test/runtime/gms-remote-test.sh`
— 11.txt: `agent/gms-remote-test/` is the single hand-maintained source root),
so installing the plugin directory is enough; no clone of this repository is
needed on the consumer machine.

## Install from this checkout

The package lifecycle installer is the portable path for all three clients:

```bash
python tools/gms_agent_dev.py install \
  --client codex --server https://CONTROLLER:5001 \
  --ca-cert /etc/gms/controller-ca.pem
python tools/gms_agent_dev.py doctor --client codex --json
```

It installs the runtime, Skill, exact Controller profile, command links, and
MCP registration together. Enroll a service token separately with a one-shot
code so it never appears in process arguments retained by a developer wrapper:

```bash
gms-agent enroll CODE
```

To refresh only the checkout's runtime and `gms-rt-*` command links without
creating or changing any Controller profile:

```bash
python tools/gms_agent_dev.py install --client none
```

For kkagent-only local plugin development, the generated payload retains its
one-line compatibility installer:

```bash
plugins/gms-remote-test/scripts/install_local.sh           # ~/.kkagent/plugins/local/gms-remote-test
plugins/gms-remote-test/scripts/install_local.sh /other/dir  # custom target
```

The script rsyncs the plugin (minus caches), updates the kkagent plugin
registry version, and is safe to re-run after every CLI or adapter change.
Restart kkagent afterwards so the new MCP server process is spawned.

## Requirements

- `GMS_REMOTE_TEST_SERVER` — Controller base URL, e.g. `https://CONTROLLER:5001`
  (required)
- `GMS_CURL_CA_CERT` — trusted CA bundle for the Controller's TLS certificate;
  use `GMS_CURL_INSECURE=1` only in controlled self-signed deployments
  (optional)

No credentials are stored in the plugin. Agents authenticate with an Agent
Service Token: mint a one-shot enrollment code in the web UI and run
`gms-rt-agent-enroll CODE` once; the 0600 token file is referenced by
`GMS_AUTH_TOKEN_FILE` and no platform password ever reaches the agent.
Password login (`gms-rt-auth-login`) and admin elevation
(`gms-rt-auth-elevate`) belong to a human CLI session outside the agent.

## Tools

| Tool | Purpose |
| --- | --- |
| `gms_rt_context` | Run the secret-free environment and credential self-check; call first. |
| `gms_rt_run` | Run any agent-safe (read-only) `gms-rt-*` command with args; the general escape hatch. Mutating/interactive commands are denied. |
| `gms_rt_commands` | Compact command inventory (one line per command), optional `group` filter. Fallback `<name> [arguments]` usage strings are omitted. |
| `gms_rt_describe` | Describe one command: usage, risk mode, auth/elevation requirements, agent-safety; served from cache with close-match suggestions. |
| `gms_rt_devices` | List devices with state, serials, transport (`gms-rt-devices-list`). |
| `gms_rt_device_console` | List serial ports or read one retained console log (`gms-rt-devices-console`). |
| `gms_rt_device_info` | Read detailed information for one or more devices. |
| `gms_rt_device_wait` | Wait boundedly for devices to reach online/fastboot/any state. |
| `gms_rt_auth_status` | Inspect the CLI session's authentication state. |
| `gms_rt_auth_login` | Establish the CLI session (username + `password_stdin`). Human context only — agents use the Agent Service Token; hidden entirely when the server runs in service-token mode. |
| `gms_rt_auth_elevate` | Admin step-up re-auth for the current session (admin credentials via `password_stdin`); unlocks elevated operations. Human context only — hidden in service-token mode. |
| `gms_rt_burn_firmware` | Burn `update.img` to device(s); requires elevation, wipes `/data` by default, optional `--wait-online`. |
| `gms_rt_test_start` | Start a test on a device (or `retry=<timestamp>` a failed report); returns `cluster_job_id`, optional `--wait`. |
| `gms_rt_jobs_list` | List durable test jobs (cheap pre-flight / busy check); rendered one line per job. |
| `gms_rt_jobs_status` | Authoritative state of one durable job (cheap polling); trimmed to key fields. |
| `gms_rt_jobs_wait` | Wait for a durable job to reach a terminal state; trimmed like `jobs_status`. |
| `gms_rt_jobs_events` | Read incremental job events (`after` sequence + `limit`). |
| `gms_rt_jobs_cancel` | Cancel one exact durable job id after explicit user intent. |
| `gms_rt_reports_list` | List finished test reports. |
| `gms_rt_redmine_issue_fetch` | Create/refresh a full Redmine evidence snapshot (raw JSON, untruncated journals, hashed attachments); start/status pattern with optional bounded `wait`. |
| `gms_rt_redmine_issue` | Snapshot status + completeness + description head. |
| `gms_rt_redmine_journals` | Complete journals, cursor-paginated (limit<=100). |
| `gms_rt_redmine_attachments` | Artifact inventory (kind, size, sha256, per-artifact status). |
| `gms_rt_redmine_artifact_search` | Fixed-string search over description/journals/artifact text with evidence refs. |
| `gms_rt_redmine_artifact_read` | Read artifact text by character window. |
| `gms_rt_redmine_image` | Return an image artifact as MCP image content (base64) + metadata; oversized originals error with a download hint. |
| `gms_rt_apk_analyze_attachment` | Import a Redmine `.apk` artifact into the JADX pipeline (owner-scoped, resource-intensive). |
| `gms_rt_apk_source_search` | Search decompiled source content (path:line:column + snippet). |
| `gms_rt_apk_source_read` | Read a line window of one decompiled file. |
| `gms_rt_sdk_sources` | List admin-configured SDK source providers. |
| `gms_rt_sdk_search` | Search an SDK source pinned to a revision; matches carry resolved commit + signed result id. |
| `gms_rt_sdk_read` | Read commit-pinned source windows; returns commit and blob SHA-256. |

## Token discipline (what the adapter does for you)

1. **`--json --non-interactive` injected** into every CLI subprocess, so tool
   output is the stable JSON envelope — no emoji, progress text, or colors.
2. **Envelope compaction**: `command` and `exit_code: 0` are dropped, empty
   `diagnostics` omitted, null/empty data fields pruned recursively; errors
   keep `exit_code`, `diagnostics`, and gain a one-line next-action `hint`
   mapped from the CLI's documented exit codes (3 → login, 5 → busy, 6 →
   retryable network, ...), so agents react without reading docs.
3. **Catalog caching**: the command/safety inventory is fetched once per
   server process (5-minute TTL) instead of once per `gms_rt_run` call;
   `gms_rt_describe` answers from cache.
4. **Compact discovery**: `gms_rt_commands` renders one line per command
   and omits the CLI's no-information `<name> [arguments]` fallback usage
   strings, instead of asking agents to pull `gms-rt-system-commands --json`
   (~6x larger). `gms_rt_run("system-docs")` renders the ~24KB API docs
   listing as one line per endpoint (~80% smaller).
5. **Compact jobs output** (v0.6.0): `gms_rt_jobs_list` renders one line
   per job (`job_id | status | attempt | devices | module | case | created |
   finished | error`), and `gms_rt_jobs_status` / `gms_rt_jobs_wait` trim
   the single-job payload to key fields — ~60-80% fewer tokens on real
   payloads. Error envelopes are never re-rendered.
6. **Typed tools for hot paths** (`context`, `devices`, `device_console`,
   `device_info`, `device_wait`, `auth_status`, `test_start`
   including retry mode, `jobs_list`, `jobs_*`, `reports_list`, `shell`,
   `auth_elevate`, `burn_firmware`) so agents don't pay schema-guessing
   round trips.

## Recommended agent workflow

```text
gms_rt_context                              # environment/profile/auth first
# Agents authenticate via GMS_AUTH_TOKEN_FILE (service token) — no login
# call and no password. gms_rt_auth_login is for a human session only.
gms_rt_commands                             # discover commands (compact)
gms_rt_describe   command=devices-wait      # risk/usage details
gms_rt_devices
gms_rt_test_start  device=RK3572  type=CTS  module=...  wait=true
gms_rt_jobs_status  job_id=<cluster_job_id> # cheap polling (trimmed output)
gms_rt_jobs_events  job_id=<cluster_job_id> after=<last_seq>
gms_rt_burn_firmware firmware_path=update.img device=RK3562GMS7 approval_token=...   # user-minted approval token
gms_rt_shell       device=RK3562GMS7 command="getprop ro.build.fingerprint"
gms_rt_logcat      device=RK3562GMS7            # adb shell logcat -v time (dump mode)
gms_rt_logcat      device=RK3562GMS7 args="-b crash -t 500"
gms_rt_logcat      device=RK3562GMS7 since="09-07 10:52:00.000"  # dump entries at/after time (device-side -t filter)
gms_rt_logcat      device=RK3562GMS7 since="09-07 10:52:00" until="09-07 11:00:00.000"  # bounded time window
# clearing the log buffer (logcat -c) is human-only via the CLI; the MCP tool denies clear=true
gms_rt_shell_exec  device=RK3562GMS7 command="settings put global wifi_on 1" approval_token="..."   # one-shot, user-minted approval token only
gms_rt_reports_list
```

Execution rules for agents (enforced by the CLI contract, see
`skills/gms-remote-test/references/agent-integration.md` in the repo):

1. Treat `ok` and the exit code in the JSON envelope as authoritative.
2. Retry only exit code `6` (network), with a bounded retry count.
3. Do not auto-retry exit codes `4` (permission) or `5` (conflict); inspect
   elevation, locks, ownership, and running work first.
4. Require explicit user authorization before `mutating` commands.
5. After `gms_rt_test_start`, read `cluster_job_id` and use
   `gms_rt_jobs_wait` / `gms_rt_jobs_status`; do not scrape progress text.

## Security boundary

The generic runner (`gms_rt_run`) only executes commands the CLI marks
`agent_safe_unattended` (read-only). Mutating/high-risk operations (reboot,
USB/IP connect/disconnect, config changes, ...) and interactive sessions
(`terminal-open`, `devices-scrcpy`, ...) are denied; they require the
dedicated typed MCP tools with explicit confirmation, or a human-run CLI.
Firmware burn is reachable only through `gms_rt_burn_firmware` (typed) after
the human user mints a server-side, short-lived, single-use approval token.
Arbitrary device shell commands are reachable only through
`gms_rt_shell_exec`, which requires a server-issued one-shot
`approval_token` (minted by the user via `gms_rt_approval_create` in their
own human session; bound to tool + device + SHA256(command), 5-minute TTL,
single use) on every single call — a client-declared `authorized=true`
boolean was never a security boundary and is no longer accepted. The
read-only `gms_rt_shell` allowlist needs no extra authorization.
`gms_rt_logcat` is dump-mode only: clearing the device log buffer
(`logcat -c`) is destructive to diagnostic evidence and stays human-only
via the CLI (`gms-rt-devices-logcat DEVICE -c`); the MCP tool denies
`clear=true`. Agent Service Token (`GMS_AUTH_TOKEN_FILE`) is the preferred
authentication for agents; the password-based `gms_rt_auth_login` /
`gms_rt_auth_elevate` tools are for a human session only and are not even
registered when the MCP server runs in service-token mode
(`GMS_AGENT_AUTH_MODE=service-token`, set by the installer).

MCP tool identifiers intentionally use underscores (`gms_rt_devices`), while
Shell CLI commands use hyphens and an explicit action
(`gms-rt-devices-list`). A bare `gms-rt-devices` command is therefore not
part of the CLI contract. Optional `GMS_MCP_TOOLSETS=core,test,evidence,admin`
filtering can reduce the advertised schema set; filtered tools are also
rejected at call time.

## Maintaining the bundled payload

Everything in this plugin except the manifests and docs is a GENERATED
release copy of `agent/gms-remote-test/` (11.txt: never edit this directory
directly), kept in sync by:

```bash
python tools/sync_agent_package.py
```

It syncs the CLI, the MCP adapter, the launcher (`mcp_launcher.py`), the
MCP reconcile helper (`agent_mcp_config.py`), the
`gms-agent` installer CLI, the `gms_agent/` Python SDK, `SKILL.md`,
`references/`, `agents/`, and validates the six-way version contract against
`agent/gms-remote-test/package.yaml` (the single version source).
Version bumps go through `python tools/release_agent.py --version X.Y.Z`,
which rewrites every declaration and re-runs the sync; distribution
archives are built with `python tools/build_agent_package.py`.

## Tests

```bash
python3 -m pytest plugins/gms-remote-test/tests -q   # MCP + packaging + SDK + reconcile, no network needed
```
