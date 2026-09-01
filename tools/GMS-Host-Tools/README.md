# GMS Host Tools

The Worker installer deploys this bundle into `~/Software` and adds one
managed source block to `~/.bashrc`.

Tracked contents:

- `env.sh`: canonical Java and Android tool environment.
- `verify.sh`: validates exact deployed paths without falling back to another
  copy already present on `PATH`.

Deployment-only artifacts (never tracked in Git; fetched on demand with
mandatory SHA256 checks):

- `jdk-11/`: fallback Java runtime for older CTS/GTS tools. Its large
  `lib/modules` image is stored as `modules.part.*` chunks and is restored
  automatically by the Worker installer.
- `platform-tools-gms-linux.zip`: the unmodified Google Android SDK
  Platform-Tools Linux archive (adb, fastboot and bundled runtime files).

Secrets are never bundled or tracked. Supply a rotated Google service-account
file at deployment time through `GMS_GTS_CREDENTIAL_FILE`; the installer copies
it to `${SOFTWARE_ROOT}/gts-rockchip.json` with mode 0600 and `env.sh` exposes
it as `APE_API_KEY`. Python remains a target-host system dependency.

Configure the controller through `configs/runtime.json` or its service
environment before deploying/reconfiguring a Worker:

```json
{
  "GMS_HOST_TOOLS_JDK_URL": "https://artifacts.example/jdk-11.tar.gz",
  "GMS_HOST_TOOLS_JDK_SHA256": "<64 lowercase hex characters>",
  "GMS_HOST_TOOLS_PLATFORM_URL": "https://artifacts.example/platform-tools.zip",
  "GMS_HOST_TOOLS_PLATFORM_SHA256": "<64 lowercase hex characters>"
}
```

When the Platform-Tools URL is not configured, `prepare_gms_host_tools.sh`
automatically downloads the pinned Google package from
`https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip`
and verifies SHA-256 before installing it. URL/checksum overrides remain
available for an access-controlled mirror, and both values must be supplied
together.

`aapt` and `aapt2` are Android Build-Tools commands and are deliberately not
added to the Platform-Tools ZIP. Install Build-Tools separately; set
`GMS_ANDROID_BUILD_TOOLS_DIR` for interactive shells or `GMS_AAPT2_PATH` for
the Controller service. A remote Worker can use
`GMS_WORKER_AAPT2_PATH=/absolute/path/to/aapt2`; without it, the Worker also
searches `PATH` and `/usr/lib/android-sdk/build-tools/*/aapt2`.

Private CAs use `GMS_HOST_TOOLS_CA_CERT`. Plain HTTP is rejected unless the
operator explicitly sets `GMS_HOST_TOOLS_ALLOW_HTTP=1` for an isolated network.
Only publish artifact URLs for binaries your organization is licensed to
redistribute. Deployments that must avoid direct Internet access can mirror the
same original Google ZIP and configure its HTTPS URL plus exact SHA-256.

Manual installation (after fetching the two artifacts into this directory):

```bash
mkdir -p "$HOME/Software"
rsync -a jdk-11/ "$HOME/Software/jdk-11/"
python3 - "$HOME/Software/jdk-11/lib" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
parts = sorted(root.glob("modules.part.*"))
with (root / "modules").open("wb") as output:
    for part in parts:
        output.write(part.read_bytes())
for part in parts:
    part.unlink()
PY
python3 /path/to/extract_zip_preserve_mode.py platform-tools-gms-linux.zip "$HOME/Software"
mkdir -p "$HOME/Software/GMS-Host-Tools"
cp env.sh verify.sh "$HOME/Software/GMS-Host-Tools/"
install -m 600 "${GMS_GTS_CREDENTIAL_FILE:?GMS_GTS_CREDENTIAL_FILE must point at the rotated service-account JSON}" \
    "$HOME/Software/gts-rockchip.json"
chmod 755 "$HOME/Software/GMS-Host-Tools/"*.sh
"$HOME/Software/GMS-Host-Tools/verify.sh"
```
