"""Firmware share transport failures keep a fixed client message."""

from unittest.mock import patch

import pytest

from features.firmware.shares_api import (
    _RemoteForbiddenError,
    _RemoteTimeoutError,
    _stat_remote,
)


def test_stat_timeout_does_not_echo_the_exception():
    with patch(
        "features.firmware.shares_api._sftp_client",
        side_effect=TimeoutError("secret-banner"),
    ), pytest.raises(_RemoteTimeoutError) as raised:
        _stat_remote("192.0.2.10", "gms", "/home/gms/update.img", {})

    assert "secret-banner" not in str(raised.value)
    assert "超时" in str(raised.value)


def test_stat_permission_error_is_not_a_connection_failure():
    with patch(
        "features.firmware.shares_api._sftp_client",
        side_effect=PermissionError("secret path"),
    ), pytest.raises(_RemoteForbiddenError) as raised:
        _stat_remote("192.0.2.10", "gms", "/home/gms/update.img", {})

    assert "secret" not in str(raised.value)
    assert "权限" in str(raised.value)
