"""Guard shared modal asset versions across all seven browser entrypoints."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINTS = (
    "web/shell/components/head.html",
    "features/redmine/ui/page.html",
    "features/gerrit/ui/page.html",
    "features/cluster/ui/page.html",
    "features/devices/ui/page.html",
    "features/automation/ui/page.html",
    "features/system/update_radar/ui/page.html",
)
ASSETS = (
    "modal-tech-dialogs.css",
    "modal-tech-cases.css",
    "dialog-frame.js",
)


def test_shared_dialog_assets_use_one_cache_version_per_resource():
    versions = {asset: {} for asset in ASSETS}
    for relative in ENTRYPOINTS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        for asset in ASSETS:
            matches = re.findall(
                rf"{re.escape(asset)}\?v=([A-Za-z0-9_.-]+)",
                source,
            )
            assert len(matches) == 1, (relative, asset, matches)
            versions[asset][relative] = matches[0]
    for asset, entry_versions in versions.items():
        assert len(set(entry_versions.values())) == 1, (asset, entry_versions)
