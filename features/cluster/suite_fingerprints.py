"""On-demand archive fingerprints, cached by immutable file identity."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from foundation.error_model import ApiError


@lru_cache(maxsize=128)
def _digest(path: str, size: int, modified: int, inode: int) -> str:
    checksum = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(block)
    current = Path(path).stat()
    if (current.st_size, current.st_mtime_ns, current.st_ino) != (size, modified, inode):
        raise ApiError.conflict('The archive changed during verification; retry after transfer completes')
    return checksum.hexdigest()


def fingerprint(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ApiError.conflict('Only regular suite archives can be fingerprinted')
    stat = path.stat()
    return {'name': path.name, 'size': stat.st_size, 'modified': int(stat.st_mtime),
            'sha256': _digest(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino)}
