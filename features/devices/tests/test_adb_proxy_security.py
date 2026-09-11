from unittest.mock import patch

import pytest

from features.devices.adb_proxy_security import (
    create_pair_grant,
    local_proxy_secret,
    pair_code_for_worker,
    validate_pair_grant,
)
from worker_agent.adb_proxy import pair_code_from_grant


def _write_secret(tmp_path, content: bytes):
    secret_path = tmp_path / "adb_proxy.key"
    secret_path.write_bytes(content)
    secret_path.chmod(0o600)
    return secret_path


def test_remote_worker_pair_code_matches_assignment_grant_derivation():
    with patch(
        "features.cluster.worker_tokens",
        return_value={"worker-source": "source-token"},
    ):
        code = pair_code_for_worker(
            "worker-source",
            "ats-worker-controller",
            "signed-grant",
        )

    assert code == pair_code_from_grant("source-token", "signed-grant")


def test_pair_grant_is_target_bound_and_rejects_tampering():
    with patch(
        "features.cluster.worker_tokens",
        return_value={"worker-source": "source-token"},
    ):
        grant = create_pair_grant(
            "worker-source", "worker-target", "ats-worker-controller"
        )
        validate_pair_grant(
            grant, "worker-source", "worker-target", "ats-worker-controller"
        )
        with pytest.raises(ValueError, match="host mismatch"):
            validate_pair_grant(
                grant, "worker-source", "other-target", "ats-worker-controller"
            )
        with pytest.raises(ValueError, match="invalid"):
            validate_pair_grant(
                grant + "x",
                "worker-source",
                "worker-target",
                "ats-worker-controller",
            )


def test_local_proxy_secret_reads_32_byte_binary_key_verbatim(tmp_path, monkeypatch):
    """A binary key whose first/last byte is whitespace must not be
    corrupted by stripping; the file stays readable on every call."""
    secret_path = _write_secret(tmp_path, b"\n" + b"A" * 31)
    monkeypatch.setenv("GMS_ADB_PROXY_SECRET_FILE", str(secret_path))

    first = local_proxy_secret()
    second = local_proxy_secret()

    assert first == b"\n" + b"A" * 31
    assert second == first


def test_local_proxy_secret_tolerates_legacy_trailing_newline(tmp_path, monkeypatch):
    secret_path = _write_secret(tmp_path, b"B" * 32 + b"\n")
    monkeypatch.setenv("GMS_ADB_PROXY_SECRET_FILE", str(secret_path))

    assert local_proxy_secret() == b"B" * 32


def test_local_proxy_secret_rejects_short_or_oversized_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_ADB_PROXY_SECRET_FILE", str(_write_secret(tmp_path, b"A" * 16)))
    with pytest.raises(RuntimeError, match="invalid"):
        local_proxy_secret()

    monkeypatch.setenv(
        "GMS_ADB_PROXY_SECRET_FILE",
        str(_write_secret(tmp_path, b"A" * 32 + b"\n\n")),
    )
    with pytest.raises(RuntimeError, match="invalid"):
        local_proxy_secret()
