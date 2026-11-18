"""Keep public configuration and architecture prose tied to actual defaults."""
from __future__ import annotations

import re
from pathlib import Path

from features.system.topology_view import EDGE_FONT_SIZE, NODE_FONT_SIZE


ROOT = Path(__file__).resolve().parents[2]


def test_topology_documented_font_sizes_match_rendering_constants():
    development = (ROOT / "docs/development.md").read_text(encoding="utf-8")
    overview = (ROOT / "docs/architecture/overview.md").read_text(encoding="utf-8")
    assert f"节点统一 {NODE_FONT_SIZE} px" in development
    assert f"连线标签统一 {EDGE_FONT_SIZE} px" in development
    assert f"统一 {NODE_FONT_SIZE}px 节点字体" in overview
    assert f"{EDGE_FONT_SIZE}px 箭头标签" in overview


def test_production_guide_distinguishes_python_entry_and_systemd_cli():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    restart = (ROOT / "restart_services.sh").read_text(encoding="utf-8")
    guide = (ROOT / "docs/deployment/production.md").read_text(encoding="utf-8")

    app_limit = re.search(r"limit_concurrency\s*=\s*(\d+)", app)
    cli_limit = re.search(r"--limit-concurrency\s+(\d+)", install)
    assert app_limit and cli_limit
    assert f"的 {app_limit.group(1)}" in guide
    assert f"--limit-concurrency {cli_limit.group(1)}" in guide
    assert f"--limit-concurrency {cli_limit.group(1)}" in restart
    assert "access_log=settings.environment != 'production'" in app
    assert "--access-log" in install and "--access-log" in restart
    assert "显式开启访问日志" in guide
