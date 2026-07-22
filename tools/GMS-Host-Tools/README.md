# GMS Host Tools

The Worker installer deploys this bundle into `~/Software` and adds one
managed source block to `~/.bashrc`.

Contents:

- `jdk-11/`: fallback Java runtime for older CTS/GTS tools. Its large
  `lib/modules` image is stored as `modules.part.*` chunks and is restored
  automatically by the Worker installer.
- `platform-tools-gms-linux.zip`: adb, fastboot, aapt, aapt2 and their bundled
  runtime files.
- GTS API credentials are never bundled. Supply a rotated Google service-account
  file at deployment time with `GMS_GTS_CREDENTIAL_FILE`.
- `env.sh`: canonical Java and Android tool environment.
- `verify.sh`: validates exact deployed paths without falling back to another
  copy already present on `PATH`.

Python remains a target-host system dependency. Keep the external GTS credential
outside the repository and grant it only to the Worker deployment process.

Manual installation:

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
install -m 600 "$GMS_GTS_CREDENTIAL_FILE" "$HOME/Software/gts-rockchip.json"
chmod 755 "$HOME/Software/GMS-Host-Tools/"*.sh
"$HOME/Software/GMS-Host-Tools/verify.sh"
```
