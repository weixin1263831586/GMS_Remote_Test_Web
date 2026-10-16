"""System archive extraction retains preflight and post-extraction protections."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from foundation.archives import (
    extract_system_archive,
    preflight_system_archive,
    sniff_archive_format,
)
from foundation.command_result import CommandResult


def _listing_run(stdout: str):
    """subprocess.run stub that records argv and returns canned listing output."""
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(stdout=stdout)

    run.calls = calls
    return run


def _seven_zip_listing(*entries: str) -> str:
    return '----------\n' + '\n'.join(entries) + '\n'


def test_unsafe_listing_prevents_any_extraction_command(tmp_path):
    source = tmp_path / 'attachment.rar'
    source.write_bytes(b'Rar!\x1a\x07\x01')
    with patch('foundation.archives.shutil.which', return_value='/usr/bin/7z'), patch(
        'foundation.archives.preflight_system_archive', side_effect=ValueError('unsafe path'),
    ), patch('foundation.archives.run_local_command') as command, pytest.raises(ValueError):
        extract_system_archive(str(source), str(tmp_path / 'members'))
    command.assert_not_called()


def test_extracted_symlink_is_rejected_before_archive_is_consumed(tmp_path):
    source = tmp_path / 'attachment.rar'
    source.write_bytes(b'Rar!\x1a\x07\x01')
    target = tmp_path / 'members'
    target.mkdir()
    (target / 'escape').symlink_to(tmp_path / 'outside')
    with patch('foundation.archives.shutil.which', return_value='/usr/bin/7z'), patch(
        'foundation.archives.preflight_system_archive', return_value=None,
    ), patch('foundation.archives.run_local_command', return_value=CommandResult(code=0)), pytest.raises(ValueError, match='符号链接'):
        extract_system_archive(str(source), str(target))


@pytest.mark.parametrize('magic', [b'Rar!\x1a\x07\x00', b'Rar!\x1a\x07\x01\x00'])
def test_rar_four_and_five_are_identified_by_content_even_with_other_extensions(tmp_path, magic):
    source = tmp_path / 'attachment.bin'
    source.write_bytes(magic)
    assert sniff_archive_format(str(source)) == 'rar'


@pytest.mark.parametrize('mode', ['lrwxrwxrwx', 'l---------', 'lrwsrwsrwt'])
def test_preflight_7z_rejects_posix_symlink_attribute_member(tmp_path, mode):
    source = tmp_path / 'attachment.7z'
    source.write_bytes(b'7z\xbc\xaf\x27\x1c')
    listing = _seven_zip_listing(
        'Path = link',
        'Size = 13',
        f'Attributes = A_ {mode}',
        'Path = normal.txt',
        'Size = 2',
        'Attributes = A_ -rw-r--r--',
    )
    with patch(
        'foundation.archives.subprocess.run', side_effect=_listing_run(listing)
    ), pytest.raises(ValueError, match='符号链接'):
        preflight_system_archive(str(source), str(tmp_path / 'out'), '7z')


def test_preflight_7z_rejects_symbolic_link_field_member(tmp_path):
    source = tmp_path / 'attachment.7z'
    source.write_bytes(b'7z\xbc\xaf\x27\x1c')
    listing = _seven_zip_listing(
        'Path = escape',
        'Size = 0',
        'Symbolic Link = /etc/passwd',
        'Attributes = A_',
    )
    with patch(
        'foundation.archives.subprocess.run', side_effect=_listing_run(listing)
    ), pytest.raises(ValueError, match='符号链接'):
        preflight_system_archive(str(source), str(tmp_path / 'out'), '7z')


def test_preflight_7z_accepts_regular_and_directory_members(tmp_path):
    source = tmp_path / 'attachment.7z'
    source.write_bytes(b'7z\xbc\xaf\x27\x1c')
    listing = _seven_zip_listing(
        'Path = dir/',
        'Folder = +',
        'Attributes = D_ drwxr-xr-x',
        'Path = dir/normal.txt',
        'Size = 2',
        'Attributes = A_ -rw-r--r--',
    )
    with patch(
        'foundation.archives.subprocess.run', side_effect=_listing_run(listing)
    ):
        preflight_system_archive(str(source), str(tmp_path / 'out'), '7z')


def test_preflight_rar_routes_listing_through_7z_and_rejects_symlink(tmp_path):
    """command='rar' 时若 7z 可用，列表必须改走 7z 明细输出——rar lb 只有
    裸文件名，无法在解压前识别符号链接成员。"""
    source = tmp_path / 'attachment.rar'
    source.write_bytes(b'Rar!\x1a\x07\x01')
    listing = _seven_zip_listing(
        'Path = link',
        'Size = 13',
        'Attributes = A_ lrwxrwxrwx',
    )
    runner = _listing_run(listing)
    with patch('foundation.archives.shutil.which', return_value='/usr/bin/7z'), patch(
        'foundation.archives.subprocess.run', side_effect=runner
    ), pytest.raises(ValueError, match='符号链接'):
        preflight_system_archive(str(source), str(tmp_path / 'out'), 'rar')
    assert runner.calls and runner.calls[0][0] == '7z'


def test_preflight_rar_without_7z_fails_closed(tmp_path):
    source = tmp_path / 'attachment.rar'
    source.write_bytes(b'Rar!\x1a\x07\x01')
    runner = _listing_run('ok.txt\n../escape.txt\n')
    with patch('foundation.archives.shutil.which', return_value=None), patch(
        'foundation.archives.subprocess.run', side_effect=runner
    ), pytest.raises(ValueError, match='需要 7z'):
        preflight_system_archive(str(source), str(tmp_path / 'out'), 'rar')
    assert runner.calls == []
