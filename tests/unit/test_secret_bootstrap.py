"""data/ 目录被整体重置后，部署本地密钥文件缺失时的自举行为。

约定：密钥文件缺失 -> 自动重建（生产模式打 WARNING）；文件存在但内容
损坏或权限错误 -> 仍然硬失败（配置错误不能被静默吞掉）。
"""

from __future__ import annotations

import os
import stat
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from features.system.skill_archive_signing import (
    SIGNING_KEY_ENV,
    sign_skill_archive,
    skill_verify_key_b64,
)
from foundation.secrets import validate_secret_configuration
from foundation.security_audit import SecurityAuditLogger


def test_master_key_autogenerates_when_file_missing(tmp_path, monkeypatch):
    key_path = tmp_path / "secrets" / "master.key"
    monkeypatch.setenv("GMS_SECRET_KEY", "")
    monkeypatch.setenv("GMS_SECRET_KEY_FILE", str(key_path))
    monkeypatch.delenv("GMS_SECRET_KEY", raising=False)
    monkeypatch.setenv("GMS_ENV", "production")

    validate_secret_configuration()

    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    # 第二次调用走"文件已存在"分支，保持幂等
    validate_secret_configuration()


def test_master_key_waits_for_concurrent_creator_to_finish(tmp_path, monkeypatch):
    """Concurrent bootstrap must wait for the publisher and reuse its key."""
    import foundation.secrets as secrets

    key_path = tmp_path / "secrets" / "master.key"
    key_path.parent.mkdir(parents=True)
    publishing, release = Event(), Event()
    original_write = secrets._atomic_write_private_file

    def delayed_publish(path, payload):
        publishing.set()
        assert release.wait(5)
        original_write(path, payload)

    monkeypatch.setattr(secrets, "_atomic_write_private_file", delayed_publish)
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = executor.submit(secrets._load_key_from_file, key_path)
        try:
            assert publishing.wait(5)
            assert not key_path.exists()
            others = [executor.submit(secrets._load_key_from_file, key_path) for _ in range(3)]
        finally:
            release.set()
        keys = [first.result(timeout=5), *(future.result(timeout=5) for future in others)]
    assert set(keys) == {key_path.read_bytes().strip()}
    ciphertext = Fernet(keys[0]).encrypt(b"test-value")
    assert all(Fernet(key).decrypt(ciphertext) == b"test-value" for key in keys)


def test_empty_master_key_is_recovered_once(tmp_path):
    import foundation.secrets as secrets

    key_path = tmp_path / "master.key"
    key_path.touch(mode=0o600)
    with ThreadPoolExecutor(max_workers=4) as executor:
        keys = list(executor.map(secrets._load_key_from_file, [key_path] * 4))
    assert set(keys) == {key_path.read_bytes().strip()}
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def _process_master_key(path):
    from foundation.secrets import _load_key_from_file

    return _load_key_from_file(path)


def test_master_key_is_shared_across_processes(tmp_path):
    key_path = tmp_path / "master.key"
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("fork")) as executor:
        keys = list(executor.map(_process_master_key, [key_path] * 8))
    assert set(keys) == {key_path.read_bytes().strip()}


def test_legacy_key_migration_preserves_key_across_concurrent_calls(tmp_path, monkeypatch):
    import foundation.secrets as secrets

    monkeypatch.setattr(secrets, "settings", SimpleNamespace(project_root=tmp_path, data_root=tmp_path / "state"))
    legacy = tmp_path / "state/secrets/master.key"
    canonical = tmp_path / "configs/secrets/master.key"
    legacy.parent.mkdir(parents=True)
    legacy.touch(mode=0o600)
    expected = Fernet.generate_key()
    legacy.write_bytes(expected + b"\n")
    with ThreadPoolExecutor(max_workers=4) as executor:
        keys = list(executor.map(secrets._load_key_from_file, [legacy] * 4))
    assert set(keys) == {expected}
    assert canonical.read_bytes().strip() == expected
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o600


def test_master_key_corrupt_file_still_raises(tmp_path, monkeypatch):
    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"not-a-fernet-key\n")
    os.chmod(key_path, 0o600)
    monkeypatch.setenv("GMS_SECRET_KEY_FILE", str(key_path))
    monkeypatch.delenv("GMS_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="valid Fernet key"):
        validate_secret_configuration()


def test_master_key_bad_permissions_still_raises(tmp_path, monkeypatch):
    key_path = tmp_path / "master.key"
    key_path.write_bytes(b"k" * 44 + b"\n")
    os.chmod(key_path, 0o644)
    monkeypatch.setenv("GMS_SECRET_KEY_FILE", str(key_path))
    monkeypatch.delenv("GMS_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="0600"):
        validate_secret_configuration()


def test_audit_key_autogenerates_when_file_missing(tmp_path, monkeypatch):
    key_path = tmp_path / "secrets" / "audit_hmac.key"
    monkeypatch.setenv("GMS_AUDIT_HMAC_KEY", "")
    monkeypatch.setenv("GMS_AUDIT_HMAC_KEY_FILE", str(key_path))
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setenv("GMS_ENV", "production")

    audit = SecurityAuditLogger(str(tmp_path / "audit.jsonl"))
    key = audit._audit_key()

    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert len(key) >= 32


def test_audit_configuration_serializes_key_bootstrap(tmp_path, monkeypatch):
    """并发启动的生产 Worker 必须共享同一把新建审计密钥。"""
    from concurrent.futures import ThreadPoolExecutor

    key_path = tmp_path / "secrets" / "audit_hmac.key"
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setenv("GMS_AUDIT_HMAC_KEY_FILE", str(key_path))
    monkeypatch.setenv("GMS_ENV", "production")

    log_path = str(tmp_path / "audit.jsonl")
    loggers = [SecurityAuditLogger(log_path) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=len(loggers)) as executor:
        list(executor.map(lambda audit: audit.validate_configuration(), loggers))

    keys = {audit._audit_key() for audit in loggers}
    assert len(keys) == 1


@pytest.mark.parametrize("legacy_exists", [False, True])
def test_audit_uses_canonical_key_and_preserves_existing_chain(tmp_path, monkeypatch, legacy_exists):
    from foundation import config

    monkeypatch.setattr(config, "settings", SimpleNamespace(project_root=tmp_path, data_root=tmp_path / "state"))
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY_FILE", raising=False)
    canonical = tmp_path / "configs/secrets/audit_hmac.key"
    canonical.parent.mkdir(parents=True)
    key = b"c" * 64
    canonical.write_bytes(key)
    canonical.chmod(0o600)
    legacy = tmp_path / "state/secrets/audit_hmac.key"
    if legacy_exists:
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"l" * 64)
        legacy.chmod(0o600)
    audit = SecurityAuditLogger(str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("GMS_AUDIT_HMAC_KEY_FILE", str(canonical))
    audit.log_event({"operation": "existing record"})
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY_FILE")
    resumed = SecurityAuditLogger(audit.log_path)
    resumed.log_event({"operation": "next record"})
    assert resumed._audit_key() == key
    assert resumed.verify_chain()["valid"]
    assert resumed.verify_chain()["signed_records"] == 2


def test_audit_legacy_key_remains_readable(tmp_path, monkeypatch):
    from foundation import config

    monkeypatch.setattr(config, "settings", SimpleNamespace(project_root=tmp_path, data_root=tmp_path / "state"))
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY_FILE", raising=False)
    legacy = tmp_path / "state/secrets/audit_hmac.key"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"l" * 64)
    legacy.chmod(0o600)
    audit = SecurityAuditLogger(str(tmp_path / "audit.jsonl"))
    audit.log_event({"operation": "legacy deployment"})
    assert audit._audit_key() == b"l" * 64
    assert audit.verify_chain()["valid"]
    assert not (tmp_path / "configs/secrets/audit_hmac.key").exists()


def test_default_audit_key_is_created_under_configuration(tmp_path, monkeypatch):
    from foundation import config

    monkeypatch.setattr(config, "settings", SimpleNamespace(project_root=tmp_path, data_root=tmp_path / "state"))
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.delenv("GMS_AUDIT_HMAC_KEY_FILE", raising=False)
    audit = SecurityAuditLogger(str(tmp_path / "audit.jsonl"))
    audit.validate_configuration()
    canonical = tmp_path / "configs/secrets/audit_hmac.key"
    assert canonical.is_file()
    assert stat.S_IMODE(canonical.stat().st_mode) == 0o600
    assert not (tmp_path / "state/secrets/audit_hmac.key").exists()


def test_skill_signing_key_autogenerates_when_missing(tmp_path, monkeypatch):
    key_path = tmp_path / "secrets" / "skill-signing-ed25519.pem"
    monkeypatch.setenv(SIGNING_KEY_ENV, str(key_path))

    signature = sign_skill_archive(b"payload")
    assert signature
    assert skill_verify_key_b64()
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_skill_signing_key_stable_across_calls(tmp_path, monkeypatch):
    """自举后同一路径后续调用必须读回同一把密钥，而不是每次重造。"""

    key_path = tmp_path / "skill-signing-ed25519.pem"
    monkeypatch.setenv(SIGNING_KEY_ENV, str(key_path))

    first = skill_verify_key_b64()
    second = skill_verify_key_b64()
    assert first == second != ""


def test_skill_signing_key_corrupt_file_still_raises(tmp_path, monkeypatch):
    key_path = tmp_path / "broken.pem"
    key_path.write_bytes(b"-----BEGIN PRIVATE KEY-----\nGarbage\n")
    monkeypatch.setenv(SIGNING_KEY_ENV, str(key_path))

    with pytest.raises(RuntimeError, match=SIGNING_KEY_ENV):
        skill_verify_key_b64()
