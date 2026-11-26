from __future__ import annotations

import unittest

from features.redmine.utils import (
    attachment_content_disposition,
    build_search_fts_query,
    detect_android_version,
    detect_soc_platform,
    sanitize_attachment_filename,
    search_query_tokens,
    similarity_level,
)


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


class SearchRuleTests(unittest.TestCase):
    def test_version_precision_remains_context_specific(self):
        self.assertEqual(detect_soc_platform("rk3576", "", ""), "RK3576")
        self.assertEqual(detect_android_version("Android 16.0", "", ""), "Android16.0")
        self.assertEqual(detect_android_version("Android 16.0", "", "", major_only=True), "Android16")

    def test_fts_preserves_distinct_query_policies(self):
        self.assertEqual(search_query_tokens('"test_x_y" a failure'), ["test_x_y", "failure"])
        self.assertEqual(build_search_fts_query("test_name failure"), '"test_name" OR "failure"')
        self.assertEqual(build_search_fts_query("test_name failure", phrase_symbols=True), '"test" "name" OR "failure"')

    def test_similarity_display_thresholds(self):
        self.assertEqual([similarity_level(x) for x in (39, 40, 69, 70)], ["low", "medium", "medium", "high"])


if __name__ == "__main__":
    unittest.main()
