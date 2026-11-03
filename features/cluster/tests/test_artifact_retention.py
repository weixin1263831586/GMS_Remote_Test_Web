"""Retention preserves reports, pins, active jobs and account ownership."""

import gzip
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from features.auth import CurrentUser
from features.cluster.artifact_retention import ArtifactRetention
from features.cluster.repository import ClusterRepository
from foundation.error_model import ApiError


@pytest.fixture
def context(tmp_path):
    repo = ClusterRepository(tmp_path / 'cluster.sqlite3')
    repo.register_worker({'worker_id': 'w', 'name': 'Worker', 'hostname': 'worker',
                          'address': '192.0.2.1', 'agent_version': '1', 'capabilities': {'adb': True}})
    repo.heartbeat('w', {'devices': [{'serial': 'A', 'state': 'available'}], 'suites': [], 'running_jobs': []})
    job = repo.create_job_with_leases({'worker_id': 'w', 'owner_id': 'alice', 'devices': ['w:A']})
    command = repo.create_command({'worker_id': 'w', 'command_type': 'start_test',
                                   'job_id': job['id'], 'attempt_id': job['current_attempt_id'], 'payload': {}})
    completed = repo.ack_command('w', command['id'], {'status': 'completed', 'result': {}, 'error': ''})
    repo.sync_job_from_command(completed)
    return repo, ArtifactRetention(repo), job


def add_artifact(context, filename='stdout.log', age=10):
    repo, service, job = context
    path = service.root / job['id'] / job['current_attempt_id'] / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'test log evidence\n' * 100)
    row = repo.record_artifact({'job_id': job['id'], 'attempt_id': job['current_attempt_id'], 'worker_id': 'w',
                               'filename': filename, 'relative_path': str(path.relative_to(service.root)),
                               'size_bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    with repo.connect() as conn:
        conn.execute('UPDATE cluster_job_artifacts SET created_at=? WHERE id=?',
                     ((datetime.now(timezone.utc) - timedelta(days=age)).isoformat(), row['id']))
    return row, path


def selection(service, owner='alice'):
    return [{key: item[key] for key in ('id', 'sha256', 'operation')}
            for item in service.preview(owner)['items'] if item['operation']]


def test_archive_then_expire_log_preserves_reports_and_summary(context):
    repo, service, job = context
    log, path = add_artifact(context)
    report, report_path = add_artifact(context, 'test_result.xml', age=100)
    service.apply('alice', selection(service))
    archived = next(item for item in repo.list_artifacts(job['id']) if item['id'] == log['id'])
    assert archived['filename'] == 'stdout.log.gz'
    assert not path.exists()
    archived_path = service.root / archived['relative_path']
    with gzip.open(archived_path, 'rb') as stream:
        assert stream.read() == b'test log evidence\n' * 100
    with repo.connect() as conn:
        conn.execute('UPDATE cluster_job_artifacts SET created_at=? WHERE id=?',
                     ((datetime.now(timezone.utc) - timedelta(days=50)).isoformat(), log['id']))
    service.apply('alice', selection(service))
    assert not archived_path.exists()
    assert report_path.exists()
    assert repo.get_job(job['id'])['status'] == 'completed'
    assert repo.list_artifacts(job['id'])[0]['id'] == report['id']


def test_pin_and_owner_checks_are_revalidated_after_preview(context):
    _, service, _ = context
    log, path = add_artifact(context)
    reviewed = selection(service)
    assert not service.preview('bob')['items']
    with pytest.raises(ApiError) as exc:
        service.pin('bob', log['id'], True)
    assert exc.value.code == 'NOT_FOUND'
    service.pin('alice', log['id'], True)
    with pytest.raises(ApiError) as exc:
        service.apply('alice', reviewed)
    assert exc.value.code == 'STATE_CONFLICT'
    assert path.is_file()


def test_active_jobs_and_changed_log_contents_are_preserved(context):
    repo, service, job = context
    _, path = add_artifact(context)
    reviewed = selection(service)
    with repo.connect() as conn:
        conn.execute("UPDATE cluster_jobs SET status='running' WHERE id=?", (job['id'],))
    assert not selection(service)
    with repo.connect() as conn:
        conn.execute("UPDATE cluster_jobs SET status='completed' WHERE id=?", (job['id'],))
    path.write_bytes(b'new evidence')
    with pytest.raises(ApiError):
        service.apply('alice', reviewed)
    assert path.read_bytes() == b'new evidence'


def test_journal_recovers_interrupted_delete_without_scanning_unregistered_files(context):
    repo, service, _ = context
    log, path = add_artifact(context)
    trash = path.with_name('recorded.trash')
    path.rename(trash)
    unrelated = path.parent / 'unrelated.log'
    unrelated.write_text('keep')
    with repo.connect() as conn:
        conn.execute('INSERT INTO cluster_retention_journal VALUES (?,?,?,?,?,?,?,?)',
                     ('op', 'alice', log['id'], 'delete', str(path.relative_to(service.root)),
                      str(trash.relative_to(service.root)), 'pending', datetime.now(timezone.utc).isoformat()))
    service.recover(operation_id='op')
    assert path.is_file()
    assert not trash.exists()
    assert unrelated.read_text() == 'keep'


def test_retention_policy_requires_valid_age_order(context):
    _, service, _ = context
    owner = CurrentUser(id='alice', username='alice', role='user')
    with pytest.raises(ApiError):
        service.save_policy(owner, enabled=True, archive_days=30, delete_days=7)
    assert service.save_policy(owner, enabled=False, archive_days=7, delete_days=0)['delete_days'] == 0


def test_changed_compressed_log_is_not_deleted(context):
    repo, service, job = context
    log, _ = add_artifact(context, age=50)
    service.apply('alice', selection(service))
    reviewed = selection(service)
    assert reviewed[0]['operation'] == 'delete'
    archived = next(item for item in repo.list_artifacts(job['id']) if item['id'] == log['id'])
    path = service.root / archived['relative_path']
    path.write_bytes(b'replaced evidence')
    with pytest.raises(ApiError) as exc:
        service.apply('alice', reviewed)
    assert exc.value.code == 'STATE_CONFLICT'
    assert path.read_bytes() == b'replaced evidence'
    assert repo.list_artifacts(job['id'])[0]['id'] == log['id']
