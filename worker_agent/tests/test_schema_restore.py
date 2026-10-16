import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing

import pytest

from worker_agent.config import WorkerConfig
from worker_agent.runtime import WorkerRuntime


def config(root):
    return WorkerConfig(worker_id='test', controller_url='https://controller.invalid',
                        token='synthetic-test-token', data_root=root)


def legacy_database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''
            CREATE TABLE commands (id TEXT PRIMARY KEY, status TEXT NOT NULL, result_json TEXT NOT NULL,
                error TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE jobs (worker_job_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
                pid INTEGER, pgid INTEGER, status TEXT NOT NULL, devices_json TEXT NOT NULL,
                work_dir TEXT NOT NULL, exit_code INTEGER, error TEXT NOT NULL);
            CREATE TABLE device_fences (device_id TEXT PRIMARY KEY, generation INTEGER NOT NULL,
                lease_id TEXT NOT NULL, attempt_id TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE usbip_assignments (source_host TEXT NOT NULL, busid TEXT NOT NULL,
                adb_server_socket TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(source_host,busid));
            INSERT INTO commands (id,status,result_json,error) VALUES ('legacy','done','{}','');
        ''')


def initialized_columns(root):
    runtime = WorkerRuntime(config(root))
    with closing(runtime.connect()) as conn:
        return {table: {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
                for table in ('commands', 'jobs', 'usbip_assignments')}


@pytest.mark.parametrize('replace', [False, True])
def test_running_worker_migrates_legacy_schema_after_restore(tmp_path, replace):
    runtime = WorkerRuntime(config(tmp_path))
    if replace:
        restored = tmp_path / 'restored.db'
        legacy_database(restored)
        restored.replace(runtime.db_path)
    else:
        runtime.db_path.unlink()
        legacy_database(runtime.db_path)
    with closing(runtime.connect()) as conn:
        assert 'operation_id' in {row[1] for row in conn.execute('PRAGMA table_info(jobs)')}
        assert 'controller_synced' in {row[1] for row in conn.execute('PRAGMA table_info(commands)')}
        assert 'generation' in {row[1] for row in conn.execute('PRAGMA table_info(usbip_assignments)')}
        assert conn.execute('PRAGMA user_version').fetchone()[0] == runtime._SCHEMA_VERSION
        assert conn.execute("SELECT controller_synced FROM commands WHERE id='legacy'").fetchone()[0] == 0


def test_concurrent_processes_migrate_one_legacy_database(tmp_path):
    legacy_database(tmp_path / 'worker.sqlite3')
    with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context('spawn')) as pool:
        results = list(pool.map(initialized_columns, [tmp_path] * 3))
    assert all('operation_id' in result['jobs'] and 'controller_synced' in result['commands']
               and 'generation' in result['usbip_assignments'] for result in results)
