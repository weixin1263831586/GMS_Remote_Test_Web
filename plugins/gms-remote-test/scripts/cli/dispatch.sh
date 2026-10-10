# Fixed dispatch command domain; sourced by gms-remote-test.sh.
# ==============================================================================
# Help Function
# ==============================================================================

_gms_rt_command_names() {
    declare -F | awk '$3 ~ /^gms-rt-/ {print $3}' | sort -u
}

_gms_rt_command_usage() {
    case "$1" in
        gms-rt-adb-forward-status|gms-rt-auth-credential-mode|gms-rt-auth-elevation-reset|gms-rt-auth-logout|gms-rt-auth-status|gms-rt-cluster-workers|gms-rt-config-read|gms-rt-desktop-vnc-status|gms-rt-desktop-vnc-stop|gms-rt-devices-list|gms-rt-devices-user-locked|gms-rt-reports-list|gms-rt-ssh-route|gms-rt-system-capabilities|gms-rt-system-commands|gms-rt-system-docs|gms-rt-system-health|gms-rt-system-help|gms-rt-system-selfcheck|gms-rt-system-version|gms-rt-test-clean|gms-rt-test-logs-stream|gms-rt-test-status|gms-rt-users-current|gms-rt-users-list|gms-rt-vpn-connect|gms-rt-vpn-disconnect|gms-rt-vpn-status)
            printf '%s' "$1"
            ;;
        gms-rt-adb-forward-start) printf '%s' 'gms-rt-adb-forward-start <source_worker_id> <target_worker_id> <serial> [serial...]' ;;
        gms-rt-adb-forward-stop) printf '%s' 'gms-rt-adb-forward-stop <source_worker_id> <target_worker_id>' ;;
        gms-rt-auth-login) printf '%s' 'gms-rt-auth-login [username] [--password-stdin]' ;;
        gms-rt-auth-elevate) printf '%s' 'gms-rt-auth-elevate [admin_username] [--password-stdin]' ;;
        gms-rt-auth-scopes-check) printf '%s' 'gms-rt-auth-scopes-check [--requires s1,s2]' ;;
        gms-rt-agent-enroll) printf '%s' 'gms-rt-agent-enroll <ENROLLMENT_CODE> [--out FILE] [--profile NAME]' ;;
        gms-rt-agent-tokens) printf '%s' 'gms-rt-agent-tokens' ;;
        gms-rt-agent-enroll-code) printf '%s' 'gms-rt-agent-enroll-code --name <NAME> [--scopes s1,s2] [--workers w1,w2|*] [--devices d1,d2|*] [--expires-days N] [--ttl-minutes N]' ;;
        gms-rt-agent-token-revoke) printf '%s' 'gms-rt-agent-token-revoke <TOKEN_ID>' ;;
        gms-rt-approval-create) printf '%s' 'gms-rt-approval-create --tool <tool> --device <serial>[,<serial>...] [--command <command>|--firmware-sha256 <sha256> [--wipe-data true|false] [--burn-mode auto|uf]]' ;;
        gms-rt-burn-firmware) printf '%s' 'gms-rt-burn-firmware <firmware_path> <devices> [wipe_data] [--approval-token TOKEN] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-gsi) printf '%s' 'gms-rt-burn-gsi <gsi_path> <devices> [wipe_data] [--wait-online[=SECONDS]]' ;;
        gms-rt-burn-serial) printf '%s' 'gms-rt-burn-serial <device_id> <serial>' ;;
        gms-rt-cluster-devices) printf '%s' 'gms-rt-cluster-devices [--worker WORKER_ID] [--query REGEX]' ;;
        gms-rt-cluster-resolve) printf '%s' 'gms-rt-cluster-resolve --device <serial> [--worker <worker_id>]' ;;
        gms-rt-config-update) printf '%s' 'gms-rt-config-update <key> <value>' ;;
        gms-rt-desktop-validate) printf '%s' 'gms-rt-desktop-validate <user@ip>' ;;
        gms-rt-desktop-vnc-start) printf '%s' 'gms-rt-desktop-vnc-start [host] [password] [vnc_password]' ;;
        gms-rt-system-command-describe) printf '%s' 'gms-rt-system-command-describe <command>' ;;
        gms-rt-devices-info|gms-rt-devices-reboot|gms-rt-devices-remount|gms-rt-devices-bootloader-lock|gms-rt-devices-bootloader-unlock|gms-rt-devices-bootloader-status)
            printf '%s' "$1 <devices>"
            ;;
        gms-rt-devices-wait) printf '%s' 'gms-rt-devices-wait <devices> [--state online|fastboot|any] [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-devices-console) printf '%s' 'gms-rt-devices-console [port_key] [--tail N] [--date YYYYMMDD] [--device SERIAL [--worker WORKER_ID]]' ;;
        gms-rt-devices-shell) printf '%s' 'gms-rt-devices-shell <device_id> [--approval-token TOKEN] [command]' ;;
        gms-rt-devices-diag) printf '%s' 'gms-rt-devices-diag <device_id> <command>' ;;
        gms-rt-devices-scrcpy) printf '%s' 'gms-rt-devices-scrcpy DEVICE1 [DEVICE2 ...]' ;;
        gms-rt-devices-screencap) printf '%s' 'gms-rt-devices-screencap <device_id>' ;;
        gms-rt-devices-ui-dump) printf '%s' 'gms-rt-devices-ui-dump <device_id>' ;;
        gms-rt-devices-snapshot) printf '%s' 'gms-rt-devices-snapshot <device_id>' ;;
        gms-rt-devices-wifi) printf '%s' 'gms-rt-devices-wifi <devices> <ssid> [password]' ;;
        gms-rt-jobs-follow) printf '%s' 'gms-rt-jobs-follow <job_id> [--after SEQUENCE] [--limit N]' ;;
        gms-rt-devices-logcat) printf '%s' 'gms-rt-devices-logcat <device_id> [-c] [logcat args]' ;;
        gms-rt-devices-push) printf '%s' 'gms-rt-devices-push <device_id> <local_file> <remote_path>' ;;
        gms-rt-jobs-list) printf '%s' 'gms-rt-jobs-list [limit:1..500]' ;;
        gms-rt-jobs-status) printf '%s' 'gms-rt-jobs-status <job_id>' ;;
        gms-rt-jobs-events) printf '%s' 'gms-rt-jobs-events <job_id> [after_sequence] [limit]' ;;
        gms-rt-jobs-wait) printf '%s' 'gms-rt-jobs-wait <job_id> [--interval SECONDS] [--max-wait SECONDS]' ;;
        gms-rt-jobs-cancel) printf '%s' 'gms-rt-jobs-cancel <job_id>' ;;
        gms-rt-system-doctor) printf '%s' 'gms-rt-system-doctor [read|device|firmware|gsi|test]' ;;
        gms-rt-system-update) printf '%s' 'gms-rt-system-update' ;;
        gms-rt-reports-analyze) printf '%s' 'gms-rt-reports-analyze <local_report.zip|test_result.xml|report_timestamp|keyword>' ;;
        gms-rt-reports-delete) printf '%s' 'gms-rt-reports-delete <report_timestamp>' ;;
        gms-rt-reports-download) printf '%s' 'gms-rt-reports-download <report_timestamp> [output_dir]' ;;
        gms-rt-ssh-ping) printf '%s' 'gms-rt-ssh-ping <test_host_ip> <client_ip>' ;;
        gms-rt-ssh-sshd) printf '%s' 'gms-rt-ssh-sshd [user@ip]' ;;
        gms-rt-system-skills) printf '%s' 'gms-rt-system-skills [skill_name]' ;;
        gms-rt-terminal-open) printf '%s' 'gms-rt-terminal-open [host] [user] [port]' ;;
        gms-rt-terminal-push) printf '%s' 'gms-rt-terminal-push <file_path> [target_path]' ;;
        gms-rt-test-start) printf '%s' 'gms-rt-test-start <device> [type] [module] [case] [suite] [--worker ID] [--wait[=SECONDS]] [--max-wait SECONDS] | --retry <timestamp> [device] [type] [suite] [options]' ;;
        gms-rt-test-stop) printf '%s' 'gms-rt-test-stop [job_id]' ;;
        gms-rt-test-suites) printf '%s' 'gms-rt-test-suites [base_path]' ;;
        gms-rt-test-suites-result) printf '%s' 'gms-rt-test-suites-result <tools_path|suite_name> [--force-refresh]' ;;
        gms-rt-test-modules) printf '%s' 'gms-rt-test-modules <tools_path|suite_name> [--filter PATTERN]' ;;
        gms-rt-apk-resolve) printf '%s' 'gms-rt-apk-resolve <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar] [--suite-path PATH]' ;;
        gms-rt-apk-analyze) printf '%s' 'gms-rt-apk-analyze <module_query> [--types cts,cts-v,vts,gts,sts] [--prefer apk|jar] [--suite-path PATH] [--wait] [--max-wait SECONDS]' ;;
        gms-rt-apk-status) printf '%s' 'gms-rt-apk-status [task_id]' ;;
        gms-rt-apk-manifest) printf '%s' 'gms-rt-apk-manifest <task_id> [--permissions]' ;;
        gms-rt-apk-source) printf '%s' 'gms-rt-apk-source <task_id> [path]' ;;
        gms-rt-apk-search) printf '%s' 'gms-rt-apk-search <task_id> <query> [--mode name|content|symbol] [--limit N] [--path FILTER] [--line N]' ;;
        gms-rt-apk-download) printf '%s' 'gms-rt-apk-download <task_id> [output.zip]' ;;
        gms-rt-usbip-install) printf '%s' 'gms-rt-usbip-install <user@ip>' ;;
        gms-rt-usbip-connect) printf '%s' 'gms-rt-usbip-connect <user@ip> [password]' ;;
        gms-rt-usbip-disconnect) printf '%s' 'gms-rt-usbip-disconnect <user@ip>' ;;
        gms-rt-usbip-status) printf '%s' 'gms-rt-usbip-status <user@ip>' ;;
        gms-rt-users-detect) printf '%s' 'gms-rt-users-detect <ip> [username] [password]' ;;
        gms-rt-users-set-username) printf '%s' 'gms-rt-users-set-username [username]' ;;
        gms-rt-redmine-issue-fetch) printf '%s' 'gms-rt-redmine-issue-fetch <issue_id_or_url> [--download none|analyzable|all] [--refresh|--no-refresh] [--wait] [--max-wait SECONDS] [--dry-run]' ;;
        gms-rt-redmine-issue-show) printf '%s' 'gms-rt-redmine-issue-show <snapshot_id | issue_id> [--issue|--snapshot]' ;;
        gms-rt-redmine-journals) printf '%s' 'gms-rt-redmine-journals <snapshot_id> [--limit N] [--cursor C]' ;;
        gms-rt-redmine-attachments) printf '%s' 'gms-rt-redmine-attachments <snapshot_id>' ;;
        gms-rt-redmine-attachment-download) printf '%s' 'gms-rt-redmine-attachment-download <artifact_id> [output_path]' ;;
        gms-rt-redmine-credentials-status) printf '%s' 'gms-rt-redmine-credentials-status' ;;
        gms-rt-redmine-triage) printf '%s' 'gms-rt-redmine-triage [--stale-days N] [--list-limit N] [--refresh]' ;;
        gms-rt-redmine-history-search) printf '%s' 'gms-rt-redmine-history-search <query> [--limit N] [--exclude-issue-id N] [--resolved-only]' ;;
        gms-rt-artifact-read) printf '%s' 'gms-rt-artifact-read <artifact_id> [--offset N] [--limit N]' ;;
        gms-rt-redmine-artifact-image) printf '%s' 'gms-rt-redmine-artifact-image <artifact_id>' ;;
        gms-rt-artifact-search) printf '%s' 'gms-rt-artifact-search <snapshot_id> <query> [--limit N]' ;;
        gms-rt-apk-analyze-attachment) printf '%s' 'gms-rt-apk-analyze-attachment <snapshot_id> <artifact_id>' ;;
        gms-rt-apk-source-read) printf '%s' 'gms-rt-apk-source-read <task_id> <path> [--offset N] [--limit N]' ;;
        gms-rt-knowledge-search) printf '%s' 'gms-rt-knowledge-search QUERY [--limit N] [--android-api-level N]' ;;
        gms-rt-sdk-sources) printf '%s' 'gms-rt-sdk-sources' ;;
        gms-rt-sdk-search) printf '%s' 'gms-rt-sdk-search --source ID --revision REV --query TEXT [--path FILTER] [--limit N]' ;;
        gms-rt-sdk-read) printf '%s' 'gms-rt-sdk-read SDK_RESULT_ID [--offset N] [--limit N]' ;;
        *) printf '%s' "$1 [arguments]" ;;
    esac
}

# Suggest the closest known commands for a mistyped name (git-style
# "did you mean"). Reads the command catalog on stdin (name<TAB>usage<TAB>summary)
# and prints a comma-separated suggestion list; empty output means no close
# match. Pure local name-distance heuristic, no network access. The program
# is passed via -c so the piped catalog keeps owning stdin (a heredoc would
# replace it and yield zero names).
_gms_rt_closest_commands() {
    local requested="$1"
    [ -n "$requested" ] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    python3 -c '
import difflib
import sys

requested = sys.argv[1]
names = [line.split("\t")[0] for line in sys.stdin.read().splitlines() if line]
matches = difflib.get_close_matches(requested, names, n=3, cutoff=0.5)
if matches:
    print(", ".join(matches))
' "$requested" 2>/dev/null || return 0
}

_gms_rt_command_summary() {
    case "$1" in
        gms-rt-adb-forward-status) printf '%s' 'List ADB proxy Workers and active source-to-target assignments' ;;
        gms-rt-adb-forward-start) printf '%s' 'Forward selected device serials from one Worker to another through adbproxy-rs' ;;
        gms-rt-adb-forward-stop) printf '%s' 'Stop one ADB proxy source-to-target Worker assignment' ;;
        gms-rt-auth-status) printf '%s' 'Show whether authentication is required and describe the current principal/session' ;;
        gms-rt-auth-login) printf '%s' 'Create and save a human API session (password prompt or --password-stdin)' ;;
        gms-rt-auth-logout) printf '%s' 'Revoke the current human API session and remove its local cookie jar' ;;
        gms-rt-auth-elevate) printf '%s' 'Activate administrator elevation for the current human session' ;;
        gms-rt-auth-elevation-reset) printf '%s' 'Clear administrator elevation from the current human session' ;;
        gms-rt-auth-credential-mode) printf '%s' 'Show whether this CLI invocation uses an Agent Token file or a session cookie' ;;
        gms-rt-auth-scopes-check) printf '%s' 'Pre-flight check that the current credential carries required agent scopes (default: Redmine evidence chain)' ;;
        gms-rt-agent-enroll) printf '%s' 'Exchange a one-shot enrollment code for an Agent Service Token stored as a 0600 file' ;;
        gms-rt-agent-tokens) printf '%s' 'List Agent Service Tokens (admin; metadata only, raw tokens are never stored)' ;;
        gms-rt-agent-enroll-code) printf '%s' 'Mint a one-shot enrollment code for a build server agent (admin + elevation)' ;;
        gms-rt-agent-token-revoke) printf '%s' 'Revoke an Agent Service Token by id (admin + elevation)' ;;
        gms-rt-approval-create) printf '%s' 'Create a one-shot approval token for a destructive agent action (human session only; multi-profile hosts: GMS_RT_HUMAN_SESSION=1 gms-rt-auth-login <user> first)' ;;
        gms-rt-cluster-workers) printf '%s' 'List registered Cluster Workers and their current availability' ;;
        gms-rt-cluster-devices) printf '%s' 'List the authoritative cross-Worker device inventory, optionally filtered by Worker or serial' ;;
        gms-rt-cluster-resolve) printf '%s' 'Resolve an exact device serial to its owning Worker without guessing ambiguous matches' ;;
        gms-rt-config-read) printf '%s' 'Read the Controller configuration visible to the current principal' ;;
        gms-rt-config-update) printf '%s' 'Update one Controller configuration key' ;;
        gms-rt-desktop-validate) printf '%s' 'Validate SSH access to a desktop host' ;;
        gms-rt-desktop-vnc-start) printf '%s' 'Start the configured VNC service on a desktop host' ;;
        gms-rt-desktop-vnc-status) printf '%s' 'Read the configured desktop VNC service status' ;;
        gms-rt-desktop-vnc-stop) printf '%s' 'Stop the configured desktop VNC service' ;;
        gms-rt-burn-firmware) printf '%s' 'Transfer and burn a firmware image, with optional approved Agent execution and online wait' ;;
        gms-rt-burn-gsi) printf '%s' 'Transfer and burn a GSI image, with optional online wait' ;;
        gms-rt-burn-serial) printf '%s' 'Program a serial number on one device' ;;
        gms-rt-system-capabilities) printf '%s' 'Print the CLI contract, global options, and exit codes' ;;
        gms-rt-system-command-describe) printf '%s' 'Describe one CLI command for machine execution' ;;
        gms-rt-system-commands) printf '%s' 'Print the machine-readable command inventory' ;;
        gms-rt-system-selfcheck) printf '%s' 'One-shot read-only agent environment report (auth, health, devices, suites, hints)' ;;
        gms-rt-system-doctor) printf '%s' 'Check controller, session, tools, devices, and suites for an operation scope' ;;
        gms-rt-system-help) printf '%s' 'Show the human-readable CLI command list' ;;
        gms-rt-system-update) printf '%s' 'Reinstall the latest Skill and CLI command links' ;;
        gms-rt-system-version) printf '%s' 'Print the local CLI version' ;;
        gms-rt-system-docs) printf '%s' 'Read the Controller API documentation catalog' ;;
        gms-rt-system-health) printf '%s' 'Check Controller service liveness and version' ;;
        gms-rt-system-skills) printf '%s' 'Download a Controller-hosted Skill archive to the current directory' ;;
        gms-rt-devices-list) printf '%s' 'List devices visible through the Controller device inventory' ;;
        gms-rt-devices-info) printf '%s' 'Read detailed properties for one or more devices' ;;
        gms-rt-devices-wait) printf '%s' 'Wait for selected devices to become visible in the requested state' ;;
        gms-rt-devices-logcat) printf '%s' 'Capture device logcat via adb shell logcat -v time (-c clears the buffer first; dump mode in non-interactive sessions)' ;;
        gms-rt-devices-console) printf '%s' 'List Controller serial ports, assess a device binding, or read retained logs' ;;
        gms-rt-devices-bootloader-lock) printf '%s' 'Lock the bootloader on one or more devices' ;;
        gms-rt-devices-bootloader-unlock) printf '%s' 'Unlock the bootloader on one or more devices' ;;
        gms-rt-devices-bootloader-status) printf '%s' 'Read bootloader lock status for one or more devices' ;;
        gms-rt-devices-reboot) printf '%s' 'Reboot one or more devices and report partial failures' ;;
        gms-rt-devices-remount) printf '%s' 'Remount one or more devices read-write and optionally reboot when required' ;;
        gms-rt-devices-shell) printf '%s' 'Open a human ADB shell or run one approved device command' ;;
        gms-rt-devices-diag) printf '%s' 'Run one read-only diagnostic device command (shared typed-readonly allowlist, locally audited)' ;;
        gms-rt-devices-push) printf '%s' 'Push one local file to a device through ADB' ;;
        gms-rt-devices-wifi) printf '%s' 'Connect one or more devices to a Wi-Fi network' ;;
        gms-rt-devices-scrcpy) printf '%s' 'Start human interactive screen mirroring for one or more devices' ;;
        gms-rt-devices-user-locked) printf '%s' 'List devices currently locked by a platform user' ;;
        gms-rt-devices-screencap) printf '%s' 'Capture one device screenshot as a base64 PNG payload' ;;
        gms-rt-devices-ui-dump) printf '%s' 'Read one device UI hierarchy as structured elements' ;;
        gms-rt-devices-snapshot) printf '%s' 'Collect a fixed read-only device diagnostic snapshot (build, activity, lock and owners)' ;;
        gms-rt-reports-list) printf '%s' 'List test reports visible to the current principal' ;;
        gms-rt-reports-analyze) printf '%s' 'Analyze a local report file or a uniquely resolved saved report' ;;
        gms-rt-reports-download) printf '%s' 'Download a saved report tree into a local output directory' ;;
        gms-rt-reports-delete) printf '%s' 'Delete one saved report by timestamp' ;;
        gms-rt-ssh-ping) printf '%s' 'Test network reachability between a test host and client address' ;;
        gms-rt-ssh-route) printf '%s' 'Read the configured SSH route information' ;;
        gms-rt-ssh-sshd) printf '%s' 'Inspect SSHD status locally or on a user@host target and show setup guidance' ;;
        gms-rt-terminal-open) printf '%s' 'Open a human interactive SSH terminal on the test host' ;;
        gms-rt-terminal-push) printf '%s' 'Upload a local file into a test-host directory' ;;
        gms-rt-test-suites-result) printf '%s' 'List tradefed results for a suite path or short suite name' ;;
        gms-rt-test-modules) printf '%s' 'List available tradefed modules for a suite (testcases/ directory)' ;;
        gms-rt-apk-resolve) printf '%s' 'Resolve a test module keyword to its APK/JAR artifact in the latest suites' ;;
        gms-rt-apk-analyze) printf '%s' 'Resolve a module artifact, copy it from the suite, and start jadx decompilation' ;;
        gms-rt-apk-status) printf '%s' 'Get one APK analysis task status, or list all tasks when no task id is given' ;;
        gms-rt-apk-manifest) printf '%s' 'Show the parsed AndroidManifest.xml, or its declared permissions with --permissions' ;;
        gms-rt-apk-source) printf '%s' 'Browse the decompiled source tree (read file windows with gms-rt-apk-source-read)' ;;
        gms-rt-apk-search) printf '%s' 'Search decompiled sources by filename (name), file content (content), or Java symbol definition (symbol)' ;;
        gms-rt-apk-download) printf '%s' 'Download the decompiled source ZIP of an analysis task' ;;
        gms-rt-redmine-issue-fetch) printf '%s' 'Create/refresh a full Redmine evidence snapshot (raw JSON, journals, attachments)' ;;
        gms-rt-redmine-issue-show) printf '%s' 'Show snapshot completeness plus issue fields and description head; accepts snapshot_id or issue_id (resolves the latest snapshot)' ;;
        gms-rt-redmine-journals) printf '%s' 'Read full (untruncated) issue journals with cursor pagination' ;;
        gms-rt-redmine-attachments) printf '%s' 'List evidence artifacts with kind, size, sha256, and per-attachment status' ;;
        gms-rt-redmine-attachment-download) printf '%s' 'Stream one evidence artifact original to a client path (reports saved path/bytes/sha256)' ;;
        gms-rt-redmine-credentials-status) printf '%s' 'Pre-flight check that the owner account has Redmine credentials configured (no secret material returned)' ;;
        gms-rt-redmine-triage) printf '%s' "List today's pending Redmine issues (waiting_my_reply + no_reply_3_days, deduped; read-only)" ;;
        gms-rt-redmine-history-search) printf '%s' "Search all historical Redmine issues (local archive + site search) for similar problems and reusable fixes (read-only)" ;;
        gms-rt-artifact-read) printf '%s' 'Read a text/log artifact derived text by char window (--offset/--limit)' ;;
        gms-rt-redmine-artifact-image) printf '%s' 'Return an image artifact as JSON with base64 payload and metadata (for MCP image tooling)' ;;
        gms-rt-artifact-search) printf '%s' 'Search description, journals, and artifact text for a fixed query with evidence refs' ;;
        gms-rt-apk-analyze-attachment) printf '%s' 'Import a Redmine .apk artifact into the JADX analysis pipeline (owner-scoped)' ;;
        gms-rt-apk-source-read) printf '%s' 'Read a window of one decompiled source file by task-relative path' ;;
        gms-rt-knowledge-search) printf '%s' 'Search background Android system-mechanism knowledge from the external wiki (read-only, ADR 0014)' ;;
        gms-rt-sdk-sources) printf '%s' 'List admin-configured SDK source providers and default revisions' ;;
        gms-rt-sdk-search) printf '%s' 'Search an SDK source at a pinned revision; matches carry commit-bound result ids' ;;
        gms-rt-sdk-read) printf '%s' 'Read a commit-pinned SDK source window by signed result id (returns commit and blob sha256)' ;;
        gms-rt-test-start) printf '%s' 'Start a test with smart args, suite short names, device prefixes, and optional --wait' ;;
        gms-rt-test-stop) printf '%s' 'Compatibility stop entry: cancel an explicit job, or the only active owned test job' ;;
        gms-rt-test-status) printf '%s' 'Read the legacy aggregate test execution status' ;;
        gms-rt-test-clean) printf '%s' 'Clean the test execution environment' ;;
        gms-rt-test-logs-stream) printf '%s' 'Stream live test logs until interrupted' ;;
        gms-rt-test-suites) printf '%s' 'List available test suite installations and tools paths' ;;
        gms-rt-jobs-list) printf '%s' 'List durable test jobs visible to the current session' ;;
        gms-rt-jobs-status) printf '%s' 'Get authoritative durable test job state' ;;
        gms-rt-jobs-events) printf '%s' 'Read incremental durable test job events' ;;
        gms-rt-jobs-wait) printf '%s' 'Wait for a durable test job to reach a terminal state' ;;
        gms-rt-jobs-cancel) printf '%s' 'Request cancellation of a durable test job' ;;
        gms-rt-jobs-follow) printf '%s' 'Return job status, incremental events and a terminal failure summary in one call' ;;
        gms-rt-usbip-install) printf '%s' 'Install USB/IP prerequisites on a specified device host' ;;
        gms-rt-usbip-connect) printf '%s' 'Start a USB/IP connection to a specified device host' ;;
        gms-rt-usbip-disconnect) printf '%s' 'Stop a USB/IP connection to a specified device host' ;;
        gms-rt-usbip-status) printf '%s' 'Read USB/IP status for a specified device host' ;;
        gms-rt-users-current) printf '%s' 'Read the current platform user identity' ;;
        gms-rt-users-detect) printf '%s' 'Detect a platform username from a remote host identity' ;;
        gms-rt-users-list) printf '%s' 'List platform users' ;;
        gms-rt-users-set-username) printf '%s' 'Set the current platform username' ;;
        gms-rt-vpn-connect) printf '%s' 'Connect the configured VPN' ;;
        gms-rt-vpn-disconnect) printf '%s' 'Disconnect the configured VPN' ;;
        gms-rt-vpn-status) printf '%s' 'Read configured VPN connection status' ;;
        gms-rt-burn-*) printf '%s' 'Perform an elevated firmware operation' ;;
        gms-rt-devices-*) printf '%s' 'Inspect or operate Android devices' ;;
        gms-rt-test-*) printf '%s' 'Inspect or operate GMS test execution' ;;
        gms-rt-auth-*) printf '%s' 'Inspect or change the authenticated CLI session' ;;
        gms-rt-system-*) printf '%s' 'Inspect controller system capabilities' ;;
        *) printf '%s' 'GMS Remote Test CLI operation' ;;
    esac
}

_gms_rt_command_catalog() {
    local command
    while IFS= read -r command; do
        printf '%s\t%s\t%s\n' \
            "$command" \
            "$(_gms_rt_command_usage "$command")" \
            "$(_gms_rt_command_summary "$command")"
    done < <(_gms_rt_command_names)
}

gms-rt-system-commands() {
    check_jq || return 1
    _gms_rt_command_catalog | jq -Rn --arg version "$GMS_RT_VERSION" '
        def category:
            split("-")[2] // "other";
        # .data 的形态（data-array / data-object / data-mixed），让调用方
        # 不必逐条试错。data-mixed 表示同一命令
        # 不同参数下形态不同（如 devices-console 带不带 port_key）。
        def output_shape:
            if test("^(gms-rt-devices-list|gms-rt-devices-info|gms-rt-jobs-list|gms-rt-reports-list|gms-rt-cluster-(devices|workers)|gms-rt-users-list|gms-rt-redmine-attachments|gms-rt-redmine-journals|gms-rt-test-(suites|modules)|gms-rt-sdk-sources|gms-rt-sdk-search|gms-rt-knowledge-search|gms-rt-artifact-search|gms-rt-redmine-history-search|gms-rt-apk-(resolve|search|manifest|source|source-read)|gms-rt-system-commands)$")
            then "data-array"
            elif test("^(gms-rt-devices-console|gms-rt-devices-screencap)$")
            then "data-mixed"
            else "data-object"
            end;
        def mode:
            if test("terminal-open|devices-shell|devices-scrcpy|devices-logcat|test-logs-stream|auth-(login|elevate)")
            then "interactive"
            elif test(
                "burn-|config-update|bootloader-(lock|unlock)|devices-(reboot|remount|push|wifi)"
                + "|reports-delete|apk-analyze|terminal-push|test-(start|stop|clean)|usbip-(install|connect|disconnect)"
                + "|vpn-(connect|disconnect)|adb-forward-(start|stop)|desktop-vnc-(start|stop)|users-set-username"
                + "|jobs-cancel|system-update"
                + "|agent-(enroll|enroll-code|token-revoke)|approval-create|auth-(logout|elevation-reset)"
            )
            then "mutating"
            else "read_only"
            end;
        # Explicit capability hints so safety classification
        # never depends on the mode regex alone. Downstream consumers ignore
        # unknown fields; server-side scope checks are never skipped because
        # of these hints.
        def risk:
            if test("apk-analyze-attachment")
            then {external_side_effects: false, resource_intensive: true, required_scope: "apk.analyze_own"}
            elif test("artifact-(read|search)|redmine-attachment-download|redmine-attachments")
            then {external_side_effects: false, resource_intensive: false, required_scope: "artifacts.read_own"}
            elif test("redmine-|artifact-")
            then {external_side_effects: false, resource_intensive: false, required_scope: "redmine.read"}
            elif test("knowledge-search")
            then {external_side_effects: false, resource_intensive: false, required_scope: "knowledge.read"}
            elif test("sdk-")
            then {external_side_effects: false, resource_intensive: false, required_scope: "sdk.read"}
            elif test("^(gms-rt-cluster-devices|gms-rt-devices-(list|console))$")
            then {external_side_effects: false, resource_intensive: false, required_scope: "devices.read"}
            elif test("^gms-rt-jobs-(list|status|events|follow|wait)$")
            then {external_side_effects: false, resource_intensive: false, required_scope: "jobs.read"}
            elif test("^(gms-rt-jobs-cancel|gms-rt-test-stop)$")
            then {external_side_effects: true, resource_intensive: false, required_scope: "tests.cancel"}
            elif test("^gms-rt-test-start$")
            then {external_side_effects: true, resource_intensive: true, required_scope: "tests.execute"}
            elif test("^gms-rt-reports-(list|analyze|download)$")
            then {
                external_side_effects: test("(analyze|download)$"),
                resource_intensive: test("analyze$"),
                required_scope: "reports.read"
            }
            elif test("^gms-rt-(adb-forward-|usbip-)")
            then {external_side_effects: true, resource_intensive: false, required_scope: "devices.lease"}
            elif test("^(gms-rt-agent-enroll|gms-rt-system-skills|gms-rt-apk-download|gms-rt-terminal-push)$")
            then {external_side_effects: true, resource_intensive: false, required_scope: ""}
            elif mode != "read_only"
            then {external_side_effects: true, resource_intensive: false, required_scope: ""}
            else {external_side_effects: false, resource_intensive: false, required_scope: ""}
            end;
        [inputs | split("\t") as $fields | $fields[0] as $name | {
            name: $name,
            category: ($name | category),
            summary: ($fields[2] // ""),
            usage: ($fields[1] // ($name + " [arguments]")),
            mode: ($name | mode),
            requires_auth: ($name | test("^(gms-rt-agent-enroll|gms-rt-auth-(credential-mode|login|status|scopes-check)|gms-rt-system-(capabilities|command-describe|commands|health|help|selfcheck|update|version)|gms-rt-test-modules)$") | not),
            requires_elevation: ($name | test(
                "burn-|config-update|devices-bootloader-(lock|unlock)|adb-forward-"
                + "|desktop-|terminal-(open|push)|usbip-(install|connect|disconnect)"
                + "|users-list|test-suites-result"
                + "|agent-(enroll-code|token-revoke)"
            )),
            requires_explicit_authorization: (($name | mode) == "mutating"),
            supports_json: true,
            output_shape: ($name | output_shape),
            agent_safe_unattended: (
                ($name | mode) == "read_only"
                and ($name | test("auth-(login|logout|elevate|elevation-reset)|approval-create|terminal-open|devices-(shell|scrcpy|logcat)|test-logs-stream") | not)
            )
        } + ($name | risk)] |
        {
            schema_version: 3,
            cli_version: $version,
            commands: .
        }'
}

gms-rt-system-command-describe() {
    local requested="${1:-}"
    [ -n "$requested" ] || {
        error "Usage: gms-rt-system-command-describe <command>"
        return "$GMS_RT_EXIT_USAGE"
    }
    check_jq || return "$GMS_RT_EXIT_OPERATION"
    local commands description
    commands=$(gms-rt-system-commands) || return "$GMS_RT_EXIT_OPERATION"
    description=$(echo "$commands" | jq -c --arg name "$requested" '.commands[] | select(.name == $name)')
    [ -n "$description" ] || {
        error "Unknown command: $requested"
        return "$GMS_RT_EXIT_USAGE"
    }
    echo "$description" | jq '.'
}

gms-rt-system-version() {
    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        jq -cn --arg version "$GMS_RT_VERSION" '{name: "gms-remote-test", version: $version}'
    else
        printf 'gms-remote-test %s\n' "$GMS_RT_VERSION"
    fi
}

gms-rt-system-capabilities() {
    check_jq || return 1
    local command_count
    command_count=$(gms-rt-system-commands | jq '.commands | length') || return 1
    jq -cn \
        --arg version "$GMS_RT_VERSION" \
        --arg server "$SERVER_URL" \
        --argjson command_count "$command_count" \
        '{
            schema_version: 3,
            name: "gms-remote-test",
            version: $version,
            server: $server,
            transport: "authenticated HTTPS API",
            output_modes: ["human", "json"],
            global_options: [
                "--json",
                "--quiet",
                "--no-color",
                "--non-interactive",
                "--yes",
                "--timeout SECONDS",
                "--server URL",
                "--ca-cert PATH",
                "--insecure"
            ],
            authentication: {
                cookie_session: true,
                password_stdin: true,
                elevation_command: "gms-rt-auth-elevate"
            },
            exit_codes: {
                success: 0,
                usage: 2,
                authentication_required: 3,
                permission_or_elevation_required: 4,
                conflict_or_busy: 5,
                network_or_timeout: 6,
                operation_failed: 7
            },
            command_count: $command_count,
            command_inventory: {
                list_command: "gms-rt-system-commands",
                describe_command: "gms-rt-system-command-describe"
            }
        }'
}

gms-rt-system-help() {
    cat << EOF
${BLUE}GMS Remote Test API Helper (FastAPI Port 5001)${NC}
========================================

${YELLOW}ADB Proxy:${NC}
  gms-rt-adb-forward-status      - List ADB Proxy hosts and assignments
  gms-rt-adb-forward-start       - Connect selected devices between Workers
  gms-rt-adb-forward-stop        - Disconnect a source-to-target assignment

${YELLOW}Authentication:${NC}
  gms-rt-auth-login [username]   - Log in and save an API session
  gms-rt-auth-status             - Show the current authentication status
  gms-rt-auth-credential-mode    - Show whether the CLI uses an Agent Token or session cookie
  gms-rt-auth-scopes-check       - Pre-flight required agent scopes (default: Redmine evidence chain)
  gms-rt-auth-logout             - Revoke and remove the saved session
  gms-rt-auth-elevate [username] - Verify an admin for sensitive operations
  gms-rt-auth-elevation-reset    - Clear administrator elevation

${YELLOW}Agent Credentials and Approval:${NC}
  gms-rt-agent-enroll            - Exchange a one-shot code for a local 0600 Agent Token
  gms-rt-agent-tokens            - List Agent Token metadata (admin)
  gms-rt-agent-enroll-code       - Mint a one-shot Agent enrollment code (admin + elevation)
  gms-rt-agent-token-revoke      - Revoke an Agent Token (admin + elevation)
  gms-rt-approval-create         - Mint a human-approved one-shot destructive-action token

${YELLOW}Cluster Inventory:${NC}
  gms-rt-cluster-workers         - List registered Workers
  gms-rt-cluster-devices         - List the authoritative cross-Worker device inventory
  gms-rt-cluster-resolve         - Resolve a device serial to its owning Worker

${YELLOW}Firmware Burning:${NC}
  gms-rt-burn-firmware           - Burn firmware image (optional --wait-online[=SECONDS])
  gms-rt-burn-gsi                - Burn GSI image (optional --wait-online[=SECONDS])
  gms-rt-burn-serial             - Burn serial number

${YELLOW}Configuration:${NC}
  gms-rt-config-read             - Read full configuration
  gms-rt-config-update           - Update configuration

${YELLOW}Desktop VNC:${NC}
  gms-rt-desktop-validate        - Validate desktop host
  gms-rt-desktop-vnc-start       - Start VNC
  gms-rt-desktop-vnc-status      - Check VNC status
  gms-rt-desktop-vnc-stop        - Stop VNC

${YELLOW}Device Management:${NC}
  gms-rt-devices-list               - List all connected devices
  gms-rt-devices-console            - List serial ports, assess device availability, or read retained logs
  gms-rt-devices-info               - Get detailed device information
  gms-rt-devices-wait               - Wait for devices to become ready
  gms-rt-devices-bootloader-lock    - Lock bootloader
  gms-rt-devices-bootloader-unlock  - Unlock bootloader
  gms-rt-devices-bootloader-status  - Check bootloader status
  gms-rt-devices-user-locked        - List user-locked devices
  gms-rt-devices-reboot             - Reboot devices
  gms-rt-devices-remount            - Remount RW (with auto-reboot prompt)
  gms-rt-devices-wifi               - Connect to WiFi
  gms-rt-devices-shell              - Open interactive ADB shell
  gms-rt-devices-diag               - Run an audited read-only diagnostic command
  gms-rt-devices-logcat             - Capture device logcat (adb shell logcat -v time; -c clears buffer first)
  gms-rt-devices-push               - Push file to device (adb push)
  gms-rt-devices-screencap          - Capture device screenshot as base64 PNG
  gms-rt-devices-ui-dump            - Dump UI layout tree as JSON elements
  gms-rt-devices-snapshot           - One-shot device state snapshot (fp/activity/lock/owners)
  gms-rt-devices-scrcpy             - Start interactive screen mirroring

${YELLOW}Reports:${NC}
  gms-rt-reports-list            - List all test reports
  gms-rt-reports-download        - Download report folder
  gms-rt-reports-analyze         - Analyze report
  gms-rt-reports-delete          - Delete report

${YELLOW}SSH Management:${NC}
  gms-rt-ssh-ping                - Test SSH connectivity
  gms-rt-ssh-route               - Check SSH route
  gms-rt-ssh-sshd          - Check SSHD status & install guide (optional: user@ip, e.g. ${DEFAULT_SSH_USER}@192.168.1.100)

${YELLOW}System:${NC}
  gms-rt-system-capabilities     - Print machine-readable CLI capabilities
  gms-rt-system-command-describe - Describe one command for an AI agent
  gms-rt-system-commands         - Print machine-readable command inventory
  gms-rt-system-docs             - Get API documentation
  gms-rt-system-doctor           - Validate a remote CLI host and operation scope
  gms-rt-system-health           - Check server health
  gms-rt-system-selfcheck        - One-shot agent environment report (auth, health, devices, suites)
  gms-rt-system-help             - Show this command list
  gms-rt-system-skills           - Download skills directory as ZIP
  gms-rt-system-update           - Update the Skill and all CLI command links
  gms-rt-system-version          - Print CLI version

${YELLOW}APK Analysis:${NC}
  gms-rt-apk-resolve             - Resolve a suite module to its APK/JAR artifact
  gms-rt-apk-analyze             - Start JADX analysis for a resolved suite module
  gms-rt-apk-status              - Read one task status, or list all tasks
  gms-rt-apk-manifest            - Read a decompiled AndroidManifest (--permissions for the permission list)
  gms-rt-apk-source              - Browse the decompiled source tree
  gms-rt-apk-search              - Search decompiled names, content, or symbol definitions
  gms-rt-apk-download            - Download the decompiled source ZIP

${YELLOW}Redmine Evidence (read-only analysis chain):${NC}
  gms-rt-redmine-issue-fetch     - Create/refresh a full evidence snapshot (journals untruncated, attachments hashed)
  gms-rt-redmine-triage          - List today's pending issues (waiting_my_reply + no_reply_3_days)
  gms-rt-redmine-history-search  - Search historical issues for similar problems and reusable fixes
  gms-rt-redmine-issue-show      - Show snapshot completeness and issue fields
  gms-rt-redmine-journals        - Read full journals with cursor pagination
  gms-rt-redmine-attachments     - List artifacts (kind, size, sha256, status)
  gms-rt-redmine-attachment-download - Save one artifact original locally
  gms-rt-redmine-credentials-status - Pre-flight check for owner Redmine credentials
  gms-rt-redmine-artifact-image  - Return an image artifact as base64 plus metadata
  gms-rt-artifact-read           - Read artifact derived text by char window
  gms-rt-artifact-search         - Search description/journals/artifact text
  gms-rt-apk-analyze-attachment  - Import a Redmine .apk artifact into JADX
  gms-rt-apk-source-read         - Read a window of one decompiled file
  gms-rt-knowledge-search        - Search background Android system-mechanism knowledge (ADR 0014)
  gms-rt-sdk-sources             - List configured SDK source providers
  gms-rt-sdk-search              - Search SDK source pinned to a revision
  gms-rt-sdk-read                - Read commit-pinned SDK source by result id

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open           - Open SSH terminal on test host
  gms-rt-terminal-push           - Push file to test host directory

${YELLOW}Test Management:${NC}
  gms-rt-test-clean              - Clean test environment
  gms-rt-test-logs-stream        - Stream logs in real-time
  gms-rt-test-start              - Start test or retry report (suite short names, --wait)
  gms-rt-test-status             - Check test status
  gms-rt-test-stop               - Stop currently running test
  gms-rt-test-suites             - List available test suites
  gms-rt-test-modules            - List tradefed modules for a suite
  gms-rt-test-suites-result      - List test results (tools path or short suite name)

${YELLOW}Durable Test Jobs:${NC}
  gms-rt-jobs-list               - List durable test jobs
  gms-rt-jobs-status             - Get one durable test job
  gms-rt-jobs-events             - Read job events incrementally
  gms-rt-jobs-wait               - Wait for a job to finish
  gms-rt-jobs-follow             - Status + incremental events + failure summary in one call
  gms-rt-jobs-cancel             - Cancel a durable test job

${YELLOW}USB/IP Connection:${NC}
  gms-rt-usbip-install           - Install USB/IP (requires host parameter)
  gms-rt-usbip-connect           - Start USB/IP connection (requires host parameter)
  gms-rt-usbip-disconnect        - Stop USB/IP connection (requires host parameter)
  gms-rt-usbip-status            - Check USB/IP status (requires host parameter)

${YELLOW}User Management:${NC}
  gms-rt-users-current           - Get current user info
  gms-rt-users-detect            - Auto-detect username
  gms-rt-users-list              - List all users
  gms-rt-users-set-username      - Set username manually

${YELLOW}VPN Management:${NC}
  gms-rt-vpn-connect             - Connect to VPN
  gms-rt-vpn-disconnect          - Disconnect VPN
  gms-rt-vpn-status              - Check VPN status

${YELLOW}Examples:${NC}
  gms-rt-devices-list
  gms-rt-devices-list --json
  printf '%s\n' "\$PASSWORD" | gms-rt-auth-login admin --password-stdin --non-interactive
  gms-rt-system-capabilities --json
  gms-rt-system-doctor test --json --non-interactive
  gms-rt-devices-wait DEVICE --state online --max-wait 300
  gms-rt-devices-bootloader-lock '["DEVICE-1", "DEVICE-2"]'
  gms-rt-desktop-vnc-start
  gms-rt-test-start DEVICE CTS TestModule
  gms-rt-test-start DEVICE CTS android-cts-17_r1 --wait
  gms-rt-test-suites-result android-cts-17_r1
  gms-rt-burn-firmware firmware.zip DEVICE --wait-online
  gms-rt-test-logs-stream
  gms-rt-reports-list

${YELLOW}Test Start (Retry Mode):${NC}
  gms-rt-test-start --retry <TIMESTAMP> <DEVICE> <TYPE> <SUITE_PATH>
  gms-rt-test-start --retry 2026.04.11_17.27.04.421_2920 c3d9b8674f4b94f6 GTS /path/to/suite

${YELLOW}Terminal:${NC}
  gms-rt-terminal-open
  gms-rt-terminal-open 192.168.1.100 $DEFAULT_SSH_USER
  gms-rt-terminal-push ./config.json

Server: ${GREEN}$SERVER_URL${NC}
Docs:   ${GREEN}${SERVER_URL}/docs${NC}
Help:   ${GREEN}${SERVER_URL}/api/system/help${NC}

Global options:
  --json                 Emit exactly one JSON envelope on stdout
  --quiet                Suppress helper progress messages where supported
  --no-color             Disable ANSI colors
  --non-interactive      Never prompt for input
  --yes                  Accept supported confirmations
  --timeout SECONDS      Override the API timeout for this invocation
  --server URL           Use another Controller for this invocation
  --ca-cert PATH         Verify HTTPS with a trusted CA certificate
  --insecure             Skip TLS verification (throwaway environments only)

Exit codes: 0 success, 2 usage, 3 authentication, 4 permission/elevation,
            5 conflict/busy, 6 network/timeout, 7 operation failure
EOF
}

_gms_rt_dispatch_usage_error() {
    local command="$1"
    local message="$2"
    if [ "$GMS_RT_OUTPUT" = "json" ] && command -v jq >/dev/null 2>&1; then
        jq -cn --arg command "$command" --arg message "$message" \
            --argjson exit_code "$GMS_RT_EXIT_USAGE" \
            '{ok: false, command: $command, exit_code: $exit_code, error: $message}'
    else
        error "$message"
    fi
    return "$GMS_RT_EXIT_USAGE"
}

_gms_rt_dispatch() {
    local command="${1:-gms-rt-system-help}"
    [ "$#" -eq 0 ] || shift
    local args=()
    local argument

    # Detect JSON mode before validating other global options so usage errors
    # still honor the one-document stdout contract regardless of option order.
    for argument in "$@"; do
        if [ "$argument" = "--json" ]; then
            GMS_RT_OUTPUT=json
            GMS_RT_QUIET=1
            NO_COLOR=1
            break
        fi
    done

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --json)
                GMS_RT_OUTPUT=json
                GMS_RT_QUIET=1
                NO_COLOR=1
                ;;
            --quiet) GMS_RT_QUIET=1 ;;
            --no-color) NO_COLOR=1 ;;
            --non-interactive) GMS_RT_NON_INTERACTIVE=1 ;;
            --yes) GMS_RT_ASSUME_YES=1 ;;
            --timeout)
                shift
                if [ "$#" -eq 0 ] || ! [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
                    _gms_rt_dispatch_usage_error "$command" "--timeout requires a positive integer"
                    return $?
                fi
                CURL_TIMEOUT="$1"
                ;;
            --timeout=*)
                CURL_TIMEOUT="${1#*=}"
                if ! [[ "$CURL_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
                    _gms_rt_dispatch_usage_error "$command" "--timeout requires a positive integer"
                    return $?
                fi
                ;;
            --server)
                shift
                if [ "$#" -eq 0 ] || ! _validate_server_url "$1"; then
                    _gms_rt_dispatch_usage_error "$command" "--server requires an http(s) Controller URL without credentials, query, or fragment"
                    return $?
                fi
                SERVER_URL="$1"
                GMS_REMOTE_TEST_SERVER="$1"
                ;;
            --server=*)
                SERVER_URL="${1#*=}"
                if ! _validate_server_url "$SERVER_URL"; then
                    _gms_rt_dispatch_usage_error "$command" "--server requires an http(s) Controller URL without credentials, query, or fragment"
                    return $?
                fi
                GMS_REMOTE_TEST_SERVER="$SERVER_URL"
                ;;
            --ca-cert)
                shift
                if [ "$#" -eq 0 ] || [ ! -r "$1" ]; then
                    _gms_rt_dispatch_usage_error "$command" "--ca-cert requires a readable certificate file"
                    return $?
                fi
                GMS_CURL_CA_CERT="$1"
                GMS_CURL_INSECURE=0
                ;;
            --ca-cert=*)
                GMS_CURL_CA_CERT="${1#*=}"
                if [ ! -r "$GMS_CURL_CA_CERT" ]; then
                    _gms_rt_dispatch_usage_error "$command" "--ca-cert requires a readable certificate file"
                    return $?
                fi
                GMS_CURL_INSECURE=0
                ;;
            --insecure)
                GMS_CURL_CA_CERT=""
                GMS_CURL_INSECURE=1
                ;;
            *) args+=("$1") ;;
        esac
        shift
    done

    export GMS_REMOTE_TEST_SERVER GMS_CURL_CA_CERT GMS_CURL_INSECURE
    _refresh_transport_config

    if [[ "$command" != gms-rt-* ]] || ! declare -F "$command" >/dev/null; then
        # Mirror the MCP adapter's closest-match hint so agents and humans
        # get an actionable correction instead of a bare "Unknown command".
        local _suggestions
        _suggestions=$(_gms_rt_command_catalog | _gms_rt_closest_commands "$command")
        if [ -n "$_suggestions" ]; then
            _gms_rt_dispatch_usage_error "$command" \
                "Unknown command: $command. Closest matches: $_suggestions"
        else
            _gms_rt_dispatch_usage_error "$command" \
                "Unknown command: $command (run 'gms-rt-system-help' to list commands)"
        fi
        return $?
    fi

    # Global --help/-h: every command previously accepted these only if its
    # own parser happened to handle them; otherwise the token was passed to
    # the command as a positional argument (e.g. devices-info "--help" was
    # treated as a device serial). Intercept exactly "--help"/"-h" as the
    # sole remaining argument and print the catalog usage line.
    if [ "${#args[@]}" -eq 1 ] && { [ "${args[0]}" = "--help" ] || [ "${args[0]}" = "-h" ]; }; then
        if [ "$GMS_RT_OUTPUT" = "json" ] && command -v jq >/dev/null 2>&1; then
            jq -cn --arg name "$command" --arg usage "$(_gms_rt_command_usage "$command")" \
                '{ok: true, command: $name, usage: $usage}'
        else
            printf 'Usage: %s\n' "$(_gms_rt_command_usage "$command")"
        fi
        return 0
    fi

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
    elif [ -n "${NO_COLOR:-}" ]; then
        RED=""; GREEN=""; YELLOW=""; BLUE=""; NC=""
    fi

    local status_file stdout_file stderr_file command_status api_status
    status_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-status.XXXXXX") || return "$GMS_RT_EXIT_OPERATION"
    stdout_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-stdout.XXXXXX") || {
        rm -f -- "$status_file"
        return "$GMS_RT_EXIT_OPERATION"
    }
    stderr_file=$(mktemp "${TMPDIR:-/tmp}/gms-rt-stderr.XXXXXX") || {
        rm -f -- "$status_file" "$stdout_file"
        return "$GMS_RT_EXIT_OPERATION"
    }
    GMS_RT_STATUS_FILE="$status_file"
    export GMS_RT_STATUS_FILE GMS_RT_OUTPUT GMS_RT_QUIET GMS_RT_NON_INTERACTIVE GMS_RT_ASSUME_YES

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        "$command" "${args[@]}" >"$stdout_file" 2>"$stderr_file"
        command_status=$?
    else
        "$command" "${args[@]}"
        command_status=$?
    fi

    api_status=$(tail -n 1 "$status_file" 2>/dev/null || true)
    if [[ "$api_status" =~ ^[1-9][0-9]*$ ]]; then
        command_status="$api_status"
    fi
    if [ "$command_status" -eq 0 ] && [ "$GMS_RT_ERROR_SEEN" = "1" ]; then
        command_status="$GMS_RT_EXIT_OPERATION"
    fi
    case "$command_status" in
        0|2|3|4|5|6|7) ;;
        *) command_status="$GMS_RT_EXIT_OPERATION" ;;
    esac

    if [ "$GMS_RT_OUTPUT" = "json" ]; then
        # NOTE: 大输出(如 logcat -d / jobs-events)不能经 shell 变量 + jq --arg 传递,
        # 否则超过单参数上限报 "Argument list too long"。改用 --rawfile 直接读临时文件。
        # 同时对未解析为 JSON 的纯文本输出做尾部截断, 保护调用方(如 AI agent)的上下文。
        # jq 自身失败（损坏安装/内存不足）绝不能沿用业务命令的成功码——
        # 之前 stub jq 返回 99 时 CLI 仍退出 0 且 stdout 为空，Agent 把
        # “序列化失败”当成功。序列化失败按操作失败上报。
        local envelope
        envelope=$(jq -cn \
            --arg command "$command" \
            --rawfile stdout "$stdout_file" \
            --rawfile stderr "$stderr_file" \
            --argjson exit_code "$command_status" '
            def json_suffix:
                ([try (
                    capture("(?s)(?<json>[\\[{].*)$").json | fromjson
                ) catch empty][0] // null);
            ($stdout | json_suffix) as $parsed |
            def truncate_text:
                if (length > 200000)
                then "...[output truncated, kept last 200000 of \(length) chars]...\n" + .[-200000:]
                else . end;
            {
                ok: ($exit_code == 0),
                command: $command,
                exit_code: $exit_code
            }
            + (if $parsed == null then {output: ($stdout | truncate_text)} else {data: $parsed} end)
            + (if $stderr == "" then {} else {diagnostics: ($stderr | truncate_text)} end)')
        local jq_status=$?
        if [ "$jq_status" -ne 0 ]; then
            error "输出序列化失败 (jq exit $jq_status)，无法生成 JSON envelope" >&2
            rm -f -- "$status_file" "$stdout_file" "$stderr_file"
            unset GMS_RT_STATUS_FILE
            return "$GMS_RT_EXIT_OPERATION"
        fi
        printf '%s\n' "$envelope"
    fi

    rm -f -- "$status_file" "$stdout_file" "$stderr_file"
    unset GMS_RT_STATUS_FILE
    return "$command_status"
}
