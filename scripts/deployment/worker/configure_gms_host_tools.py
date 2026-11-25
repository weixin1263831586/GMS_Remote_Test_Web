#!/usr/bin/env python3
"""Maintain the shell startup block for tools deployed under ~/Software."""

from __future__ import annotations

import re
import sys
from pathlib import Path


START = "# >>> GMS Host Tools >>>"
END = "# <<< GMS Host Tools <<<"
LEGACY_PATTERNS = (
    re.compile(r"^export (JAVA_HOME|JRE_HOME|CLASSPATH)=.*$"),
    re.compile(
        r"^export PATH=(?:\$\{?JAVA_HOME\}?/bin|[^: ]*Software/"
        r"(?:jdk-11/bin|android-sdk-linux/tools|platform-tools|"
        r"sdk_tools_new/sdk_tools)):\$PATH$"
    ),
)


def configure_bashrc(path: Path) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = re.sub(
        rf"\n?{re.escape(START)}.*?{re.escape(END)}\n?",
        "\n",
        text,
        flags=re.DOTALL,
    )
    lines = [
        line
        for line in text.splitlines()
        if not any(pattern.match(line.strip()) for pattern in LEGACY_PATTERNS)
    ]
    block = [
        START,
        'if [ -f "$HOME/Software/GMS-Host-Tools/env.sh" ]; then',
        '    source "$HOME/Software/GMS-Host-Tools/env.sh"',
        "fi",
        END,
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*lines, "", *block]) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: configure_gms_host_tools.py BASHRC")
    configure_bashrc(Path(sys.argv[1]))
