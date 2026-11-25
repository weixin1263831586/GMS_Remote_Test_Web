#!/usr/bin/env bash
# Manual deployment uses the same authenticated Controller-side bundle builder.
set -Eeuo pipefail
umask 077

if [[ $# -ne 5 ]]; then
    echo "Usage: $0 WORKER_ID USER@HOST[:PORT] CONTROLLER_URL SUITE_ROOT MAX_JOBS" >&2
    exit 2
fi
worker_id="$1"
connection="$2"
controller="$3"
suite_root="$4"
max_jobs="$5"
[[ "$worker_id" =~ ^[a-zA-Z0-9._-]+$ && "$controller" == https://* && "$max_jobs" =~ ^[0-9]+$ ]] || exit 2
[[ "$connection" == *@* ]] || exit 2
user="${connection%%@*}"
host="${connection#*@}"
port=22
ipv6_host_pattern='^\[([^]]+)\](:([0-9]+))?
    port="${BASH_REMATCH[3]:-22}"
    host="${BASH_REMATCH[1]}"
elif [[ "$host" =~ ^([^:]+):([0-9]+)$ ]]; then
    port="${BASH_REMATCH[2]}"
    host="${BASH_REMATCH[1]}"
fi
[[ "$user" =~ ^[a-zA-Z0-9._-]+$ && "$host" =~ ^[a-zA-Z0-9.:_-]+$ && "$host" != -* ]] || exit 2
[[ "$port" =~ ^[0-9]{1,5}$ ]] && [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || exit 2
target="$user@$host"
scp_target="$target"
[[ "$host" != *:* ]] || scp_target="$user@[$host]"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "$root"
credential="${GMS_GTS_CREDENTIAL_FILE:-}"
[[ -f "$credential" ]] || { echo "Set GMS_GTS_CREDENTIAL_FILE to the GTS service account file" >&2; exit 2; }
work="$(mktemp -d)"
remote=""
cleanup() {
    rm -rf -- "$work"
    if [[ -n "$remote" ]]; then
        ssh -p "$port" -- "$target" "rm -rf -- '$remote'" || true
    fi
}
trap cleanup EXIT
read -rsp "Worker Token: " token
printf '\n'
[[ -n "$token" ]] || exit 2
printf '%s\n' "$token" > "$work/token"
unset token
if [[ -x "$root/gms-runtime" ]]; then
    GMS_PROJECT_ROOT="$root" "$root/gms-runtime" worker-bundle "$work/bundle.tar.gz"
else
    python3 -c 'import sys; from pathlib import Path; from bootstrap.env_loader import load_runtime_env; load_runtime_env(); from features.cluster.deployment.api import build_worker_bundle; build_worker_bundle(Path.cwd(), Path(sys.argv[1]))' "$work/bundle.tar.gz"
fi
remote="$(ssh -p "$port" -- "$target" 'umask 077; mktemp -d /tmp/gms-worker-deploy.XXXXXXXX')"
[[ "$remote" =~ ^/tmp/gms-worker-deploy\.[a-zA-Z0-9]+$ ]] || { remote=""; exit 1; }
install -m 600 "$credential" "$work/gts.json"
scp -P "$port" -- "$work/bundle.tar.gz" "$work/token" "$work/gts.json" "$scp_target:$remote/"
printf -v command 'set -e; cd %q; chmod 600 token gts.json; tar -xzf bundle.tar.gz; GMS_DEFAULT_MAX_JOBS=%q bash scripts/deployment/worker/install_cluster_worker.sh %q %q token controller-ca.crt %q %q gts.json' "$remote" "$max_jobs" "$worker_id" "$controller" "$suite_root" "$host"
ssh -t -p "$port" -- "$target" "$command"

if [[ "$host" =~ $ipv6_host_pattern ]]; then
    port="${BASH_REMATCH[3]:-22}"
    host="${BASH_REMATCH[1]}"
elif [[ "$host" =~ ^([^:]+):([0-9]+)$ ]]; then
    port="${BASH_REMATCH[2]}"
    host="${BASH_REMATCH[1]}"
fi
[[ "$user" =~ ^[a-zA-Z0-9._-]+$ && "$host" =~ ^[a-zA-Z0-9.:_-]+$ && "$host" != -* ]] || exit 2
[[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port > 0 && 10#$port <= 65535)) || exit 2
target="$user@$host"
scp_target="$target"
[[ "$host" != *:* ]] || scp_target="$user@[$host]"
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
cd "$root"
credential="${GMS_GTS_CREDENTIAL_FILE:-}"
[[ -f "$credential" ]] || { echo "Set GMS_GTS_CREDENTIAL_FILE to the GTS service account file" >&2; exit 2; }
work="$(mktemp -d)"
remote=""
cleanup() {
    rm -rf -- "$work"
    if [[ -n "$remote" ]]; then
        ssh -p "$port" -- "$target" "rm -rf -- '$remote'" || true
    fi
}
trap cleanup EXIT
read -rsp "Worker Token: " token
printf '\n'
[[ -n "$token" ]] || exit 2
printf '%s\n' "$token" > "$work/token"
unset token
if [[ -x "$root/gms-runtime" ]]; then
    GMS_PROJECT_ROOT="$root" "$root/gms-runtime" worker-bundle "$work/bundle.tar.gz"
else
    python3 -c 'import sys; from pathlib import Path; from bootstrap.env_loader import load_runtime_env; load_runtime_env(); from features.cluster.deployment.api import build_worker_bundle; build_worker_bundle(Path.cwd(), Path(sys.argv[1]))' "$work/bundle.tar.gz"
fi
remote="$(ssh -p "$port" -- "$target" 'umask 077; mktemp -d /tmp/gms-worker-deploy.XXXXXXXX')"
[[ "$remote" =~ ^/tmp/gms-worker-deploy\.[a-zA-Z0-9]+$ ]] || { remote=""; exit 1; }
install -m 600 "$credential" "$work/gts.json"
scp -P "$port" -- "$work/bundle.tar.gz" "$work/token" "$work/gts.json" "$scp_target:$remote/"
printf -v command 'set -e; cd %q; chmod 600 token gts.json; tar -xzf bundle.tar.gz; GMS_DEFAULT_MAX_JOBS=%q bash scripts/deployment/worker/install_cluster_worker.sh %q %q token controller-ca.crt %q %q gts.json' "$remote" "$max_jobs" "$worker_id" "$controller" "$suite_root" "$host"
ssh -t -p "$port" -- "$target" "$command"
