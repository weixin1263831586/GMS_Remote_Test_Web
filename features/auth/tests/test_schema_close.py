"""initialize_auth_schema 必须关闭 sqlite 连接。

``with sqlite3.connect(...)`` 只管理事务不关闭连接；初始化在多个
Web/Worker/CLI 进程里高频执行，泄漏的连接会耗尽 fd。用包装连接记录
close 调用钉住 closing() 语义。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class _TrackedConnection:
    def __init__(self, inner: sqlite3.Connection):
        self._inner = inner
        self.closed = False

    def close(self):
        self.closed = True
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)


class AuthSchemaConnectionCloseTests(unittest.TestCase):
    def test_schema_init_closes_connection_even_on_success(self):
        from features.auth import schema

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "auth.sqlite3"
            originals: list[_TrackedConnection] = []
            original_connect = sqlite3.connect

            def tracked_connect(*args, **kwargs):
                conn = _TrackedConnection(original_connect(*args, **kwargs))
                originals.append(conn)
                return conn

            with patch.object(schema.sqlite3, "connect", side_effect=tracked_connect):
                schema.initialize_auth_schema(db_path)

            self.assertTrue(originals)
            self.assertTrue(all(conn.closed for conn in originals))

    def test_schema_init_closes_connection_on_error(self):
        from features.auth import schema

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "auth.sqlite3"
            originals: list[_TrackedConnection] = []
            original_connect = sqlite3.connect

            def tracked_connect(*args, **kwargs):
                conn = _TrackedConnection(original_connect(*args, **kwargs))
                originals.append(conn)
                return conn

            with patch.object(schema.sqlite3, "connect", side_effect=tracked_connect), patch.object(
                schema, "initialize_auth_attempt_schema", side_effect=sqlite3.OperationalError("boom")
            ), self.assertRaises(sqlite3.OperationalError):
                schema.initialize_auth_schema(db_path)

            self.assertTrue(originals)
            self.assertTrue(all(conn.closed for conn in originals))


if __name__ == "__main__":
    unittest.main()
