# Agent workflows — verified playbooks

Every flow here was exercised end to end (MCP adapter → CLI → HTTP → JSON
envelope) against a stub Controller mirroring the real response shapes, plus
the elevation matrix below. Keep this file updated only with verified flows;
annotate the context when adding new ones.

Two integration surfaces exist:

- **kkagent MCP plugin** (`gms_rt_*` tools) — preferred inside kkagent;
  token-optimized envelopes, typed tools, safety gate. See the mapping table
  in `agent-integration.md`.
- **Standalone CLI** (`gms-rt-*` with `--json --non-interactive`) — for any
  other agent or shell.

## 1. Health-check bootstrap (no side effects)

```
gms_rt_run(system-health)            # controller alive?
gms_rt_run(system-doctor, ["test"])  # binaries + auth + devices + suites
gms_rt_auth_status                   # session + elevation window
```

`doctor` scopes: `read` (cheapest), `device`, `test`, `firmware`, `gsi`.
Blockers come back as a list with a matching exit code (3 auth, 4 elevation,
5 no devices, 7 binaries).

## 2. Session bootstrap

```
gms_rt_auth_login(username, password_stdin)   # password only via stdin
gms_rt_auth_status                            # authenticated:true
```

Wrong/no session → every protected call returns `exit_code:3` with hint
`authenticate with gms_rt_auth_login`. One login persists in the cookie jar
for the process lifetime (default
`${XDG_STATE_HOME:-$HOME/.local/state}/gms-remote-test/session.cookies`).

## 3. Run a test (full lifecycle)

```
gms_rt_test_start(device="RK3572", type="CTS", suite="android-cts-17_r1")
# → data.cluster_job_id; device prefixes and short suite names auto-resolve
gms_rt_jobs_events(job_id, after=-1, limit=500)   # incremental; use next_after
gms_rt_jobs_events(job_id, after=<next_after>)    # poll only new events
gms_rt_jobs_wait(job_id, max_wait=21600)          # or test_start(wait=true)
gms_rt_jobs_list(limit=5)                         # cheap busy check
gms_rt_reports_list()                             # finished runs
```

Verified behaviors:

- Starting on a busy device → `exit_code:5`, hint
  `conflict/busy: check gms_rt_jobs_list, retry when free`.
- Retry mode: `gms_rt_test_start(retry="<report_timestamp>", device=..., type=...)`
  maps to `--retry`; module/case are ignored in retry mode.
- `jobs-wait` on an unknown job surfaces the server error body
  (`data.error: "job X not found"`) — do not retry blindly.
- Event polling is incremental: pass the previous `next_after` to avoid
  re-reading history.

## 4. Elevation matrix (human-gated)

| State | Elevated read-only commands | Mutating commands |
|---|---|---|
| logged in, not elevated | exit 4 + hint (blocked) | denied by plugin gate |
| Agent tries `auth-elevate` via `gms_rt_run` | denied (not agent-safe) | denied |
| Human elevates via CLI (outside agent) | **5/5 unlocked**: `users-list`, `adb-forward-status`, `desktop-validate`, `desktop-vnc-status`, `test-suites-result` | still denied (8/8 verified) |
| After `auth-elevation-reset` or expiry | blocked again (exit 4) | denied |

Human elevation (run outside the agent, never inside):

```bash
printf '%s\n' "$ADMIN_PASSWORD" |
  gms-rt-auth-elevate "$ADMIN_USERNAME" --password-stdin --non-interactive --json
```

The elevation window is visible via `gms_rt_auth_status`
(`elevated`, `elevated_until`).

## 5. Device inspection

```
gms_rt_devices()                                  # inventory
gms_rt_run(devices-info, ["<serial>"])            # per-device detail
gms_rt_run(devices-bootloader-status, ["<serial>"])
gms_rt_run(devices-wait, ["<prefix>", "--state", "online", "--max-wait", "300"])
```

Device list entries use `status` + `protocol` (`adb`/`fastboot`);
`devices-wait` matches on those fields.

`devices-info` may transiently fail with exit_code 6 (curl cannot connect)
right after a Controller restart — retry once after a few seconds before
escalating to `system-health`.

### 5.1 Read-only device shell (`gms_rt_shell`, plugin >= 0.5.0)

For unattended device diagnosis, the plugin exposes a typed tool backed by
`gms-rt-devices-shell` with a strict read-only allowlist:

```
gms_rt_shell(device="RK3562GMS7", command="getprop ro.build.fingerprint")
gms_rt_shell(device="RK3562GMS7", command="logcat -d -b crash -v threadtime")
gms_rt_shell(device="RK3562GMS7", command="ls /data/anr/")
gms_rt_shell(device="RK3562GMS7", command="cat /data/anr/anr_2026-09-05-12-56-34-035")
gms_rt_shell(device="RK3562GMS7", command="dumpsys window")
gms_rt_shell(device="RK3562GMS7", command="settings get secure user_setup_complete")
```

- Allowlisted binaries: `getprop dumpsys logcat ls cat ps pidof settings stat uptime vmstat df wm`.
- Denied: any chaining (`|`, `;`, `&&`), redirection, globs, quoting,
  mutating binaries (`am pm cmd input svc reboot`), `logcat` without dump
  flags (`-d/-t/-T`), `settings` subcommands other than `get`,
  `wm` other than `size`/`density` (read), mutating `dumpsys` args
  (`unplug reset disable enable kill force-stop ...`).
- Typical agent diagnosis loop: `gms_rt_devices` -> `gms_rt_shell(getprop...)`
  -> `gms_rt_shell(logcat -d -b events -v threadtime)` -> `gms_rt_shell(ls /data/anr/)`
  -> `gms_rt_shell(cat /data/anr/<file>)`.
- Everything else (reboot, push, Wi-Fi, remount, log clear) still requires a
  human-run `gms-rt-devices-*` CLI command — do not attempt to bypass.

## 6. Token-cheap discovery

```
gms_rt_commands(group="jobs")      # one line per command, filtered
gms_rt_describe(command="devices-wait")   # risk/usage; close-match suggestions
gms_rt_run(system-docs)            # API endpoints, one line each (~80% smaller)
```

## 7. Error recovery cheat sheet

| exit_code | meaning | next action |
|---|---|---|
| 2 | usage | `gms_rt_describe` the command; check arg order |
| 3 | auth | `gms_rt_auth_login` |
| 4 | permission/elevation | ask the human to elevate; do not self-elevate |
| 5 | conflict/busy | `gms_rt_jobs_list`, wait and retry |
| 6 | network/timeout | safe to retry with backoff (bounded) |
| 7 | operation failed | read `data`/`diagnostics`; do not blind-retry |

Failed envelopes carry a machine-readable `hint` field with exactly this
guidance — prefer it over doc lookups.

## 8. What the plugin gate always denies

Mutating (burn, reboot, usbip, vpn-connect/disconnect, config-update,
test-start via `gms_rt_run`, reports-delete, users-set-username,
system-update, adb-forward start/stop, terminal-push, test-clean/stop,
jobs-cancel) and interactive (terminal-open/push, devices-shell raw /
scrcpy, test-logs-stream). Route these through typed tools where they exist
(`gms_rt_test_start`, and the read-only allowlist wrapper `gms_rt_shell`)
or a human-run CLI. The generic gate was verified as 35/35 denied with
elevation active.

## 9. Install / upgrade ("翻版")

```bash
plugins/gms-remote-test/scripts/install_local.sh          # local kkagent
plugins/gms-remote-test/scripts/install_local.sh /target  # any directory
```

Restart kkagent after installing; the registry version updates automatically.
