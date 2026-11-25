"""One database for all GMS update categories and durable scan evidence."""
from contextlib import closing
from pathlib import Path

from foundation.config import settings
from foundation.database import open_initialized_sqlite

from .mainline_records import init_db as init_mainline
from .update_records import init_db as init_updates


DB_PATH = settings.data_root / 'gms_update.sqlite3'
SCHEMA_VERSION = 3


def init_db(conn):
    # All domain migrations and the final version stamp share one write lock.
    # A failed migration must not publish a partly upgraded unified database.
    conn.execute('BEGIN IMMEDIATE')
    init_updates(conn, manage_transaction=False)
    init_mainline(conn, manage_transaction=False)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(gms_update_scan_runs)')}
    for name, definition in (
        ('state', "TEXT NOT NULL DEFAULT 'completed'"),
        ('parser_version', "TEXT NOT NULL DEFAULT ''"),
        ('trigger', "TEXT NOT NULL DEFAULT 'manual'"),
    ):
        if name not in columns:
            conn.execute(f'ALTER TABLE gms_update_scan_runs ADD COLUMN {name} {definition}')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(gms_update_change_events)')}
    if 'origin' not in columns:
        conn.execute("ALTER TABLE gms_update_change_events ADD COLUMN origin TEXT NOT NULL DEFAULT 'unclassified'")
    conn.execute('''CREATE TABLE IF NOT EXISTS gms_update_source_results (
        run_id INTEGER NOT NULL, source_key TEXT NOT NULL, state TEXT NOT NULL,
        discovered INTEGER NOT NULL DEFAULT 0, fetched INTEGER NOT NULL DEFAULT 0,
        parsed INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(run_id, source_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS gms_update_documents (
        run_id INTEGER NOT NULL, source_key TEXT NOT NULL, url TEXT NOT NULL,
        status_code INTEGER NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL,
        attempts INTEGER NOT NULL, PRIMARY KEY(run_id, source_key, url))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_update_source_results ON gms_update_source_results(source_key,run_id)')
    conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
    conn.commit()


def connect(path: Path = DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return open_initialized_sqlite(path, init_db, timeout=30)


def initialize(path: Path):
    with closing(connect(path)):
        pass
