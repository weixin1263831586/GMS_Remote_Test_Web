import io
import tempfile
import unittest
from pathlib import Path

from foundation.archives import safe_extract_member_path
from foundation.database import connect_sqlite
from foundation.networking import parse_host_address, sanitize_url
from foundation.uploads import copy_fileobj_to_path, safe_upload_target_path


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


class NetworkingTests(unittest.TestCase):
    def test_parse_host_address_splits_username(self):
        self.assertEqual(
            parse_host_address('user@192.168.1.2'),
            ('user', '192.168.1.2'),
        )

    def test_sanitize_url_adds_https(self):
        self.assertEqual(sanitize_url('example.com'), 'https://example.com')
