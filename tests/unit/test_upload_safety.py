"""Upload session confinement and atomic publication, using temporary files only."""
import io
from pathlib import Path

import pytest

from features.firmware.chunk_uploads import upload_session_dir
from foundation.uploads import copy_fileobj_to_path, safe_upload_token


@pytest.mark.parametrize('client, upload', [('owner', '.'), ('owner', '..'), ('.', 'u'), ('..', '..')])
def test_reserved_tokens_cannot_escape_firmware_session_namespace(tmp_path, client, upload):
    path = Path(upload_session_dir(str(tmp_path), client, upload)).resolve()
    assert path.is_relative_to(tmp_path)
    assert len(path.relative_to(tmp_path).parts) == 2
    assert safe_upload_token(upload) not in {'.', '..'}


def test_failed_upload_preserves_previous_file_and_removes_temporary(tmp_path):
    target = tmp_path / 'report.zip'
    target.write_bytes(b'previous complete upload')
    with pytest.raises(ValueError, match='文件过大'):
        copy_fileobj_to_path(io.BytesIO(b'new oversized data'), str(target), max_size=2, chunk_size=1)
    assert target.read_bytes() == b'previous complete upload'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['report.zip']


def test_read_failure_preserves_previous_file(tmp_path):
    target = tmp_path / 'report.zip'
    target.write_bytes(b'original')
    class Broken:
        def read(self, _):
            raise OSError('connection lost')
    with pytest.raises(OSError):
        copy_fileobj_to_path(Broken(), str(target))
    assert target.read_bytes() == b'original'
    assert len(list(tmp_path.iterdir())) == 1


def test_complete_upload_replaces_symlink_without_overwriting_its_target(tmp_path):
    outside = tmp_path / 'other.txt'
    outside.write_bytes(b'protected')
    upload = tmp_path / 'upload.txt'
    upload.symlink_to(outside)
    assert copy_fileobj_to_path(io.BytesIO(b'uploaded'), str(upload)) == 8
    assert upload.read_bytes() == b'uploaded' and not upload.is_symlink()
    assert outside.read_bytes() == b'protected'
