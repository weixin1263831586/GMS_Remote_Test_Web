"""Installed identity is verified from user 0's actual APK through fenced SSH."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from features.devices import debug_identity as identity
from foundation.error_model import ApiError


DIGEST = "a" * 64
APP = {"package": "com.example.app", "signers": ["b" * 64], "version_code": "1", "version_name": "1.0"}


def test_installed_base_apk_is_pulled_verified_and_cleaned():
    ssh = MagicMock()
    sftp = ssh.open_sftp.return_value.__enter__.return_value
    sftp.stat.return_value = SimpleNamespace(st_size=7)
    sftp.get.side_effect = lambda remote, local: Path(local).write_bytes(b"fixture")

    def command(_ssh, args, *_rest):
        assert args[:3] == ["adb", "-s", "SERIAL"] or args[0] in {"mkdir", "rm"}
        joined = " ".join(args)
        if "pm list packages" in joined:
            return "package:com.example.app\n"
        if "pm path" in joined:
            return "package:/data/app/example/base.apk\npackage:/data/app/example/split_config.en.apk\n"
        if "stat -c" in joined:
            return "7\n"
        if "sha256sum" in joined:
            return DIGEST + "  /data/app/example/base.apk\n"
        return ""

    check = MagicMock()
    with patch.object(identity, "run", side_effect=command) as run, patch.object(identity, "inspect_apk", return_value=APP) as inspect:
        result = identity.installed_identity(ssh, "SERIAL", APP["package"], check)
    assert result == {**APP, "sha256": DIGEST}
    assert inspect.call_args.args[1] == DIGEST
    assert run.call_args.args[1][0:2] == ["rm", "-rf"]
    assert sum("pull" in call.args[1] for call in run.call_args_list) == 1
    assert check.call_count >= 6


@pytest.mark.parametrize("bad_output", ["package:/data/app/path with spaces/base.apk", "Failure: package manager unavailable"])
def test_ambiguous_installed_paths_fail_closed(bad_output):
    with patch.object(identity, "run", return_value=bad_output) as run, pytest.raises(ApiError):
        identity.installed_identity(MagicMock(), "SERIAL", APP["package"])
    assert run.call_count == 1


def test_missing_app_cannot_authorize_debugging():
    with patch.object(identity, "run", return_value=""), pytest.raises(ApiError) as caught:
        identity.require_installed(MagicMock(), {"serial": "SERIAL", **APP})
    assert caught.value.status_code == 403


def test_lease_loss_during_installed_identity_prevents_pull():
    def fence():
        raise ApiError.conflict("lease transferred")

    with patch.object(identity, "run") as run, pytest.raises(ApiError):
        identity.installed_identity(MagicMock(), "SERIAL", APP["package"], fence)
    run.assert_not_called()


def test_verified_identity_cache_is_fenced_by_worker_install_fingerprint_and_ttl(monkeypatch):
    identity._identity_cache.clear()
    ssh = MagicMock()
    session = {"serial": "SERIAL", "package": APP["package"], "signers": APP["signers"]}
    with patch.object(identity, "installed_fingerprint", return_value=("/data/app/base.apk", "boot:stat:v1:" + DIGEST)) as probe, \
            patch.object(identity, "installed_identity", return_value={**APP, "sha256": DIGEST}) as verify:
        identity.require_installed(ssh, session, worker_id="worker-a")
        identity.require_installed(ssh, session, worker_id="worker-a")
        assert verify.call_count == 1
        identity.require_installed(ssh, session, worker_id="worker-b")
        assert verify.call_count == 2
        probe.side_effect = [("/data/app/base.apk", "boot:stat:v2:" + DIGEST), ("/data/app/base.apk", "boot:stat:v2:" + DIGEST)]
        identity.require_installed(ssh, session, worker_id="worker-a")
        assert verify.call_count == 3
        assert probe.call_count == 7
    identity.invalidate_installed_identity("worker-a", "SERIAL", APP["package"])
    assert ("worker-a", "SERIAL", 0, APP["package"]) not in identity._identity_cache


def test_verified_identity_cache_expires(monkeypatch):
    identity._identity_cache.clear()
    clock = [100.0]
    monkeypatch.setattr(identity.time, "monotonic", lambda: clock[0])
    session = {"serial": "SERIAL", "package": APP["package"], "signers": APP["signers"]}
    with patch.object(identity, "installed_fingerprint", return_value=("/data/app/base.apk", "boot:stat:" + DIGEST)), \
            patch.object(identity, "installed_identity", return_value={**APP, "sha256": DIGEST}) as verify:
        identity.require_installed(MagicMock(), session, worker_id="worker")
        clock[0] += identity._IDENTITY_TTL_SECONDS + 1
        identity.require_installed(MagicMock(), session, worker_id="worker")
    assert verify.call_count == 2


def test_content_change_with_identical_install_metadata_rechecks_signer():
    identity._identity_cache.clear()
    digest = [DIGEST]
    session = {'serial': 'SERIAL', 'package': APP['package'], 'signers': APP['signers']}

    def command(_ssh, args, *_rest):
        joined = ' '.join(args)
        if 'pm list packages' in joined:
            return 'package:' + APP['package']
        if 'pm path' in joined:
            return 'package:/data/app/example/base.apk'
        if 'stat -c' in joined:
            return '7:1700000000:123'
        if 'boot_id' in joined:
            return '12345678-1234-1234-1234-123456789abc'
        if 'dumpsys' in joined:
            return 'versionCode=1\nlastUpdateTime=unchanged'
        if 'sha256sum' in joined:
            return digest[0] + '  /data/app/example/base.apk'
        raise AssertionError(args)

    with patch.object(identity, 'run', side_effect=command), patch.object(
        identity, 'installed_identity', return_value={**APP, 'sha256': DIGEST},
    ) as verify:
        identity.require_installed(MagicMock(), session, worker_id='worker')
        identity.require_installed(MagicMock(), session, worker_id='worker')
        assert verify.call_count == 1
        # External tools can preserve path, inode, size and timestamps.
        digest[0] = 'c' * 64
        verify.return_value = {**APP, 'sha256': digest[0], 'signers': ['d' * 64]}
        with pytest.raises(ApiError) as caught:
            identity.require_installed(MagicMock(), session, worker_id='worker')
        assert caught.value.status_code == 403
        assert verify.call_count == 2


def test_missing_identity_during_fill_fails_closed_even_if_metadata_reappears():
    identity._identity_cache.clear()
    with patch.object(identity, 'installed_fingerprint', return_value=('/data/app/base.apk', 'stable')), \
            patch.object(identity, 'installed_identity', return_value=None), pytest.raises(ApiError) as caught:
        identity.require_installed(MagicMock(), {'serial': 'SERIAL', **APP}, worker_id='worker')
    assert caught.value.status_code == 409


def test_verified_bytes_must_match_the_observed_fingerprint_digest():
    identity._identity_cache.clear()
    with patch.object(identity, 'installed_fingerprint', return_value=('/data/app/base.apk', 'boot:' + DIGEST)), \
            patch.object(identity, 'installed_identity', return_value={**APP, 'sha256': 'c' * 64}), \
            pytest.raises(ApiError) as caught:
        identity.require_installed(MagicMock(), {'serial': 'SERIAL', **APP}, worker_id='worker')
    assert caught.value.status_code == 409
