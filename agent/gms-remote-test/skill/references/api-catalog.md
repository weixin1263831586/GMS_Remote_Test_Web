# GMS Remote Test CLI catalog

Use the current helper's `gms-rt-system-help` output as the authoritative CLI
list. The web application exposes additional APIs that intentionally have no
CLI wrapper.

## Naming layers

The two public naming styles are intentional:

- Shell commands use hyphens and normally name an action, for example
  `gms-rt-devices-list` and `gms-rt-devices-console`.
- MCP tool identifiers use underscores because they are API/function names,
  for example `gms_rt_devices` and `gms_rt_device_console`.

Do not expect a bare “gms-rt-devices” executable: device operations are a
command family, and tab completion lists their action suffixes. Use
`gms-rt-devices-list` for inventory. The stable MCP inventory tool remains
`gms_rt_devices`; its description names the CLI equivalent.

Run `python tools/audit_gms_agent_contract.py` from the repository root to
detect drift among implementations, the CLI catalog, these docs, and MCP
schemas/handlers.

## Authentication

| Command | Purpose | Context |
|---|---|---|
| `gms-rt-auth-status` | Inspect authentication requirement and current session | agent + human |
| `gms-rt-auth-credential-mode` | Inspect whether the local CLI uses an Agent Token file or session cookie | agent + human |
| `gms-rt-agent-enroll CODE` | Exchange a one-shot code for a 0600 Agent Service Token file | **agent** |
| `gms-rt-auth-login [username]` | Create and save a server session (password via stdin) | **human only** |
| `gms-rt-auth-logout` | Revoke and remove the saved session | agent + human |
| `gms-rt-auth-elevate [username]` | Re-authenticate an administrator for sensitive operations (password via stdin) | **human only** |
| `gms-rt-auth-elevation-reset` | Clear administrator elevation | agent + human |

The backend authenticates normal API calls with the `gms_session` cookie or
an Agent Service Token (`GMS_AUTH_TOKEN_FILE`). The helper sends the saved
cookie / token through every HTTP path, including uploads, downloads, DELETE
requests, and log streams. AI agents must never hold or type platform or
admin passwords: `gms-rt-auth-login` and `gms-rt-auth-elevate` are run by the
human user in their own shell.

## Command groups

| Group | Commands |
|---|---|
| Agent credentials and approval | `gms-rt-agent-tokens`, `gms-rt-agent-enroll-code`, `gms-rt-agent-token-revoke`, `gms-rt-approval-create` |
| Cluster inventory | `gms-rt-cluster-workers`, `gms-rt-cluster-devices`, `gms-rt-cluster-resolve` |
| Test | `gms-rt-test-start`, `gms-rt-test-stop`, `gms-rt-test-status`, `gms-rt-test-clean`, `gms-rt-test-suites`, `gms-rt-test-modules`, `gms-rt-test-suites-result`, `gms-rt-test-logs-stream` |
| Devices | `gms-rt-devices-list`, `gms-rt-devices-info`, `gms-rt-devices-console`, `gms-rt-devices-wait`, `gms-rt-devices-reboot`, `gms-rt-devices-remount`, `gms-rt-devices-shell`, `gms-rt-devices-logcat`, `gms-rt-devices-push`, `gms-rt-devices-wifi`, `gms-rt-devices-scrcpy`, `gms-rt-devices-screencap`, `gms-rt-devices-ui-dump`, `gms-rt-devices-snapshot`, `gms-rt-devices-user-locked` |
| Bootloader | `gms-rt-devices-bootloader-lock`, `gms-rt-devices-bootloader-unlock`, `gms-rt-devices-bootloader-status` |
| Reports | `gms-rt-reports-list`, `gms-rt-reports-analyze`, `gms-rt-reports-download`, `gms-rt-reports-delete` |
| APK analysis | `gms-rt-apk-resolve`, `gms-rt-apk-analyze`, `gms-rt-apk-status`, `gms-rt-apk-manifest`, `gms-rt-apk-source`, `gms-rt-apk-search`, `gms-rt-apk-download`, `gms-rt-apk-analyze-attachment`, `gms-rt-apk-source-read` |
| Desktop and terminal | `gms-rt-desktop-validate`, `gms-rt-desktop-vnc-start`, `gms-rt-desktop-vnc-status`, `gms-rt-desktop-vnc-stop`, `gms-rt-terminal-open`, `gms-rt-terminal-push` |
| Firmware | `gms-rt-burn-firmware`, `gms-rt-burn-gsi`, `gms-rt-burn-serial` |
| Connectivity | `gms-rt-ssh-ping`, `gms-rt-ssh-route`, `gms-rt-ssh-sshd`, `gms-rt-vpn-connect`, `gms-rt-vpn-disconnect`, `gms-rt-vpn-status`, `gms-rt-usbip-install`, `gms-rt-usbip-connect`, `gms-rt-usbip-disconnect`, `gms-rt-usbip-status`, `gms-rt-adb-forward-status`, `gms-rt-adb-forward-start`, `gms-rt-adb-forward-stop` |
| Users | `gms-rt-users-current`, `gms-rt-users-detect`, `gms-rt-users-list`, `gms-rt-users-set-username` |
| Durable jobs | `gms-rt-jobs-list`, `gms-rt-jobs-status`, `gms-rt-jobs-events`, `gms-rt-jobs-follow`, `gms-rt-jobs-wait`, `gms-rt-jobs-cancel` |
| Config and files | `gms-rt-config-read`, `gms-rt-config-update`, `gms-rt-files-progress` |
| System | `gms-rt-system-capabilities`, `gms-rt-system-command-describe`, `gms-rt-system-commands`, `gms-rt-system-docs`, `gms-rt-system-doctor`, `gms-rt-system-health`, `gms-rt-system-help`, `gms-rt-system-selfcheck`, `gms-rt-system-skills`, `gms-rt-system-update`, `gms-rt-system-version` |
| Code search | `gms-rt-opengrok-search` |
| Redmine evidence | `gms-rt-redmine-issue-fetch`, `gms-rt-redmine-issue-show`, `gms-rt-redmine-journals`, `gms-rt-redmine-attachments`, `gms-rt-redmine-attachment-download`, `gms-rt-redmine-artifact-image`, `gms-rt-artifact-read`, `gms-rt-artifact-search` |
| SDK sources | `gms-rt-sdk-sources`, `gms-rt-sdk-search`, `gms-rt-sdk-read` |

Related commands intentionally have different contracts:

- `gms-rt-test-stop` is the compatibility convenience entry point: without a
  job id it cancels the only active owned test. `gms-rt-jobs-cancel` always
  targets one durable job id and is the precise automation surface.
- `gms-rt-system-doctor` performs strict, scope-specific readiness checks;
  `gms-rt-system-selfcheck` is a best-effort Agent bootstrap report whose
  sections degrade independently and return recovery hints.
- `gms-rt-devices-list` is the Controller device view;
  `gms-rt-cluster-devices` is the authoritative cross-Worker inventory, and
  `gms-rt-cluster-resolve` resolves an exact serial to its owning Worker.
- `gms-rt-jobs-events` returns raw incremental events,
  `gms-rt-jobs-follow` combines status/events/failure summary, and
  `gms-rt-jobs-wait` blocks until a terminal state.
- `gms-rt-apk-search` is the single lookup surface: `--mode name` searches
  filenames, `--mode content` searches decompiled file contents, and
  `--mode symbol` locates a Java symbol definition.
- `gms-rt-apk-status` without a task id lists all analysis tasks;
  `gms-rt-apk-manifest --permissions` returns only declared permissions.

## Redmine evidence workflow (read-only, 2026-09-08 plan)

Pre-flight checklist (run both before the first fetch; ~10 seconds to
discover a scope or credential gap instead of failing at step 4):

```bash
gms-rt-auth-scopes-check --json --non-interactive   # required: redmine.read, artifacts.read_own, apk.analyze_own, sdk.read
gms-rt-redmine-credentials-status --json --non-interactive  # configured must be true
```

The evidence chain is read-only against Redmine and owner-scoped on the
Controller. Snapshots keep raw issue JSON (SHA-256 pinned), full journals
(no truncation), and attachment originals with per-artifact audit status.

```bash
# 1. Create/refresh a full snapshot (refresh always contacts Redmine)
gms-rt-redmine-issue-fetch 648526 --refresh --download all --wait --json --non-interactive
#    (--refresh is the default; --no-refresh may reuse a fresh ready snapshot;
#     --dry-run only validates preconditions — base_url + credentials — without
#     creating a snapshot)

# 2. Inspect completeness (complete=false means gaps — check errors[])
#    The first argument accepts a snapshot_id OR a plain issue_id (the
#    latest snapshot for that issue is resolved server-side; use --issue /
#    --snapshot to disambiguate explicitly)
gms-rt-redmine-issue-show SNAP --json --non-interactive
gms-rt-redmine-issue-show 648526 --json --non-interactive

# 3. Read journals in pages (limit<=100, follow next_cursor)
gms-rt-redmine-journals SNAP --limit 50 --json --non-interactive

# 4. List artifacts (kind/size/sha256/status per attachment)
gms-rt-redmine-attachments SNAP --json --non-interactive

# 5. Search across description/journals/artifact text. Text members inside
#    .zip attachments (logcat, test_result.xml, ...) are extracted at fetch
#    time and searchable too; zip hits are cited as
#    attachment:<file>.zip!/<member> with a line number.
gms-rt-artifact-search SNAP --query 'AssertionError' --json --non-interactive

# 6. Read long text by window (offset/limit chars)
gms-rt-artifact-read ART --offset 0 --limit 65536 --json --non-interactive

# 7. Optional: import an .apk artifact into JADX, then search its source
gms-rt-apk-analyze-attachment SNAP ART --json --non-interactive
gms-rt-apk-status TASK --json --non-interactive
gms-rt-apk-search TASK 'testMethod' --mode content --json --non-interactive
gms-rt-apk-source-read TASK com/example/Test.java --offset 0 --limit 400 --json --non-interactive

# 8. Bind SDK conclusions to an exact commit (admin-configured sources)
gms-rt-sdk-sources --json --non-interactive
gms-rt-sdk-search --source SRC --revision REV --query SYMBOL --json --non-interactive
gms-rt-sdk-read --result-id RID --source SRC --path P --commit SHA --json --non-interactive
```

Citation format for analysis output:

```text
[redmine:648526/journal:912345]
[redmine:648526/attachment:776655#sha256=<prefix>]
[apk:TASK/sources/com/example/Test.java:L120]
[sdk:SRC@<commit>/path/to/File.java:L88]
```

Agent scopes: `redmine.read`, `artifacts.read_own`, `apk.analyze_own`,
`sdk.read`. Missing credentials, private issues, partial downloads, or
unknown SDK revisions surface as explicit errors — never as
`complete=true`.

## Examples

```bash
gms-rt-system-doctor device --json --non-interactive
gms-rt-devices-list --json
gms-rt-devices-console --json
gms-rt-devices-console usb-FTDI_FT232R_USB_UART_A6022883-if00-port0 --tail 500
gms-rt-devices-wait DEVICE-1 --state online --max-wait 300 --json --non-interactive
gms-rt-devices-info 'DEVICE-1 DEVICE-2'
gms-rt-test-status
gms-rt-test-start DEVICE-1 CTS CtsPermissionTestCases
gms-rt-test-start DEVICE-1 CTS android-cts-17_r1 --wait
gms-rt-test-start --retry REPORT_TIMESTAMP DEVICE-1 GTS /path/to/suite
gms-rt-test-suites-result android-cts-17_r1
gms-rt-jobs-wait JOB_ID --max-wait 21600 --json --non-interactive
gms-rt-reports-list
gms-rt-reports-analyze REPORT_TIMESTAMP
gms-rt-apk-resolve CtsCamera
gms-rt-apk-analyze CtsCamera --wait --max-wait 600 --json --non-interactive
gms-rt-apk-source TASK_ID com/example/Foo.java --view
gms-rt-apk-search TASK_ID Permission --limit 20
```

Install the command first with the Controller-hosted installer described in
`SKILL.md`. Run `gms-rt-system-update` to refresh both the Skill and standalone CLI
command links.

Unattended (agent) authentication uses the Agent Service Token, never a
password:

```bash
GMS_RT_PROFILE=<agent-host-user> gms-rt-agent-enroll "$CODE" --json --non-interactive
```

## Agent contract

All commands accept these global options after the command name:

| Option | Behavior |
|---|---|
| `--json` | Emit one JSON envelope on stdout |
| `--quiet` | Suppress supported progress messages |
| `--no-color` | Disable ANSI output |
| `--non-interactive` | Never prompt |
| `--yes` | Accept supported confirmations |
| `--timeout SECONDS` | Override the API timeout |
| `--server URL` | Use another Controller for one invocation |
| `--ca-cert PATH` | Verify the Controller with a trusted CA file |
| `--insecure` | Explicitly allow a controlled self-signed Controller |

The JSON envelope always includes `ok`, `command`, and `exit_code`. It includes
`data` when the command produced a JSON response, otherwise `output`; stderr is
returned as `diagnostics`.

| Exit | Meaning |
|---:|---|
| 0 | Success |
| 2 | Invalid invocation |
| 3 | Authentication required or failed |
| 4 | Permission denied or administrator elevation required |
| 5 | Conflict, busy resource, lock, or rate limit |
| 6 | Network, server, TLS, or timeout failure |
| 7 | Operation failed |

Run `gms-rt-system-capabilities --json` for runtime discovery,
`gms-rt-system-commands --json` for the current command inventory, and
`gms-rt-system-command-describe COMMAND --json` for exact usage and risk metadata.
Capabilities reference the inventory commands instead of embedding a duplicate
copy of the full inventory.
Exact backend payload fields can still change; inspect the current route when
individual fields are used programmatically.

## Short names and blocking waits

Suite commands accept short suite names; device commands accept unique serial
prefixes. Both resolve through the live inventory (`/api/test/suites`,
`/api/devices/list`) and fall back to the raw value with a precise server
error when the reference is ambiguous or unknown.

```bash
gms-rt-test-suites-result android-cts-17_r1
gms-rt-test-start RK3572 CTS CtsPermissionTestCases --wait --max-wait 3600
gms-rt-burn-firmware firmware.zip RK3572 --wait-online
gms-rt-burn-gsi system.img RK3572 --wait-online=900
```

`/api/test/parse-args` also resolves `android-*` short names server-side, so
API callers get the same behavior without the CLI.
