#!/usr/bin/env bash
# LEGACY compatibility launcher (10.txt §十八: superseded by mcp_launcher.py).
#
# The Python launcher (mcp_launcher.py) is now the canonical entry point and
# reads the TOML profiles directly. This shim only forwards for manifests or
# setups that still reference mcp_launcher.sh; it will be retired with the
# legacy .env profiles.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/mcp_launcher.py" "$@"
