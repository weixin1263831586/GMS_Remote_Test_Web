"""Guard against drift in the generated CLI command reference.

docs/cli/command-reference.md is a generated document: it must always
match what tools/generate_cli_docs.py derives from the CLI's own command
catalog.  A stale document means a new gms-rt command shipped without
refreshing the docs — run `python3 tools/generate_cli_docs.py` to fix.
"""

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = PROJECT_ROOT / "tools/generate_cli_docs.py"


def test_generated_cli_docs_are_fresh():
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        "docs/cli/command-reference.md is stale; "
        "run `python3 tools/generate_cli_docs.py` to update it.\n"
        f"{result.stdout}{result.stderr}"
    )
