#!/usr/bin/env python3
"""Encrypted, verified backup and offline restore for GMS Remote Test.

The archive contains runtime data and the small set of host-local files needed
to recover the Controller.  Archives are authenticated with AES-256-GCM and
carry a SHA-256 manifest.  Restore is deliberately offline and refuses to run
while a Controller owns the process lock.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAGIC = b'GMSBACKUP\x01'
NONCE_SIZE = 12
TAG_SIZE = 16
SCHEMA_VERSION = 1
ARCHIVE_SUFFIX = '.gmsbak'


class BackupError(RuntimeError):
    """Expected validation or backup failure."""


class _EncryptingWriter:
    def __init__(self, target: BinaryIO, key: bytes) -> None:
        self._target = target
        self._nonce = os.urandom(NONCE_SIZE)
        self._encryptor = Cipher(algorithms.AES(key), modes.GCM(self._nonce)).encryptor()
        self._encryptor.authenticate_additional_data(MAGIC)
        self._target.write(MAGIC + self._nonce)
        self._position = 0
        self._finished = False

    def write(self, data: bytes) -> int:
        if self._finished:
            raise ValueError('backup encryption stream is closed')
        payload = bytes(data)
        encrypted = self._encryptor.update(payload)
        if encrypted:
            self._target.write(encrypted)
        self._position += len(payload)
        return len(payload)

    def tell(self) -> int:
        return self._position

    def flush(self) -> None:
        self._target.flush()

    def finish(self) -> None:
        if self._finished:
            return
        tail = self._encryptor.finalize()
        if tail:
            self._target.write(tail)
        self._target.write(self._encryptor.tag)
        self._target.flush()
        os.fsync(self._target.fileno())
        self._finished = True


class _DecryptingReader(io.RawIOBase):
    def __init__(self, source: BinaryIO, key: bytes) -> None:
        self._source = source
        header = source.read(len(MAGIC) + NONCE_SIZE)
        if len(header) != len(MAGIC) + NONCE_SIZE or not header.startswith(MAGIC):
            raise BackupError('not a supported GMS backup archive')
        nonce = header[len(MAGIC) :]
        source.seek(0, os.SEEK_END)
        archive_size = source.tell()
        ciphertext_start = len(MAGIC) + NONCE_SIZE
        ciphertext_size = archive_size - ciphertext_start - TAG_SIZE
        if ciphertext_size <= 0:
            raise BackupError('backup archive is truncated')
        source.seek(archive_size - TAG_SIZE)
        tag = source.read(TAG_SIZE)
        source.seek(ciphertext_start)
        self._remaining = ciphertext_size
        self._decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        self._decryptor.authenticate_additional_data(MAGIC)
        self._buffer = bytearray()
        self._finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        requested = len(buffer)
        while len(self._buffer) < requested and not self._finished:
            if self._remaining:
                chunk = self._source.read(min(1024 * 1024, self._remaining))
                if not chunk:
                    raise BackupError('backup ciphertext is truncated')
                self._remaining -= len(chunk)
                self._buffer.extend(self._decryptor.update(chunk))
            else:
                try:
                    self._buffer.extend(self._decryptor.finalize())
                except Exception as exc:
                    raise BackupError('backup authentication failed (wrong key or corrupted archive)') from exc
                self._finished = True
        count = min(requested, len(self._buffer))
        buffer[:count] = self._buffer[:count]
        del self._buffer[:count]
        return count


def _load_key(path: Path) -> bytes:
    try:
        encoded = path.read_bytes().strip()
    except OSError as exc:
        raise BackupError(f'cannot read backup key {path}: {exc}') from exc
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f'backup key permissions must be 0600: {path}')
    try:
        key = base64.urlsafe_b64decode(encoded)
    except Exception as exc:
        raise BackupError('backup key must be URL-safe base64') from exc
    if len(key) != 32:
        raise BackupError('backup key must decode to exactly 32 bytes')
    return key


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if path.stat().st_size < 16:
        return False
    with path.open('rb') as handle:
        return handle.read(16) == b'SQLite format 3\x00'


def _copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f'file:{source.resolve()}?mode=ro'
    with (
        sqlite3.connect(source_uri, uri=True, timeout=30) as source_db,
        sqlite3.connect(target) as target_db,
    ):
        source_db.backup(target_db)
    shutil.copystat(source, target, follow_symlinks=False)


def _copy_file(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise BackupError(f'backup source must not contain symlinks: {source}')
    mode = source.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise BackupError(f'backup source is not a regular file: {source}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_sqlite(source):
        _copy_sqlite(source, target)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def _copy_tree(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise BackupError(f'backup source must not be a symlink: {source}')
    target.mkdir(parents=True, exist_ok=True)
    shutil.copystat(source, target, follow_symlinks=False)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.name == 'controller.lock' or child.name.endswith(('-wal', '-shm', '-journal')):
            continue
        destination = target / child.name
        if child.is_symlink():
            raise BackupError(f'backup source must not contain symlinks: {child}')
        if child.is_dir():
            _copy_tree(child, destination)
        elif child.is_file():
            _copy_file(child, destination)
        else:
            raise BackupError(f'unsupported backup source entry: {child}')


def _stage_sources(project_root: Path, run_home: Path | None, stage: Path) -> None:
    payload = stage / 'payload'
    data_root = project_root / 'data'
    if not data_root.is_dir():
        raise BackupError(f'runtime data directory does not exist: {data_root}')
    _copy_tree(data_root, payload / 'project' / 'data')

    for relative in (
        Path('configs/runtime.json'),
        Path('configs/config_runtime.json'),
        Path('configs/cluster.json'),
    ):
        source = project_root / relative
        if source.is_file():
            _copy_file(source, payload / 'project' / relative)
    certificate_dir = project_root / 'configs/certs'
    if certificate_dir.is_dir():
        _copy_tree(certificate_dir, payload / 'project' / 'configs/certs')

    if run_home:
        ssh_root = run_home / '.ssh'
        for name in ('gms_web_app_rsa', 'gms_web_app_rsa.pub', 'known_hosts'):
            source = ssh_root / name
            if source.is_file():
                _copy_file(source, payload / 'run_home' / '.ssh' / name)


def _manifest(stage: Path) -> dict:
    entries = []
    for path in sorted((stage / 'payload').rglob('*')):
        relative = path.relative_to(stage).as_posix()
        if path.is_dir():
            if not (
                relative == 'payload/project/data'
                or relative.startswith('payload/project/data/')
                or relative == 'payload/project/configs/certs'
                or relative.startswith('payload/project/configs/certs/')
                or relative == 'payload/run_home/.ssh'
                or relative.startswith('payload/run_home/.ssh/')
            ):
                continue
            info = path.stat()
            entries.append(
                {
                    'path': relative,
                    'type': 'directory',
                    'mode': stat.S_IMODE(info.st_mode),
                    'uid': info.st_uid,
                    'gid': info.st_gid,
                }
            )
            continue
        info = path.stat()
        entries.append(
            {
                'path': relative,
                'type': 'file',
                'sha256': _sha256(path),
                'size': info.st_size,
                'mode': stat.S_IMODE(info.st_mode),
                'uid': info.st_uid,
                'gid': info.st_gid,
            }
        )
    return {
        'schema_version': SCHEMA_VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'hostname': socket.gethostname(),
        'entries': entries,
    }


def _write_archive(stage: Path, output: Path, key: bytes) -> None:
    temp_path = output.with_name(f'.{output.name}.{uuid.uuid4().hex}.tmp')
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as raw:
            encrypted = _EncryptingWriter(raw, key)
            with tarfile.open(fileobj=encrypted, mode='w|gz') as archive:
                archive.add(stage / 'manifest.json', arcname='manifest.json')
                archive.add(stage / 'payload', arcname='payload')
            encrypted.finish()
        os.replace(temp_path, output)
        os.chmod(output, 0o600)
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _safe_member_name(name: str) -> PurePosixPath:
    normalized = PurePosixPath(name)
    if normalized.is_absolute() or '..' in normalized.parts:
        raise BackupError(f'unsafe archive member: {name}')
    if not normalized.parts or normalized.parts[0] not in {'manifest.json', 'payload'}:
        raise BackupError(f'unexpected archive member: {name}')
    return normalized


def _extract_verified(archive_path: Path, key: bytes, target: Path) -> dict:
    seen_files: set[str] = set()
    with archive_path.open('rb') as raw:
        decrypted = io.BufferedReader(_DecryptingReader(raw, key), 1024 * 1024)
        try:
            with tarfile.open(fileobj=decrypted, mode='r|gz') as archive:
                for member in archive:
                    relative = _safe_member_name(member.name)
                    destination = target.joinpath(*relative.parts)
                    if member.issym() or member.islnk():
                        raise BackupError(f'backup archive contains a forbidden link: {member.name}')
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise BackupError(f'backup archive contains unsupported entry: {member.name}')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise BackupError(f'cannot read archive member: {member.name}')
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    seen_files.add(relative.as_posix())
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError(f'cannot decrypt or extract backup: {exc}') from exc

    manifest_path = target / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError('backup manifest is absent or invalid') from exc
    if manifest.get('schema_version') != SCHEMA_VERSION:
        raise BackupError('unsupported backup manifest schema')
    expected = {entry.get('path'): entry for entry in manifest.get('entries', [])}
    if not expected or None in expected:
        raise BackupError('backup manifest has no valid entries')
    expected_files = {relative: entry for relative, entry in expected.items() if entry.get('type', 'file') == 'file'}
    expected_directories = {relative: entry for relative, entry in expected.items() if entry.get('type') == 'directory'}
    actual_payload = seen_files - {'manifest.json'}
    if set(expected_files) != actual_payload:
        raise BackupError('backup contents do not match the manifest')
    for relative, entry in expected_files.items():
        path = target.joinpath(*PurePosixPath(relative).parts)
        if path.stat().st_size != entry.get('size') or _sha256(path) != entry.get('sha256'):
            raise BackupError(f'backup integrity check failed: {relative}')
        os.chmod(path, int(entry.get('mode', 0o600)) & 0o777)
    for relative, entry in expected_directories.items():
        path = target.joinpath(*PurePosixPath(relative).parts)
        if not path.is_dir():
            raise BackupError(f'backup directory is absent: {relative}')
        os.chmod(path, int(entry.get('mode', 0o700)) & 0o777)
    return manifest


def create_backup(args: argparse.Namespace) -> Path:
    project_root = args.project_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    key = _load_key(args.key_file.resolve())
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = output_dir / f'gms-backup-{timestamp}-{uuid.uuid4().hex[:8]}{ARCHIVE_SUFFIX}'
    with (
        _offline_controller_lock(project_root / 'data'),
        tempfile.TemporaryDirectory(prefix='gms-backup-stage-') as temp,
    ):
        stage = Path(temp)
        os.chmod(stage, 0o700)
        _stage_sources(
            project_root,
            args.run_home.resolve() if args.run_home else None,
            stage,
        )
        manifest = _manifest(stage)
        manifest_path = stage / 'manifest.json'
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        os.chmod(manifest_path, 0o600)
        _write_archive(stage, output, key)
    _apply_retention(output_dir, args.keep)
    return output


def _apply_retention(output_dir: Path, keep: int) -> None:
    if keep < 1:
        raise BackupError('backup retention must keep at least one archive')
    archives = sorted(
        (path for path in output_dir.glob(f'gms-backup-*{ARCHIVE_SUFFIX}') if path.is_file() and not path.is_symlink()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for expired in archives[keep:]:
        expired.unlink()


def verify_backup(args: argparse.Namespace) -> dict:
    key = _load_key(args.key_file.resolve())
    with tempfile.TemporaryDirectory(prefix='gms-backup-verify-') as temp:
        target = Path(temp)
        os.chmod(target, 0o700)
        return _extract_verified(args.archive.resolve(), key, target)


@contextmanager
def _offline_controller_lock(data_root: Path) -> Iterator[None]:
    lock_path = data_root / 'controller.lock'
    try:
        # Production backup units mount the application tree read-only.  flock
        # only needs an open descriptor when the Controller-created lock file
        # already exists.
        handle = lock_path.open('r', encoding='utf-8')
    except FileNotFoundError:
        data_root.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open('a+', encoding='utf-8')
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError('restore requires the Controller and local Worker services to be stopped') from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _restore_file(source: Path, destination: Path, entry: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f'.{destination.name}.{uuid.uuid4().hex}.restore')
    shutil.copyfile(source, temp)
    os.chmod(temp, int(entry.get('mode', 0o600)) & 0o777)
    if os.geteuid() == 0:
        os.chown(temp, int(entry.get('uid', 0)), int(entry.get('gid', 0)))
    os.replace(temp, destination)


def restore_backup(args: argparse.Namespace) -> Path:
    if args.confirm != 'RESTORE':
        raise BackupError('restore requires --confirm RESTORE')
    project_root = args.project_root.resolve()
    key = _load_key(args.key_file.resolve())
    with (
        _offline_controller_lock(project_root / 'data'),
        tempfile.TemporaryDirectory(prefix='gms-backup-restore-') as temp,
    ):
        target = Path(temp)
        os.chmod(target, 0o700)
        manifest = _extract_verified(args.archive.resolve(), key, target)
        entries = {entry['path']: entry for entry in manifest['entries']}
        restored_data = target / 'payload/project/data'
        if not restored_data.is_dir():
            raise BackupError('backup does not contain project runtime data')
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        previous_data = project_root / f'data.pre-restore-{timestamp}'
        current_data = project_root / 'data'
        if previous_data.exists():
            raise BackupError(f'restore safety directory already exists: {previous_data}')
        current_data.rename(previous_data)
        try:
            shutil.copytree(restored_data, current_data, copy_function=shutil.copy2)
            for relative, entry in entries.items():
                source = target.joinpath(*PurePosixPath(relative).parts)
                if entry.get('type') == 'directory':
                    if relative.startswith(('payload/project/data', 'payload/project/')):
                        destination = project_root / PurePosixPath(relative).relative_to('payload/project')
                    elif relative.startswith('payload/run_home/') and args.run_home:
                        destination = args.run_home.resolve() / PurePosixPath(relative).relative_to('payload/run_home')
                    else:
                        continue
                    destination.mkdir(parents=True, exist_ok=True)
                    os.chmod(destination, int(entry.get('mode', 0o700)) & 0o777)
                    if os.geteuid() == 0:
                        os.chown(
                            destination,
                            int(entry.get('uid', 0)),
                            int(entry.get('gid', 0)),
                        )
                    continue
                if relative.startswith('payload/project/') and not relative.startswith('payload/project/data/'):
                    destination = project_root / PurePosixPath(relative).relative_to('payload/project')
                    _restore_file(source, destination, entry)
                elif relative.startswith('payload/run_home/'):
                    if not args.run_home:
                        continue
                    destination = args.run_home.resolve() / PurePosixPath(relative).relative_to('payload/run_home')
                    _restore_file(source, destination, entry)
            for relative, entry in entries.items():
                if entry.get('type') != 'file' or not relative.startswith('payload/project/data/'):
                    continue
                destination = project_root / PurePosixPath(relative).relative_to('payload/project')
                os.chmod(destination, int(entry.get('mode', 0o600)) & 0o777)
                if os.geteuid() == 0:
                    os.chown(
                        destination,
                        int(entry.get('uid', 0)),
                        int(entry.get('gid', 0)),
                    )
        except Exception:
            if current_data.exists():
                shutil.rmtree(current_data)
            previous_data.rename(current_data)
            raise
    return previous_data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument('--key-file', required=True, type=Path)

    create = subparsers.add_parser('create', help='create and verify an encrypted backup')
    common(create)
    create.add_argument('--project-root', required=True, type=Path)
    create.add_argument('--run-home', type=Path)
    create.add_argument('--output-dir', required=True, type=Path)
    create.add_argument('--keep', type=int, default=14)

    verify = subparsers.add_parser('verify', help='decrypt and validate an archive')
    common(verify)
    verify.add_argument('--archive', required=True, type=Path)

    restore = subparsers.add_parser('restore', help='perform an offline restore')
    common(restore)
    restore.add_argument('--archive', required=True, type=Path)
    restore.add_argument('--project-root', required=True, type=Path)
    restore.add_argument('--run-home', type=Path)
    restore.add_argument('--confirm', required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'create':
            output = create_backup(args)
            verify_args = argparse.Namespace(archive=output, key_file=args.key_file)
            manifest = verify_backup(verify_args)
            print(
                json.dumps(
                    {
                        'success': True,
                        'archive': str(output),
                        'created_at': manifest['created_at'],
                        'files': len(manifest['entries']),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.command == 'verify':
            manifest = verify_backup(args)
            print(
                json.dumps(
                    {
                        'success': True,
                        'created_at': manifest['created_at'],
                        'files': len(manifest['entries']),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            previous = restore_backup(args)
            print(
                json.dumps(
                    {'success': True, 'previous_data': str(previous)},
                    ensure_ascii=False,
                )
            )
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f'backup error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
