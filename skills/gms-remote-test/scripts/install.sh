#!/usr/bin/env bash
set -euo pipefail

SKILL_NAME="gms-remote-test"
DEFAULT_SERVER_URL='__GMS_REMOTE_TEST_SERVER__'
DEFAULT_DOWNLOAD_URL='__GMS_SKILL_DOWNLOAD_URL__'
SERVER_URL="${GMS_REMOTE_TEST_SERVER:-$DEFAULT_SERVER_URL}"
DOWNLOAD_URL="${GMS_SKILL_DOWNLOAD_URL:-$DEFAULT_DOWNLOAD_URL}"
# GMS_SKILLS_DIR is the agent-neutral override. Keep the Codex-specific name
# for backward compatibility with existing installations.
SKILLS_DIR="${GMS_SKILLS_DIR:-${GMS_CODEX_SKILLS_DIR:-${CODEX_HOME:-${HOME}/.codex}/skills}}"
TARGET_DIR="${SKILLS_DIR}/${SKILL_NAME}"
BIN_DIR="${GMS_BIN_DIR:-${HOME}/.local/bin}"
RUNTIME_BIN_DIR="${GMS_RUNTIME_BIN_DIR:-${XDG_DATA_HOME:-${HOME}/.local/share}/gms-remote-test/bin}"
PROFILE_FILE="${GMS_PROFILE_FILE:-${HOME}/.profile}"

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '%s\n' "$*"
}

case "$SERVER_URL" in
    __GMS_*|'') fail "请从 Controller 的 /api/system/skills/install.sh 获取安装脚本，或设置 GMS_REMOTE_TEST_SERVER" ;;
esac
case "$DOWNLOAD_URL" in
    __GMS_*|'') fail "缺少技能包下载地址 GMS_SKILL_DOWNLOAD_URL" ;;
esac

command -v curl >/dev/null 2>&1 || fail "需要 curl"

CURL_ARGS=(-fsSL)
if [ -n "${GMS_INSTALL_CA_CERT:-}" ]; then
    CURL_ARGS+=(--cacert "$GMS_INSTALL_CA_CERT")
elif [[ "$DOWNLOAD_URL" == https://* ]] && [ "${GMS_INSTALL_INSECURE:-1}" != "0" ]; then
    CURL_ARGS+=(-k)
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gms-remote-test-install.XXXXXX")
cleanup() {
    rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

ARCHIVE_PATH="${TEMP_DIR}/${SKILL_NAME}.zip"
EXTRACT_DIR="${TEMP_DIR}/extract"
mkdir -p "$EXTRACT_DIR"

info "Downloading ${SKILL_NAME} from ${SERVER_URL}"
curl "${CURL_ARGS[@]}" "$DOWNLOAD_URL" -o "$ARCHIVE_PATH"

if command -v python3 >/dev/null 2>&1; then
    python3 - "$ARCHIVE_PATH" "$EXTRACT_DIR" <<'PY'
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive_path = Path(sys.argv[1])
destination = Path(sys.argv[2]).resolve()
with zipfile.ZipFile(archive_path) as archive:
    for entry in archive.infolist():
        path = PurePosixPath(entry.filename)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe ZIP entry: {entry.filename}")
    archive.extractall(destination)
PY
elif command -v unzip >/dev/null 2>&1; then
    while IFS= read -r entry; do
        case "/${entry}/" in
            */../*|//*)
                fail "技能包包含不安全路径: ${entry}"
                ;;
        esac
    done < <(unzip -Z1 "$ARCHIVE_PATH")
    unzip -q "$ARCHIVE_PATH" -d "$EXTRACT_DIR"
else
    fail "需要 python3 或 unzip 来安装技能包"
fi

SOURCE_DIR="${EXTRACT_DIR}/${SKILL_NAME}"
[ -f "${SOURCE_DIR}/SKILL.md" ] || fail "技能包缺少 ${SKILL_NAME}/SKILL.md"
[ -f "${SOURCE_DIR}/scripts/gms-remote-test.sh" ] || fail "技能包缺少 CLI"
[ -f "${SOURCE_DIR}/scripts/install.sh" ] || fail "技能包缺少更新脚本"

mkdir -p "$SKILLS_DIR" "$BIN_DIR" "$RUNTIME_BIN_DIR"
STAGING_DIR="${SKILLS_DIR}/.${SKILL_NAME}.new.$$"
BACKUP_DIR="${SKILLS_DIR}/.${SKILL_NAME}.backup.$$"
rm -rf -- "$STAGING_DIR" "$BACKUP_DIR"
cp -a "$SOURCE_DIR" "$STAGING_DIR"
chmod 755 \
    "${STAGING_DIR}/scripts/gms-remote-test.sh" \
    "${STAGING_DIR}/scripts/install.sh"

LEGACY_WRAPPER_PATH="${BIN_DIR}/gms-rt"
LEGACY_ALIAS_PATH="${BIN_DIR}/gms-remote-test"
DISPATCHER_PATH="${RUNTIME_BIN_DIR}/gms-rt-dispatcher"
DISPATCHER_TMP="${DISPATCHER_PATH}.tmp.$$"

is_managed_command_link() {
    local path="$1" target
    [ -L "$path" ] || return 1
    target=$(readlink "$path")
    [ "$target" = "$DISPATCHER_PATH" ] || [ "$target" = "$LEGACY_WRAPPER_PATH" ]
}

# Install every public helper function as its own executable name. This makes
# the complete gms-rt-* command set discoverable through normal PATH completion.
mapfile -t COMMAND_NAMES < <(
    sed -n 's/^\(gms-rt-[a-z0-9-]*\)().*/\1/p' \
        "${STAGING_DIR}/scripts/gms-remote-test.sh" | sort -u
)
COMMAND_NAMES+=("gms-rt-update")
for command_name in "${COMMAND_NAMES[@]}"; do
    command_link="${BIN_DIR}/${command_name}"
    if { [ -e "$command_link" ] || [ -L "$command_link" ]; } \
            && ! is_managed_command_link "$command_link"; then
        fail "不会覆盖已有命令: ${command_link}"
    fi
done

if [ -e "$TARGET_DIR" ]; then
    mv "$TARGET_DIR" "$BACKUP_DIR"
fi
if ! mv "$STAGING_DIR" "$TARGET_DIR"; then
    [ ! -e "$BACKUP_DIR" ] || mv "$BACKUP_DIR" "$TARGET_DIR"
    fail "无法安装到 ${TARGET_DIR}"
fi
rm -rf -- "$BACKUP_DIR"

install_portable_jq() {
    local os_name machine asset checksum url target temporary
    os_name=$(uname -s)
    machine=$(uname -m)
    [ "$os_name" = "Linux" ] || fail "自动安装 jq 目前仅支持 Linux；请先安装 jq"
    case "$machine" in
        x86_64|amd64)
            asset="jq-linux-amd64"
            checksum="020468de7539ce70ef1bceaf7cde2e8c4f2ca6c3afb84642aabc5c97d9fc2a0d"
            ;;
        aarch64|arm64)
            asset="jq-linux-arm64"
            checksum="6bc62f25981328edd3cfcfe6fe51b073f2d7e7710d7ef7fcdac28d4e384fc3d4"
            ;;
        *)
            fail "不支持自动安装 jq 的架构: ${machine}"
            ;;
    esac
    url="https://github.com/jqlang/jq/releases/download/jq-1.8.1/${asset}"
    target="${RUNTIME_BIN_DIR}/jq"
    temporary="${target}.tmp.$$"
    info "jq not found; installing verified jq 1.8.1 (${machine})"
    curl -fsSL "$url" -o "$temporary"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$checksum" "$temporary" | sha256sum -c - >/dev/null
    elif command -v shasum >/dev/null 2>&1; then
        [ "$(shasum -a 256 "$temporary" | awk '{print $1}')" = "$checksum" ] \
            || fail "jq SHA-256 校验失败"
    else
        rm -f -- "$temporary"
        fail "缺少 sha256sum/shasum，无法校验 jq"
    fi
    chmod 755 "$temporary"
    mv "$temporary" "$target"
}

if ! command -v jq >/dev/null 2>&1 && [ ! -x "${RUNTIME_BIN_DIR}/jq" ]; then
    install_portable_jq
fi

{
    printf '#!/usr/bin/env bash\n'
    printf 'set -e\n'
    printf 'HELPER=%q\n' "${TARGET_DIR}/scripts/gms-remote-test.sh"
    printf 'INSTALLER=%q\n' "${TARGET_DIR}/scripts/install.sh"
    printf 'export GMS_REMOTE_TEST_SERVER=%q\n' "$SERVER_URL"
    printf 'export GMS_SKILL_DOWNLOAD_URL=%q\n' "$DOWNLOAD_URL"
    printf 'export GMS_CODEX_SKILLS_DIR=%q\n' "$SKILLS_DIR"
    printf 'export GMS_SKILLS_DIR=%q\n' "$SKILLS_DIR"
    printf 'export GMS_BIN_DIR=%q\n' "$BIN_DIR"
    printf 'export GMS_RUNTIME_BIN_DIR=%q\n' "$RUNTIME_BIN_DIR"
    printf 'export PATH=%q:"$PATH"\n' "$RUNTIME_BIN_DIR"
    printf 'export GMS_INSTALL_INSECURE=%q\n' "${GMS_INSTALL_INSECURE:-1}"
    printf 'export GMS_CURL_INSECURE=%q\n' "${GMS_INSTALL_INSECURE:-1}"
    if [ -n "${GMS_INSTALL_CA_CERT:-}" ]; then
        printf 'export GMS_INSTALL_CA_CERT=%q\n' "$GMS_INSTALL_CA_CERT"
        printf 'export GMS_CURL_CA_CERT=%q\n' "$GMS_INSTALL_CA_CERT"
    fi
    cat <<'WRAPPER'
invoked_name=${0##*/}
case "$invoked_name" in
    gms-rt-update)
        exec "$INSTALLER" "$@"
        ;;
    gms-rt-*)
        exec "$HELPER" "$invoked_name" "$@"
        ;;
    *)
        if [ "$#" -eq 0 ]; then
            exec "$HELPER" gms-rt-system-help
        fi
        printf 'Error: 不支持空格子命令格式。请使用 gms-rt-%s\n' "$1" >&2
        printf '查看完整命令: gms-rt-system-help\n' >&2
        exit 2
        ;;
esac
WRAPPER
} > "$DISPATCHER_TMP"
chmod 755 "$DISPATCHER_TMP"
mv "$DISPATCHER_TMP" "$DISPATCHER_PATH"

# Remove only stale links previously managed by this installer, then recreate
# the current command inventory. Other files in BIN_DIR are never touched.
for command_link in "${BIN_DIR}"/gms-rt-*; do
    is_managed_command_link "$command_link" || continue
    rm -f -- "$command_link"
done
for command_name in "${COMMAND_NAMES[@]}"; do
    ln -s "$DISPATCHER_PATH" "${BIN_DIR}/${command_name}"
done

# Migrate old public dispatcher aliases only when they are known to belong to
# this installer. Unknown user-managed files are preserved.
if [ -L "$LEGACY_ALIAS_PATH" ]; then
    legacy_alias_target=$(readlink "$LEGACY_ALIAS_PATH")
    if [ "$legacy_alias_target" = "$LEGACY_WRAPPER_PATH" ] \
            || [ "$legacy_alias_target" = "$DISPATCHER_PATH" ]; then
        rm -f -- "$LEGACY_ALIAS_PATH"
    fi
fi
if [ -f "$LEGACY_WRAPPER_PATH" ] && [ ! -L "$LEGACY_WRAPPER_PATH" ] \
        && grep -Fq 'scripts/gms-remote-test.sh' "$LEGACY_WRAPPER_PATH" \
        && grep -Fq 'GMS_SKILL_DOWNLOAD_URL' "$LEGACY_WRAPPER_PATH"; then
    rm -f -- "$LEGACY_WRAPPER_PATH"
fi

case ":$PATH:" in
    *":${BIN_DIR}:"*) path_ready=true ;;
    *) path_ready=false ;;
esac
if [ "$path_ready" = false ]; then
    touch "$PROFILE_FILE"
    if ! grep -Fq '# GMS Remote Test CLI' "$PROFILE_FILE"; then
        {
            printf '\n# GMS Remote Test CLI\n'
            printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
        } >> "$PROFILE_FILE"
    fi
fi

info "Installed Skill: ${TARGET_DIR}"
info "Installed CLI Runtime: ${DISPATCHER_PATH}"
info "Installed Commands: ${#COMMAND_NAMES[@]} (gms-rt-*)"
if [ "$path_ready" = true ]; then
    info "Run: gms-rt-auth-login USERNAME"
    info "     gms-rt-devices-list"
    info "Update later: gms-rt-update"
else
    info "Open a new shell, then run: gms-rt-auth-login USERNAME"
    info "                            gms-rt-devices-list"
fi
