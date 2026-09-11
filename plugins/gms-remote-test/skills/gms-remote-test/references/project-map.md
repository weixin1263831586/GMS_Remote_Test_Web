# GMS Remote Test Project Map

Use this map to locate the owning layer before changing behavior. Search the
current checkout for the exact route/function because filenames and response
fields may evolve.

## Ownership

| Area | Source of truth | Primary verification |
|---|---|---|
| Agent package/version/manifests | `agent/gms-remote-test/` | `tools/release_agent.py --check` |
| CLI command contract | `agent/gms-remote-test/runtime/gms-remote-test.sh` | `features/system/tests/test_skill_cli.py` |
| MCP adapter and tool schemas | `agent/gms-remote-test/runtime/mcp_server.py` | `agent/gms-remote-test/tests/test_mcp_server.py` |
| Installer/profile lifecycle | `agent/gms-remote-test/runtime/gms-agent`, `runtime/gms_agent/package_manager.py`, `runtime/mcp_launcher.py` | `agent/gms-remote-test/tests/test_installer_lifecycle.py` |
| Generated plugin payload | `plugins/gms-remote-test/` | generate with `tools/sync_agent_package.py`; never hand-edit |
| Authentication/authorization | `features/auth/` | matching `features/auth/tests` and caller-domain tests |
| Devices/console | `features/devices/` | `features/devices/tests` |
| Cluster workers/jobs | `features/cluster/` | matching cluster tests |
| Test execution | `features/test_execution/` | matching execution and CLI contract tests |
| Reports | `features/reports/` | matching report tests |
| Firmware | `features/firmware/` | matching firmware tests; verify approval/elevation paths |
| Redmine/APK/SDK evidence | `features/redmine/`, APK/system routes and source providers | domain tests plus MCP evidence tests |
| Shared configuration/security | `foundation/`, `bootstrap/` | targeted unit/contract tests |
| Web shell/navigation | `web/` | JS syntax plus repeated-navigation checks |

## Change tracing

For an API behavior change, trace this chain before editing:

```text
gms_rt_* MCP tool
  -> gms-rt-* CLI function
  -> /api/... FastAPI route
  -> feature service/repository
  -> persisted job/report/device state
```

Confirm authentication, scope/elevation, ownership/Worker targeting, error
shape, and timeout behavior at every boundary. Keep route handlers thin and
put reusable behavior in the feature service.

## Agent package workflow

```bash
bash -n agent/gms-remote-test/runtime/gms-remote-test.sh
python3 -m pytest agent/gms-remote-test/tests -q
python tools/sync_agent_package.py .
python3 -m pytest plugins/gms-remote-test/tests -q
python tools/release_agent.py --check
```

Run source and generated-plugin tests in separate pytest processes because
they intentionally contain modules with identical names.

## Local state that is not source

Do not commit or casually rewrite `configs/*.json`, `configs/certs/`,
`data/`, runtime databases, tokens, cookie jars, logs, PID files, installed
profiles, or user MCP configuration. Tests must redirect HOME/XDG state and
all module-level profile/runtime roots into a temporary directory.
