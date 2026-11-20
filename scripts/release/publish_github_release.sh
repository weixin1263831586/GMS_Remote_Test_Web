#!/usr/bin/env bash
# Trusted-publisher helper. Run only AFTER build_binary_release.py's native
# doctor, isolated production smoke, source-leak and GPG signing checks pass.
set -Eeuo pipefail
umask 077

REPO="weixin1263831586/GMS_Remote_Test_Web"
DIST="${GMS_DIST_DIR:-dist}"
REF="${GMS_RELEASE_TARGET_REF:-ui/unify-page-edge-8px-20261120}"
PRERELEASE=false
fail() { printf 'GitHub Release publish refused: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: GMS_RELEASE_SIGNING_KEY=<trusted GPG key ID> \
       ./scripts/release/publish_github_release.sh VERSION [--dist-dir PATH] [--prerelease]

The signed compiled tar.gz, .sha256 and .sig must already exist in dist/.
Publishes four release assets: tar.gz, .sha256, .sig, publisher.asc.
The GitHub Release notes include a copyable one-line install command and the
publisher key fingerprint. A user must verify that fingerprint through a trusted
channel before installation. Use --prerelease for test-only builds, paired with
the installer --tag flag (GitHub /releases/latest excludes prereleases).
EOF
}
(($# > 0)) || { usage; exit 2; }
VERSION="$1"; shift
[[ "$VERSION" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$ ]] || fail "invalid release version"
while (($#)); do
  case "$1" in
    --dist-dir) (($# >= 2)) || fail "missing --dist-dir value"; DIST="$2"; shift 2 ;;
    --prerelease) PRERELEASE=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
for cmd in gh gpg sha256sum; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done
[[ -n "${GMS_RELEASE_SIGNING_KEY:-}" ]] || fail "set GMS_RELEASE_SIGNING_KEY on the trusted build host"
tag="gms-v$VERSION"
name="gms-web-app-$VERSION-ubuntu22.04-x86_64.tar.gz"
DIST="$(cd "$DIST" && pwd -P)"
for part in "$DIST/$name" "$DIST/$name.sha256" "$DIST/$name.sig"; do
  [[ -s "$part" ]] || fail "signed release artifact missing: $part"
done
(cd "$DIST" && sha256sum -c "$name.sha256") || fail "release checksum validation failed"
tmp="$(mktemp -d -t gms-release-publish-XXXXXXXX)"
trap 'rm -rf -- "$tmp"' EXIT
gpg --batch --armor --export "$GMS_RELEASE_SIGNING_KEY" > "$tmp/publisher.asc"
[[ -s "$tmp/publisher.asc" ]] || fail "cannot export trusted GPG public key"
fingerprint="$(gpg --batch --with-colons --fingerprint --list-keys "$GMS_RELEASE_SIGNING_KEY" \
  | awk -F: '$1=="fpr" {print toupper($10); exit}')"
[[ "$fingerprint" =~ ^[A-F0-9]{40}$ ]] || fail "invalid publisher fingerprint"
status="$(gpg --batch --status-fd 1 --verify "$DIST/$name.sig" "$DIST/$name" 2>/dev/null)" \
  || fail "detached signature cannot be verified"
printf '%s\n' "$status" | awk -v expected="$fingerprint" \
  '$1=="[GNUPG:]" && $2=="VALIDSIG" && (toupper($3)==expected || toupper($NF)==expected) {ok=1} END {exit !ok}' \
  || fail "release signature is not from selected trusted publisher"

gh auth status >/dev/null || fail "GitHub CLI is not authenticated"
if gh release view "$tag" --repo "$REPO" >/dev/null 2>&1; then
  fail "release already exists: $tag; do not silently overwrite signed artifacts"
fi
# Keep variable expansion for release metadata, but escape Markdown
# backticks so Bash does not treat fenced code blocks as substitutions.
cat > "$tmp/notes.md" <<EOF
## GMS Remote Test Web — compiled independent installation

Target: Ubuntu 22.04 x86_64. This release delivers a compiled runtime, not
the project's plain Python backend source. The front-end and public installer
remain inspectable. Verify publisher fingerprint through a separate trusted channel.

Publisher signing fingerprint:
\`\`\`text
$fingerprint
\`\`\`

### Copy into another server

\`\`\`bash
curl -fsSLo gms-install.sh https://raw.githubusercontent.com/weixin1263831586/GMS_Remote_Test_Web/$REF/scripts/release/install_from_github.sh && bash gms-install.sh --fingerprint $fingerprint $(if "$PRERELEASE"; then printf '%s' "--tag $tag"; fi)
\`\`\`

This command downloads the release archive, SHA-256, detached GPG signature
and publisher public key from GitHub, then verifies them before invoking sudo.
The installer prompts for hostname/port, administrator credentials and optional
Redmine credentials. No source checkout is needed.

See: https://github.com/$REPO/blob/$REF/docs/deployment/compiled-independent-install.md
EOF
args=()
if "$PRERELEASE"; then args+=(--prerelease); fi
gh release create "$tag" "$DIST/$name" "$DIST/$name.sha256" "$DIST/$name.sig" \
  "$tmp/publisher.asc" --repo "$REPO" --target "$REF" --title "GMS private compiled $VERSION" \
  --notes-file "$tmp/notes.md" "${args[@]}"
echo "GitHub Release published: https://github.com/$REPO/releases/tag/$tag"
echo "Publisher fingerprint: $fingerprint"
