import unittest

from features.test_execution.suites import (
    TRADEFED_BINARY_MAP,
    TRADEFED_BINARY_REVERSE_MAP,
    build_suite_info,
    detect_test_type_from_suite_path,
    get_test_type_from_binary,
)


class SuitePathTypeDetectionTests(unittest.TestCase):
    def test_nested_cts_verifier_layout_is_cts_v(self):
        # Nested Verifier layout: the first /android-cts hit must not win.
        path = (
            "/home/user/GMS-Suite/android-cts-verifier-16_r1/"
            "android-cts-verifier/android-cts-v-host/tools/cts-v-host-tradefed"
        )
        self.assertEqual(detect_test_type_from_suite_path(path), "cts-v")

    def test_flat_cts_verifier_layout_is_cts_v(self):
        self.assertEqual(
            detect_test_type_from_suite_path("/mnt/suites/android-cts-verifier-12_r1/tools"),
            "cts-v",
        )

    def test_gts_root_is_not_plain_gts(self):
        self.assertEqual(
            detect_test_type_from_suite_path("/mnt/suites/android-gts-root-10_r1/tools"),
            "gts-root",
        )

    def test_generic_suite_types(self):
        cases = {
            "/mnt/suites/android-cts-17_r1/android-cts/tools": "cts",
            "/mnt/suites/android-vts-16_r1/tools": "vts",
            "/mnt/suites/android-gts-9_r1/tools": "gts",
            "/mnt/suites/android-sts-13_r1/tools": "sts",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(detect_test_type_from_suite_path(path), expected)

    def test_unknown_path_returns_none(self):
        self.assertIsNone(detect_test_type_from_suite_path("/mnt/suites/some-random-dir"))
        self.assertIsNone(detect_test_type_from_suite_path(""))
        self.assertIsNone(detect_test_type_from_suite_path(None))

    def test_cts_verifier_launcher_keeps_nested_tools_path(self):
        path = (
            "/home/user/GMS-Suite/android-cts-verifier-16_r1/"
            "android-cts-verifier/android-cts-v-host/tools/cts-v-host-tradefed"
        )
        info = build_suite_info(path)
        self.assertIsNotNone(info)
        self.assertEqual(info["test_type"], "cts-v")
        self.assertEqual(
            info["tools_path"],
            "/home/user/GMS-Suite/android-cts-verifier-16_r1/"
            "android-cts-verifier/android-cts-v-host/tools",
        )
        self.assertEqual(
            info["version"],
            "android-cts-verifier-16_r1",
        )

    def test_cts_v_marker_priority_over_cts(self):
        # Marker order must evaluate cts-v markers before the generic cts one.
        markers = __import__(
            "features.test_execution.suites", fromlist=["SUITE_PATH_TYPE_MARKERS"]
        ).SUITE_PATH_TYPE_MARKERS
        order = [suite_type for _, suite_type in markers]
        self.assertLess(order.index("cts-v"), order.index("cts"))
        self.assertLess(order.index("gts-root"), order.index("gts"))

    def test_reverse_binary_map_has_no_conflicting_entries(self):
        # 'cts-tradefed' is shared by cts and gsi; the reverse map must resolve
        # it to exactly one type instead of silently keeping the last write.
        self.assertEqual(
            len(TRADEFED_BINARY_REVERSE_MAP),
            len(set(TRADEFED_BINARY_MAP.values())),
        )
        for binary, suite_type in TRADEFED_BINARY_REVERSE_MAP.items():
            owners = [t for t, b in TRADEFED_BINARY_MAP.items() if b == binary]
            self.assertEqual(
                suite_type,
                owners[0],
                f"{binary} must reverse to its first owner, not a later overwrite",
            )

    def test_shared_cts_launcher_is_cts_even_without_android_dir(self):
        self.assertEqual(get_test_type_from_binary("cts-tradefed"), "cts")
        info = build_suite_info("/opt/suites/tools/cts-tradefed")
        self.assertEqual(info["test_type"], "cts")


if __name__ == "__main__":
    unittest.main()
