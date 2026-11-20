#!/usr/bin/env bash
# Copy-and-run installer for a compiled GMS GitHub Release.
# This script ONLY downloads/validates release artifacts, then delegates to the
# compiled package's interactive installer. It never checks out application code.
set -Eeuo pipefail
umask 077

REPOSITORY="weixin1263831586/GMS_Remote_Test_Web"
TAG=""
FINGERPRINT="${GMS_RELEASE_GPG_FINGERPRINT:-}"
DOWNLOAD_ONLY=false

fail() { printf 'GMS GitHub install refused: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: bash install_from_github.sh [--tag gms-v1.0.0]
       [--fingerprint 40_HEX_GPG_FINGERPRINT] [--download-only]
       [-- installer options: --no-admin-prompt, --host, --port, ...]

Without --tag, download the latest *published stable* GitHub Release.
A trusted publisher GPG fingerprint is mandatory (may be typed interactively).
The script checks SHA-256, detached GPG signature and tar paths BEFORE sudo.
--download-only verifies the compiled bundle without installing it.
EOF
}
installer_args=()
while (($#)); do
  case "$1" in
    --tag) (($# >= 2)) || fail "--tag requires a value"; TAG="$2"; shift 2 ;;
    --fingerprint) (($# >= 2)) || fail "--fingerprint requires a value"; FINGERPRINT="$2"; shift 2 ;;
    --download-only) DOWNLOAD_ONLY=true; shift ;;
    --) shift; installer_args=("$@"); break ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ -z "$TAG" || "$TAG" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,90}$ ]] || fail "invalid release tag"
for tool in curl python3 gpg sha256sum tar; do
  command -v "$tool" >/dev/null 2>&1 || fail "missing prerequisite: $tool"
done
if [[ -z "$FINGERPRINT" ]]; then
  [[ -t 0 ]] || fail "provide a trusted signer fingerprint using --fingerprint"
  printf '%s\n' "Enter the 40-hex publisher fingerprint from an independently trusted source."
  read -r -p 'Trusted GPG fingerprint: ' FINGERPRINT
fi
FINGERPRINT="$(printf '%s' "$FINGERPRINT" | tr '[:lower:]' '[:upper:]')"
[[ "$FINGERPRINT" =~ ^[A-F0-9]{40}$ ]] || fail "publisher fingerprint must contain exactly 40 hex digits"

work="$(mktemp -d -t gms-github-release-XXXXXXXX)"
trap 'rm -rf -- "$work"' EXIT
api="https://api.github.com/repos/$REPOSITORY/releases/latest"
if [[ -n "$TAG" ]]; then api="https://api.github.com/repos/$REPOSITORY/releases/tags/$TAG"; fi
curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -H 'Accept: application/vnd.github+json' \
  --connect-timeout 15 --max-time 120 "$api" -o "$work/release.json" \
  || fail "GitHub Release not found or inaccessible. Publish a signed Release first."

# GitHub metadata is untrusted. Validate exact asset names, URLs and uniqueness
# before passing any downloaded file to sha256sum, gpg, tar or sudo.
python3 - "$work/release.json" "$REPOSITORY" "$work/assets.txt" <<'PY'
import json
import pathlib
import re
import sys
from urllib.parse import urlparse

data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
repo = sys.argv[2]
assets = data.get("assets", [])
if data.get("draft") or (data.get("prerelease") and not data.get("tag_name")):
    raise SystemExit("release not published")
archive = re.compile(r"^gms-web-app-[A-Za-z0-9._-]+-ubuntu22\.04-x86_64\.tar\.gz$")
bundles = [item["name"] for item in assets if archive.fullmatch(item.get("name", ""))]
if len(bundles) != 1:
    raise SystemExit("expected exactly one compiled Ubuntu 22.04 x86_64 archive")
name = bundles[0]
required = (name, name + ".sha256", name + ".sig", "publisher.asc")
urls = []
for filename in required:
    matches = [item for item in assets if item.get("name") == filename]
    if len(matches) != 1:
        raise SystemExit("missing or duplicate release asset: " + filename)
    url = matches[0].get("browser_download_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise SystemExit("release asset URL is not HTTPS github.com")
    if not parsed.path.startswith("/" + repo + "/releases/download/"):
        raise SystemExit("release asset belongs to another repository")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit("unsafe release URL")
    urls.append((filename, url))
pathlib.Path(sys.argv[3]).write_text(
    "".join(filename + "\t" + url + "\n" for filename, url in urls),
    encoding="utf-8",
)
PY

while IFS=$'\t' read -r filename url; do
  [[ -n "$filename" && -n "$url" ]] || fail "malformed release metadata"
  printf 'Downloading %s\n' "$filename"
  curl -fsSL --retry 3 --proto '=https' --tlsv1.2 --connect-timeout 15 \
    --max-time 3600 "$url" -o "$work/$filename" \
    || fail "GitHub asset download failed: $filename"
done < "$work/assets.txt"
archive="$(cut -f 1 "$work/assets.txt" | head -1)"
(cd "$work" && sha256sum -c "$archive.sha256") || fail "release checksum mismatch"

export GNUPGHOME="$work/gnupg"
install -d -m 0700 "$GNUPGHOME"
gpg --batch --quiet --import "$work/publisher.asc" || fail "invalid publisher public key"
actual="$(gpg --batch --with-colons --fingerprint --list-keys \
  | awk -F: '$1=="fpr" {print toupper($10); exit}')"
[[ "$actual" == "$FINGERPRINT" ]] || fail "publisher fingerprint does not match trusted value"
status="$(gpg --batch --status-fd 1 --verify "$work/$archive.sig" "$work/$archive" 2>/dev/null)" \
  || fail "release GPG signature invalid"
printf '%s\n' "$status" | awk -v expected="$FINGERPRINT" \
  '$1=="[GNUPG:]" && $2=="VALIDSIG" && (toupper($3)==expected || toupper($NF)==expected) { ok=1 } END { exit !ok }' \
  || fail "the release was not signed by the pinned publisher key"
printf '%s\n' "Signed archive verified against trusted publisher: $FINGERPRINT"

python3 - "$work/$archive" <<'PY'
import pathlib
import sys
import tarfile
name = pathlib.Path(sys.argv[1])
seen = set()
total = 0
with tarfile.open(name, "r:gz") as archive:
    for index, member in enumerate(archive):
        if index > 250000:
            raise SystemExit("too many release members")
        path = pathlib.PurePosixPath(member.name)
        if (path.is_absolute() or not path.parts or path.parts[0] != "gms-web-app"
                or any(piece in (".", "..") for piece in member.name.split("/"))
                or not (member.isdir() or member.isfile()) or member.mode & 0o6000):
            raise SystemExit("unsafe release member: " + member.name)
        if member.name in seen:
            raise SystemExit("duplicate release member: " + member.name)
        seen.add(member.name)
        if member.isfile():
            total += member.size
            if total > 30 * 1024**3:
                raise SystemExit("release expands beyond 30 GiB")
if "gms-web-app/install.sh" not in seen or "gms-web-app/manifest.sha256" not in seen:
    raise SystemExit("compiled installer or manifest is missing")
PY
tar -xzf "$work/$archive" -C "$work" || fail "extraction failed"
[[ -f "$work/gms-web-app/manifest.json" && -f "$work/gms-web-app/manifest.sha256" ]] \
  || fail "compiled release manifest missing"
(cd "$work/gms-web-app" && sha256sum --quiet -c manifest.sha256) \
  || fail "release file manifest mismatch"

if "$DOWNLOAD_ONLY"; then
  printf '%s\n' "Download, publisher signature, archive and manifest validation passed."
  exit 0
fi
command -v sudo >/dev/null 2>&1 || fail "sudo is required for installation"
printf '%s\n' "Verified compiled installer ready; starting interactive installation."
# Sudo starts only AFTER verification; no source checkout and no shell-eval of
# release metadata. Administrator and Redmine secrets are entered by getpass.
cd "$work/gms-web-app"
sudo bash ./install.sh "${installer_args[@]}"
