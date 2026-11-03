"""Owner-scoped retention for terminal-job stdout/stderr; reports are preserved."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from foundation.error_model import ApiError


def initialize_retention_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS cluster_retention_policies (
        owner_id TEXT PRIMARY KEY, username TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
        archive_days INTEGER NOT NULL DEFAULT 7, delete_days INTEGER NOT NULL DEFAULT 30,
        last_run_at TEXT NOT NULL DEFAULT '')''')
    conn.execute('''CREATE TABLE IF NOT EXISTS cluster_artifact_pins (
        artifact_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, pinned INTEGER NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS cluster_retention_journal (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
        operation TEXT NOT NULL, old_path TEXT NOT NULL, new_path TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)''')


class ArtifactRetention:
    def __init__(self, repository):
        self.repo = repository
        self.root = (repository.db_path.parent / 'artifacts').resolve()

    def policy(self, owner):
        with self.repo.connect() as conn:
            row = conn.execute('SELECT * FROM cluster_retention_policies WHERE owner_id=?', (owner,)).fetchone()
        return dict(row) if row else {'owner_id': owner, 'enabled': False, 'archive_days': 7, 'delete_days': 30}

    def save_policy(self, principal, *, enabled, archive_days, delete_days):
        if not 1 <= archive_days <= 3650 or (delete_days and not archive_days < delete_days <= 3650):
            raise ApiError.malformed_request('Delete age must exceed archive age; zero disables deletion')
        with self.repo.connect() as conn:
            conn.execute('''INSERT INTO cluster_retention_policies
                (owner_id,username,enabled,archive_days,delete_days) VALUES (?,?,?,?,?)
                ON CONFLICT(owner_id) DO UPDATE SET username=excluded.username,enabled=excluded.enabled,
                    archive_days=excluded.archive_days,delete_days=excluded.delete_days''',
                         (principal.resource_owner_id, principal.username, enabled, archive_days, delete_days))
        return self.policy(principal.resource_owner_id)

    def owned_artifact(self, conn, owner, artifact_id):
        row = conn.execute('''SELECT a.*,j.status AS job_status,COALESCE(p.pinned,0) AS pinned
            FROM cluster_job_artifacts a JOIN cluster_jobs j ON j.id=a.job_id
            LEFT JOIN cluster_artifact_pins p ON p.artifact_id=a.id
            WHERE a.id=? AND j.owner_id=?''', (artifact_id, owner)).fetchone()
        if not row:
            raise ApiError.not_found('Artifact not found')
        return dict(row)

    def pin(self, owner, artifact_id, pinned):
        with self.repo.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.owned_artifact(conn, owner, artifact_id)
            conn.execute('''INSERT INTO cluster_artifact_pins VALUES (?,?,?)
                ON CONFLICT(artifact_id) DO UPDATE SET pinned=excluded.pinned''', (artifact_id, owner, pinned))

    def _path(self, relative):
        path = self.root / relative
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(self.root) or any(
            parent.is_symlink() for parent in path.parents if parent != self.root and parent.is_relative_to(self.root)
        ):
            raise ApiError.conflict('Artifact path is not a regular retained path')
        return path

    @staticmethod
    def operation(item, policy):
        if (item['pinned'] or item['job_status'] not in ('completed', 'failed', 'cancelled')
                or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            return ''
        try:
            created = datetime.fromisoformat(item['created_at'].replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - created.replace(tzinfo=created.tzinfo or timezone.utc)).total_seconds() / 86400
        except ValueError:
            return ''
        if item['filename'] in ('stdout.log', 'stderr.log') and age >= policy['archive_days'] and item['size_bytes'] <= 256 * 1024**2:
            return 'archive'
        if (item['filename'] in ('stdout.log.gz', 'stderr.log.gz') and item['artifact_type'] == 'retained_log'
                and policy['delete_days'] and age >= policy['delete_days']):
            return 'delete'
        return ''

    def preview(self, owner):
        policy = self.policy(owner)
        with self.repo.connect() as conn:
            rows = conn.execute('''SELECT a.*,j.status AS job_status,COALESCE(p.pinned,0) AS pinned
                FROM cluster_job_artifacts a JOIN cluster_jobs j ON j.id=a.job_id
                LEFT JOIN cluster_artifact_pins p ON p.artifact_id=a.id WHERE j.owner_id=?
                ORDER BY a.created_at DESC,a.id LIMIT 1000''', (owner,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            items.append({key: item[key] for key in ('id', 'job_id', 'filename', 'size_bytes', 'sha256', 'created_at', 'pinned')} |
                         {'operation': self.operation(item, policy)})
        return {'policy': {key: policy[key] for key in ('enabled', 'archive_days', 'delete_days')},
                'items': items, 'total_bytes': sum(item['size_bytes'] for item in items),
                'candidate_bytes': sum(item['size_bytes'] for item in items if item['operation']),
                'limited_to': 1000}

    def _check(self, conn, owner, selection, policy):
        item = self.owned_artifact(conn, owner, selection['id'])
        operation = self.operation(item, policy)
        if not operation or selection['operation'] != operation or selection['sha256'] != item['sha256']:
            raise ApiError.conflict('Artifact, pin or retention policy changed; refresh the preview')
        path = self._path(item['relative_path'])
        if not path.is_file():
            raise ApiError.conflict('Artifact file is unavailable')
        return item, path, operation

    def apply(self, owner, selections):
        if not selections or len(selections) > 25:
            raise ApiError.malformed_request('Select between 1 and 25 previewed log artifacts')
        if len({selection['id'] for selection in selections}) != len(selections):
            raise ApiError.malformed_request('Duplicate artifact selection')
        self.recover()
        policy = self.policy(owner)
        with self.repo.connect() as conn:
            for selection in selections:
                self._check(conn, owner, selection, policy)
        completed = []
        for selection in selections:
            self._apply_one(owner, selection)
            completed.append(selection['id'])
        return {'completed': completed}

    def _apply_one(self, owner, selection):
        policy = self.policy(owner)
        with self.repo.connect() as conn:
            item, source, operation = self._check(conn, owner, selection, policy)
        identity = source.stat()
        operation_id = 'retention-' + uuid.uuid4().hex
        temporary = source.with_name('.' + operation_id + '.tmp')
        target = source.with_name(source.name + '.' + operation_id + ('.gz' if operation == 'archive' else '.trash'))
        try:
            if operation == 'archive':
                original_checksum = hashlib.sha256()
                with source.open('rb') as input_file, gzip.open(temporary, 'wb', compresslevel=1) as output:
                    for block in iter(lambda: input_file.read(1024 * 1024), b''):
                        original_checksum.update(block)
                        output.write(block)
                if original_checksum.hexdigest() != item['sha256']:
                    raise ApiError.conflict('Artifact contents differ from the preview fingerprint')
                checksum = hashlib.sha256()
                with temporary.open('rb') as compressed:
                    for block in iter(lambda: compressed.read(1024 * 1024), b''):
                        checksum.update(block)
            else:
                checksum = hashlib.sha256()
                with source.open('rb') as input_file:
                    for block in iter(lambda: input_file.read(1024 * 1024), b''):
                        checksum.update(block)
                if checksum.hexdigest() != item['sha256']:
                    raise ApiError.conflict('Artifact contents differ from the preview fingerprint')
            with self.repo.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                self._check(conn, owner, selection, self.policy(owner))
                current = source.stat()
                if (current.st_size, current.st_mtime_ns, current.st_ino) != (identity.st_size, identity.st_mtime_ns, identity.st_ino):
                    raise ApiError.conflict('Artifact changed during archive preparation')
                conn.execute('INSERT INTO cluster_retention_journal VALUES (?,?,?,?,?,?,?,?)',
                             (operation_id, owner, item['id'], operation, str(source.relative_to(self.root)),
                              str(target.relative_to(self.root)), 'pending', datetime.now(timezone.utc).isoformat()))
            with self.repo.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                self._check(conn, owner, selection, self.policy(owner))
                final_stat = source.stat()
                if (final_stat.st_size, final_stat.st_mtime_ns, final_stat.st_ino) != (identity.st_size, identity.st_mtime_ns, identity.st_ino):
                    raise ApiError.conflict('Artifact changed before retention commit')
                if operation == 'archive':
                    os.replace(temporary, target)
                    conn.execute('''UPDATE cluster_job_artifacts SET filename=?,relative_path=?,artifact_type='retained_log',
                        size_bytes=?,sha256=? WHERE id=?''', (item['filename'] + '.gz', str(target.relative_to(self.root)),
                                                             target.stat().st_size, checksum.hexdigest(), item['id']))
                else:
                    os.replace(source, target)
                    conn.execute('DELETE FROM cluster_job_artifacts WHERE id=?', (item['id'],))
                self.repo._append_timeline_conn(conn, event_type='artifact.' + operation, source='retention',
                                               message='Retained log ' + operation, job_id=item['job_id'],
                                               attempt_id=item['attempt_id'], operation_id=operation_id,
                                               payload={'artifact_id': item['id'], 'sha256': item['sha256'], 'owner_id': owner})
            (source if operation == 'archive' else target).unlink(missing_ok=True)
            with self.repo.connect() as conn:
                conn.execute("UPDATE cluster_retention_journal SET status='completed' WHERE id=?", (operation_id,))
        except Exception:
            self.recover(operation_id=operation_id)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def recover(self, *, operation_id=None):
        """Recover only journaled paths, never scan or remove arbitrary runtime files."""
        with self.repo.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT * FROM cluster_retention_journal WHERE status='pending' AND " +
                                ('id=?' if operation_id else 'created_at<?'),
                                (operation_id or (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),)).fetchall()
            for row in rows:
                old, new = self._path(row['old_path']), self._path(row['new_path'])
                artifact = conn.execute('SELECT relative_path FROM cluster_job_artifacts WHERE id=?', (row['artifact_id'],)).fetchone()
                if row['operation'] == 'archive':
                    if artifact and artifact['relative_path'] == row['new_path'] and new.is_file():
                        old.unlink(missing_ok=True)
                    elif old.is_file():
                        new.unlink(missing_ok=True)
                elif artifact and not old.exists() and new.is_file():
                    os.replace(new, old)
                elif artifact is None:
                    new.unlink(missing_ok=True)
                conn.execute("UPDATE cluster_retention_journal SET status='recovered' WHERE id=?", (row['id'],))


def run_enabled_retention(repository):
    from features.auth import auth_service

    service = ArtifactRetention(repository)
    service.recover()
    threshold = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with repository.connect() as conn:
        policies = [dict(row) for row in conn.execute('SELECT * FROM cluster_retention_policies WHERE enabled=1 AND last_run_at<?', (threshold,))]
    for policy in policies:
        user = auth_service.get_enabled_user(policy['username'])
        if not user or user.resource_owner_id != policy['owner_id']:
            continue
        with repository.connect() as conn:
            claimed = conn.execute('''UPDATE cluster_retention_policies SET last_run_at=?
                WHERE owner_id=? AND enabled=1 AND last_run_at=?''',
                                   (datetime.now(timezone.utc).isoformat(), policy['owner_id'], policy['last_run_at'])).rowcount
        if claimed:
            selections = [{'id': item['id'], 'sha256': item['sha256'], 'operation': item['operation']}
                          for item in service.preview(policy['owner_id'])['items'] if item['operation']][:5]
            if selections:
                service.apply(policy['owner_id'], selections)
