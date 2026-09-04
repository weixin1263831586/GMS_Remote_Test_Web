# gms-remote-test plugin — agent maintenance contract

This directory is a self-contained kkagent MCP plugin. Read this file before
changing anything here.

## Layout

| Path | Role |
|---|---|
| `kk.plugin.json` | Plugin manifest; bump `version` when exposed tool behavior changes. |
| `scripts/mcp_server.py` | stdio MCP adapter. Tool schemas, envelope compaction, security gate. |
| `scripts/gms-remote-test.sh` | **Copy** of `skills/gms-remote-test/scripts/gms-remote-test.sh`. Never edit here. |
| `scripts/sync_cli.sh` | Copies the CLI from the skill source into this plugin. |
| `scripts/install_local.sh` | One-command install/upgrade into `~/.kkagent/plugins/local/gms-remote-test` and registry sync. |
| `tests/test_mcp_server.py` | Offline unit tests (stub CLI, no Controller needed). |

## Change flow (mandatory order)

1. Edit the CLI in `skills/gms-remote-test/scripts/gms-remote-test.sh` — never in this copy.
2. `bash scripts/sync_cli.sh`
3. `python3 tests/test_mcp_server.py` — must pass before shipping.
4. Adapter changes: keep every existing tool name and argument contract
   (API compatibility); add tests for new behavior.
5. Bump `version` in `kk.plugin.json` AND `SERVER_VERSION` in
   `scripts/mcp_server.py` together.
6. `bash scripts/install_local.sh` to activate locally; restart kkagent.

## Security boundary (do not weaken)

- `gms_rt_run` executes only commands the CLI catalog marks
  `agent_safe_unattended` (read-only). Mutating/elevated commands are denied.
- `gms-rt-terminal-open`, `gms-rt-terminal-push`, `gms-rt-devices-scrcpy`
  are denied outright (`_DENIED_COMMANDS`).
- Passwords travel only via `password_stdin` → subprocess stdin; never in
  args, logs, or tool descriptions.
- Elevation is a human action (`gms-rt-auth-elevate` outside the agent);
  elevated sessions only unlock the 5 elevated read-only commands
  (`users-list`, `adb-forward-status`, `desktop-validate`,
  `desktop-vnc-status`, `test-suites-result`). Verified by the
  elevation matrix in the skill's `references/agent-workflows.md`.

## Token discipline (regression-test any output change)

1. `--json --non-interactive` injected into every subprocess.
2. Envelope compaction: drop `command`/`exit_code:0`, prune empty data
   fields, add exit-code `hint` on errors.
3. Catalog cached per process (5 min TTL); `gms_rt_commands` renders one
   line per command and omits `<name> [arguments]` fallback usage.
4. `gms_rt_run("system-docs")` renders one line per endpoint (~24KB → ~4.5KB).
5. Typed tools for hot paths so agents skip describe+run round trips.

## Verification quick sheet

```bash
python3 tests/test_mcp_server.py        # offline unit tests
bash -n scripts/gms-remote-test.sh      # CLI syntax
python3 scripts/check_source_secrets.py plugins/gms-remote-test   # from repo root
python3 -m pytest tests/test_agent_coverage.py -q                  # repo agent tests
```
