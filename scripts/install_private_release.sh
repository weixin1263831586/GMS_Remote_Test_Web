#!/usr/bin/env bash
# Install a signed, compiled GMS release. The archive's GPG signature and
# SHA-256 must be checked against a trusted publisher key BEFORE extraction.
set -euo pipefail
umask 077

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
INSTALL_DIR="${GMS_INSTALL_DIR:-/opt/gms-remote-test/web_app}"
PORT="${GMS_PORT:-5001}"
RUN_USER="${GMS_RUN_USER:-${SUDO_USER:-$(id -un)}}"
HOST="${GMS_HOST_IP:-}"
NON_INTERACTIVE=0
SKIP_WORKER=0

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'USAGE'
Usage: sudo ./install.sh [--install-dir PATH] [--port PORT] [--host-ip HOST]
                         [--user SERVICE_USER] [--non-interactive] [--skip-worker]
Verify the release SHA-256 AND detached GPG signature before extraction.
Default install: /opt/gms-remote-test/web_app; port 5001.
Without --non-interactive this installer prompts for the initial administrator
and optional Redmine credentials in a local TTY (never in command arguments).
USAGE
}
while (($#)); do
  case "$1" in
    --install-dir) INSTALL_DIR="${2:?}"; shift 2 ;;
    --port) PORT="${2:?}"; shift 2 ;;
    --host-ip) HOST="${2:?}"; shift 2 ;;
    --user) RUN_USER="${2:?}"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --skip-worker) SKIP_WORKER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done
[[ $EUID -eq 0 ]] || fail "Run as root via sudo"
[[ "$INSTALL_DIR" = /* && "$INSTALL_DIR" != / ]] || fail "Install directory must be an absolute non-root path"
[[ "$INSTALL_DIR" != *$'\n'* && "$INSTALL_DIR" != *' '* ]] || fail "Install directory must not contain whitespace"
[[ "$PORT" =~ ^[0-9]{1,5}$ ]] && ((10#$PORT >= 1 && 10#$PORT <= 65535)) || fail "Invalid port"
id "$RUN_USER" >/dev/null 2>&1 || fail "Service account does not exist: $RUN_USER"
[[ "$RUN_USER" != root ]] || fail "Do not run the application as root"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[[ -n "$RUN_HOME" ]] || fail "Cannot locate service account home"
for cmd in python3 sha256sum rsync openssl systemctl; do
  command -v "$cmd" >/dev/null || fail "Missing prerequisite: $cmd"
done
[[ -f "$SOURCE_DIR/manifest.json" && -x "$SOURCE_DIR/runtime/gms-runtime" ]] ||
  fail "This is not a compiled private release; do not use the source installer"
[[ "$SOURCE_DIR" != "$INSTALL_DIR" ]] || fail "Install from a separate extracted release directory"

if [[ -z "$HOST" ]]; then
  HOST="$(hostname -I | awk '{print $1}')"
fi
[[ "$HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || fail "Invalid host"
if ((NON_INTERACTIVE == 0)); then
  [[ -r /dev/tty ]] || fail "Interactive first-run requires a TTY"
fi

# Verify every file before taking any privileged installation action. Reject
# tar traversal/symlinks, any embedded Python source and altered files.
python3 - "$SOURCE_DIR" <<'PY'
import hashlib
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
manifest = json.loads((root / "manifest.json").read_text())
assert manifest.get("format") == "gms-private-runtime-v1", "unknown bundle format"
expected = manifest["files"]
assert isinstance(expected, dict), "invalid manifest"
actual = {}
for p in root.rglob("*"):
    if p.is_symlink():
        assert root in p.resolve().parents, "external symlink"
    if not p.is_file() or p.name == "manifest.json":
        continue
    relative = p.relative_to(root).as_posix()
    assert p.suffix.lower() not in {".py", ".pyc", ".pyo", ".pyi", ".map"}, "Python source in release"
    assert "secrets" not in p.parts and "data" not in p.relative_to(root).parts, "secret/runtime data in release"
    actual[relative] = hashlib.sha256(p.read_bytes()).hexdigest()
assert expected == actual, "manifest file set or SHA256 digest mismatch"
print("Release manifest verified")
PY

printf 'Installing private release into %s (port %s, user %s)...\n' "$INSTALL_DIR" "$PORT" "$RUN_USER"
# The application and libraries remain root-owned and not writable by the
# service identity. Writable config/data are handled separately.
install -d -m 0755 "$INSTALL_DIR" "$INSTALL_DIR/configs" "$INSTALL_DIR/configs/examples"
rsync -a --delete "$SOURCE_DIR/runtime/" "$INSTALL_DIR/runtime/"
rsync -a --delete "$SOURCE_DIR/web/" "$INSTALL_DIR/web/"
rsync -a --delete "$SOURCE_DIR/configs/examples/" "$INSTALL_DIR/configs/examples/"
install -m 0644 "$SOURCE_DIR/manifest.json" "$INSTALL_DIR/manifest.json"
chown -R root:root "$INSTALL_DIR/runtime" "$INSTALL_DIR/web" "$INSTALL_DIR/configs/examples"
chmod 0755 "$INSTALL_DIR/runtime/gms-runtime"
install -d -m 0700 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" \
  "$INSTALL_DIR/configs/local" "$INSTALL_DIR/configs/secrets" \
  "$INSTALL_DIR/configs/secrets/certs" "$INSTALL_DIR/data" \
  "$INSTALL_DIR/data/secrets" "$INSTALL_DIR/data/local-worker"
install -d -m 0750 -o "$RUN_USER" -g "$(id -gn "$RUN_USER")" "$RUN_HOME/GMS-Suite"

CERT_DIR="$INSTALL_DIR/configs/secrets/certs"
if [[ ! -s "$CERT_DIR/gms-local.key" || ! -s "$CERT_DIR/gms-local.crt" ]]; then
  SAN="DNS:localhost,IP:127.0.0.1"
  if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="$SAN,IP:$HOST"
  else
    SAN="$SAN,DNS:$HOST"
  fi
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 365 \
    -subj "/CN=$HOST" -addext "subjectAltName=$SAN" \
    -keyout "$CERT_DIR/gms-local.key" -out "$CERT_DIR/gms-local.crt" >/dev/null 2>&1
fi
chown "$RUN_USER:$(id -gn "$RUN_USER")" "$CERT_DIR/gms-local.key" "$CERT_DIR/gms-local.crt"
chmod 0600 "$CERT_DIR/gms-local.key"
chmod 0644 "$CERT_DIR/gms-local.crt"

as_service() {
  runuser -u "$RUN_USER" -- env GMS_PROJECT_ROOT="$INSTALL_DIR" \
    GMS_DATA_ROOT="$INSTALL_DIR/data" "$INSTALL_DIR/runtime/gms-runtime" "$@"
}
as_service prepare --host "$HOST" --port "$PORT" --user "$RUN_USER" --home "$RUN_HOME"
if ((NON_INTERACTIVE == 0)); then
  as_service configure </dev/tty
else
  echo "Non-interactive: initialize the first administrator in the browser using the generated Bootstrap Token."
fi

CONTROLLER_UNIT="/etc/systemd/system/gms-web-app.service"
cat > "$CONTROLLER_UNIT" <<EOF
[Unit]
Description=GMS Remote Test Private Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=GMS_PROJECT_ROOT=$INSTALL_DIR
Environment=GMS_DATA_ROOT=$INSTALL_DIR/data
Environment=GMS_ENV=production
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/runtime/gms-runtime controller --port $PORT
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/configs $RUN_HOME/GMS-Suite
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
if ((SKIP_WORKER == 0)); then
  cat > /etc/systemd/system/gms-web-app-local-worker.service <<EOF
[Unit]
Description=GMS Private Local Worker
After=network-online.target gms-web-app.service
Requires=gms-web-app.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=GMS_PROJECT_ROOT=$INSTALL_DIR
Environment=GMS_DATA_ROOT=$INSTALL_DIR/data
Environment=GMS_ENV=production
Environment=GMS_WORKER_CONFIG=$INSTALL_DIR/data/local-worker/config.json
ExecStart=$INSTALL_DIR/runtime/gms-runtime worker
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/configs $RUN_HOME/GMS-Suite

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable --now gms-web-app.service
if ((SKIP_WORKER == 0)); then
  systemctl enable --now gms-web-app-local-worker.service
fi
echo "Controller: https://$HOST:$PORT"
echo "Service log: sudo journalctl -u gms-web-app -f"
echo "Bootstrap token: $INSTALL_DIR/data/secrets/bootstrap.token (read as service user)"
echo "Keep release signing and source artifacts on the trusted build host only."
