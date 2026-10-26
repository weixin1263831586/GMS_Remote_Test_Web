"""Deleting history serializes with independently enqueued analysis jobs."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from features.redmine.daily_brief_models import DailyBriefRun
from features.redmine.daily_brief_repository import DailyBriefRepository


def test_delete_holds_write_lock_before_active_check(tmp_path, monkeypatch):
    deleting = DailyBriefRepository(tmp_path)
    queue = DailyBriefRepository(tmp_path)
    deleting.create_run(DailyBriefRun(owner_id="owner", brief_date="2026-10-23",
                                     mode="issue:1", run_id="one", status="completed"))
    checked, finish_delete, trying_enqueue = Event(), Event(), Event()
    connect = deleting._connect

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, sql, params=()):
            cursor = self.conn.execute(sql, params)
            if sql.startswith("SELECT COUNT(*) FROM redmine_daily_brief_jobs"):
                assert self.conn.in_transaction
                checked.set()
                assert finish_delete.wait(timeout=5)
            return cursor

    monkeypatch.setattr(deleting, "_connect", lambda: Connection(connect()))

    def enqueue():
        trying_enqueue.set()
        return queue.enqueue_job("one")

    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(deleting.delete_issue_history, "owner", 1)
        try:
            assert checked.wait(timeout=5)
            queued = pool.submit(enqueue)
            assert trying_enqueue.wait(timeout=5)
            # An independent writer cannot enqueue between the check and
            # deletion; after commit it must observe the deleted run.
            with pytest.raises(TimeoutError):
                queued.result(timeout=0.1)
        finally:
            finish_delete.set()
        assert deletion.result(timeout=5) == 1
        with pytest.raises(ValueError, match="not found"):
            queued.result(timeout=5)
    assert queue.get_run("one") is None
