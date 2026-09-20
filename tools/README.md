# tools

`tools/` contains external tools, packaged utilities, and project
maintenance tooling required by GMS_Remote_Test_Web.

## Top-level directories

Third-party or independently maintained tools may live directly under
`tools/`, for example:

- `adbproxy-rs/`
- `android-internals-wiki/` — local clone; do not commit its contents
  (refresh with `tools/scripts/setup/update_android_internals_wiki.sh`)
- `GMS-Host-Tools/`
- `gms-worker-native/`
- `jadx/`
- `tesseract/`
- `upgrade_tool/` — vendored Rockchip upgrade binary

Top-level data/binary files (`jq-linux-amd64`, `misc.img`,
`scrcpy-linux-x86_64-*.tar.gz`) are packaged utilities
served to clients via `features/system/utility_tools_api.py` (stable
`tool_id` manifest — files may move under `tools/` without UI changes).

## Upstream sources & provenance (usbip 模式)

Every third-party tool with source code follows the **usbip pattern**:

- The directory under `tools/` keeps ONLY the executable artifacts plus its
  own provenance manifest (`<name>.provenance.json`).
- The source itself lives in a local git clone of the GMS fork and is
  **never committed** to this repository (`.gitignore` excludes it).

| Tool | git 里保留 | Source (GMS fork) | Upstream |
|---|---|---|---|
| `usbip/` | `usbipd` + `usbipd.provenance.json` | weixin1263831586/usbip | jiegec/usbip |
| `adbproxy-rs/` | `adbproxy-rs.provenance.json` + `dist/` | weixin1263831586/adbproxy-rs | Ken-u/adbproxy-rs |
| `jadx/` | `jadx.provenance.json` + `bin/` + `lib/` | weixin1263831586/jadx | skylot/jadx |
| `android-internals-wiki/` | nothing (gitignored clone) | weixin1263831586/android-internals-wiki | Gracker/android-internals-wiki |

When updating a tool: pull/rebase the fork clone from upstream, rebuild or
fetch the new artifact, replace it in the tool directory, then refresh the
directory's own `<name>.provenance.json` (pinned version/commit, sha256).
The `usbipd` binary/manifest match is enforced by
`tests/test_usbipd_provenance.py`; directory contents and provenance
presence are gated by `tests/architecture/test_tools_layout.py`.

## Project-maintained scripts

Python/Shell scripts maintained as part of GMS_Remote_Test_Web must live
under `tools/scripts/`, categorized as:

| Directory | Contents |
|---|---|
| `scripts/agent/` | Agent package toolchain: `audit_contract.py` → `sync_package.py` → `build_package.py` → `release.py` → `dev.py` |
| `scripts/docs/` | Documentation generators (`generate_cli_docs.py`, `generate_guide_screenshots.py`) |
| `scripts/deployment/` | Host deployment helpers (systemd installers) |
| `scripts/maintenance/` | Repo maintenance (`update_size_baseline.py`, `refactor_visual_parity.py`, `vulture_allowlist.py`) |
| `scripts/migrations/` | One-shot data migrations |
| `scripts/testing/` | Manual test harnesses (`transport_soak.py`) |
| `scripts/setup/` | Environment setup helpers |
| `scripts/utilities/` | User-downloadable utilities (served via stable tool IDs) |
| `scripts/_common.py` | Shared helpers (`find_repo_root`) — never run directly |

Do not add new standalone `.py`/`.sh` files directly to `tools/` — the
architecture gate `tests/architecture/test_tools_layout.py` fails on them.

## Repo-root resolution

Scripts must never hard-code `Path(__file__).parents[N]` for the repo root
(the category subdirectories may be reorganized). Use the structural
lookup in `tools/scripts/_common.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import find_repo_root

REPO_ROOT = find_repo_root()
```
