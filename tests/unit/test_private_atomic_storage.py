"""Atomic private storage publishes only complete text or binary files."""

import stat
from unittest.mock import patch

import pytest

from foundation.private_config import write_atomic_bytes, write_atomic_text


def test_binary_roundtrip_preserves_bytes_and_private_mode(tmp_path):
    path = tmp_path / "nested" / "evidence.bin"
    payload = b"\x00\xff\r\n"
    write_atomic_bytes(path, payload, sync_directory=True)
    assert path.read_bytes() == payload
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("binary", [False, True])
def test_failed_publication_keeps_previous_file_and_removes_temporary(tmp_path, binary):
    path = tmp_path / "payload"
    path.write_bytes(b"old")
    with (
        patch("foundation.private_config.os.replace", side_effect=OSError("fixture")),
        pytest.raises(OSError, match="fixture"),
    ):
        if binary:
            write_atomic_bytes(path, b"new")
        else:
            write_atomic_text(path, "new")
    assert path.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [path]
