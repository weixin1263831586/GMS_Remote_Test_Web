"""Generated module permissions must converge in both directions."""

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("content_matches", [True, False])
def test_sync_removes_executable_bit_from_plain_module(tmp_path, content_matches):
    path = Path(__file__).resolve().parents[1] / "tools/sync_agent_package.py"
    spec = importlib.util.spec_from_file_location("agent_sync_modes", path)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    source = tmp_path / "source.py"
    target = tmp_path / "target.py"
    source.write_text("VALUE = 1\n")
    target.write_text(source.read_text() if content_matches else "old\n")
    target.chmod(0o755)
    assert sync.sync_one(source, target)
    assert target.stat().st_mode & 0o777 == 0o644
    assert target.read_bytes() == source.read_bytes()
    assert not sync.sync_one(source, target)
