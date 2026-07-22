#!/usr/bin/env bash
set -Eeuo pipefail

: "${GMS_PROJECT_ROOT:?GMS_PROJECT_ROOT is required}"
: "${GMS_RUN_HOME:?GMS_RUN_HOME is required}"
: "${GMS_BACKUP_DIR:?GMS_BACKUP_DIR is required}"
: "${GMS_BACKUP_KEY_FILE:?GMS_BACKUP_KEY_FILE is required}"
: "${GMS_CONTROLLER_SERVICE:?GMS_CONTROLLER_SERVICE is required}"
: "${GMS_LOCAL_WORKER_SERVICE:?GMS_LOCAL_WORKER_SERVICE is required}"

keep="${GMS_BACKUP_KEEP:-14}"
[[ "${keep}" =~ ^[1-9][0-9]*$ ]] || {
    echo "GMS_BACKUP_KEEP must be a positive integer" >&2
    exit 2
}

controller_was_active=false
worker_was_active=false
systemctl is-active --quiet "${GMS_CONTROLLER_SERVICE}" && controller_was_active=true
systemctl is-active --quiet "${GMS_LOCAL_WORKER_SERVICE}" && worker_was_active=true

restart_services() {
    if [[ "${controller_was_active}" == "true" ]]; then
        systemctl start "${GMS_CONTROLLER_SERVICE}" || true
    fi
    if [[ "${worker_was_active}" == "true" ]]; then
        systemctl start "${GMS_LOCAL_WORKER_SERVICE}" || true
    fi
}
trap restart_services EXIT

if [[ "${worker_was_active}" == "true" ]]; then
    systemctl stop "${GMS_LOCAL_WORKER_SERVICE}"
fi
if [[ "${controller_was_active}" == "true" ]]; then
    systemctl stop "${GMS_CONTROLLER_SERVICE}"
fi

"${GMS_PROJECT_ROOT}/.venv/bin/python" \
    "${GMS_PROJECT_ROOT}/scripts/gms_backup.py" create \
    --project-root "${GMS_PROJECT_ROOT}" \
    --run-home "${GMS_RUN_HOME}" \
    --output-dir "${GMS_BACKUP_DIR}" \
    --key-file "${GMS_BACKUP_KEY_FILE}" \
    --keep "${keep}"
