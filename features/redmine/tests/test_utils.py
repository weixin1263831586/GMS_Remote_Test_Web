from __future__ import annotations

import unittest

from features.redmine.utils import attachment_content_disposition, sanitize_attachment_filename


class RedmineUtilsTests(unittest.TestCase):
    def test_sanitize_attachment_filename_uses_basename_and_removes_controls(self):
        name = sanitize_attachment_filename("../logs/evil\n\"name.zip")

        self.assertEqual(name, "evil_name.zip")
        self.assertNotIn("/", name)
        self.assertNotIn("\n", name)
        self.assertNotIn('"', name)

    def test_sanitize_attachment_filename_falls_back_for_empty_or_dot_names(self):
        self.assertEqual(sanitize_attachment_filename("..", "attachment_12"), "attachment_12")
        self.assertEqual(sanitize_attachment_filename("", "attachment_12"), "attachment_12")

    def test_sanitize_attachment_filename_preserves_extension_when_truncating(self):
        name = sanitize_attachment_filename("a" * 240 + ".zip")

        self.assertLessEqual(len(name), 180)
        self.assertTrue(name.endswith(".zip"))

    def test_attachment_content_disposition_escapes_unsafe_filename_parts(self):
        header = attachment_content_disposition('../bad"name\n.zip')

        self.assertTrue(header.startswith('attachment; filename="bad_name_.zip"'))
        self.assertIn("filename*=UTF-8''bad_name_.zip", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("../", header)


if __name__ == "__main__":
    unittest.main()
