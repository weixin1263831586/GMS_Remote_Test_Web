from __future__ import annotations

import os
import re
import tarfile
import urllib.parse
import zipfile


ARCHIVE_EXTENSIONS = (
    '.zip',
    '.tar.gz',
    '.tgz',
    '.tar.bz2',
    '.tar',
    '.rar',
    '.7z',
)
_SANITIZE_FILENAME_RE = re.compile(r'[^\w\-_.\[\]]')
_SANITIZE_DIRNAME_RE = re.compile(r'[^A-Za-z0-9._-]+')


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


def safe_extract_member_path(base_dir: str, member_name: str) -> str:
    target = os.path.abspath(os.path.join(base_dir, member_name))
    base = os.path.abspath(base_dir)
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f'压缩包包含不安全路径: {member_name}')
    return target


def strip_common_archive_root(
    names: list[str],
) -> tuple[str, list[tuple[str, str]]]:
    files = [name for name in names if name and not name.endswith('/')]
    top_levels = {name.split('/', 1)[0] for name in files if '/' in name}
    if len(top_levels) == 1:
        root = top_levels.pop()
        return root, [
            (name, name[len(root) + 1 :])
            for name in names
            if name not in {root, root + '/'}
        ]
    return '', [(name, name) for name in names]
