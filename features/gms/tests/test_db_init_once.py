"""Unified database schema initializes once across concurrent query connections."""
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.gms import mainline_queries, queries, repository
from foundation import database


class GmsUpdateInitOnceTests(unittest.TestCase):
    def test_both_query_domains_share_one_schema_initialization(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gms_update.sqlite3'
            real_init = repository.init_db
            calls = []
            errors = []
            def counting_init(conn):
                calls.append(1)
                real_init(conn)
            def read(module):
                try:
                    conn = module._connect_db()
                    try:
                        conn.execute('SELECT 1').fetchall()
                    finally:
                        conn.close()
                except Exception as exc:
                    errors.append(exc)
            with patch.object(repository, 'init_db', side_effect=counting_init), \
                 patch.object(queries, 'DB_PATH', path), \
                 patch.object(mainline_queries, 'DB_PATH', path):
                try:
                    threads = [threading.Thread(target=read,args=(module,))
                               for module in (queries,mainline_queries) for _ in range(4)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join()
                    self.assertFalse(errors)
                    self.assertEqual(len(calls),1)
                finally:
                    database.invalidate_schema(path)

    def test_failed_migration_rolls_back_all_domain_schema_changes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'gms_update.sqlite3'
            with (patch.object(repository, 'init_mainline', side_effect=RuntimeError('migration failed')),
                  self.assertRaisesRegex(RuntimeError, 'migration failed')):
                repository.connect(path)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertIsNone(conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='gms_update_scan_runs'"
                ).fetchone())
            finally:
                conn.close()
            conn = repository.connect(path)
            try:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],
                                 repository.SCHEMA_VERSION)
            finally:
                conn.close()
                database.invalidate_schema(path)
