# skills/gms-remote-test — agent maintenance contract

This skill is the **source of truth** for the `gms-rt` CLI and the agent
integration documentation. The kkagent plugin in `plugins/gms-remote-test`
bundles a copy of the CLI; changes there are downstream.

## Layout

| Path | Role |
|---|---|
| `SKILL.md` | Agent-facing operating manual. Keep claims verified against the current checkout. |
| `scripts/gms-remote-test.sh` | The CLI. Single source; the plugin copy and installed `~/.local/bin/gms-rt-*` links derive from it. |
| `scripts/install.sh` | Remote-host installer (Controller-served). |
| `references/api-catalog.md` | Command catalog tables. |
| `references/agent-integration.md` | CLI-in-agent bootstrap for Codex/Claude/Kimi. |
| `references/agent-workflows.md` | Battle-tested workflows, error-recovery recipes, elevation matrix. |
| `agents/openai.yaml` | Codex-style interface hints. |
| `references/project-maintenance.md` | Repository-maintenance playbook. |

Note: there is deliberately no `agents/kkagent.yaml`. kkagent's Skill
parser does not consume per-agent YAML metadata; a file that looks
authoritative but is never read is worse than none (10.txt §十一). The
kkagent prompt path is the plugin manifest's `interface` block.

## Change flow

1. Edit `scripts/gms-remote-test.sh` (or docs) here.
2. `bash -n scripts/gms-remote-test.sh`
3. Downstream sync: `plugins/gms-remote-test/scripts/sync_package.sh`
4. Plugin tests: `python3 plugins/gms-remote-test/tests/test_mcp_server.py
   && python3 plugins/gms-remote-test/tests/test_packaging.py`
5. Bump `GMS_RT_VERSION` in the CLI when command behavior changes, and the
   three manifest versions (kk/kimi/.codex-plugin) together when
   plugin-visible behavior changes.

## CLI invariants (preserve)

- Exactly one JSON envelope on stdout in `--json` mode: `{ok, command,
  exit_code, data|output, diagnostics?}`; exit codes 0/2/3/4/5/6/7 with the
  documented meanings. Error paths must surface the server error body
  (see `jobs-wait` 404 handling) instead of an empty envelope.
- `--non-interactive` never prompts; passwords only via `--password-stdin`
  or `GMS_REMOTE_TEST_PASSWORD` in controlled **human** environments —
  never in agent context (agents use the Agent Service Token).
- Human-facing formatting (tables, emoji) is terminal-only; the `--json`
  path emits machine data (`reports-list`, `test-suites` follow this).
- Shell tooling must tolerate response-shape variance, not fail a success
  path on formatting (see `ssh-ping` route_commands and
  `test-suites-result` grep handling).
- The command catalog (`gms-rt-system-commands`) is the safety source for
  the plugin gate; new commands must set sensible mode/elevation regexes.

## Doc invariants

- SKILL.md examples must run against the current checkout; stale endpoint
  counts or fields get removed.
- agent-workflows.md records only flows verified end to end (stub or real
  Controller); annotate the verification context when adding flows.
