"""mainline/update_monitor 读路径的一次性 schema 初始化回归。

审核项：_connect_db 曾在每个请求连接上跑 init_db（mainline 的 init 持
BEGIN IMMEDIATE 写锁），并发读会互相排队。现在经 foundation 的
ensure_schema 进程内只初始化一次。
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.system import mainline_issues, update_monitor
from foundation import sqlite_schema


def _assert_init_runs_once(testcase: unittest.TestCase, module) -> None:
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "db.sqlite3"
        real_init = module.init_db
        calls = []

        def counting_init(conn):
            calls.append(1)
            real_init(conn)

        def connect_and_read():
            conn = module._connect_db()
            try:
                conn.execute("SELECT 1").fetchall()
            finally:
                conn.close()

        with patch.object(module, "DB_PATH", db_path), \
                patch.object(module, "init_db", side_effect=counting_init):
            try:
                for _ in range(3):
                    connect_and_read()
                threads = [threading.Thread(target=connect_and_read) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                testcase.assertEqual(len(calls), 1)
            finally:
                sqlite_schema.invalidate_schema(db_path)


class MainlineInitOnceTests(unittest.TestCase):
    def test_init_db_runs_once_per_process_across_connections(self):
        _assert_init_runs_once(self, mainline_issues.api)


class UpdateMonitorInitOnceTests(unittest.TestCase):
    def test_init_db_runs_once_per_process_across_connections(self):
        _assert_init_runs_once(self, update_monitor.api_support)


if __name__ == "__main__":
    unittest.main()
