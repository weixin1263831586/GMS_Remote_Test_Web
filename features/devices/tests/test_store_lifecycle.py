import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from features.devices import debug_service, deployment_service
from features.devices.debug_store import DebugStore
from features.devices.deployment_store import DeploymentStore
from foundation.error_model import ApiError


def test_polling_reuses_initialized_stores(tmp_path, monkeypatch):
    cluster = SimpleNamespace(repository=SimpleNamespace(db_path=tmp_path / 'cluster.sqlite3'))
    monkeypatch.setattr(deployment_service, 'get_cluster_service', lambda: cluster)
    deployments = deployment_service.store()
    debug = debug_service.store()
    # Reads and factory lookups must proceed while another process owns a write lock.
    with deployments.connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert deployment_service.store() is deployments
    with debug.connect() as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert debug_service.store() is debug


@pytest.mark.parametrize('service,table', [
    (deployment_service, 'deployments'), (debug_service, 'debug_sessions'),
])
@pytest.mark.parametrize('rebuild', ['replace', 'truncate', 'legacy'])
def test_cached_stores_reinitialize_rebuilt_databases(tmp_path, monkeypatch, service, table, rebuild):
    cluster = SimpleNamespace(repository=SimpleNamespace(db_path=tmp_path / 'cluster.sqlite3'))
    monkeypatch.setattr(deployment_service, 'get_cluster_service', lambda: cluster)
    cached = service.store()
    if rebuild == 'replace':
        replacement = tmp_path / 'replacement.db'
        with closing(sqlite3.connect(replacement)):
            pass
        replacement.replace(cached.db)
    elif rebuild == 'truncate':
        cached.db.write_bytes(b'')
    else:
        # A restore can contain an older schema instead of an empty file.
        cached.db.unlink()
        with closing(sqlite3.connect(cached.db)) as conn:
            if service is deployment_service:
                conn.execute('CREATE TABLE deployments (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, '
                             "status TEXT NOT NULL, expires_at REAL NOT NULL, payload TEXT NOT NULL, "
                             "result TEXT NOT NULL DEFAULT '{}')")
            else:
                conn.execute('CREATE TABLE debug_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, '
                             "actor_id TEXT NOT NULL, status TEXT NOT NULL, expires_at REAL NOT NULL, "
                             "busy TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL)")
            conn.commit()
    assert service.store() is cached
    with closing(cached.connect()) as conn:
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        if service is deployment_service:
            assert 'execution_token' in {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        else:
            assert conn.execute('SELECT count(*) FROM debug_operations').fetchone()[0] == 0


def test_approval_precheck_keeps_queue_alive_without_reopening_expired_work(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('features.devices.deployment_store.time.time', lambda: now[0])
    store = DeploymentStore(tmp_path)
    store.create({'id': 'request', 'owner_id': 'owner', 'expires_at': now[0] + 300})
    store.start('request', 'owner', 'human')
    for _ in range(4):
        now[0] += 100
        store.approval_heartbeat('request')
    assert store.get('request', 'owner')['status'] == 'queued'
    now[0] += 121
    with pytest.raises(ApiError):
        store.approval_heartbeat('request')
    assert store.get('request', 'owner')['status'] == 'interrupted'


def test_debug_approval_renews_only_while_reserved(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr('features.devices.debug_store.time.time', lambda: now[0])
    store = DebugStore(Path(tmp_path))
    store.create({'id': 'session', 'owner_id': 'owner', 'actor_id': 'agent', 'expires_at': 1300.0})
    store.reserve_approval('session', 'owner')
    now[0] = 1290.0
    store.approval_heartbeat('session')
    now[0] = 1310.0
    assert store.activate('session', 'owner', {'expires_at': 1400.0})['status'] == 'active'
    with pytest.raises(ApiError):
        store.approval_heartbeat('session')
