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

## Requirements

- `GMS_REMOTE_TEST_SERVER` — Controller base URL, e.g. `https://CONTROLLER:5001`
  (required)
- `GMS_CURL_CA_CERT` — trusted CA bundle for the Controller's TLS certificate;
  use `GMS_CURL_INSECURE=1` only in controlled self-signed deployments
  (optional)

No credentials are stored in the plugin. Authenticate inside the agent via
`gms_rt_run` with `password_stdin` (the secret is forwarded on stdin and
never logged), or authenticate outside the agent session.

## Tools

| Tool | Purpose |
| --- | --- |
| `gms_rt_run` | Run any `gms-rt-*` command with args; the general escape hatch (74 commands). Interactive commands (`terminal-open`, `terminal-push`, `devices-scrcpy`) are denied. |
| `gms_rt_describe` | Describe one command: usage, risk mode, auth/elevation requirements, agent-safety. |
| `gms_rt_devices` | List devices with state, serials, transport. |
| `gms_rt_auth_status` | Inspect the CLI session's authentication state. |
| `gms_rt_test_start` | Start a test on a device; returns `cluster_job_id`, optional `--wait`. |
| `gms_rt_jobs_wait` | Wait for a durable job to reach a terminal state. |
| `gms_rt_jobs_events` | Read incremental job events. |
| `gms_rt_reports_list` | List finished test reports. |

## Recommended agent workflow

```text
gms_rt_auth_status                  # check the session
gms_rt_run  command=gms-rt-auth-login  args=USERNAME  password_stdin=...
gms_rt_devices
gms_rt_test_start  device=RK3572  type=CTS  module=...  wait=true
gms_rt_jobs_events  job_id=<cluster_job_id>
gms_rt_reports_list
```

Execution rules for agents (enforced by the CLI contract, see
`skills/gms-remote-test/references/agent-integration.md` in the repo):

1. Treat `ok` and the exit code in the JSON envelope as authoritative.
2. Retry only exit code `6` (network), with a bounded retry count.
3. Do not auto-retry exit codes `4` (permission) or `5` (conflict); inspect
   elevation, locks, ownership, and running work first.
4. Require explicit user authorization before `mutating` commands.
5. After `gms_rt_test_start`, read `cluster_job_id` and use `gms_rt_jobs_wait`;
   do not scrape progress text.

## Maintaining the bundled CLI

The CLI is a copy, kept in sync by hand:

```bash
plugins/gms-remote-test/scripts/sync_cli.sh
```

Run it after `skills/gms-remote-test/scripts/gms-remote-test.sh` changes, and
bump `version` in `kk.plugin.json` when the behavior of exposed tools changes.

## Tests

```bash
python3 plugins/gms-remote-test/tests/test_mcp_server.py
```
