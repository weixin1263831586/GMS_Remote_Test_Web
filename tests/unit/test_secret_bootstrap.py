"""data/ 目录被整体重置后，部署本地密钥文件缺失时的自举行为。

约定：密钥文件缺失 -> 自动重建（生产模式打 WARNING）；文件存在但内容
损坏或权限错误 -> 仍然硬失败（配置错误不能被静默吞掉）。
"""

from __future__ import annotations

import os
import stat

import pytest

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
