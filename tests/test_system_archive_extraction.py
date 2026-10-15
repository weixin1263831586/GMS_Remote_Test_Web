"""System archive extraction retains preflight and post-extraction protections."""

from unittest.mock import patch

import pytest

from foundation.archives import extract_system_archive, sniff_archive_format
from foundation.command_result import CommandResult


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
