"""终态 run 必须收敛残留的进行中 issue 行。

回归：Redmine #661066 的 run 因 GMS 身份预检超时以 failed 终态结束，但
issue 行停在 pending，前端读到「run 失败 + issue 进行中」的矛盾组合，持续
渲染「停止分析」按钮；停止端点对终态 run 只回 already_terminal，用户看到
的就是点了没反应。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from features.redmine.daily_brief.models import DailyBriefIssue
from features.redmine.daily_brief.repository import DailyBriefRepository


@pytest.fixture
def repository(tmp_path: Path) -> DailyBriefRepository:
    return DailyBriefRepository(tmp_path / "u1")


def _issue(run_id: str, issue_id: int, status: str) -> DailyBriefIssue:
    return DailyBriefIssue(
        run_id=run_id, issue_id=issue_id, buckets=[], status=status,
    )


def test_settle_pending_issues_converges_active_rows(repository):
    repository.upsert_issue(_issue("run-1", 661066, "pending"))
    repository.upsert_issue(_issue("run-1", 661067, "analyzing"))
    repository.upsert_issue(_issue("run-1", 661068, "completed"))

    changed = repository.settle_pending_issues("run-1", "failed", "identity preflight failed")

    assert changed == 2
    settled = repository.get_issue("run-1", 661066)
    assert settled.status == "failed"
    assert settled.error == "identity preflight failed"
    assert settled.finished_at  # terminal rows carry a timestamp
    # An issue that already reached a terminal state keeps its own verdict.
    assert repository.get_issue("run-1", 661068).status == "completed"


def test_settle_pending_issues_is_scoped_to_one_run(repository):
    repository.upsert_issue(_issue("run-1", 661066, "pending"))
    repository.upsert_issue(_issue("run-2", 661066, "pending"))

    repository.settle_pending_issues("run-1", "failed")

    assert repository.get_issue("run-1", 661066).status == "failed"
    assert repository.get_issue("run-2", 661066).status == "pending"


def test_settle_pending_issues_is_idempotent(repository):
    repository.upsert_issue(_issue("run-1", 661066, "pending"))
    assert repository.settle_pending_issues("run-1", "failed") == 1
    # Second pass has nothing left in flight.
    assert repository.settle_pending_issues("run-1", "failed") == 0
