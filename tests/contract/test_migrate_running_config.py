"""Contract: migrate_running_config.sh terminal-status guard stays model-sourced.

The offline migration refuses to stop services while durable jobs are
unfinished. Its automation terminal whitelist used to be hardcoded and
drifted from ``features.automation.models.TERMINAL_STATUSES`` — a historic
``aoa_failed`` run was misread as unfinished and blocked the whole
migration (audit P1). These tests pin the guard to the model-sourced set.
"""

import re
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "migrate_running_config.sh"


def _guard_python() -> str:
    """Extract the terminal-status guard heredoc from the migration script."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.DOTALL)
    assert match, "migration script must keep the terminal-status guard heredoc"
    return match.group(1)


def _run_guard(data_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _guard_python(), str(data_root)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
    )


def _seed_runs(data_root: Path, statuses: tuple[str, ...]) -> None:
    database = data_root / "data" / "automation" / "automation.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE automation_runs (status TEXT)")
        connection.executemany(
            "INSERT INTO automation_runs VALUES (?)", [(status,) for status in statuses]
        )


def test_aoa_failed_run_does_not_block_migration(tmp_path):
    """终态 aoa_failed 曾被硬编码白名单漏掉：不得再误判为未完成。"""
    _seed_runs(tmp_path, ("aoa_failed",))
    result = _run_guard(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Unfinished jobs exist" not in result.stdout


def test_every_model_terminal_status_is_accepted(tmp_path):
    """TERMINAL_STATUSES 里的每个终态都必须放行，防止再次漂移。"""
    from features.automation.models import TERMINAL_STATUSES

    _seed_runs(tmp_path, tuple(sorted(TERMINAL_STATUSES)))
    result = _run_guard(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unfinished_run_still_blocks_migration(tmp_path):
    """守护语义保持：非终态 Run 仍然阻断迁移并给出原提示。"""
    _seed_runs(tmp_path, ("testing",))
    result = _run_guard(tmp_path)
    assert result.returncode != 0
    assert "Unfinished jobs exist" in result.stdout + result.stderr


def test_guard_imports_terminal_statuses_from_models():
    """守护代码必须从模型导入终态，禁止回退成硬编码白名单。"""
    code = _guard_python()
    assert "from features.automation.models import TERMINAL_STATUSES" in code
    # 白名单回归的特征：automation 行内重新出现字面量终态元组
    automation_line = re.search(
        r"'automation/automation\.sqlite3'.*?\)", code, re.DOTALL)
    assert automation_line, "automation guard entry must stay declared"
    assert "aoa_failed" not in automation_line.group(0)
