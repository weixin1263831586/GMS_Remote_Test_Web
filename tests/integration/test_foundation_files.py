import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from foundation.archives import safe_extract_member_path
from foundation.database import connect_sqlite
from foundation.files import create_zip_from_directory
from foundation.networking import parse_host_address, sanitize_url
from foundation.uploads import (
    copy_fileobj_to_path,
    remote_home_file_path,
    safe_upload_target_path,
    upload_temp_root,
)


class SafePathTests(unittest.TestCase):
    def test_archive_member_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            safe_extract_member_path(tmp, '../escape.txt')

    def test_upload_target_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            safe_upload_target_path(tmp, '/etc/passwd')

    def test_copy_fileobj_removes_partial_file_after_size_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / 'upload.bin'
            with self.assertRaises(ValueError):
                copy_fileobj_to_path(
                    io.BytesIO(b'123456'),
                    str(destination),
                    max_size=3,
                    chunk_size=2,
                )
            self.assertFalse(destination.exists())

    def test_upload_temp_root_keeps_namespace_under_system_temp(self):
        root = Path(upload_temp_root('../custom-gms'))

        self.assertEqual(root.name, 'custom-gms')
        self.assertEqual(root.parent, Path(tempfile.gettempdir()))

    def test_remote_home_file_path_rejects_unsafe_parts(self):
        self.assertEqual(
            remote_home_file_path('gms', '../payload.zip'),
            '/home/gms/payload.zip',
        )
        with self.assertRaises(ValueError):
            remote_home_file_path('../bad', 'payload.zip')


class DatabaseTests(unittest.TestCase):
    def test_connect_sqlite_creates_parent_and_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nested/app.sqlite3'
            with connect_sqlite(path) as connection:
                connection.execute('CREATE TABLE sample (value TEXT)')
                connection.execute('INSERT INTO sample VALUES (?)', ('ok',))
            with connect_sqlite(path) as connection:
                value = connection.execute('SELECT value FROM sample').fetchone()[0]
            self.assertEqual(value, 'ok')


class ZipFileTests(unittest.TestCase):
    def test_create_zip_skips_symlink_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'real.log').write_text('ok', encoding='utf-8')
            (source / 'linked.log').symlink_to(source / 'real.log')

            result = create_zip_from_directory(str(source), base_dir_for_arcnames=str(source))

            self.assertIsNotNone(result)
            data, count = result
            self.assertEqual(count, 1)
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.namelist(), ['real.log'])

    def test_create_zip_rejects_arcname_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            source.mkdir()
            (source / 'real.log').write_text('ok', encoding='utf-8')

            result = create_zip_from_directory(str(source), base_dir_for_arcnames=str(root / 'other'))

            self.assertIsNone(result)


class NetworkingTests(unittest.TestCase):
    def test_parse_host_address_splits_username(self):
        self.assertEqual(
            parse_host_address('user@192.168.1.2'),
            ('user', '192.168.1.2'),
        )

    def test_sanitize_url_adds_https(self):
        self.assertEqual(sanitize_url('example.com'), 'https://example.com')
