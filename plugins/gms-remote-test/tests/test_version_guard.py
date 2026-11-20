"""version_guard: resident MCP servers follow an upgraded `current` install.

Regression anchor: a kkagent-daemon singleton spawned 0.22.57 kept serving
for nine days across two upgrades; the bundled CLI lacked the 480s SDK
search curl timeout fix, so gms_rt_sdk_search failed with a misleading
"无法连接到服务器" 30-second curl exit 28 until the stale PID was killed
by hand.
"""

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ("runtime" if (ROOT / "runtime").is_dir() else "scripts")
sys.path.insert(0, str(RUNTIME))
import version_guard  # noqa: E402


@pytest.fixture
def install(tmp_path):
    """A minimal versions/<old>/scripts + current layout."""
    versions = tmp_path / "versions"
    old = versions / "0.1.0"
    new = versions / "0.2.0"
    for version in (old, new):
        (version / "scripts").mkdir(parents=True)
        (version / "scripts" / "mcp_server.py").write_text("# server\n")
    (tmp_path / "current").symlink_to(new)
    return old, new


def _script(version_dir: Path) -> str:
    return str(version_dir / "scripts" / "mcp_server.py")


def test_staleness_detects_current_drift(install):
    old, new = install
    info = version_guard.staleness(_script(old))
    assert info == {
        "own_version": "0.1.0",
        "current_version": "0.2.0",
        "target_script": _script(new),
    }


def test_staleness_is_quiet_when_current_matches(tmp_path):
    versions = tmp_path / "versions"
    same = versions / "0.1.0"
    (same / "scripts").mkdir(parents=True)
    (same / "scripts" / "mcp_server.py").write_text("# server\n")
    (tmp_path / "current").symlink_to(same)
    # current resolves to the very versiondir of this process: quiet.
    assert version_guard.staleness(_script(same)) is None
    # current removed entirely: repo-checkout style, never guess.
    (tmp_path / "current").unlink()
    assert version_guard.staleness(_script(same)) is None


def test_staleness_ignores_non_versioned_layout(tmp_path):
    checkout = tmp_path / "repo" / "scripts"
    checkout.mkdir(parents=True)
    (checkout / "mcp_server.py").write_text("# server\n")
    assert version_guard.staleness(str(checkout / "mcp_server.py")) is None


def test_staleness_requires_full_new_install(install):
    old, new = install
    (new / "scripts" / "mcp_server.py").unlink()
    # Half-deleted upgrade: keep serving the old image.
    assert version_guard.staleness(_script(old)) is None


def test_follow_reexecs_into_current_target(install):
    old, new = install
    calls = []

    def fake_execv(path, argv):
        calls.append((path, argv))

    info = version_guard.staleness(_script(old))
    version_guard.follow(info, argv=[sys.executable, _script(old)], execv=fake_execv)
    assert calls == [(sys.executable, [sys.executable, _script(new)])]


def test_follow_failure_keeps_serving_old_image(install, capsys):
    old, _ = install

    def boom(_path, _argv):
        raise OSError("etxtbsy")

    assert version_guard.follow(
        version_guard.staleness(_script(old)),
        argv=[sys.executable, _script(old)],
        execv=boom,
    ) is False
    assert "keep serving 0.1.0" in capsys.readouterr().err


def test_mcp_server_main_loop_follows_before_each_read(install, monkeypatch):
    """The guard runs before readline: exec happens in the idle gap."""
    import mcp_server as server

    _, new = install
    # Pretend this very process IS the versioned old server; `current`
    # has moved to `new`, so main must execv before the first readline.
    monkeypatch.setattr(server.version_guard, "staleness", lambda _p: {
        "own_version": "0.1.0", "current_version": "0.2.0",
        "target_script": _script(new),
    })
    execed = []
    monkeypatch.setattr(server.version_guard, "follow",
                        lambda info, **kw: execed.append(info["target_script"]) or True)
    monkeypatch.setattr(server.sys, "stdin", io.StringIO(""))

    server.main()

    assert execed == [_script(new)]


def test_version_guard_ships_without_heavy_imports():
    """The guard must stay import-clean (it runs inside the idle loop)."""
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import sys, json; sys.path.insert(0, {str(RUNTIME)!r}); "
         "import version_guard; "
         "print(json.dumps(sorted(m for m in sys.modules if m in "
         "('requests', 'paramiko', 'httpx'))))"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []
