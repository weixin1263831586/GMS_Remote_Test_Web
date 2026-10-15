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
