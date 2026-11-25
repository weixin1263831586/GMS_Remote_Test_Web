"""Shared streaming file digest contract."""

import hashlib
import io

from foundation.files import sha256_file, sha256_stream


def test_file_digest_across_multiple_chunks(tmp_path):
    content = b"abc" * (1024 * 1024)
    path = tmp_path / "artifact"
    path.write_bytes(content)
    assert sha256_file(path) == hashlib.sha256(content).hexdigest()


def test_stream_digest_starts_at_current_position():
    source = io.BytesIO(b"prefix-payload")
    source.seek(7)
    assert sha256_stream(source) == hashlib.sha256(b"payload").hexdigest()
    assert source.tell() == len(b"prefix-payload")
