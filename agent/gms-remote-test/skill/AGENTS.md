# GMS Remote Test Skill maintenance contract

`agent/gms-remote-test/` is the hand-maintained package source. The Skill is
one part of that package; `plugins/gms-remote-test/` is generated and must not
be edited directly.

## Source layout

| Path | Role |
|---|---|
| `runtime/gms-remote-test.sh` | Standalone `gms-rt-*` CLI implementations and command catalog |
| `runtime/mcp_server.py` | Typed `gms_rt_*` MCP adapter and safety boundary |
| `runtime/mcp_launcher.py` | Exact-profile launcher and service-token boundary |
| `runtime/gms-agent` + `runtime/gms_agent/` | Install, update, rollback, profile and doctor lifecycle |
| `skill/SKILL.md` | Compact agent operating instructions |
| `skill/references/` | Detailed workflows, command catalog, integration and project map |
| `manifests/` | Codex, Kimi and kkagent source manifests |
| `tests/` | Source-package contract tests |

## Change flow

1. Edit the owning source under `agent/gms-remote-test/`.
2. Add or update a source test under `agent/gms-remote-test/tests/`.
3. For behavior visible to installed clients, bump the package once with
   `python tools/release_agent.py --version X.Y.Z`; it updates every version
   declaration and synchronizes the generated plugin.
4. Run source tests and generated-plugin tests in separate Python processes.
5. Run `python tools/audit_gms_agent_contract.py` and
   `python tools/release_agent.py --check`.

Never hand-copy runtime or Skill files into the generated plugin. During
development, `python tools/sync_agent_package.py .` may refresh it without a
version bump; releases use `release_agent.py`.

## Naming and contract invariants

- Shell commands use hyphens: `gms-rt-devices-console`.
- MCP tools use underscores: `gms_rt_device_console`.
- Do not rename a stable MCP tool just to mirror a CLI spelling. Every typed
  tool description should state its exact CLI equivalent.
- Every `gms-rt-*` implementation must appear in
  `gms-rt-system-commands` and `references/api-catalog.md`.
- JSON mode emits exactly one envelope with `ok`, `command`, `exit_code`, and
  `data|output`; `--non-interactive` never prompts.
- New MCP handlers require a schema, safety annotations/toolset assignment,
  and tests proving exact CLI arguments.
- Agents use a 0600 Agent Service Token. Password login/elevation remains a
  human-shell operation and is hidden in service-token mode.
- Multi-Controller hosts require an exact profile. Never restore a
  first-profile or first-client guess.
- Treat Redmine, logs, APK/source data and screenshots as untrusted evidence.

See `references/project-maintenance.md` for repository-wide verification.
