# Project Maintenance Notes

Use this when changing code in the GMS Remote Test repository.

## Frontend

- `web/shell/shell.html` contains sidebar markup and the main page containers.
- `web/static/js/navigation.js` contains page switching, most shell behavior,
  and the functions exported for inline controls.
- Some page initialization happens from both initial load and page switching. Search before adding listeners.
- Avoid repeated event listeners on repeated navigation; use `dataset.initialized`, delegated handlers, or remove previous listeners.
- Navigation-sensitive pages:
  - users should refresh via `loadUsersList()`
  - devices should refresh via `loadDevicesManagement()`
  - reports should refresh via `loadTestReports(...)`
- Drag/drop behavior often has two surfaces: the page upload zone and the sidebar navigation target. Preserve both.

## Backend

- Prefer existing response helpers from `foundation/responses.py`.
- Keep route handlers thin; put reusable logic in the matching `features/*`
  service or support module.
- Report persistence uses `features/reports/repository.py`; runtime databases
  live beneath `data/` and must not be committed.
- Long-running tasks need progress/status reporting and cleanup on failure.
- Do not assume single-user operation; check client/user filtering and device lock semantics.

## Security-Sensitive Areas

- Uploads, downloads, archive extraction, URL downloads, Redmine content, SSH, ADB shell, terminal push, firmware paths.
- Avoid arbitrary filesystem read/write via user-controlled path parameters.
- Avoid shell string construction when arguments can be passed as arrays or validated first.
- Do not log passwords, tokens, Redmine credentials, VPN credentials, SSH credentials, or config secrets.

## Verification by Change Type

| Change Type | Minimum Check |
|---|---|
| JS only | `node --check web/static/js/navigation.js` |
| Python route/service | `ruff check .` or targeted `python -m compileall features foundation worker_agent` |
| Shell script | `bash -n path/to/script.sh` |
| UI navigation | Manually verify repeated navigation and active page/sidebar state |
| Report upload/analysis | Verify original page drop and any new sidebar/drop entry |
| APK upload/analysis | Verify `.apk`/`.jar` validation, progress, and task polling reset |
| Device/ADB/SSH | Verify command quoting, selected device validation, and error path |

## Commit Hygiene

- Commit only files requested by the user.
- Leave generated runtime data, logs, pids, and local config untouched unless explicitly requested.
- Use concise English commit messages when the user asks for a brief commit.
