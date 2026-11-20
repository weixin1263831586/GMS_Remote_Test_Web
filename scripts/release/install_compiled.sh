#!/usr/bin/env bash
# Independent compiled GMS installer: verify signed archive before extraction!
set -Eeuo pipefail
umask 077
PKG="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${GMS_INSTALL_PREFIX:-/opt/gms-remote-test}"
STATE="${GMS_STATE_DIR:-/var/lib/gms-remote-test}"
ETC="${GMS_CONFIG_DIR:-/etc/gms-remote-test}"
RUN_USER="${GMS_RUN_USER:-${SUDO_USER:-gms-web}}"
HOST="${GMS_HOST_IP:-}"
PORT="${GMS_PORT:-5001}"
SERVICE="${GMS_SERVICE_NAME:-gms-web-app}"
YES=false
SKIP_ADMIN=false
OFFLINE=false
fail(){ echo "ERROR: $*" >&2; exit 1; }
usage(){ cat <<'EOF'
sudo ./install.sh [--user USER] [--host IP_OR_DNS] [--port 5001]
  [--prefix /opt/gms-remote-test] [--state-dir /var/lib/gms-remote-test]
  [--config-dir /etc/gms-remote-test] [--service-name gms-web-app]
  [--yes] [--no-admin-prompt] [--offline]
Interactive: create admin account, configure optional per-user Redmine.
EOF
}
while (($#)); do
 case "$1" in
 --help|-h) usage; exit 0;;
 --user) RUN_USER="$2"; shift 2;;
 --host) HOST="$2"; shift 2;;
 --port) PORT="$2"; shift 2;;
 --prefix) PREFIX="$2"; shift 2;;
 --state-dir) STATE="$2"; shift 2;;
 --config-dir) ETC="$2"; shift 2;;
 --service-name) SERVICE="$2"; shift 2;;
 --yes) YES=true; shift;;
 --no-admin-prompt) SKIP_ADMIN=true; shift;;
 --offline) OFFLINE=true; shift;;
 *) fail "unknown option: $1";;
 esac
done
[[ "$(id -u)" -eq 0 ]] || fail "sudo/root is required"
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || fail "Linux x86_64 required"
# shellcheck source=/dev/null
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 22.04 ]] || fail "Ubuntu 22.04 required"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((10#$PORT > 0 && 10#$PORT <= 65535)) || fail "invalid HTTPS port"
[[ "$SERVICE" =~ ^[a-z][a-z0-9_-]*$ ]] || fail "invalid service name"
for p in "$PREFIX" "$STATE" "$ETC"; do
 [[ "$p" =~ ^/[a-zA-Z0-9_./-]+$ && "$p" != / ]] || fail "unsafe absolute directory: $p"
done
[[ -x "$PKG/runtime/gms-runtime" ]] || fail "compiled binary missing"
[[ -f "$PKG/manifest.json" && -f "$PKG/manifest.sha256" ]] || fail "manifest missing"
(cd "$PKG" && sha256sum --quiet -c manifest.sha256) || fail "package integrity failure"
VERSION="$(python3 - "$PKG/manifest.json" <<'PY'
import json,re,sys
m=json.load(open(sys.argv[1],encoding="utf-8"))
v=str(m.get("version",""))
if m.get("format")!="gms-compiled-v1" or not re.fullmatch(r"[a-zA-Z0-9._-]+",v):
    raise SystemExit("invalid signed release metadata")
print(v)
PY
)"
command -v systemctl >/dev/null || fail "systemd missing"
command -v runuser >/dev/null || fail "runuser missing"
command -v flock >/dev/null || fail "flock missing"
# Offline installations require preinstalled packages; otherwise apt gets
# explicit control of operating-system dependencies (never curl | bash).
packages=(ca-certificates curl openssl rsync openssh-client openssh-server iproute2
          libudev1 libusb-1.0-0 x11vnc novnc websockify)
if ! $OFFLINE; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}"
else
  for p in "${packages[@]}"; do
    dpkg-query -W --showformat='${Status}' "$p" 2>/dev/null | grep -qx 'install ok installed' \
      || fail "offline prerequisite missing: $p"
  done
fi
[[ -n "$HOST" ]] || HOST="$(hostname -I | awk '{print $1}')"
if ! $YES; then
 read -r -p "HTTPS host [$HOST]: " v; HOST="${v:-$HOST}"
 read -r -p "HTTPS port [$PORT]: " v; PORT="${v:-$PORT}"
 read -r -p "Service user [$RUN_USER]: " v; RUN_USER="${v:-$RUN_USER}"
fi
[[ "$HOST" =~ ^[a-zA-Z0-9._-]+$ ]] || fail "invalid hostname"
[[ "$PORT" =~ ^[0-9]+$ ]] && ((10#$PORT > 0 && 10#$PORT <= 65535)) || fail "invalid port"
[[ "$RUN_USER" =~ ^[a-z_][a-z0-9_-]*$?$ ]] || fail "invalid user"
if ! id "$RUN_USER" >/dev/null 2>&1; then
 [[ "$RUN_USER" == gms-web ]] || fail "user $RUN_USER does not exist"
 useradd --system --create-home --home-dir /var/lib/gms-web --shell /usr/sbin/nologin gms-web
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -d "$RUN_HOME" ]] || fail "service home missing"
exec 8>/tmp/gms-compiled-installer.lock
flock -n 8 || fail "installer already running"
if ! $YES; then
 read -r -p "Install compiled GMS $VERSION? [y/N] " v
 [[ "$v" =~ ^[Yy]$ ]] || fail "cancelled"
fi

install -d -m 0755 "$PREFIX" "$PREFIX/releases"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$STATE" "$ETC" "$ETC/configs" "$ETC/configs/local" "$ETC/configs/secrets" "$STATE/local-worker"
install -d -m 0755 "$ETC/configs/examples"
cp -an "$PKG/runtime/configs/examples/." "$ETC/configs/examples/"
TARGET="$PREFIX/releases/$VERSION"
if [[ -e "$TARGET" ]]; then
 [[ -f "$TARGET/release-manifest.json" ]] || fail "existing version has no immutable manifest"
 cmp -s "$PKG/manifest.json" "$TARGET/release-manifest.json" \
   || fail "version $VERSION already installed with different content"
else
 STAGE="$PREFIX/releases/.$VERSION.install.$$"
 trap '[[ -z "${STAGE:-}" ]] || rm -rf -- "$STAGE"' EXIT
 install -d -m 0755 "$STAGE"
 cp -a "$PKG/runtime" "$STAGE/runtime"
 cp "$PKG/manifest.json" "$STAGE/release-manifest.json"
 rm -rf "$STAGE/runtime/configs"
 ln -s "$ETC/configs" "$STAGE/runtime/configs"
 ln -s "$STATE" "$STAGE/runtime/data"
 mv -T "$STAGE" "$TARGET"
 STAGE=""
fi
[[ -x "$TARGET/runtime/gms-runtime" ]] || fail "installed binary missing"
[[ "$(readlink -f "$TARGET/runtime/configs")" == "$(readlink -f "$ETC/configs")" ]] || fail "config link mismatch"
[[ "$(readlink -f "$TARGET/runtime/data")" == "$(readlink -f "$STATE")" ]] || fail "data link mismatch"

ARGS=(init --host "$HOST" --port "$PORT")
$SKIP_ADMIN && ARGS+=(--no-admin-prompt)
runuser -u "$RUN_USER" -- env "GMS_PROJECT_ROOT=$TARGET/runtime" "GMS_DATA_ROOT=$STATE" "$TARGET/runtime/gms-runtime" "${ARGS[@]}"

OLD="$(readlink "$PREFIX/current" 2>/dev/null || true)"
NEXT="$PREFIX/.current-next.$$"
ln -s "$TARGET/runtime" "$NEXT"
mv -Tf "$NEXT" "$PREFIX/current"

cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=GMS compiled Controller
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$PREFIX/current
Environment=GMS_ENV=production
Environment=GMS_PROJECT_ROOT=$PREFIX/current
Environment=GMS_DATA_ROOT=$STATE
ExecStart=$PREFIX/current/gms-runtime controller
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$STATE $ETC
LimitNOFILE=65536
[Install]
WantedBy=multi-user.target
EOF
cat > "/etc/systemd/system/$SERVICE-local-worker.service" <<EOF
[Unit]
Description=GMS compiled local Worker
Requires=$SERVICE.service
After=$SERVICE.service
[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$PREFIX/current
Environment=GMS_ENV=production
Environment=GMS_PROJECT_ROOT=$PREFIX/current
Environment=GMS_DATA_ROOT=$STATE
Environment=GMS_WORKER_CONFIG=$STATE/local-worker/config.json
ExecStart=$PREFIX/current/gms-runtime worker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$STATE $ETC $RUN_HOME/GMS-Suite
[Install]
WantedBy=multi-user.target
EOF
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0700 "$STATE/backups"
cat > "/etc/systemd/system/$SERVICE-backup.service" <<EOF
[Unit]
Description=GMS compiled encrypted backup
[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$PREFIX/current
Environment=GMS_PROJECT_ROOT=$PREFIX/current
Environment=GMS_DATA_ROOT=$STATE
ExecStart=$PREFIX/current/gms-runtime backup create --project-root $PREFIX/current --key-file $ETC/configs/secrets/backup.key --output-dir $STATE/backups --keep 14
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$STATE $ETC
EOF
cat > "/etc/systemd/system/$SERVICE-backup.timer" <<EOF
[Unit]
Description=GMS encrypted daily backup
[Timer]
OnCalendar=*-*-* 02:30:00
RandomizedDelaySec=45min
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE.service" "$SERVICE-local-worker.service" "$SERVICE-backup.timer" >/dev/null
if ! systemctl restart "$SERVICE.service" || ! systemctl is-active --quiet "$SERVICE.service" ||
   ! curl --silent --show-error --fail --insecure --retry 8 --retry-delay 1 --retry-connrefused \
      --connect-timeout 2 --max-time 5 "https://127.0.0.1:$PORT/api/auth/status" >/dev/null; then
 if [[ -n "$OLD" ]]; then
   ln -s "$OLD" "$NEXT"; mv -Tf "$NEXT" "$PREFIX/current"
   systemctl restart "$SERVICE.service" || true
 fi
 fail "Controller failed; old runtime restored if available"
fi
systemctl restart "$SERVICE-local-worker.service" || echo "Local Worker pending host tools"
systemctl start "$SERVICE-backup.timer"
echo "Installed $VERSION: https://$HOST:$PORT"
echo "Data: $STATE ; config: $ETC/configs"
echo "Logs: journalctl -u $SERVICE.service -f"
