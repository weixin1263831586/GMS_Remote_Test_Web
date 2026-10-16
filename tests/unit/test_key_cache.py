import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from foundation import secrets, security_audit


@pytest.fixture(params=['secret', 'audit'])
def key_source(request, tmp_path, monkeypatch):
    path = tmp_path / 'key'
    first = Fernet.generate_key()
    path.write_bytes(first)
    path.chmod(0o600)
    if request.param == 'secret':
        monkeypatch.delenv('GMS_SECRET_KEY', raising=False)
        monkeypatch.setenv('GMS_SECRET_KEY_FILE', str(path))
        monkeypatch.setattr(secrets, '_KEY_CACHE', {})
        return path, first, secrets._load_key, secrets, '_load_key_from_file'
    monkeypatch.delenv('GMS_AUDIT_HMAC_KEY', raising=False)
    monkeypatch.setenv('GMS_AUDIT_HMAC_KEY_FILE', str(path))
    monkeypatch.setattr(security_audit, '_AUDIT_KEY_CACHE', {})
    logger = security_audit.SecurityAuditLogger(str(tmp_path / 'audit.jsonl'))
    return path, first, logger._audit_key, logger, '_load_audit_key'


def replace_key(path, value, *, preserve_mtime=False):
    previous = path.stat()
    replacement = path.with_name('replacement')
    replacement.write_bytes(value)
    replacement.chmod(0o600)
    if preserve_mtime:
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    replacement.replace(path)


def test_atomic_rotation_with_preserved_size_mtime_and_mode(key_source):
    path, first, read, _, _ = key_source
    assert read() == first
    second = Fernet.generate_key()
    replace_key(path, second, preserve_mtime=True)
    assert read() == second


def test_rotation_during_loading_retries_instead_of_pinning_old_key(key_source, monkeypatch):
    path, _, read, owner, attribute = key_source
    original = getattr(owner, attribute)
    second = Fernet.generate_key()
    calls = []

    def rotating_load(*args):
        key = original(*args)
        calls.append(True)
        if len(calls) == 1:
            replace_key(path, second)
        return key

    monkeypatch.setattr(owner, attribute, rotating_load)
    assert read() == second
    assert read() == second
    assert len(calls) == 2


def test_permission_change_during_loading_is_revalidated(key_source, monkeypatch):
    path, _, read, owner, attribute = key_source
    original = getattr(owner, attribute)

    def change_permissions(*args):
        key = original(*args)
        path.chmod(0o644)
        return key

    monkeypatch.setattr(owner, attribute, change_permissions)
    with pytest.raises(RuntimeError, match='permissions'):
        read()


def test_missing_source_never_reuses_old_key(key_source):
    path, first, read, _, _ = key_source
    assert read() == first
    path.unlink()
    assert read() != first


def test_cold_concurrent_readers_load_once(key_source, monkeypatch):
    _, first, read, owner, attribute = key_source
    original = getattr(owner, attribute)
    calls = []
    barrier = Barrier(8)

    def load(*args):
        calls.append(True)
        return original(*args)

    def worker(_):
        barrier.wait(timeout=5)
        return read()

    monkeypatch.setattr(owner, attribute, load)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(worker, range(8))) == [first] * 8
    assert len(calls) == 1


def test_repeated_rotation_fails_without_publishing_a_key(key_source, monkeypatch):
    path, _, read, owner, attribute = key_source
    original = getattr(owner, attribute)

    def rotate(*args):
        key = original(*args)
        replace_key(path, Fernet.generate_key())
        return key

    monkeypatch.setattr(owner, attribute, rotate)
    with pytest.raises(RuntimeError, match='changed repeatedly'):
        read()
