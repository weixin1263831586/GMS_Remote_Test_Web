#!/usr/bin/env bash
# Install the Redmine Daily Brief systemd units for this checkout.
#
# Run from any directory:
#   ./tools/scripts/deployment/install_daily_brief_systemd.sh
#
# The script intentionally invokes sudo interactively and never accepts a
# password through an argument, environment variable, or standard input.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPO_DIR="$(cd -- "${SCRIPT_DIR}/../../.." && pwd -P)"
readonly UNIT_SOURCE_DIR="${REPO_DIR}/deploy/systemd"
readonly WEB_UNIT="gms-web-app.service"
readonly WORKER_UNIT="gms-redmine-daily-brief-worker.service"
readonly NIGHTLY_TIMER="gms-redmine-daily-brief.timer"
readonly DELTA_TIMER="gms-redmine-daily-brief-delta.timer"

readonly UNIT_FILES=(
  "gms-redmine-daily-brief-worker.service"
  "gms-redmine-daily-brief.service"
  "gms-redmine-daily-brief-delta.service"
  "${NIGHTLY_TIMER}"
  "${DELTA_TIMER}"
)

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

escape_sed_replacement() {
  # Escape the three characters meaningful in a |...| sed replacement.
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

[[ -d "${UNIT_SOURCE_DIR}" ]] || die "systemd template directory missing: ${UNIT_SOURCE_DIR}"
for unit in "${UNIT_FILES[@]}"; do
  [[ -f "${UNIT_SOURCE_DIR}/${unit}" ]] || die "systemd template missing: ${unit}"
done

require_command systemctl
require_command sudo
require_command sed
require_command grep
require_command python3

[[ "$(uname -s)" == "Linux" ]] || die "this installer requires Linux systemd"
systemctl show "${WEB_UNIT}" --property=LoadState --value 2>/dev/null | grep -qx 'loaded' \
  || die "${WEB_UNIT} is not installed; install and start the Web service first"

service_user="$(systemctl show "${WEB_UNIT}" --property=User --value)"
service_group="$(systemctl show "${WEB_UNIT}" --property=Group --value)"
working_directory="$(systemctl show "${WEB_UNIT}" --property=WorkingDirectory --value)"

[[ -n "${service_user}" ]] || die "${WEB_UNIT} has no configured User"
[[ -n "${service_group}" ]] || service_group="${service_user}"
[[ -n "${working_directory}" ]] || working_directory="${REPO_DIR}"
[[ "${working_directory}" == "${REPO_DIR}" ]] \
  || die "${WEB_UNIT} WorkingDirectory (${working_directory}) does not match this checkout (${REPO_DIR})"
[[ -x /usr/bin/python3 ]] || die "expected Python interpreter is unavailable: /usr/bin/python3"
service_home="$(python3 -c 'import pwd, sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' "${service_user}")"
[[ -d "${service_home}" ]] || die "home directory for ${service_user} is unavailable: ${service_home}"
# systemd does not load the user's shell profile.  Include the usual
# user-local binary directory, where kkagent is installed by its installer.
agent_path="${service_home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

printf 'Installing Daily Brief units for %s (user: %s, group: %s)\n' \
  "${REPO_DIR}" "${service_user}" "${service_group}"
printf 'Worker PATH includes %s\n' "${service_home}/.local/bin"
printf 'sudo may prompt in this terminal; never enter a password into this script as an argument.\n'
sudo -v

staging_dir="$(mktemp -d "${TMPDIR:-/tmp}/gms-daily-brief.XXXXXX")"
cleanup() {
  rm -rf -- "${staging_dir}"
}
trap cleanup EXIT

repo_escaped="$(escape_sed_replacement "${REPO_DIR}")"
user_escaped="$(escape_sed_replacement "${service_user}")"
group_escaped="$(escape_sed_replacement "${service_group}")"

for unit in "${UNIT_FILES[@]}"; do
  sed \
    -e "s|/opt/gms-remote-test|${repo_escaped}|g" \
    -e "s|^User=gms$|User=${user_escaped}|" \
    -e "s|^Group=gms$|Group=${group_escaped}|" \
    -e "s|^ExecStart=${repo_escaped}/\\.venv/bin/python |ExecStart=/usr/bin/python3 |" \
    "${UNIT_SOURCE_DIR}/${unit}" > "${staging_dir}/${unit}"
done

# This deployment runs the Web application under gms-web-app.service rather
# than the generic gms-remote-test.service expected by the packaged template.
sed -i "s|gms-remote-test\\.service|${WEB_UNIT}|" \
  "${staging_dir}/${WORKER_UNIT}"
# Keep kkagent discoverable when the Worker is launched by systemd rather
# than an interactive shell.  The other units only enqueue work; they do not
# execute kkagent themselves.
sed -i "/^\\[Service\\]$/aEnvironment=PATH=${agent_path}" \
  "${staging_dir}/${WORKER_UNIT}"

sudo install -o root -g root -m 0644 \
  "${staging_dir}/gms-redmine-daily-brief-worker.service" \
  "${staging_dir}/gms-redmine-daily-brief.service" \
  "${staging_dir}/gms-redmine-daily-brief-delta.service" \
  "${staging_dir}/${NIGHTLY_TIMER}" \
  "${staging_dir}/${DELTA_TIMER}" \
  /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable "${WORKER_UNIT}"
sudo systemctl restart "${WORKER_UNIT}"
sudo systemctl enable "${NIGHTLY_TIMER}"
sudo systemctl restart "${NIGHTLY_TIMER}"
sudo systemctl enable "${DELTA_TIMER}"
sudo systemctl restart "${DELTA_TIMER}"

if ! sudo systemctl is-active --quiet "${WORKER_UNIT}"; then
  sudo systemctl status --no-pager --full "${WORKER_UNIT}" || true
  die "Daily Brief Worker did not reach active state"
fi

printf '\nDaily Brief Worker is active. Scheduled runs:\n'
sudo systemctl list-timers --all --no-pager \
  "${NIGHTLY_TIMER}" "${DELTA_TIMER}"

printf '\nReadiness diagnostic (a failure here means the AI/Agent configuration still needs attention):\n'
if ! sudo -u "${service_user}" -- \
  env "PATH=${agent_path}" /usr/bin/python3 -m features.redmine.daily_brief_cli doctor; then
  printf 'warning: deployment succeeded, but the readiness diagnostic reported a problem.\n' >&2
  printf 'See: journalctl -u %s -n 100 --no-pager\n' "${WORKER_UNIT}" >&2
  exit 2
fi

printf '\nDeployment and readiness diagnostic completed successfully.\n'
