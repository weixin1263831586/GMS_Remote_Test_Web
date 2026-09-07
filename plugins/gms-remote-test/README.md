# GMS Remote Test plugin (kkagent)

Drive the GMS Remote Test Controller from kkagent as an MCP plugin: device
inventory, CTS/GTS/VTS/STS execution, durable job status, reports, firmware
burn, USB/IP, and VPN — all through the bundled, versioned `gms-rt` CLI.

The plugin is **self-contained**: `scripts/gms-remote-test.sh` is a copy of
the repository CLI (`skills/gms-remote-test/scripts/gms-remote-test.sh`), so
installing the plugin directory is enough; no clone of this repository is
needed on the consumer machine.

## Install

Copy or upload this directory (`plugins/gms-remote-test/`) to the internal
kk plugin forge (`bjc/kk-plugins`) and install it from the marketplace, or
install it locally from disk in kkagent.

One-line local install ("翻版") on any machine with this repo checkout:

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

No credentials are stored in the plugin. Authenticate inside the agent with
`gms_rt_auth_login` (the password travels on stdin and is never logged), or
authenticate outside the agent session.

## Tools

| Tool | Purpose |
| --- | --- |
| `gms_rt_run` | Run any agent-safe (read-only) `gms-rt-*` command with args; the general escape hatch. Mutating/interactive commands are denied. |
| `gms_rt_commands` | Compact command inventory (one line per command), optional `group` filter. Fallback `<name> [arguments]` usage strings are omitted. |
| `gms_rt_describe` | Describe one command: usage, risk mode, auth/elevation requirements, agent-safety; served from cache with close-match suggestions. |
| `gms_rt_devices` | List devices with state, serials, transport. |
| `gms_rt_auth_status` | Inspect the CLI session's authentication state. |
| `gms_rt_auth_login` | Establish the CLI session (username + `password_stdin`). |
| `gms_rt_auth_elevate` | Admin step-up re-auth for the current session (admin credentials via `password_stdin`); unlocks elevated operations. |
| `gms_rt_burn_firmware` | Burn `update.img` to device(s); requires elevation, wipes `/data` by default, optional `--wait-online`. |
| `gms_rt_test_start` | Start a test on a device (or `retry=<timestamp>` a failed report); returns `cluster_job_id`, optional `--wait`. |
| `gms_rt_jobs_list` | List durable test jobs (cheap pre-flight / busy check); rendered one line per job. |
| `gms_rt_jobs_status` | Authoritative state of one durable job (cheap polling); trimmed to key fields. |
| `gms_rt_jobs_wait` | Wait for a durable job to reach a terminal state; trimmed like `jobs_status`. |
| `gms_rt_jobs_events` | Read incremental job events (`after` sequence + `limit`). |
| `gms_rt_reports_list` | List finished test reports. |

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
6. **Typed tools for hot paths** (`devices`, `auth_status`, `test_start`
   including retry mode, `jobs_list`, `jobs_*`, `reports_list`, `shell`,
   `auth_elevate`, `burn_firmware`) so agents don't pay schema-guessing
   round trips.

## Recommended agent workflow

```text
gms_rt_auth_status                          # check the session
gms_rt_auth_login  username=admin  password_stdin=...   # only if needed
gms_rt_auth_elevate username=<admin> password_stdin=... # only for burn/elevated ops
gms_rt_commands                             # discover commands (compact)
gms_rt_describe   command=devices-wait      # risk/usage details
gms_rt_devices
gms_rt_test_start  device=RK3572  type=CTS  module=...  wait=true
gms_rt_jobs_status  job_id=<cluster_job_id> # cheap polling (trimmed output)
gms_rt_jobs_events  job_id=<cluster_job_id> after=<last_seq>
gms_rt_burn_firmware firmware_path=update.img device=RK3562GMS7   # requires elevation
gms_rt_shell       device=RK3562GMS7 command="getprop ro.build.fingerprint"
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
`gms_rt_auth_elevate` with admin credentials the user explicitly provided.
Passwords are only accepted via `password_stdin` and are forwarded on
stdin, never logged.

## Maintaining the bundled CLI

The CLI is a copy, kept in sync by hand:

```bash
plugins/gms-remote-test/scripts/sync_cli.sh
```

Run it after `skills/gms-remote-test/scripts/gms-remote-test.sh` changes, and
bump `version` in `kk.plugin.json` when the behavior of exposed tools changes.

## Tests

```bash
python3 plugins/gms-remote-test/tests/test_mcp_server.py   # 59 tests, no network needed
```
