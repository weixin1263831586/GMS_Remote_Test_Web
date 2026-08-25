# GMS Remote Test CLI catalog

Use the current helper's `gms-rt-system-help` output as the authoritative CLI
list. The web application exposes additional APIs that intentionally have no
CLI wrapper.

## Authentication

| Command | Purpose |
|---|---|
| `gms-rt-auth-status` | Inspect authentication requirement and current session |
| `gms-rt-auth-login [username]` | Create and save a server session |
| `gms-rt-auth-logout` | Revoke and remove the saved session |
| `gms-rt-auth-elevate [username]` | Re-authenticate an administrator for sensitive operations |
| `gms-rt-auth-elevation-reset` | Clear administrator elevation |

The backend authenticates normal API calls with the `gms_session` cookie. The
helper sends the saved cookie through every HTTP path, including uploads,
downloads, DELETE requests, and log streams.

## Command groups

| Group | Commands |
|---|---|
| Test | `gms-rt-test-start`, `gms-rt-test-stop`, `gms-rt-test-status`, `gms-rt-test-clean`, `gms-rt-test-suites`, `gms-rt-test-suites-result`, `gms-rt-test-logs-stream` |
| Devices | `gms-rt-devices-list`, `gms-rt-devices-info`, `gms-rt-devices-wait`, `gms-rt-devices-reboot`, `gms-rt-devices-remount`, `gms-rt-devices-shell`, `gms-rt-devices-push`, `gms-rt-devices-wifi`, `gms-rt-devices-scrcpy`, `gms-rt-devices-user-locked` |
| Bootloader | `gms-rt-devices-bootloader-lock`, `gms-rt-devices-bootloader-unlock`, `gms-rt-devices-bootloader-status` |
| Reports | `gms-rt-reports-list`, `gms-rt-reports-analyze`, `gms-rt-reports-download`, `gms-rt-reports-delete` |
| Desktop and terminal | `gms-rt-desktop-validate`, `gms-rt-desktop-vnc-start`, `gms-rt-desktop-vnc-status`, `gms-rt-desktop-vnc-stop`, `gms-rt-terminal-open`, `gms-rt-terminal-push` |
| Firmware | `gms-rt-burn-firmware`, `gms-rt-burn-gsi`, `gms-rt-burn-serial` |
| Connectivity | `gms-rt-ssh-ping`, `gms-rt-ssh-route`, `gms-rt-ssh-sshd`, `gms-rt-vpn-connect`, `gms-rt-vpn-disconnect`, `gms-rt-vpn-status`, `gms-rt-usbip-install`, `gms-rt-usbip-connect`, `gms-rt-usbip-disconnect`, `gms-rt-usbip-status`, `gms-rt-adb-forward-status`, `gms-rt-adb-forward-start`, `gms-rt-adb-forward-stop` |
| Users | `gms-rt-users-current`, `gms-rt-users-detect`, `gms-rt-users-list`, `gms-rt-users-set-username` |
| Durable jobs | `gms-rt-jobs-list`, `gms-rt-jobs-status`, `gms-rt-jobs-events`, `gms-rt-jobs-wait`, `gms-rt-jobs-cancel` |
| Config and files | `gms-rt-config-read`, `gms-rt-config-update`, `gms-rt-files-progress` |
| System | `gms-rt-system-capabilities`, `gms-rt-system-command-describe`, `gms-rt-system-commands`, `gms-rt-system-docs`, `gms-rt-system-doctor`, `gms-rt-system-health`, `gms-rt-system-help`, `gms-rt-system-skills`, `gms-rt-system-update`, `gms-rt-system-version` |
| Code search | `gms-rt-opengrok-search` |

## Examples

```bash
gms-rt-auth-login admin
gms-rt-system-doctor device --json --non-interactive
gms-rt-devices-list --json
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
```

For unattended authentication, avoid command-line password arguments:

```bash
printf '%s\n' "$PASSWORD" |
  gms-rt-auth-login admin --password-stdin --non-interactive --json
```

Install the command first with the Controller-hosted installer described in
`SKILL.md`. Run `gms-rt-system-update` to refresh both the Skill and standalone CLI
command links.

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
