#!/usr/bin/env bash
# Run by the operator with sudo. Passwords never enter the migration process.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo 'Run this maintenance command with sudo in your own terminal.' >&2
    exit 1
fi

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
controller="${GMS_SERVICE_NAME:-gms-web-app.service}"
worker="${GMS_LOCAL_WORKER_SERVICE:-gms-worker-agent.service}"
[[ "${controller}" =~ ^[A-Za-z0-9_.@-]+$ && "${worker}" =~ ^[A-Za-z0-9_.@-]+$ ]] || exit 2
service_root="$(systemctl show "${controller}" -p WorkingDirectory --value)"
[[ "${service_root}" == "${project_root}" ]] || {
    echo 'The Controller does not use this checkout; no service was stopped.' >&2
    exit 2
}
operator="$(systemctl show "${controller}" -p User --value)"
[[ -n "${operator}" && "${operator}" != root ]] || {
    echo 'A non-root Controller service user is required.' >&2
    exit 2
}
operator_uid="$(id -u "${operator}")"
python_bin="${project_root}/.venv/bin/python"
[[ -x "${python_bin}" ]] || python_bin="$(command -v python3)"

as_operator() {
    runuser -u "${operator}" -- env XDG_RUNTIME_DIR="/run/user/${operator_uid}" "$@"
}

# Refuse an interruption of durable jobs before stopping either service.
as_operator "${python_bin}" - "${project_root}" <<'PY'
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
checks = (
    ('cluster/cluster.sqlite3', 'cluster_jobs', ('completed', 'failed', 'cancelled')),
    ('build/build.sqlite3', 'build_jobs', ('completed', 'failed', 'cancelled')),
    ('automation/automation.sqlite3', 'automation_runs', (
        'completed', 'cancelled', 'failed', 'jenkins_failed', 'artifact_missing',
        'flash_failed', 'test_failed', 'analysis_failed', 'reporting_failed',
    )),
)
for name, table, terminal in checks:
    path = root / 'data' / name
    if not path.is_file():
        continue
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        if not connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
            continue
        statuses = connection.execute(f'SELECT status FROM {table}').fetchall()
        if any(status not in terminal for (status,) in statuses):
            raise SystemExit('Unfinished jobs exist; no service was stopped.')
PY

controller_was_active=false
worker_was_active=false
systemctl is-active --quiet "${controller}" && controller_was_active=true
as_operator systemctl --user is-active --quiet "${worker}" && worker_was_active=true

restore_services() {
    local result=$?
    trap - EXIT
    if [[ "${controller_was_active}" == true ]]; then
        systemctl start "${controller}" || result=1
    fi
    if [[ "${worker_was_active}" == true ]]; then
        as_operator systemctl --user start "${worker}" || result=1
    fi
    exit "${result}"
}
trap restore_services EXIT

if [[ "${worker_was_active}" == true ]]; then
    as_operator systemctl --user stop "${worker}"
fi
if [[ "${controller_was_active}" == true ]]; then
    systemctl stop "${controller}"
fi
as_operator "${python_bin}" "${project_root}/scripts/migrate_config_layout.py" --apply
# The EXIT handler restores precisely the services that were active on entry.
