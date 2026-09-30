"""Static command discovery for installed monolithic and modular CLI payloads."""

import re
from pathlib import Path


def cli_command_names(cli: Path) -> set[str]:
    source = cli.read_text(encoding="utf-8")
    modules = re.findall(r'^source "\$_gms_runtime_dir/cli/([a-z0-9-]+\.sh)"', source, re.M)
    sources = [source, *((cli.parent / "cli" / name).read_text(encoding="utf-8")
                         for name in modules)]
    return {name for text in sources
            for name in re.findall(r"^(gms-rt-[a-z0-9-]+)\(\)", text, re.M)}
