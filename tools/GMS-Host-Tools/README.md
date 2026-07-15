# GMS Host Tools

The Worker installer deploys this bundle into `~/Software` and adds one
managed source block to `~/.bashrc`.

Contents:

- `jdk-11/`: fallback Java runtime for older CTS/GTS tools. Its large
  `lib/modules` image is stored as `modules.part.*` chunks and is restored
  automatically by the Worker installer.
- `platform-tools-gms-linux.zip`: adb, fastboot, aapt, aapt2 and their bundled
  runtime files.
- `gts-rockchip.json`: GTS API credential, installed with mode `0600`.
- `env.sh`: canonical Java and Android tool environment.
- `verify.sh`: validates exact deployed paths without falling back to another
  copy already present on `PATH`.

Python remains a target-host system dependency. Treat this bundle as sensitive
because it contains the GTS API credential; do not publish it or commit it to a
public repository.

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
install -m 600 gts-rockchip.json "$HOME/Software/gts-rockchip.json"
chmod 755 "$HOME/Software/GMS-Host-Tools/"*.sh
"$HOME/Software/GMS-Host-Tools/verify.sh"
```
