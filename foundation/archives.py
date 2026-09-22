from __future__ import annotations

import os
import re
import stat
import subprocess
import tarfile
import urllib.parse
import zipfile
from typing import BinaryIO


ARCHIVE_EXTENSIONS = (
    '.zip',
    '.tar.gz',
    '.tgz',
    '.tar.bz2',
    '.tar',
)
# Extensions accepted at upload time (test suites are large; rar/7z travel
# through the system-extractor path with preflight + post-scan, NOT through
# `tar -xf`). Extraction dispatch is by CONTENT sniffing, never by filename
# alone — a tar renamed to .rar used to fall into the raw `tar -xf` path and
# bypass the member/budget/symlink policy entirely .
UPLOAD_ARCHIVE_EXTENSIONS = (*ARCHIVE_EXTENSIONS, '.rar', '.7z')
_SANITIZE_FILENAME_RE = re.compile(r'[^\w\-_.\[\]]')
_SANITIZE_DIRNAME_RE = re.compile(r'[^A-Za-z0-9._-]+')
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024


def sanitize_suite_filename_from_url(url: str) -> str:
    parsed_url = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed_url.path) or 'test-suite.zip'
    return _SANITIZE_FILENAME_RE.sub('_', filename)


def derive_suite_dir_name_from_archive(archive_path: str) -> str:
    name = os.path.basename(archive_path or '').strip()
    for extension in ('.tar.bz2', '.tar.gz', '.tgz', '.zip', '.tar'):
        if name.endswith(extension):
            return name[: -len(extension)]
    return os.path.splitext(name)[0] or 'test-suite'


def sanitize_suite_dir_name(name: str | None, fallback: str) -> str:
    raw = (name or fallback or 'test-suite').strip().strip('/\\')
    safe = _SANITIZE_DIRNAME_RE.sub('_', raw).strip('._-')
    return safe or 'test-suite'


def is_complete_archive_file(path: str) -> bool:
    try:
        if path.endswith('.zip'):
            return zipfile.is_zipfile(path)
        if path.endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2')):
            return tarfile.is_tarfile(path)
        return os.path.getsize(path) > 0
    except OSError:
        return False


def sniff_archive_format(path: str) -> str:
    """Identify the REAL archive format from file content, not filename.

    Extension-based dispatch let a tarball renamed ``payload.rar`` reach a
    raw ``tar -xf`` fallback and bypass every member/budget/symlink check
    . Dispatch order: zip → tar (any compression, via content
    probe) → 7z/rar (magic bytes). Returns '' when the content matches no
    known archive format.
    """
    try:
        with open(path, 'rb') as handle:
            magic = handle.read(8)
    except OSError:
        return ''
    if not magic:
        return ''
    if magic.startswith(b'PK\x03\x04') or magic.startswith(b'PK\x05\x06'):
        return 'zip'
    if magic[:2] == b'\x1f\x8b' or magic[:3] == b'BZh':
        # gzip/bzip2 stream: accept as tar only when tarfile can open it.
        try:
            with tarfile.open(path, 'r:*'):
                return 'tar'
        except (tarfile.TarError, OSError):
            return ''
    try:
        with tarfile.open(path, 'r:') as _probe:
            _probe.next()
        return 'tar'
    except (tarfile.TarError, OSError, EOFError):
        pass
    if magic.startswith(b'7z\xbc\xaf\x27\x1c'):
        return '7z'
    if magic[:7] == b'Rar!\x1a\x07':
        return 'rar'
    return ''


def safe_extract_member_path(base_dir: str, member_name: str) -> str:
    target = os.path.abspath(os.path.join(base_dir, member_name))
    base = os.path.abspath(base_dir)
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f'压缩包包含不安全路径: {member_name}')
    return target


def copy_archive_member(
    source: BinaryIO,
    destination: BinaryIO,
    budget: dict[str, int],
    *,
    max_files: int = MAX_ARCHIVE_FILES,
    max_bytes: int = MAX_ARCHIVE_EXPANDED_BYTES,
    chunk_size: int = 1024 * 1024,
) -> None:
    """Copy one archive member while enforcing a shared expansion budget."""
    budget['files'] = budget.get('files', 0) + 1
    if budget['files'] > max_files:
        raise ValueError(f'压缩包文件数量超过限制: {max_files}')
    while chunk := source.read(chunk_size):
        budget['bytes'] = budget.get('bytes', 0) + len(chunk)
        if budget['bytes'] > max_bytes:
            raise ValueError(f'压缩包展开大小超过限制: {max_bytes} bytes')
        destination.write(chunk)


def strip_common_archive_root(
    names: list[str],
) -> tuple[str, list[tuple[str, str]]]:
    files = [name for name in names if name and not name.endswith('/')]
    top_levels = {name.split('/', 1)[0] for name in files if '/' in name}
    if len(top_levels) == 1:
        root = top_levels.pop()
        prefix = root + '/'
        return root, [
            (name, name[len(prefix):] if name.startswith(prefix) else name)
            for name in names
            if name not in {root, prefix}
        ]
    return '', [(name, name) for name in names]


def enforce_post_extraction_safety(base_dir: str) -> None:
    """Scan a directory extracted by a system tool (rar/7z) and enforce the
    same constraints applied to zip/tar extraction: reject symlinks, path
    traversal, file-count bombs, and decompression bombs.

    Without this, ``rar``/``7z`` bypass every safety check that
    :func:`safe_extract_member_path` and :func:`copy_archive_member`
    enforce for zip/tar. Lives in foundation so every feature shares ONE
    archive security policy (review: unify archive extraction).
    """
    base = os.path.abspath(base_dir)
    file_count = 0
    total_bytes = 0
    for root, dirs, files in os.walk(base, followlinks=False):
        for name in [*dirs, *files]:
            full = os.path.join(root, name)
            try:
                mode = os.lstat(full).st_mode
            except OSError as exc:
                raise ValueError(f'无法检查压缩包成员: {name}') from exc
            if stat.S_ISLNK(mode):
                raise ValueError(f'压缩包包含不安全符号链接: {os.path.relpath(full, base)}')
            if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                raise ValueError(f'压缩包包含不安全特殊文件: {os.path.relpath(full, base)}')
            resolved = os.path.realpath(full)
            try:
                confined = os.path.commonpath((base, resolved)) == base
            except ValueError:
                confined = False
            if not confined:
                raise ValueError(f'压缩包包含不安全路径: {os.path.relpath(full, base)}')
            if stat.S_ISDIR(mode):
                continue
            file_count += 1
            if file_count > MAX_ARCHIVE_FILES:
                raise ValueError(f'压缩包文件数量超过限制: {MAX_ARCHIVE_FILES}')
            try:
                total_bytes += os.lstat(full).st_size
            except OSError:
                pass
            if total_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                raise ValueError(
                    f'压缩包展开大小超过限制: {MAX_ARCHIVE_EXPANDED_BYTES} bytes'
                )


def preflight_system_archive(
    archive_path: str,
    target_dir: str,
    command: str,
    *,
    timeout: int = 120,
) -> None:
    """Validate RAR/7z member paths before the external tool writes files."""
    if command == 'rar':
        completed = subprocess.run(
            [command, 'lb', archive_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        members = [(line.strip(), 0) for line in completed.stdout.splitlines()]
    else:
        completed = subprocess.run(
            [command, 'l', '-slt', archive_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        details = completed.stdout.partition('----------')[2]
        members = []
        member_name = ''
        member_size = 0
        for line in [*details.splitlines(), 'Path = ']:
            if line.startswith('Path = '):
                if member_name:
                    members.append((member_name, member_size))
                member_name = line.removeprefix('Path = ').strip()
                member_size = 0
            elif line.startswith('Size = '):
                try:
                    member_size = max(0, int(line.removeprefix('Size = ').strip()))
                except ValueError:
                    member_size = 0

    file_count = 0
    total_bytes = 0
    for member_name, member_size in members:
        if not member_name:
            continue
        normalized = member_name.replace('\\', '/')
        safe_extract_member_path(target_dir, normalized)
        file_count += 1
        total_bytes += member_size
        if file_count > MAX_ARCHIVE_FILES:
            raise ValueError(f'压缩包文件数量超过限制: {MAX_ARCHIVE_FILES}')
        if total_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
            raise ValueError(
                f'压缩包展开大小超过限制: {MAX_ARCHIVE_EXPANDED_BYTES} bytes'
            )
