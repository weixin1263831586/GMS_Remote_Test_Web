# gms-rt 命令参考

> 此文件由 `tools/generate_cli_docs.py` 生成，请勿手工编辑。
> 真源：`agent/gms-remote-test/runtime/gms-remote-test.sh`
> （`_gms_rt_command_usage` / `_gms_rt_command_summary` 命令目录，与
> `gms-rt-system-commands` 的机器可读清单同源）。

| 命令 | 用途 | 用法 |
|---|---|---|
| `gms-rt-adb-forward-start` | Forward selected device serials from one Worker to another through adbproxy-rs | gms-rt-adb-forward-start <source_worker_id> <target_worker_id> <serial> [serial...] |
| `gms-rt-adb-forward-status` | List ADB proxy Workers and active source-to-target assignments |  |
| `gms-rt-adb-forward-stop` | Stop one ADB proxy source-to-target Worker assignment | gms-rt-adb-forward-stop <source_worker_id> <target_worker_id> |
| `gms-rt-agent-enroll` | Exchange a one-shot enrollment code for an Agent Service Token stored as a 0600 file | gms-rt-agent-enroll <ENROLLMENT_CODE> [--out FILE] [--profile NAME] |
| `gms-rt-agent-enroll-code` | Mint a one-shot enrollment code for a build server agent (admin + elevation) | gms-rt-agent-enroll-code --name <NAME> [--scopes s1,s2] [--workers w1,w2\|*] [--devices d1,d2\|*] [--expires-days N] [--ttl-minutes N] |
| `gms-rt-agent-token-revoke` | Revoke an Agent Service Token by id (admin + elevation) | gms-rt-agent-token-revoke <TOKEN_ID> |
| `gms-rt-agent-tokens` | List Agent Service Tokens (admin; metadata only, raw tokens are never stored) | gms-rt-agent-tokens |
| `gms-rt-apk-analyze` | Resolve a module artifact, copy it from the suite, and start jadx decompilation | gms-rt-apk-analyze <module_query> [--types cts,vts,gts,sts] [--prefer apk\|jar] [--wait] [--max-wait SECONDS] |
| `gms-rt-apk-analyze-attachment` | Import a Redmine .apk artifact into the JADX analysis pipeline (owner-scoped) | gms-rt-apk-analyze-attachment <snapshot_id> <artifact_id> |
| `gms-rt-apk-download` | Download the decompiled source ZIP of an analysis task | gms-rt-apk-download <task_id> [output.zip] |
| `gms-rt-apk-manifest` | Show the parsed AndroidManifest.xml, or its declared permissions with --permissions | gms-rt-apk-manifest <task_id> [--permissions] |
| `gms-rt-apk-resolve` | Resolve a test module keyword to its APK/JAR artifact in the latest suites | gms-rt-apk-resolve <module_query> [--types cts,vts,gts,sts] [--prefer apk\|jar] |
| `gms-rt-apk-search` | Search decompiled sources by filename (name), file content (content), or Java symbol definition (symbol) | gms-rt-apk-search <task_id> <query> [--mode name\|content\|symbol] [--limit N] [--path FILTER] [--line N] |
| `gms-rt-apk-source` | Browse the decompiled source tree (read file windows with gms-rt-apk-source-read) | gms-rt-apk-source <task_id> [path] |
| `gms-rt-apk-source-read` | Read a window of one decompiled source file by task-relative path | gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N] |
| `gms-rt-apk-status` | Get one APK analysis task status, or list all tasks when no task id is given | gms-rt-apk-status [task_id] |
| `gms-rt-approval-create` | Create a one-shot approval token for a destructive agent action (human session only) | gms-rt-approval-create --tool <gms_rt_tool> --device <serial>[,<serial>...] [--command <command>\|--firmware-sha256 <sha256> [--wipe-data true\|false] [--burn-mode auto\|uf]] |
| `gms-rt-artifact-read` | Read a text/log artifact derived text by char window (--offset/--limit) | gms-rt-artifact-read <artifact_id> [--offset N] [--limit N] |
| `gms-rt-artifact-search` | Search description, journals, and artifact text for a fixed query with evidence refs | gms-rt-artifact-search <snapshot_id> <query> [--limit N] |
| `gms-rt-auth-credential-mode` | Show whether this CLI invocation uses an Agent Token file or a session cookie |  |
| `gms-rt-auth-elevate` | Activate administrator elevation for the current human session | gms-rt-auth-elevate [admin_username] [--password-stdin] |
| `gms-rt-auth-elevation-reset` | Clear administrator elevation from the current human session |  |
| `gms-rt-auth-login` | Create and save a human API session (password prompt or --password-stdin) | gms-rt-auth-login [username] [--password-stdin] |
| `gms-rt-auth-logout` | Revoke the current human API session and remove its local cookie jar |  |
| `gms-rt-auth-scopes-check` | Pre-flight check that the current credential carries required agent scopes (default: Redmine evidence chain) | gms-rt-auth-scopes-check [--requires s1,s2] |
| `gms-rt-auth-status` | Show whether authentication is required and describe the current principal/session |  |
| `gms-rt-burn-firmware` | Transfer and burn a firmware image, with optional approved Agent execution and online wait | gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--approval-token TOKEN] [--wait-online[=SECONDS]] |
| `gms-rt-burn-gsi` | Transfer and burn a GSI image, with optional online wait | gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]] |
| `gms-rt-burn-serial` | Program a serial number on one device | gms-rt-burn-serial <device_id> <serial> |
| `gms-rt-cluster-devices` | List the authoritative cross-Worker device inventory, optionally filtered by Worker or serial | gms-rt-cluster-devices [--worker WORKER_ID] [--query REGEX] |
| `gms-rt-cluster-resolve` | Resolve an exact device serial to its owning Worker without guessing ambiguous matches | gms-rt-cluster-resolve --device <serial> [--worker <worker_id>] |
| `gms-rt-cluster-workers` | List registered Cluster Workers and their current availability |  |
| `gms-rt-config-read` | Read the Controller configuration visible to the current principal |  |
| `gms-rt-config-update` | Update one Controller configuration key | gms-rt-config-update <key> <value> |
| `gms-rt-desktop-validate` | Validate SSH access to a desktop host | gms-rt-desktop-validate <user@ip> |
| `gms-rt-desktop-vnc-start` | Start the configured VNC service on a desktop host | gms-rt-desktop-vnc-start [host] [password] [vnc_password] |
| `gms-rt-desktop-vnc-status` | Read the configured desktop VNC service status |  |
| `gms-rt-desktop-vnc-stop` | Stop the configured desktop VNC service |  |
| `gms-rt-devices-bootloader-lock` | Lock the bootloader on one or more devices |  |
| `gms-rt-devices-bootloader-status` | Read bootloader lock status for one or more devices |  |
| `gms-rt-devices-bootloader-unlock` | Unlock the bootloader on one or more devices |  |
| `gms-rt-devices-console` | List Controller serial ports or read one port retained console log | gms-rt-devices-console [port_key] [--tail N] [--date YYYYMMDD] |
| `gms-rt-devices-info` | Read detailed properties for one or more devices |  |
| `gms-rt-devices-list` | List devices visible through the Controller device inventory |  |
| `gms-rt-devices-logcat` | Capture device logcat via adb shell logcat -v time (-c clears the buffer first; dump mode in non-interactive sessions) | gms-rt-devices-logcat <device_id> [-c] [logcat args] |
| `gms-rt-devices-push` | Push one local file to a device through ADB | gms-rt-devices-push <device_id> <local_file> <remote_path> |
| `gms-rt-devices-reboot` | Reboot one or more devices and report partial failures |  |
| `gms-rt-devices-remount` | Remount one or more devices read-write and optionally reboot when required |  |
| `gms-rt-devices-scrcpy` | Start human interactive screen mirroring for one or more devices | gms-rt-devices-scrcpy DEVICE1 [DEVICE2 ...] |
| `gms-rt-devices-screencap` | Capture one device screenshot as a base64 PNG payload | gms-rt-devices-screencap <device_id> |
| `gms-rt-devices-shell` | Open a human ADB shell or run one approved device command | gms-rt-devices-shell <device_id> [--approval-token TOKEN] [command] |
| `gms-rt-devices-snapshot` | Collect a fixed read-only device diagnostic snapshot (build, activity, lock and owners) | gms-rt-devices-snapshot <device_id> |
| `gms-rt-devices-ui-dump` | Read one device UI hierarchy as structured elements | gms-rt-devices-ui-dump <device_id> |
| `gms-rt-devices-user-locked` | List devices currently locked by a platform user |  |
| `gms-rt-devices-wait` | Wait for selected devices to become visible in the requested state | gms-rt-devices-wait <devices> [--state online\|fastboot\|any] [--interval SECONDS] [--max-wait SECONDS] |
| `gms-rt-devices-wifi` | Connect one or more devices to a Wi-Fi network | gms-rt-devices-wifi <devices> <ssid> [password] |
| `gms-rt-files-progress` | Read upload progress, optionally for one upload id | gms-rt-files-progress [upload_id] |
| `gms-rt-jobs-cancel` | Request cancellation of a durable test job | gms-rt-jobs-cancel <job_id> |
| `gms-rt-jobs-events` | Read incremental durable test job events | gms-rt-jobs-events <job_id> [after_sequence] [limit] |
| `gms-rt-jobs-follow` | Return job status, incremental events and a terminal failure summary in one call | gms-rt-jobs-follow <job_id> [--after SEQUENCE] [--limit N] |
| `gms-rt-jobs-list` | List durable test jobs visible to the current session | gms-rt-jobs-list [limit:1..500] |
| `gms-rt-jobs-status` | Get authoritative durable test job state | gms-rt-jobs-status <job_id> |
| `gms-rt-jobs-wait` | Wait for a durable test job to reach a terminal state | gms-rt-jobs-wait <job_id> [--interval SECONDS] [--max-wait SECONDS] |
| `gms-rt-opengrok-search` | Search the configured OpenGrok index | gms-rt-opengrok-search <query> [true\|false] |
| `gms-rt-redmine-artifact-image` | Return an image artifact as JSON with base64 payload and metadata (for MCP image tooling) | gms-rt-redmine-artifact-image <artifact_id> |
| `gms-rt-redmine-attachment-download` | Stream one evidence artifact original to a client path (reports saved path/bytes/sha256) | gms-rt-redmine-attachment-download <artifact_id> [output_path] |
| `gms-rt-redmine-attachments` | List evidence artifacts with kind, size, sha256, and per-attachment status | gms-rt-redmine-attachments <snapshot_id> |
| `gms-rt-redmine-credentials-status` | Pre-flight check that the owner account has Redmine credentials configured (no secret material returned) | gms-rt-redmine-credentials-status |
| `gms-rt-redmine-issue-fetch` | Create/refresh a full Redmine evidence snapshot (raw JSON, journals, attachments) | gms-rt-redmine-issue-fetch <issue_id_or_url> [--download none\|analyzable\|all] [--refresh\|--no-refresh] [--wait] [--max-wait SECONDS] |
| `gms-rt-redmine-issue-show` | Show snapshot completeness plus issue fields and description head; accepts snapshot_id or issue_id (resolves the latest snapshot) | gms-rt-redmine-issue-show <snapshot_id \| issue_id> [--issue\|--snapshot] |
| `gms-rt-redmine-journals` | Read full (untruncated) issue journals with cursor pagination | gms-rt-redmine-journals <snapshot_id> [--limit N] [--cursor C] |
| `gms-rt-reports-analyze` | Analyze a local report file or a uniquely resolved saved report | gms-rt-reports-analyze <local_report.zip\|test_result.xml\|report_timestamp\|keyword> |
| `gms-rt-reports-delete` | Delete one saved report by timestamp | gms-rt-reports-delete <report_timestamp> |
| `gms-rt-reports-download` | Download a saved report tree into a local output directory | gms-rt-reports-download <report_timestamp> [output_dir] |
| `gms-rt-reports-list` | List test reports visible to the current principal |  |
| `gms-rt-sdk-read` | Read a commit-pinned SDK source window by signed result id (returns commit and blob sha256) | gms-rt-sdk-read SDK_RESULT_ID [--offset N] [--limit N] |
| `gms-rt-sdk-search` | Search an SDK source at a pinned revision; matches carry commit-bound result ids | gms-rt-sdk-search --source ID --revision REV --query TEXT [--path FILTER] [--limit N] |
| `gms-rt-sdk-sources` | List admin-configured SDK source providers and default revisions | gms-rt-sdk-sources |
| `gms-rt-ssh-ping` | Test network reachability between a test host and client address | gms-rt-ssh-ping <test_host_ip> <client_ip> |
| `gms-rt-ssh-route` | Read the configured SSH route information |  |
| `gms-rt-ssh-sshd` | Inspect SSHD status locally or on a user@host target and show setup guidance | gms-rt-ssh-sshd [user@ip] |
| `gms-rt-system-capabilities` | Print the CLI contract, global options, and exit codes |  |
| `gms-rt-system-command-describe` | Describe one CLI command for machine execution | gms-rt-system-command-describe <gms-rt-command> |
| `gms-rt-system-commands` | Print the machine-readable command inventory |  |
| `gms-rt-system-docs` | Read the Controller API documentation catalog |  |
| `gms-rt-system-doctor` | Check controller, session, tools, devices, and suites for an operation scope | gms-rt-system-doctor [read\|device\|firmware\|gsi\|test] |
| `gms-rt-system-health` | Check Controller service liveness and version |  |
| `gms-rt-system-help` | Show the human-readable CLI command list |  |
| `gms-rt-system-selfcheck` | One-shot read-only agent environment report (auth, health, devices, suites, hints) |  |
| `gms-rt-system-skills` | Download a Controller-hosted Skill archive to the current directory | gms-rt-system-skills [skill_name] |
| `gms-rt-system-update` | Reinstall the latest Skill and CLI command links | gms-rt-system-update |
| `gms-rt-system-version` | Print the local CLI version |  |
| `gms-rt-terminal-open` | Open a human interactive SSH terminal on the test host | gms-rt-terminal-open [host] [user] [port] |
| `gms-rt-terminal-push` | Upload a local file into a test-host directory | gms-rt-terminal-push <file_path> [target_path] |
| `gms-rt-test-clean` | Clean the test execution environment |  |
| `gms-rt-test-logs-stream` | Stream live test logs until interrupted |  |
| `gms-rt-test-modules` | List available tradefed modules for a suite (testcases/ directory) | gms-rt-test-modules <tools_path\|suite_name> [--filter PATTERN] |
| `gms-rt-test-start` | Start a test with smart args, suite short names, device prefixes, and optional --wait | gms-rt-test-start <device> [type] [module] [case] [suite] [--worker ID] [--wait[=SECONDS]] [--max-wait SECONDS] \| --retry <timestamp> [device] [type] [suite] [options] |
| `gms-rt-test-status` | Read the legacy aggregate test execution status |  |
| `gms-rt-test-stop` | Compatibility stop entry: cancel an explicit job, or the only active owned test job | gms-rt-test-stop [job_id] |
| `gms-rt-test-suites` | List available test suite installations and tools paths | gms-rt-test-suites [base_path] |
| `gms-rt-test-suites-result` | List tradefed results for a suite path or short suite name | gms-rt-test-suites-result <tools_path\|suite_name> [--force-refresh] |
| `gms-rt-usbip-connect` | Start a USB/IP connection to a specified device host | gms-rt-usbip-connect <user@ip> [password] |
| `gms-rt-usbip-disconnect` | Stop a USB/IP connection to a specified device host | gms-rt-usbip-disconnect <user@ip> |
| `gms-rt-usbip-install` | Install USB/IP prerequisites on a specified device host | gms-rt-usbip-install <user@ip> |
| `gms-rt-usbip-status` | Read USB/IP status for a specified device host | gms-rt-usbip-status <user@ip> |
| `gms-rt-users-current` | Read the current platform user identity |  |
| `gms-rt-users-detect` | Detect a platform username from a remote host identity | gms-rt-users-detect <ip> [username] [password] |
| `gms-rt-users-list` | List platform users |  |
| `gms-rt-users-set-username` | Set the current platform username | gms-rt-users-set-username [username] |
| `gms-rt-vpn-connect` | Connect the configured VPN |  |
| `gms-rt-vpn-disconnect` | Disconnect the configured VPN |  |
| `gms-rt-vpn-status` | Read configured VPN connection status |  |
