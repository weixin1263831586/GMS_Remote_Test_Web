import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from foundation.sqlite_schema import ensure_schema, invalidate_schema


def test_readers_initialize_once_without_acquiring_write_lock_again(tmp_path):
    db = tmp_path / 'state.db'
    calls = []

    def initialize(conn):
        calls.append(True)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE entries (id TEXT)')

    def read():
        conn = sqlite3.connect(db, timeout=1)
        try:
            ensure_schema(conn, db, initialize)
            return conn.execute('SELECT count(*) FROM entries').fetchone()[0]
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: read(), range(16))) == [0] * 16
    assert calls == [True]
    # An unrelated writer may hold the lock while initialized readers proceed.
    writer = sqlite3.connect(db)
    try:
        writer.execute('BEGIN IMMEDIATE')
        assert read() == 0
    finally:
        writer.close()


def test_failed_initialization_is_not_cached(tmp_path):
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as conn:
        try:
            ensure_schema(conn, db, lambda _: (_ for _ in ()).throw(ValueError('migration failed')))
        except ValueError:
            pass
        ensure_schema(conn, db, lambda c: c.execute('CREATE TABLE entries (id TEXT)'))
        assert conn.execute('SELECT count(*) FROM entries').fetchone()[0] == 0


def test_rebuilt_file_with_same_inode_is_initialized(tmp_path):
    db = tmp_path / 'state.db'
    calls = []

    def initialize(conn):
        calls.append(True)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS entries (id TEXT)')

    with closing(sqlite3.connect(db)) as conn:
        ensure_schema(conn, db, initialize)
    inode = db.stat().st_ino
    db.write_bytes(b'')
    assert db.stat().st_ino == inode
    with closing(sqlite3.connect(db)) as conn:
        ensure_schema(conn, db, initialize)
        assert conn.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
    assert len(calls) == 2


def test_restore_invalidation_and_external_ddl_recheck_schema(tmp_path):
    db = tmp_path / 'state.db'
    calls = []

    def initialize(conn):
        calls.append(True)
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS entries (id TEXT)')

    with closing(sqlite3.connect(db)) as conn:
        ensure_schema(conn, db, initialize)
        conn.execute('DROP TABLE entries')
        conn.commit()
        ensure_schema(conn, db, initialize)
        invalidate_schema(db)
        ensure_schema(conn, db, initialize)
    assert len(calls) == 3
